# BTC 5m Quick Trade

只做 Polymarket **BTC 5 分钟 Up/Down**（`btc-updown-5m`）的快捷交易面板 + 只读链上持仓研究榜。基于 JamesLHW/polymarket-quick-trade 重写，原多市场面板与终端榜单已移除。

## 文件

| 文件 | 作用 |
|---|---|
| `btc5m_trade.py` | 交易面板：CLOB WS 盘口、预拉回合、盘口模拟 + 分段滑点上限、FOK 买 / FAK 卖、风控、SSE 深色 UI、token 鉴权 |
| `chain_research.py` | 只读链上扫描：增量 `TransferSingle/Batch`、净持仓榜、对冲候选、SQLite 历史与结算、研究提示（不下单） |
| `run_btc5m.py` | 推荐入口：面板 + 链上榜一起启动，默认锁盘 |
| `CHAIN_RESEARCH.md` | 链上榜的口径、配置与限制 |

## 快速开始

```bash
pip install -r requirements.txt
cp .env.template .env        # 填私钥 / 资金地址 / ALCHEMY_KEY
python run_btc5m.py          # 仅交易面板: python btc5m_trade.py
```

终端会打印带 token 的链接，浏览器自动打开。首次运行建议把 `MAX_ORDER_USD` 调到 5。

## 快捷键

`↑` 买 UP · `↓` 买 DOWN · `Q`/`W` 卖出全部 UP/DOWN · `1-5` 金额 · `Esc` 撤销挂单 · `K` 锁盘/解锁

## 风控默认值（.env 可改）

单笔 ≤ $200 · 单边敞口 ≤ $500 · 热键冷却 400ms · 最后 5 秒禁止市价买 · 滑点上限 0.03 / 0.05 / 0.08（剩余 >240s / 60–240s / <60s）

## 已知事项

- `py-clob-client-v2` 已被 Polymarket 标记弃用，计划迁移到统一 SDK `polymarket-client`。
- 成交均价为盘口模拟估算，尚未接入 user 频道确认真实 fill。
- 链上榜为实验版：初始化余额未逐地址对账、命中率不等于盈利，详见 `CHAIN_RESEARCH.md`。
- 未做实盘联调；交易前请先以小额验证。
