# BTC 5m Quick Trade (fork of JamesLHW/polymarket-quick-trade)

专注 Polymarket **BTC 5 分钟 Up/Down** 市场的一键交易面板。原版多市场脚本 `quick_trade.py` 与链上大户榜 `onchain_leaderboard.py` 保留未动，新的主程序是 **`btc5m_trade.py`**。

## 快速开始

```bash
pip install -r requirements.txt
cp .env.template .env   # 填入私钥 / 资金地址
python btc5m_trade.py   # 终端会打印带 token 的链接, 浏览器自动打开
```

## 相对原版的改动

| 项目 | 原版 | 本版 |
|---|---|---|
| 市场 | BTC/ETH × 5m/15m/1h 切换 | 仅 `btc-updown-5m`，去掉 1h slug 时区换算等逻辑 |
| 盘口 | 每 0.1s 轮询 REST `/book` | CLOB WebSocket 推送（断线 3s 后自动 REST 兜底） |
| 换轮 | 新回合开始后再查 token | 结束前 15s 预拉下一轮 token，零等待 |
| 市价买入 | 固定 +0.10 保护价盲打 | 沿 asks 逐档模拟成交，均价滑点超过阶段上限即拒绝；保护价 = 最差档 +0.01 |
| 卖出全部 | FOK | 默认 FAK，可部分成交 |
| 风控 | 无 | 热键冷却、单笔上限、单边敞口上限、最后 N 秒禁买、一键锁盘 (K) |
| 前端 | 轮询 `/api/state`，内嵌 HTML | SSE 推送，深色重构 UI：现价/锚定价/bp 差值、倒计时进度条、深度柱状盘口、实时成交模拟、连接状态灯 |
| 安全 | 无鉴权、`verify=False` | 启动随机 token + Origin 校验；TLS 校验保持开启；凭证文件 0600 |
| builder code | 写死作者的 | 从 `.env` 读取，可留空 |
| 日志 | 内存 50 条 | 追加写入 `trades.jsonl` |

## 快捷键

`↑` 买 UP · `↓` 买 DOWN · `Q`/`W` 卖出全部 UP/DOWN · `1-5` 金额 · `Esc` 撤销挂单 · `K` 锁盘/解锁

## 已知事项 / 下一步

- 依赖的 `py-clob-client-v2` 已被 Polymarket 标记弃用，计划迁移到统一 SDK `polymarket-client`。
- 尚未接入 CLOB user 频道确认真实成交均价，目前成交价为盘口模拟估算。
- `onchain_leaderboard.py` 仍为独立运行脚本，尚未与本面板联动。
