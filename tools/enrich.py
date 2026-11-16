#!/usr/bin/env python3
"""政策库正文回填管线：抓取原文页正文 → 填充 kk 字段 → 补抽文号 → 重打包 data.json.gz + 更新 manifest。

用法: python3 tools/enrich.py [--limit N] [--workers 8]
输入: data.json.gz / manifest.json（原地更新）
输出: data.json.gz（含 kk/n 增强）+ manifest.json（bytes/sha256 刷新）+ tools/enrich_report.json
"""
import json, re, sys, time, gzip, hashlib, os, shutil
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA_MAIN = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36'
UA_ALT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36 Edg/125.0'
HDR_SETS = [
    {'User-Agent': UA_MAIN, 'Accept-Language': 'zh-CN,zh;q=0.9',
     'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'},
    {'User-Agent': UA_ALT, 'Accept-Language': 'zh-CN,zh;q=0.9', 'Referer': 'https://www.chinatax.gov.cn/'},
]
TIMEOUT = 15
MIN_CHARS = 120
BOILER = re.compile(r'^(当前位置|首页|首 页|打印|分享|字体|无障碍|简体|繁体|移动版|网站地图|版权所有|备案|'
                    r'相关链接|附件[-：:]?$|扫一扫|分享到|【打印】|关闭窗口|下一篇|上一篇|字号|背景||)$')


class BlockParser(HTMLParser):
    """按顶层容器（div/td/li/section/article）收集整块文本，段落以换行保留"""
    SKIP = {'script', 'style', 'nav', 'header', 'footer', 'noscript', 'iframe', 'select', 'form', 'svg'}
    FLUSH_ON = {'div', 'td', 'li', 'section', 'article', 'tr', 'table'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self._skip, self._buf = [], 0, []
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.FLUSH_ON:
            self.flush()
    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == 'p' and not self._skip:
            self._buf.append('\n')
        elif tag in self.FLUSH_ON:
            self.flush()
    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)
    def flush(self):
        t = ''.join(self._buf).strip()
        self._buf = []
        if len(re.sub(r'\s', '', t)) >= MIN_CHARS:
            self.blocks.append(t)


def extract(html):
    p = BlockParser()
    try:
        p.feed(html)
    except Exception:
        pass
    if not p.blocks:
        return ''
    # 选最长块；若次长块与其有共同前缀上下文则合并相邻片段（简单策略：只取最长块，避免污染）
    best = max(p.blocks, key=len)
    lines = [ln.strip() for ln in re.split(r'\n+', re.sub(r'[ \t\u3000]+', ' ', best))]
    return '\n'.join(ln for ln in lines if ln and not BOILER.match(ln))


DOC_NO = re.compile(r'[（(]?((?:19|20)\d{2})\s*年\s*第?\s*(\d{1,3})\s*号[）)]?$')
DOC_NO_MID = re.compile(r'((?:19|20)\d{2})\s*年\s*第\s*(\d{1,3})\s*号')


def extract_docno(title):
    """从标题补抽文号：仅对高置信模式（公告/通知/令标题尾部）"""
    t = title or ''
    if re.search(r'(解读|图解|热点问答|办事指南|指引|指南)$', t):
        return ''
    m = DOC_NO_MID.search(t)
    if m and re.search(r'(公告|通告|令|通知|布告)', t):
        return '%s年第%s号' % (m.group(1), m.group(2))
    return ''


def fetch(item):
    url = item['u']
    t0 = time.time()
    for hdr in HDR_SETS:
        try:
            req = Request(url, headers=hdr)
            with urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read(3_000_000)
                enc = r.headers.get_content_charset() or 'utf-8'
                html = raw.decode(enc, 'ignore')
            txt = extract(html)
            if len(txt) >= MIN_CHARS:
                return {'url': url, 'ok': True, 'ms': int((time.time() - t0) * 1000),
                        'chars': len(txt), 'text': txt, 'err': ''}
        except Exception as e:
            last = repr(e)[:100]
        if item.get('delay'):
            time.sleep(item['delay'])
        time.sleep(0.15)
    return {'url': url, 'ok': False, 'ms': int((time.time() - t0) * 1000), 'chars': 0,
            'text': '', 'err': last if 'last' in dir() else 'unknown'}


def main():
    limit = int(sys.argv[sys.argv.index('--limit') + 1]) if '--limit' in sys.argv else 0
    workers = int(sys.argv[sys.argv.index('--workers') + 1]) if '--workers' in sys.argv else 8
    hosts = sys.argv[sys.argv.index('--hosts') + 1].split(',') if '--hosts' in sys.argv else None
    delay = float(sys.argv[sys.argv.index('--delay') + 1]) if '--delay' in sys.argv else 0.0

    data_path = os.path.join(ROOT, 'data.json.gz')
    manifest_path = os.path.join(ROOT, 'manifest.json')
    payload = json.load(gzip.open(data_path, 'rt', encoding='utf-8'))
    manifest = json.load(open(manifest_path, encoding='utf-8'))
    recs = payload['d']
    if limit:
        recs = recs[:limit]

    # 现有 k 多为 ≤300 字摘要：长度不足阈值的一律视为待升级
    todo = [r for r in recs if len((r.get('k') or '').strip()) < 600]
    if hosts:
        hs = set(hosts)
        todo = [r for r in todo if urlparse(r.get('u', '')).netloc in hs]
    print('总数 %d · 待回填/升级 %d · 并发 %d · 限域 %s' % (len(recs), len(todo), workers, hosts or '全部'))
    results = {}
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, {'u': r['u'], 'delay': delay}): r for r in todo if r.get('u')}
        for f in as_completed(futs):
            res = f.result()
            results[futs[f]['u']] = res
            done += 1
            if done % 500 == 0:
                okn = sum(1 for v in results.values() if v['ok'])
                print('  进度 %d/%d · 成功 %d · %.0fs' % (done, len(futs), okn, time.time() - t0))

    filled = 0
    upgraded = 0
    docno_added = 0
    for r in recs:
        res = results.get(r.get('u'))
        if res and res['ok']:
            old = (r.get('k') or '').strip()
            if len(res['text']) > len(old):
                r['k'] = res['text']
                filled += 1
                if old:
                    upgraded += 1
        if not (r.get('n') or '').strip():
            dn = extract_docno(r.get('t', ''))
            if dn:
                r['n'] = dn
                docno_added += 1

    okn = sum(1 for v in results.values() if v['ok'])
    fail_hosts = {}
    for v in results.values():
        if not v['ok']:
            h = urlparse(v['url']).netloc
            fail_hosts[h] = fail_hosts.get(h, 0) + 1

    blob = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    gz = gzip.compress(blob, 9)
    shutil.copy2(data_path, data_path + '.prev')
    with open(data_path, 'wb') as f:
        f.write(gz)
    manifest['bytes'] = len(gz)
    # 校验口径 = 浏览器实际收到的字节（GitHub Pages 原样下发 gz），即 gz 文件的 SHA-256 前 16 位
    manifest['sha256'] = hashlib.sha256(gz).hexdigest()[:16]
    manifest['b'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    manifest['built_at'] = manifest['b']
    manifest['fulltext'] = {'filled': filled, 'attempted': len(todo), 'ok': okn, 'at': manifest['b']}
    json.dump(manifest, open(manifest_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    json.dump({'ok': okn, 'attempted': len(todo), 'filled': filled, 'docno_added': docno_added,
               'fail_hosts': fail_hosts, 'gz_bytes': len(gz)},
              open(os.path.join(ROOT, 'tools', 'enrich_report.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('回填成功 %d/%d（%.0f%%）· 文号补抽 %d · 新 data.json.gz %.2f MB'
          % (okn, len(todo), okn / max(1, len(todo)) * 100, docno_added, len(gz) / 1048576))
    print('失败域名 Top:', sorted(fail_hosts.items(), key=lambda x: -x[1])[:8])


if __name__ == '__main__':
    main()
