#!/usr/bin/env python3
"""每日数据包合并：把 taxsync 构建产物并入线上包，只动数据三件套。

为什么需要「合并」而不是直接覆盖：
  构建产物里的 ``k``（正文摘录）是按 clause_limit=250 截断的，
  线上包里的 ``k`` 已被 ``tools/enrich.py`` 回填成原文正文（平均 607 字，最长 11 万字）。
  直接覆盖会让全部正文退化成 250 字摘录，检索与 AI 引用的质量会明显下降。

合并规则（幂等、可重跑）：
  - 以原文链接 ``u`` 为业务键（实测构建产物与线上包 100% 对得上）；
  - 新包某条的 ``k`` 比旧包短且标题未变 → 保留旧包的 ``k``；
  - 其余字段一律以新包为准（标题/日期/效力/分类都可能更新）；
  - 新文档保持构建产物的短摘录，留给 ``tools/enrich.py`` 后续补全。

不碰的东西：``index.html`` 永不覆盖（线上 UI 与 webui_desk 构建产物并不等价）。

用法:
    python3 tools/daily_data.py --site-dir <tax-agent>/out/site [--dry-run]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_DIR = Path("/Users/apple/WorkBuddy/2026-10-16-13-23-31/tax-agent/_backups")

#: 合并后文档数跌破这个比例就中止（防止把残缺构建推上线）
MIN_KEEP_RATIO = 0.98


def load_gz(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def dump_gz(obj: dict) -> bytes:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, 9)


def doc_key(d: dict) -> str:
    return (d.get("u") or "").strip() or f"{d.get('t', '')}\u0000{d.get('d', '')}"


#: 判断「内容变了没有」时忽略的字段：
#:   - b / built_at / stale_days / freshness / sync_state：构建期时间戳与状态；
#:   - lib：文库快照计数，跟随 library.json.gz 变化，不该连累 7.3MB 政策包；
#:   - ai / hls：元信息与另一数据源状态，构建产物里根本没有。
#: 不忽略它们，每晚都会生成「字节不同、内容相同」的假变更。
VOLATILE_M = ("b", "built_at", "stale_days", "freshness", "sync_state", "lib", "ai", "hls")


def payload_core(obj: dict) -> str:
    """内容指纹：去掉易变字段后的规范化 JSON。用于判断「真的变了没有」。"""
    m = {k: v for k, v in (obj.get("m") or {}).items() if k not in VOLATILE_M}
    return json.dumps({"m": m, "d": obj.get("d") or []},
                      ensure_ascii=False, sort_keys=True)


def lib_core(raw: bytes) -> str:
    """库快照的内容指纹：只忽略构建时间戳 ``m.b``。"""
    obj = json.loads(gzip.decompress(raw).decode("utf-8"))
    m = dict(obj.get("m") or {})
    m.pop("b", None)
    obj["m"] = m
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def merge(new: dict, old: dict) -> tuple[dict, dict]:
    new_docs = new.get("d") or []
    old_map = {doc_key(d): d for d in (old.get("d") or [])}
    kept = 0
    for d in new_docs:
        o = old_map.get(doc_key(d))
        if not o:
            continue
        if (o.get("t") or "") != (d.get("t") or ""):
            continue
        if len(o.get("k") or "") > len(d.get("k") or ""):
            d["k"] = o["k"]
            kept += 1
        if not (d.get("n") or "").strip() and (o.get("n") or "").strip():
            d["n"] = o["n"]
    new["d"] = new_docs
    return new, {"kept_fulltext": kept, "old_n": len(old.get("d") or []), "new_n": len(new_docs)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site-dir", required=True, help="taxsync 构建产物目录（含 data.json.gz / manifest.json）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--sync-state", default=None,
                    help="覆盖清单里的 sync_state（夜间任务在源站采集成功时传 ok）")
    args = ap.parse_args()

    site = Path(args.site_dir)
    new_path, man_path = site / "data.json.gz", site / "manifest.json"
    live_path, live_man_path = ROOT / "data.json.gz", ROOT / "manifest.json"
    for p in (new_path, man_path):
        if not p.exists():
            print(f"× 缺少构建产物：{p}")
            return 2

    new = load_gz(new_path)
    manifest = json.loads(man_path.read_text(encoding="utf-8"))
    old = load_gz(live_path) if live_path.exists() else {"d": []}
    live_man = json.loads(live_man_path.read_text(encoding="utf-8")) if live_man_path.exists() else {}

    merged, stats = merge(new, old)
    n = len(merged["d"])

    # ---- 护栏：不接受倒退的数据 ----
    old_maxd = str(live_man.get("maxd") or "")
    new_maxd = str((merged.get("m") or {}).get("maxd") or manifest.get("maxd") or "")
    if old_maxd and new_maxd and new_maxd < old_maxd:
        print(f"× 中止：新包 maxd={new_maxd} 早于线上 {old_maxd}")
        return 3
    if stats["old_n"] and n < stats["old_n"] * MIN_KEEP_RATIO:
        print(f"× 中止：合并后 {n} 条，低于线上 {stats['old_n']} 条的 {MIN_KEEP_RATIO:.0%}")
        return 3

    # ---- 文库：内容一致就不重写（3.8MB 不值得每晚白推）----
    lib_out = None
    lib_changed = False
    new_lib = site / "library.json.gz"
    live_lib = ROOT / "library.json.gz"
    if new_lib.exists() and (manifest.get("optional_state") == "ok"):
        new_lib_bytes = new_lib.read_bytes()
        same = live_lib.exists() and lib_core(live_lib.read_bytes()) == lib_core(new_lib_bytes)
        if not same:
            lib_out = new_lib_bytes
            lib_changed = True
            manifest["lib_bytes"] = len(new_lib_bytes)
            manifest["lib_sha256"] = hashlib.sha256(new_lib_bytes).hexdigest()[:16]
        elif live_man:                           # 内容一致 → 沿用线上体积/哈希/计数
            for k in ("lib_bytes", "lib_sha256", "lib", "lib_counts", "lib_enc", "lib_file"):
                if k in live_man:
                    manifest[k] = live_man[k]

    # ---- 真的变了才重写：忽略时间戳/文库计数比内容 ----
    data_changed = not live_path.exists() or payload_core(merged) != payload_core(old)
    merged_m = merged.setdefault("m", {})
    if data_changed:
        counts = manifest.get("lib_counts") or manifest.get("lib") or merged_m.get("lib")
        if counts:
            merged_m["lib"] = counts             # 让包内计数与实际发布的文库一致
        if "ai" not in merged_m and (old.get("m") or {}).get("ai"):
            merged_m["ai"] = old["m"]["ai"]      # 构建产物不产出 ai 块，沿用线上
    else:
        for k in ("lib", "ai", "hls"):           # 内容没变 → 这些元信息也沿用线上
            if k in (old.get("m") or {}):
                merged_m[k] = old["m"][k]
    # 包内 m.sync_state 必须与 --sync-state 一致：页面状态条读的是包内副本，
    # 只改 manifest.json 会让「源站采集成功」的包仍显示「上次采集失败」。
    if args.sync_state and merged_m.get("sync_state") != args.sync_state:
        merged_m["sync_state"] = args.sync_state
        data_changed = True                      # sync_state 在 VOLATILE_M 里，不影响内容指纹
    data_bytes = dump_gz(merged) if data_changed else live_path.read_bytes()

    # ---- manifest：以新构建为准，只打补丁 ----
    manifest.update({
        "bytes": len(data_bytes),
        "sha256": hashlib.sha256(data_bytes).hexdigest()[:16],
        "n": n,
        "enc": "gzip",
        "file": "data.json.gz",
    })
    for k in ("fulltext",):                      # 正文回填管线的统计只存在于线上清单
        if k in live_man and k not in manifest:
            manifest[k] = live_man[k]
    if not data_changed and live_man:            # 内容没变 → 时间戳沿用线上，避免假变更
        for k in ("b", "built_at", "optional_at"):
            if k in live_man:
                manifest[k] = live_man[k]
        if "optional" in live_man:
            manifest["optional"] = live_man["optional"]
    if args.sync_state:
        manifest["sync_state"] = args.sync_state

    print(f"· 线上 {stats['old_n']} 条 → 新构建 {stats['new_n']} 条 → 合并 {n} 条")
    print(f"· 保留回填正文 {stats['kept_fulltext']} 条（其余走新构建摘录）")
    print(f"· 数据包 {len(data_bytes):,}B sha256={manifest['sha256']} maxd={new_maxd} "
          f"freshness={manifest.get('freshness')} stale_days={manifest.get('stale_days')}")
    print(f"· 内容变化：数据 {'有' if data_changed else '无（不重写）'} / 文库 {'有' if lib_changed else '无（不重写）'}")
    print(f"· library {'更新 ' + str(len(lib_out)) + 'B' if lib_out else '无变化，跳过'}")

    if args.dry_run:
        print("· --dry-run：未写入任何文件")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    if data_changed:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if live_path.exists():
            shutil.copy2(live_path, BACKUP_DIR / f"data.json.gz.{stamp}")
        tmp = live_path.with_suffix(".gz.tmp")
        tmp.write_bytes(data_bytes)
        tmp.replace(live_path)
    if lib_out:
        (ROOT / "library.json.gz").write_bytes(lib_out)
    man_tmp = live_man_path.with_suffix(".json.tmp")
    man_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    man_tmp.replace(live_man_path)
    wrote = ["manifest.json"]
    if data_changed:
        wrote.insert(0, "data.json.gz")
    if lib_out:
        wrote.append("library.json.gz")
    print(f"√ 已写入 {' / '.join(wrote)}（备份在 {BACKUP_DIR}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
