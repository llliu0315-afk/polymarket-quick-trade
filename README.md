# BTC 5m Quick Trade

只做 Polymarket **BTC 5 分钟 Up/Down**（`btc-updown-5m`）的快捷交易面板 + 只读链上持仓研究榜。基于 JamesLHW/polymarket-quick-trade 重写，原多市场面板与终端榜单已移除。

## 文件

| 文件 | 作用 |
|---|---|
| `btc5m_trade.py` | 交易面板：CLOB WS 盘口、事件驱动换轮、时钟校准、含费成交模拟、分段滑点上限、FOK 买 / FAK 卖、乐观持仓、风控、SSE 深色 UI、token 鉴权 |
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

## 费用与盈亏平衡

市价单按 taker 费估算：`fee = TAKER_FEE_RATE × 股数 × p × (1-p)`，默认 0.07（加密类）。面板显示“含费均价”，二元合约到期付 $1，所以含费均价即**盈亏平衡胜率**：例如含费 0.553 表示该方向胜率需高于 55.3% 才有正期望。限价挂单成交为 maker，不计 taker 费。费率以官方文档或 `clob-markets` 接口为准，可在 `.env` 调整或设为 0。

## 风控默认值（.env 可改）

单笔 ≤ $200 · 单边敞口 ≤ $500 · 热键冷却 400ms · 最后 5 秒禁止市价买 · 滑点上限 0.03 / 0.05 / 0.08（剩余 >240s / 60–240s / <60s）

## 运行机制

- 后台线程在回合边界、预拉时刻（结束前 15 秒）、下单后被立即唤醒，空闲时最多每 5 秒轮询一次余额。
- 启动及每 30 分钟用 CLOB `/time` 校准时钟，偏差 >1.5 秒才采用（接口只返回整秒）。
- 下单成功后持仓先按模拟份额更新并标“待确认”，最多保持 8 秒，链上余额变化后以真实值覆盖。

## 已知事项

- `py-clob-client-v2` 已被 Polymarket 标记弃用，计划迁移到统一 SDK `polymarket-client`。
- 成交均价与费用为盘口模拟估算，尚未接入 user 频道确认真实 fill。
- 首次运行会打印 5 条 CLOB WS 原始消息（`WS_DEBUG=1`），用于确认 `price_change` 字段结构；不匹配时会退回 REST 兜底，面板 CLOB WS 指示灯仍亮但盘口每秒刷新。
- 链上榜为实验版：初始化余额未逐地址对账、命中率不等于盈利，详见 `CHAIN_RESEARCH.md`。链上榜回合计算仍用本地时间，时钟偏差 >1.5 秒时提示可能因回合不一致被抑制。
- 未做实盘联调；交易前请先以小额验证。
