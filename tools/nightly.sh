#!/bin/zsh
# 每晚 20:00 数据更新流水线（launchd: com.vane.taxsync.nightly）
#
# 只做一件事：把源站新增政策同步进线上数据包。
#   sync(yingtax) → web-build(不产 standalone) → daily_data.py 合并 → 有变化才提交推送
#
# 红线：
#   * 绝不调用 `run.py publish` / `publish.py`（会 force 覆盖 gh-pages 上的 index.html 与历史）
#   * 绝不 git add index.html / data.json.gz.prev
#   * 页面 UI 由人工维护，本脚本只动数据三件套
set -u
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export TAXSYNC_OPTIONAL=1
export TAXSYNC_YINGTAX_SHARECODE="${TAXSYNC_YINGTAX_SHARECODE:-DOREOH6DCR}"

AGENT="/Users/apple/WorkBuddy/2026-10-16-13-23-31/tax-agent"
REPO="/Users/apple/WorkBuddy/工作台搭建/tax-policy-qa"
LOG_DIR="$REPO/tools/logs"
LOG="$LOG_DIR/nightly.log"
LOCK_DIR="$LOG_DIR/nightly.lock"
DATA_FILES=(data.json.gz library.json.gz manifest.json)

mkdir -p "$LOG_DIR"

log() { print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

notify() {
  local title="$1" msg="$2"
  /usr/bin/osascript -e "display notification \"${msg//\"/\\\"}\" with title \"${title}\"" \
    >/dev/null 2>&1 || true
}

fail() { log "× $1"; notify "税策智库·夜间更新失败" "$1"; exit "${2:-1}"; }

# ---- 单实例：上一轮没跑完就跳过（超过 90 分钟的锁视为残留）----
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  age=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
  if [ "$age" -gt 5400 ]; then
    log "· 清理残留锁（${age}s）"
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || fail "无法获取运行锁"
  else
    log "· 上一轮仍在运行（${age}s），本轮跳过"
    exit 0
  fi
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

# ---- 日志轮转：超过 5MB 只留最后 2000 行 ----
if [ -f "$LOG" ] && [ "$(stat -f %z "$LOG")" -gt 5242880 ]; then
  tail -n 2000 "$LOG" > "$LOG.rot" && mv "$LOG.rot" "$LOG"
fi

log "===== 夜间更新开始 ====="

# ---- 1. 采集（只跑 yingtax；HLS 站点自身 403，不阻塞）----
cd "$AGENT" || fail "采集目录不存在"
log "· [1/5] 同步 yingtax"
if ! /opt/homebrew/bin/python3 run.py sync --site yingtax >>"$LOG" 2>&1; then
  fail "源站采集失败，本轮不构建、不推送（线上保持旧数据）"
fi

# ---- 2. 构建（必须 --no-standalone，否则 V6 体积护栏不过）----
log "· [2/5] web-build"
if ! /opt/homebrew/bin/python3 run.py web-build --no-standalone >>"$LOG" 2>&1; then
  fail "构建或 V 系列校验失败，本轮不推送"
fi

# ---- 3. 合并进发布仓库（内容幂等：没新文档就不重写大文件）----
cd "$REPO" || fail "发布仓库不存在"
log "· [3/5] 合并数据包"
if ! /opt/homebrew/bin/python3 tools/daily_data.py --site-dir "$AGENT/out/site" --sync-state ok >>"$LOG" 2>&1; then
  fail "数据合并护栏拦截（maxd 倒退或条数骤降），本轮不推送"
fi

if [ "$(git rev-parse --abbrev-ref HEAD)" != "gh-pages" ]; then
  fail "当前分支不是 gh-pages，拒绝提交"
fi

# ---- 4. 有变化才提交（只加数据三件套）----
git add -- "${DATA_FILES[@]}"
if git diff --cached --quiet -- "${DATA_FILES[@]}"; then
  log "· [4/5] 数据无变化，不产生提交"
  log "===== 夜间更新结束（无变化）====="
  exit 0
fi

STAMP="$(date '+%Y-%m-%d')"
N="$(/opt/homebrew/bin/python3 -c 'import json;print(json.load(open("manifest.json"))["n"])')"
MAXD="$(/opt/homebrew/bin/python3 -c 'import json;print(json.load(open("manifest.json"))["maxd"])')"
if git diff --cached --name-only | grep -qx 'data.json.gz'; then
  MSG="数据包每日更新 ${STAMP}：${N} 条 / 最新 ${MAXD}（仅数据更新，UI 未变）"
  CHANGED="1"
else
  MSG="数据无新增，刷新文库与新鲜度标记 ${STAMP}（仅数据更新，UI 未变）"
  CHANGED="0"
fi
git commit -q -m "$MSG" || fail "本地提交失败"
log "· [4/5] 已提交：$MSG"

# ---- 5. 推送（先探针；最多 2 次尝试，符合「失败两次即止损」）----
code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 \
  'https://github.com/vane1981-2011/tax-policy-qa.git/info/refs?service=git-receive-pack' 2>/dev/null || echo 000)
if [ "$code" = "000" ]; then
  log "· [5/5] git 端点无响应（\($code\)），保留本地提交，等待网络恢复"
  fail "GitHub 链路不通，已保留本地提交（下次运行会自动补推）" 4
fi

push_once() {
  GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=0 -c http.postBuffer=524288000 \
    push origin HEAD >>"$LOG" 2>&1
}

if ! push_once; then
  log "· 首次推送失败，60 秒后重试（仅此一次）"
  sleep 60
  if ! push_once; then
    fail "推送两次均失败，本地提交已保留（下次运行自动补推）" 5
  fi
fi
log "· [5/5] 已推送 origin/gh-pages"

# ---- 收尾：线上确认（Pages 有缓存，最多等 3×15 秒；失败不影响本地结果）----
if [ "$CHANGED" = "1" ]; then
  for i in 1 2 3; do
    sleep 15
    live=$(curl -sS --max-time 20 \
      "https://vane1981-2011.github.io/tax-policy-qa/manifest.json?t=$(date +%s)" 2>/dev/null \
      | /opt/homebrew/bin/python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("maxd"),d.get("n"))' 2>/dev/null)
    log "· 线上核验第 ${i} 次：${live:-读取失败}"
    [ "$live" = "$MAXD $N" ] && break
  done
fi

log "===== 夜间更新完成：${N} 条 / 最新 ${MAXD} ====="
notify "税策智库·已更新" "${N} 条 · 最新 ${MAXD}"
exit 0
