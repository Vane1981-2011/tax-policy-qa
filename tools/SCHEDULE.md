# 定时更新调度（2026-11-20 起生效）

## 当前生效

| 时段 | 时间 | 任务 |
|---|---|---|
| 早晨（9 点前） | 08:10 | `com.vane.taxsync.publish` → `tools/nightly.sh` |
| 中午（12:00–14:00） | 12:30 | 同上 |
| 晚上（18:00 后） | 20:00 | 同上 |

每次执行闭环：`同步 yingtax（含 6 个扩展模块）→ web-build → daily_data 合并护栏 → 有变化才提交 → 推送 → 线上核验`。
内容无变化时不产生提交（幂等）。

## 已停用（保留 plist 以便回滚，文件后缀 .disabled）

| 任务 | 原频率 | 停用原因 |
|---|---|---|
| `com.vane.taxsync` | 每 15 分钟（≈96 次/天） | 积分消耗过大，收益低 |
| `com.vane.taxsync.daily`（09:00） | 1 次/天 | 每轮被 `hls` 源 403 拖垮且从不发布；**用户不需要日报** |
| `com.vane.taxsync.nightly`（20:00） | 1 次/天 | 已合并进 `com.vane.taxsync.publish` |

## 不产出 / 不维护

- **日报已退役**：`tax-agent/out/daily/*.md` 不再生成（历史文件保留，仅作存档）。
- `hls`（`hls-tax-policy.app.workbuddy.host`）长期 403，属厂商侧封锁，**不做重试**。

## 自查

```bash
tail -30 "/Users/apple/WorkBuddy/工作台搭建/tax-policy-qa/tools/logs/nightly.log"
launchctl list | grep com.vane.taxsync
```

页面状态条应显示「数据新鲜（≤1 天）· 采集正常」。

## 回滚

```bash
cd ~/Library/LaunchAgents
launchctl bootout gui/$(id -u)/com.vane.taxsync.publish
mv com.vane.taxsync.nightly.plist.disabled com.vane.taxsync.nightly.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.vane.taxsync.nightly.plist
```
