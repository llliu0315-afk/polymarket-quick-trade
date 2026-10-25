"""Run BTC 5m trading UI plus read-only chain research leaderboard."""
import threading
import btc5m_trade as panel
from chain_research import Leaderboard

with panel.state_lock:
    panel.state["locked"] = True
board = Leaderboard(panel)
original_snapshot = panel._snapshot

def snapshot():
    result = original_snapshot()
    result["leaderboard"] = board.snapshot()
    return result

panel._snapshot = snapshot
panel.HTML = panel.HTML.replace('</style>', """
.lb{grid-column:1/-1}.lb table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
.lb td,.lb th{padding:7px;text-align:right;border-bottom:1px solid var(--line)}.lb td:first-child{text-align:left}.lb .scroll{overflow:auto;max-height:420px}
</style>""").replace('<div class="wrap">', '<div class="wrap"><section class="card lb"><h3>链上净持仓 · BTC 5m</h3><p id="lbstatus">等待 RPC</p><p>⚖ 双向低净敞口候选，不代表已确认做市商。胜率是历史方向命中率，不是利润；仅观察，不自动跟单。</p><p id="lbnotice"></p><div class="scroll"><table><thead><tr><th>地址</th><th>UP股</th><th>DOWN股</th><th>净UP股</th><th>本次变化</th><th>双向候选</th><th>样本/命中</th><th>95%下界</th></tr></thead><tbody id="lbrows"></tbody></table></div></section>')
panel.HTML = panel.HTML.replace('chips();connect();', """
const oldRender=render;
render=function(){oldRender();if(!S||!S.leaderboard)return;const l=S.leaderboard;
$('lbstatus').textContent=l.status+' · 区块 '+(l.block||'—')+' · 数据年龄 '+(l.updated?Math.max(0,Date.now()/1000-l.updated).toFixed(1)+'s':'—');
$('lbnotice').textContent='候选净库存 '+Number(l.candidate_skew||0).toFixed(1)+'股 · '+((l.signals||[]).length?'研究观察：'+l.signals.map(s=>s.side.toUpperCase()+' @ '+s.ask).join(' / '):'无新提示；数据不足时不提示');
const body=$('lbrows');body.replaceChildren();for(const r of(l.rows||[])){const tr=document.createElement('tr');for(const v of[r.address,...['up','down','net','change'].map(k=>Number(r[k]).toFixed(1)),r.hedged_candidate?'⚖ 候选':'—',r.samples+'/'+r.wins,(100*r.lower_bound).toFixed(1)+'%']){const td=document.createElement('td');td.textContent=v;tr.appendChild(td)}body.appendChild(tr)}};
chips();connect();""")

if __name__ == '__main__':
    threading.Thread(target=board.run, daemon=True, name='chain-research').start()
    panel.main()
