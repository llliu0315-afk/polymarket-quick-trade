#!/usr/bin/env python3
"""
btc5m_trade.py — Polymarket BTC 5 分钟 Up/Down 专用快捷交易面板

启动:  python btc5m_trade.py
打开:  http://127.0.0.1:8890  (启动时终端会打印带 token 的完整链接)

快捷键:
  ↑ = 买 UP        ↓ = 买 DOWN
  Q = 卖出全部 UP   W = 卖出全部 DOWN
  1-5 = 选金额      Esc = 撤销全部挂单      K = 锁盘/解锁

相对原版 quick_trade.py 的改动:
  * 只保留 btc-updown-5m 单市场, 删除多市场切换/1h slug 等逻辑
  * 盘口改为 CLOB WebSocket 推送 (REST 轮询仅作断线兜底)
  * 回合结束前 15 秒预拉下一轮 token_id, 换轮零等待
  * 下单前沿盘口逐档模拟成交, 按回合阶段限制最大滑点
  * 卖出全部默认 FAK (可部分成交), 买入 FOK
  * 热键冷却 / 单笔上限 / 单边敞口上限 / 一键锁盘
  * 页面通过 SSE 推送状态, 所有写接口需 token + Origin 校验
  * 不再关闭 TLS 校验; builder code 从 .env 读取 (可留空)
  * 交易记录追加写入 trades.jsonl
"""

import asyncio
import json
import math
import os
import secrets
import subprocess
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime, timezone

import requests
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ── 常量 ──────────────────────────────────────────────────────────────────────
CHAIN_ID = 137
SIGNATURE_TYPE = int(os.getenv("SIGNATURE_TYPE", "3"))   # 3 = Polymarket Deposit Wallet
CLOB_HTTP = "https://clob.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
RTDS_WS = "wss://ws-live-data.polymarket.com"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
STRIKE_API = "https://polymarket.com/api/crypto/crypto-price"

SLUG_PREFIX = "btc-updown-5m"
ROUND_SECONDS = 300
PREFETCH_BEFORE_END = 15          # 回合结束前 N 秒预拉下一轮 token

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8890"))
BUILDER_CODE = os.getenv("BUILDER_CODE", "").strip()   # 留空则不附带

# ── 风控参数 (.env 可覆盖) ─────────────────────────────────────────────────────
MAX_ORDER_USD = float(os.getenv("MAX_ORDER_USD", "200"))
MAX_SIDE_EXPOSURE_USD = float(os.getenv("MAX_SIDE_EXPOSURE_USD", "500"))
HOTKEY_COOLDOWN_MS = int(os.getenv("HOTKEY_COOLDOWN_MS", "400"))
# 按剩余秒数分档的最大允许滑点 (相对最优价, 单位: 美元/股)
SLIPPAGE_TIERS = [
    (240, float(os.getenv("SLIP_EARLY", "0.03"))),   # 剩余 > 240s
    (60,  float(os.getenv("SLIP_MID", "0.05"))),     # 60 ~ 240s
    (0,   float(os.getenv("SLIP_LATE", "0.08"))),    # < 60s
]
BLOCK_MARKET_LAST_SECONDS = int(os.getenv("BLOCK_LAST_SECS", "5"))   # 最后 N 秒禁止市价买入

AMOUNT_PRESETS = [5, 10, 20, 50, 100]

# ── 账号 ──────────────────────────────────────────────────────────────────────
def _load_accounts():
    accts = []
    pk, fd = os.getenv("QUICK_PRIVATE_KEY", ""), os.getenv("QUICK_FUNDER", "")
    if pk and fd:
        accts.append({"private_key": pk, "funder": fd, "label": fd[:6] + ".." + fd[-4:]})
    for i in range(2, 20):
        pk = os.getenv(f"QUICK_PRIVATE_KEY_{i}", "") or os.getenv(f"QUICK_PRIVATE_KEY_{i:02d}", "")
        fd = os.getenv(f"QUICK_FUNDER_{i}", "") or os.getenv(f"QUICK_FUNDER_{i:02d}", "")
        if pk and fd:
            accts.append({"private_key": pk, "funder": fd, "label": fd[:6] + ".." + fd[-4:]})
        else:
            break
    return accts

ACCOUNTS = _load_accounts()
_account_idx = 0
_account_lock = threading.Lock()

def _current_account():
    with _account_lock:
        idx = _account_idx
    if idx < len(ACCOUNTS):
        return ACCOUNTS[idx]
    return {"private_key": "", "funder": "", "label": "N/A"}

# ── 全局状态 ──────────────────────────────────────────────────────────────────
state_lock = threading.Lock()
state = {
    "round_ts": 0, "round_end": 0,
    "token_ids": {},            # {"up": id, "down": id}
    "next_token_ids": {}, "next_round_ts": 0,
    "strike": 0.0, "strike_confirmed": False,
    "btc": 0.0,
    "up_bid": 0.0, "up_ask": 0.0, "down_bid": 0.0, "down_ask": 0.0,
    "up_book": {"bids": [], "asks": []}, "down_book": {"bids": [], "asks": []},
    "up_position": 0.0, "down_position": 0.0,
    "usdc_balance": 0.0,
    "locked": False,
    "conn": {"rtds": False, "clob_ws": False},
    "last_book_ts": 0.0,
}
_rtds_prices = {"btc/usd": 0.0}      # 兼容 onchain_leaderboard 嵌入写入
_embedded = False
trade_log = deque(maxlen=60)
_refresh_event = threading.Event()   # 交易后立即刷新余额/持仓
_last_trade_ts = 0.0
_trade_gate = threading.Lock()
API_TOKEN = secrets.token_urlsafe(24)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = os.path.join(_BASE_DIR, "trades.jsonl")

_http = requests.Session()
_http.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})


def _get_json(url, params=None, timeout=5):
    """GET → JSON. 保持 TLS 校验; 若被 WAF 拦截, 回退系统 curl (同样校验证书)."""
    try:
        r = _http.get(url, params=params, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    try:
        full = url if not params else url + "?" + requests.compat.urlencode(params)
        res = subprocess.run(["curl", "-s", "-H", "User-Agent: Mozilla/5.0", full],
                             capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
    except Exception:
        pass
    return None


# ── CLOB client ───────────────────────────────────────────────────────────────
_clob = None
_clob_idx = -1
_clob_lock = threading.Lock()

def _creds_path(funder):
    return os.path.join(_BASE_DIR, f".btc5m_creds_{funder[:8].lower()}.json")

def _load_creds(funder):
    try:
        p = _creds_path(funder)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            d = json.load(f)
        if d.get("funder") != funder:
            return None
        from py_clob_client_v2.clob_types import ApiCreds
        return ApiCreds(api_key=d["api_key"], api_secret=d["api_secret"], api_passphrase=d["api_passphrase"])
    except Exception:
        return None

def _save_creds(funder, creds):
    try:
        p = _creds_path(funder)
        with open(p, "w") as f:
            json.dump({"funder": funder, "api_key": creds.api_key,
                       "api_secret": creds.api_secret, "api_passphrase": creds.api_passphrase}, f)
        os.chmod(p, 0o600)
    except Exception:
        pass

def get_clob_client():
    global _clob, _clob_idx
    acct = _current_account()
    with _account_lock:
        idx = _account_idx
    with _clob_lock:
        if _clob is not None and _clob_idx == idx:
            return _clob
        if not acct["private_key"] or not acct["funder"]:
            raise RuntimeError("账号未配置 (.env QUICK_PRIVATE_KEY / QUICK_FUNDER)")
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.clob_types import ApiCreds
        c = ClobClient(host=CLOB_HTTP, key=acct["private_key"], chain_id=CHAIN_ID,
                       signature_type=SIGNATURE_TYPE, funder=acct["funder"])
        # 优先使用 .env 中显式提供的凭证 (deposit wallet 推荐)
        ek, es, ep = os.getenv("CLOB_API_KEY", ""), os.getenv("CLOB_API_SECRET", ""), os.getenv("CLOB_API_PASSPHRASE", "")
        if idx == 0 and ek and es and ep:
            c.set_api_creds(ApiCreds(api_key=ek, api_secret=es, api_passphrase=ep))
        else:
            cached = _load_creds(acct["funder"])
            if cached:
                c.set_api_creds(cached)
            else:
                creds = c.create_or_derive_api_key()
                c.set_api_creds(creds)
                _save_creds(acct["funder"], creds)
        _clob, _clob_idx = c, idx
        print(f"✅ CLOB client [{acct['label']}] 就绪")
        return c


# ── 回合 / 锚定价 ─────────────────────────────────────────────────────────────
def round_ts_now():
    now = int(time.time())
    return now - now % ROUND_SECONDS

def fetch_token_ids(rts: int) -> dict:
    data = _get_json(GAMMA_EVENTS, params={"slug": f"{SLUG_PREFIX}-{rts}"}, timeout=8)
    try:
        mkt = data[0]["markets"][0]
        toks, outs = mkt.get("clobTokenIds", "[]"), mkt.get("outcomes", "[]")
        toks = json.loads(toks) if isinstance(toks, str) else toks
        outs = json.loads(outs) if isinstance(outs, str) else outs
        return {o.lower(): toks[i] for i, o in enumerate(outs) if i < len(toks)}
    except Exception:
        return {}

def fetch_strike(rts: int):
    """返回 (price, confirmed)."""
    s = datetime.fromtimestamp(rts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    e = datetime.fromtimestamp(rts + ROUND_SECONDS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = _get_json(STRIKE_API, params={"symbol": "BTC", "eventStartTime": s,
                                         "variant": "fiveminute", "endDate": e})
    try:
        op = float(data.get("openPrice") or 0)
        if op > 0:
            return op, not data.get("incomplete", False)
    except Exception:
        pass
    return 0.0, False


# ── 盘口模拟 / 风控 ───────────────────────────────────────────────────────────
def max_slippage_now() -> float:
    with state_lock:
        remain = state["round_end"] - time.time()
    for threshold, slip in SLIPPAGE_TIERS:
        if remain > threshold:
            return slip
    return SLIPPAGE_TIERS[-1][1]

def simulate_buy(asks, amount_usd):
    """沿 asks 逐档吃单, 返回 (shares, avg_price, worst_price, filled_usd)."""
    left, shares, worst = amount_usd, 0.0, 0.0
    for lvl in sorted(asks, key=lambda x: x["p"]):
        if left <= 1e-9:
            break
        take_usd = min(left, lvl["p"] * lvl["s"])
        shares += take_usd / lvl["p"]
        left -= take_usd
        worst = lvl["p"]
    filled = amount_usd - left
    avg = filled / shares if shares else 0.0
    return shares, avg, worst, filled

def simulate_sell(bids, qty):
    left, usd, worst = qty, 0.0, 0.0
    for lvl in sorted(bids, key=lambda x: -x["p"]):
        if left <= 1e-9:
            break
        take = min(left, lvl["s"])
        usd += take * lvl["p"]
        left -= take
        worst = lvl["p"]
    filled = qty - left
    avg = usd / filled if filled else 0.0
    return filled, avg, worst, usd

def _risk_check(side, amount_usd):
    global _last_trade_ts
    with state_lock:
        if state["locked"]:
            return "已锁盘 (按 K 解锁)"
        pos = state[f"{side}_position"]
        px = state[f"{side}_ask"] or state[f"{side}_bid"]
        remain = state["round_end"] - time.time()
    if amount_usd > MAX_ORDER_USD:
        return f"单笔超限 ${amount_usd:.0f} > ${MAX_ORDER_USD:.0f}"
    if pos * px + amount_usd > MAX_SIDE_EXPOSURE_USD:
        return f"{side.upper()} 敞口将超 ${MAX_SIDE_EXPOSURE_USD:.0f}"
    if remain < BLOCK_MARKET_LAST_SECONDS:
        return f"回合最后 {BLOCK_MARKET_LAST_SECONDS}s 禁止市价买入"
    now = time.time()
    if (now - _last_trade_ts) * 1000 < HOTKEY_COOLDOWN_MS:
        return "操作过快 (冷却中)"
    return None


# ── 交易执行 ──────────────────────────────────────────────────────────────────
def _log(success, msg, ms, extra=None):
    rec = {"time": datetime.now().strftime("%H:%M:%S"), "success": success,
           "message": msg, "elapsed_ms": ms}
    trade_log.appendleft(rec)
    try:
        with open(TRADES_FILE, "a") as f:
            full = dict(rec, ts=time.time(), round_ts=state["round_ts"],
                        account=_current_account()["label"], **(extra or {}))
            f.write(json.dumps(full, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _refresh_event.set()

def _market_args(token_id, amount, price, side_const, otype):
    from py_clob_client_v2.clob_types import MarketOrderArgs
    kw = dict(token_id=token_id, amount=amount, price=price, side=side_const, order_type=otype)
    if BUILDER_CODE:
        kw["builder_code"] = BUILDER_CODE
    return MarketOrderArgs(**kw)

def execute_buy(side, amount_usd):
    global _last_trade_ts
    t0 = time.time()
    err = _risk_check(side, amount_usd)
    if err:
        return {"success": False, "message": err}
    with state_lock:
        token_id = state["token_ids"].get(side)
        asks = list(state[f"{side}_book"]["asks"])
        best_ask = state[f"{side}_ask"]
    if not token_id:
        return {"success": False, "message": "回合未就绪"}
    if not asks or best_ask <= 0:
        return {"success": False, "message": "无卖盘"}
    shares, avg, worst, filled = simulate_buy(asks, amount_usd)
    slip = max_slippage_now()
    if filled + 0.01 < amount_usd:
        return {"success": False, "message": f"深度不足: 仅可成交 ${filled:.2f}"}
    if avg - best_ask > slip:
        return {"success": False, "message": f"预计滑点 {avg-best_ask:.3f} > 上限 {slip:.2f}, 拒绝"}
    limit_px = round(min(worst + 0.01, 0.99), 2)
    try:
        from py_clob_client_v2.clob_types import OrderType
        from py_clob_client_v2.order_builder.constants import BUY
        client = get_clob_client()
        _last_trade_ts = time.time()
        signed = client.create_market_order(_market_args(token_id, round(amount_usd, 2), limit_px, BUY, OrderType.FOK))
        resp = client.post_order(signed, OrderType.FOK)
        ms = int((time.time() - t0) * 1000)
        status = (resp or {}).get("status", "")
        oid = (resp or {}).get("orderID", (resp or {}).get("id", ""))
        ok = status not in ("", "unmatched", "failed") if status else True
        msg = f"BUY {side.upper()} ~{shares:.1f}sh @≈{avg:.3f} (${amount_usd:.0f}) [{status or 'sent'}]"
        _log(ok, msg, ms, {"side": side, "action": "buy", "usd": amount_usd, "est_avg": avg, "order_id": oid})
        return {"success": ok, "message": msg, "elapsed_ms": ms}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        msg = f"BUY {side.upper()} 失败: {str(e)[:120]}"
        _log(False, msg, ms)
        return {"success": False, "message": msg, "elapsed_ms": ms}

def execute_sell(side, amount, mode="fak"):
    """amount = 'all' | USD. 默认 FAK (允许部分成交)."""
    global _last_trade_ts
    t0 = time.time()
    with state_lock:
        if state["locked"]:
            return {"success": False, "message": "已锁盘"}
        token_id = state["token_ids"].get(side)
        bids = list(state[f"{side}_book"]["bids"])
        best_bid = state[f"{side}_bid"]
        pos = state[f"{side}_position"]
    if not token_id:
        return {"success": False, "message": "回合未就绪"}
    if pos <= 0.01:
        return {"success": False, "message": "无持仓"}
    if not bids or best_bid <= 0:
        return {"success": False, "message": "无买盘"}
    if (time.time() - _last_trade_ts) * 1000 < HOTKEY_COOLDOWN_MS:
        return {"success": False, "message": "操作过快 (冷却中)"}
    qty = pos if amount == "all" else min(pos, float(amount) / best_bid)
    qty = math.floor(qty * 100) / 100
    if qty <= 0:
        return {"success": False, "message": "可卖份额为 0"}
    filled, avg, worst, usd = simulate_sell(bids, qty)
    limit_px = round(max(worst - 0.01, 0.01), 2) if filled > 0 else round(max(best_bid - 0.05, 0.01), 2)
    try:
        from py_clob_client_v2.clob_types import OrderType
        from py_clob_client_v2.order_builder.constants import SELL
        otype = OrderType.FAK if mode == "fak" else OrderType.FOK
        client = get_clob_client()
        _last_trade_ts = time.time()
        signed = client.create_market_order(_market_args(token_id, round(qty, 2), limit_px, SELL, otype))
        resp = client.post_order(signed, otype)
        ms = int((time.time() - t0) * 1000)
        status = (resp or {}).get("status", "")
        oid = (resp or {}).get("orderID", (resp or {}).get("id", ""))
        ok = status not in ("unmatched", "failed")
        tag = "ALL " if amount == "all" else ""
        msg = f"SELL {tag}{side.upper()} {qty:.1f}sh @≈{avg:.3f} (${usd:.1f}) [{status or 'sent'}]"
        _log(ok, msg, ms, {"side": side, "action": "sell", "shares": qty, "est_avg": avg, "order_id": oid})
        return {"success": ok, "message": msg, "elapsed_ms": ms}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        msg = f"SELL {side.upper()} 失败: {str(e)[:120]}"
        _log(False, msg, ms)
        return {"success": False, "message": msg, "elapsed_ms": ms}

def execute_limit(side, buy_or_sell, price, amount_usd):
    t0 = time.time()
    with state_lock:
        if state["locked"]:
            return {"success": False, "message": "已锁盘"}
        token_id = state["token_ids"].get(side)
    if not token_id:
        return {"success": False, "message": "回合未就绪"}
    price = round(float(price), 2)
    if not 0.01 <= price <= 0.99:
        return {"success": False, "message": "限价需在 0.01-0.99"}
    if amount_usd > MAX_ORDER_USD:
        return {"success": False, "message": f"单笔超限 ${MAX_ORDER_USD:.0f}"}
    shares = round(amount_usd / price, 2)
    try:
        from py_clob_client_v2.clob_types import OrderArgs, OrderType
        from py_clob_client_v2.order_builder.constants import BUY, SELL
        client = get_clob_client()
        kw = dict(token_id=token_id, price=price, size=shares, side=BUY if buy_or_sell == "buy" else SELL)
        if BUILDER_CODE:
            kw["builder_code"] = BUILDER_CODE
        signed = client.create_order(OrderArgs(**kw))
        resp = client.post_order(signed, OrderType.GTC)
        ms = int((time.time() - t0) * 1000)
        oid = (resp or {}).get("orderID", (resp or {}).get("id", ""))
        msg = f"LIMIT {buy_or_sell.upper()} {side.upper()} {shares:.1f}sh @ {price:.2f}"
        _log(True, msg, ms, {"side": side, "action": f"limit_{buy_or_sell}", "price": price, "order_id": oid})
        return {"success": True, "message": msg, "elapsed_ms": ms}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        msg = f"LIMIT 失败: {str(e)[:120]}"
        _log(False, msg, ms)
        return {"success": False, "message": msg, "elapsed_ms": ms}

def execute_cancel_all():
    t0 = time.time()
    try:
        resp = get_clob_client().cancel_all()
        n = len(resp.get("canceled", [])) if isinstance(resp, dict) else 0
        ms = int((time.time() - t0) * 1000)
        _log(True, f"已撤销 {n} 笔挂单", ms)
        return {"success": True, "message": f"已撤销 {n} 笔挂单", "elapsed_ms": ms}
    except Exception as e:
        ms = int((time.time() - t0) * 1000)
        _log(False, f"撤单失败: {str(e)[:120]}", ms)
        return {"success": False, "message": f"撤单失败: {str(e)[:120]}", "elapsed_ms": ms}

def handle_trade(d: dict) -> dict:
    with _trade_gate:
        action, side = d.get("action", ""), d.get("side", "")
        amount = d.get("amount", 20)
        if action == "cancel_all":
            return execute_cancel_all()
        if action == "toggle_lock":
            with state_lock:
                state["locked"] = not state["locked"]
                return {"success": True, "message": "已锁盘" if state["locked"] else "已解锁", "locked": state["locked"]}
        if side not in ("up", "down"):
            return {"success": False, "message": "无效方向"}
        if action == "buy":
            return execute_buy(side, float(amount))
        if action == "sell_all":
            return execute_sell(side, "all", d.get("mode", "fak"))
        if action == "sell":
            return execute_sell(side, float(amount), d.get("mode", "fak"))
        if action in ("limit_buy", "limit_sell"):
            return execute_limit(side, action.split("_")[1], d.get("price", 0), float(amount))
        return {"success": False, "message": f"未知操作 {action}"}


# ── 后台: 回合 / 锚定价 / 余额 ────────────────────────────────────────────────
def round_worker():
    last_round, strike_done = 0, 0
    last_bal, last_bal_idx = 0.0, -1
    while True:
        strike_pending = False
        try:
            rts = round_ts_now()
            # 新回合: 优先使用预拉的 token
            if rts != last_round:
                with state_lock:
                    pre = state["next_token_ids"] if state["next_round_ts"] == rts else {}
                toks = pre or fetch_token_ids(rts)
                if toks:
                    with state_lock:
                        state.update(round_ts=rts, round_end=rts + ROUND_SECONDS, token_ids=toks,
                                     strike=0.0, strike_confirmed=False, up_position=0.0, down_position=0.0,
                                     next_token_ids={}, next_round_ts=0,
                                     up_book={"bids": [], "asks": []}, down_book={"bids": [], "asks": []},
                                     up_bid=0.0, up_ask=0.0, down_bid=0.0, down_ask=0.0)
                    last_round = rts
                    _refresh_event.set()
                    print(f"🔄 新回合 {datetime.fromtimestamp(rts).strftime('%H:%M:%S')} {'(预拉)' if pre else ''}")
            # 预拉下一轮
            with state_lock:
                need_pre = (state["round_end"] - time.time() <= PREFETCH_BEFORE_END
                            and state["next_round_ts"] != rts + ROUND_SECONDS)
            if need_pre:
                nxt = fetch_token_ids(rts + ROUND_SECONDS)
                if nxt:
                    with state_lock:
                        state["next_token_ids"], state["next_round_ts"] = nxt, rts + ROUND_SECONDS
            # 锚定价
            if last_round and strike_done != last_round:
                strike_pending = True
                px, ok = fetch_strike(last_round)
                if px > 0:
                    with state_lock:
                        state["strike"], state["strike_confirmed"] = px, ok
                    if ok:
                        strike_done = last_round
                        strike_pending = False
                        print(f"📌 锚定价 ${px:,.2f}")
            # 余额 / 持仓 (5s 或交易后立即)
            acct = _current_account()
            with _account_lock:
                idx = _account_idx
            if acct["private_key"] and (idx != last_bal_idx or time.time() - last_bal >= 5 or _refresh_event.is_set()):
                _refresh_event.clear()
                last_bal, last_bal_idx = time.time(), idx
                try:
                    from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                    c = get_clob_client()
                    d = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
                    b = d.get("balance") or d.get("collateral", {}).get("balance", 0) if isinstance(d, dict) else 0
                    with state_lock:
                        state["usdc_balance"] = float(b) / 1e6 if b else 0.0
                        toks = dict(state["token_ids"])
                    for sd in ("up", "down"):
                        if toks.get(sd):
                            cd = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(toks[sd])))
                            cb = cd.get("balance", 0) if isinstance(cd, dict) else 0
                            with state_lock:
                                state[f"{sd}_position"] = float(cb) / 1e6 if cb else 0.0
                except Exception as e:
                    print(f"[balance] {e}")
        except Exception as e:
            print(f"[round_worker] {e}")
        time.sleep(1 if strike_pending else 2)


# ── 后台: WebSocket (CLOB 盘口 + RTDS 现价) ───────────────────────────────────
def _apply_book(side, bids, asks):
    tb = sorted(({"p": float(b["price"]), "s": float(b["size"])} for b in bids), key=lambda x: -x["p"])[:8]
    ta = sorted(({"p": float(a["price"]), "s": float(a["size"])} for a in asks), key=lambda x: x["p"])[:8]
    with state_lock:
        state[f"{side}_book"] = {"bids": tb, "asks": ta}
        state[f"{side}_bid"] = tb[0]["p"] if tb else 0.0
        state[f"{side}_ask"] = ta[0]["p"] if ta else 0.0
        state["last_book_ts"] = time.time()

def _apply_price_change(side, changes):
    """price_change 事件: 增量更新对应档位."""
    with state_lock:
        book = {"bids": list(state[f"{side}_book"]["bids"]), "asks": list(state[f"{side}_book"]["asks"])}
    for ch in changes:
        px, sz = float(ch.get("price", 0)), float(ch.get("size", 0))
        key = "bids" if str(ch.get("side", "")).upper() == "BUY" else "asks"
        lv = [l for l in book[key] if abs(l["p"] - px) > 1e-9]
        if sz > 0:
            lv.append({"p": px, "s": sz})
        book[key] = lv
    _apply_book(side, [{"price": l["p"], "size": l["s"]} for l in book["bids"]],
                [{"price": l["p"], "size": l["s"]} for l in book["asks"]])

def ws_worker():
    import websockets

    async def clob_loop():
        while True:
            with state_lock:
                toks = dict(state["token_ids"])
            ids = [v for v in toks.values() if v]
            if not ids:
                await asyncio.sleep(1)
                continue
            inv = {v: k for k, v in toks.items()}
            try:
                async with websockets.connect(CLOB_WS, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"assets_ids": ids, "type": "market"}))
                    with state_lock:
                        state["conn"]["clob_ws"] = True
                    async for raw in ws:
                        with state_lock:
                            if dict(state["token_ids"]) != toks:
                                break          # 换轮 → 重新订阅
                        if not raw or not raw.strip():
                            continue
                        try:
                            data = json.loads(raw)
                        except ValueError:
                            continue
                        for item in (data if isinstance(data, list) else [data]):
                            if not isinstance(item, dict):
                                continue
                            evt, aid = item.get("event_type"), item.get("asset_id")
                            if evt == "book" and aid in inv:
                                _apply_book(inv[aid], item.get("bids", []), item.get("asks", []))
                            elif evt == "price_change":
                                pcs = item.get("price_changes") or item.get("changes") or []
                                by_side = {}
                                for pc in pcs:
                                    a = pc.get("asset_id", aid)
                                    if a in inv:
                                        by_side.setdefault(inv[a], []).append(pc)
                                for sd, chs in by_side.items():
                                    _apply_price_change(sd, chs)
            except Exception as e:
                print(f"[clob_ws] {e}")
            with state_lock:
                state["conn"]["clob_ws"] = False
            await asyncio.sleep(1)

    async def rtds_loop():
        while True:
            try:
                async with websockets.connect(RTDS_WS, ping_interval=20, ping_timeout=10) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": [
                        {"topic": "crypto_prices_chainlink", "type": "*"}]}))
                    with state_lock:
                        state["conn"]["rtds"] = True
                    async for raw in ws:
                        if not raw or not raw.strip():
                            continue
                        try:
                            data = json.loads(raw)
                        except ValueError:
                            continue
                        p = data.get("payload", {}) if isinstance(data, dict) else {}
                        if isinstance(p, dict) and p.get("value") and str(p.get("symbol", "")).lower() == "btc/usd":
                            v = float(p["value"])
                            if v > 0:
                                _rtds_prices["btc/usd"] = v
                                with state_lock:
                                    state["btc"] = v
            except Exception as e:
                print(f"[rtds] {e}")
            with state_lock:
                state["conn"]["rtds"] = False
            await asyncio.sleep(2)

    async def rest_fallback():
        """WS 断开或 3s 无盘口更新时, 以 1s REST 兜底."""
        s = requests.Session()
        while True:
            await asyncio.sleep(1)
            with state_lock:
                stale = time.time() - state["last_book_ts"] > 3
                toks = dict(state["token_ids"])
            if not stale:
                continue
            for sd, tid in toks.items():
                try:
                    r = s.get(f"{CLOB_HTTP}/book", params={"token_id": tid}, timeout=2)
                    if r.status_code == 200:
                        b = r.json()
                        _apply_book(sd, b.get("bids", []), b.get("asks", []))
                except Exception:
                    pass

    async def main():
        await asyncio.gather(clob_loop(), rtds_loop(), rest_fallback())

    asyncio.new_event_loop().run_until_complete(main())


# ── Web ───────────────────────────────────────────────────────────────────────
def _snapshot():
    with state_lock:
        s = json.loads(json.dumps(state))
    s["btc"] = s["btc"] or _rtds_prices.get("btc/usd", 0.0)
    s["now"] = time.time()
    s["account"] = _current_account()["label"]
    s["accounts"] = [a["label"] for a in ACCOUNTS]
    s["account_idx"] = _account_idx
    s["log"] = list(trade_log)
    s["max_slip"] = max_slippage_now()
    s["limits"] = {"order": MAX_ORDER_USD, "exposure": MAX_SIDE_EXPOSURE_USD}
    s.pop("next_token_ids", None)
    return s

def _authorized(req):
    if req.headers.get("X-Auth-Token") != API_TOKEN:
        return False
    origin = req.headers.get("Origin", "")
    return (not origin) or origin.startswith(f"http://{HOST}:{PORT}") or origin.startswith(f"http://localhost:{PORT}")

async def index(req):
    if req.query.get("token") != API_TOKEN:
        return web.Response(text="需要 token: 请使用终端打印的完整链接打开", status=403)
    return web.Response(text=HTML.replace("__TOKEN__", API_TOKEN).replace("__PRESETS__", json.dumps(AMOUNT_PRESETS)),
                        content_type="text/html", charset="utf-8")

async def trade_api(req):
    if not _authorized(req):
        return web.json_response({"success": False, "message": "unauthorized"}, status=401)
    data = await req.json()
    res = await asyncio.get_event_loop().run_in_executor(None, handle_trade, data)
    return web.json_response(res)

async def account_api(req):
    global _account_idx
    if not _authorized(req):
        return web.json_response({"success": False}, status=401)
    d = await req.json()
    i = int(d.get("idx", 0))
    if 0 <= i < len(ACCOUNTS):
        with _account_lock:
            _account_idx = i
        _refresh_event.set()
        return web.json_response({"success": True})
    return web.json_response({"success": False, "message": "无效账号"})

async def stream(req):
    if req.query.get("token") != API_TOKEN:
        return web.Response(status=403)
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await resp.prepare(req)
    try:
        while True:
            await resp.write(f"data: {json.dumps(_snapshot())}\n\n".encode())
            await asyncio.sleep(0.25)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return resp

HTML = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>BTC 5m · Quick Trade</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0b0e14;--panel:#121722;--line:#1f2736;--txt:#e6e9ef;--mut:#7d879c;--up:#22c55e;--dn:#ef4444;--acc:#3b82f6;--warn:#f59e0b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);font:14px/1.4 ui-sans-serif,system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.top{display:flex;align-items:center;gap:14px;padding:10px 16px;border-bottom:1px solid var(--line);background:var(--panel)}
.top h1{font-size:15px;margin:0;letter-spacing:.5px}.dot{width:8px;height:8px;border-radius:50%;display:inline-block;background:#555;margin-right:4px}
.dot.on{background:var(--up)}.pill{padding:3px 9px;border:1px solid var(--line);border-radius:999px;color:var(--mut);font-size:12px}
.grow{flex:1}select,input{background:#0f141d;color:var(--txt);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font-size:13px}
.wrap{display:grid;grid-template-columns:1fr 1fr 320px;gap:12px;padding:12px 16px;max-width:1500px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
.hero{grid-column:1/3;display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;align-items:center}
.k{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px}.v{font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
.v.sm{font-size:18px}.delta{font-size:13px;margin-left:6px}.pos{color:var(--up)}.neg{color:var(--dn)}
.bar{height:6px;background:#1b2230;border-radius:4px;overflow:hidden;margin-top:6px}.bar i{display:block;height:100%;background:var(--acc);transition:width .25s linear}
.bar i.late{background:var(--warn)}.bar i.end{background:var(--dn)}
.side{display:flex;flex-direction:column;gap:10px}.side.up .big{background:linear-gradient(180deg,#16a34a,#15803d)}.side.down .big{background:linear-gradient(180deg,#dc2626,#b91c1c)}
.big{border:0;border-radius:12px;color:#fff;padding:16px;font-size:18px;font-weight:700;cursor:pointer;display:flex;justify-content:space-between;align-items:center;transition:transform .05s}
.big:active{transform:scale(.98)}.big small{display:block;font-weight:400;font-size:12px;opacity:.85}.big .px{font-size:30px;font-variant-numeric:tabular-nums}
.row{display:flex;gap:8px}.btn{flex:1;border:1px solid var(--line);background:#0f141d;color:var(--txt);border-radius:8px;padding:9px;cursor:pointer;font-size:13px}
.btn:hover{border-color:var(--acc)}.btn.danger{color:#fca5a5}.btn.active{background:var(--acc);border-color:var(--acc);color:#fff}
.book{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-variant-numeric:tabular-nums;font-size:12px}
.lvl{position:relative;display:flex;justify-content:space-between;padding:2px 6px;border-radius:4px;overflow:hidden}
.lvl b{position:absolute;top:0;bottom:0;right:0;opacity:.14;z-index:0}.lvl span{position:relative;z-index:1}
.lvl.bid b{background:var(--up)}.lvl.ask b{background:var(--dn)}.lvl.bid{text-align:left}
.posbox{display:flex;justify-content:space-between;font-size:13px;color:var(--mut)}.posbox strong{color:var(--txt);font-variant-numeric:tabular-nums}
.chips{display:flex;gap:6px;flex-wrap:wrap}.chip{padding:7px 0;flex:1;text-align:center;border:1px solid var(--line);border-radius:8px;cursor:pointer;font-weight:600;background:#0f141d}
.chip.active{background:var(--acc);border-color:var(--acc)}.est{font-size:12px;color:var(--mut);margin-top:6px}.est b{color:var(--txt)}
.log{max-height:360px;overflow:auto;font-size:12px;font-variant-numeric:tabular-nums}.log div{padding:5px 0;border-bottom:1px dashed var(--line);display:flex;gap:8px}
.log .t{color:var(--mut);white-space:nowrap}.log .ok{color:var(--up)}.log .bad{color:var(--dn)}.log .ms{margin-left:auto;color:var(--mut)}
.keys{font-size:11px;color:var(--mut);display:grid;grid-template-columns:1fr 1fr;gap:3px 10px}.keys kbd{background:#1b2230;border-radius:4px;padding:1px 5px;color:var(--txt);font-family:inherit}
.lock{border:1px solid var(--warn);color:var(--warn);background:transparent;border-radius:8px;padding:5px 10px;cursor:pointer;font-size:12px}.lock.on{background:var(--warn);color:#000}
.toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:#1b2230;border:1px solid var(--line);padding:10px 16px;border-radius:10px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:70vw}
.toast.show{opacity:1}.toast.bad{border-color:var(--dn)}.toast.ok{border-color:var(--up)}
@media(max-width:1100px){.wrap{grid-template-columns:1fr 1fr}.hero{grid-column:1/3}.right{grid-column:1/3}}
</style></head><body>
<div class="top"><h1>BTC 5m · Quick Trade</h1>
<span class="pill"><span class="dot" id="d_rtds"></span>RTDS</span><span class="pill"><span class="dot" id="d_clob"></span>CLOB WS</span>
<span class="pill" id="round">—</span><div class="grow"></div>
<span class="pill">pUSD <b id="bal">0.00</b></span>
<select id="acct"></select><button class="lock" id="lock" onclick="act({action:'toggle_lock'})">🔒 锁盘 (K)</button></div>
<div class="wrap">
 <div class="card hero">
  <div><div class="k">BTC 现价 (Chainlink)</div><div class="v" id="btc">—</div></div>
  <div><div class="k">锚定价 <span id="sk_tag"></span></div><div class="v" id="strike">—</div><div class="delta" id="delta"></div></div>
  <div><div class="k">剩余</div><div class="v" id="left">—</div><div class="bar"><i id="prog" style="width:0"></i></div><div class="est">当前最大滑点 <b id="slip">—</b> · 单笔上限 $<b id="lim">—</b></div></div>
 </div>
 <div class="card side up">
  <button class="big" onclick="buy('up')"><span>买 UP <small>↑ · FOK · 按盘口模拟</small></span><span class="px" id="up_ask">—</span></button>
  <div class="posbox"><span>持仓 <strong id="up_pos">0</strong> sh</span><span>市值 <strong id="up_val">$0</strong></span><span>bid <strong id="up_bid">—</strong></span></div>
  <div class="est" id="up_est"></div>
  <div class="row"><button class="btn danger" onclick="act({action:'sell_all',side:'up'})">卖出全部 (Q) FAK</button><button class="btn" onclick="act({action:'sell',side:'up',amount:amt})">卖 $<span class="amtlbl"></span></button></div>
  <div class="book" id="up_book"></div>
  <div class="row"><input id="up_lp" placeholder="限价 0.xx" style="width:90px"><button class="btn" onclick="lim('up','limit_buy')">限价买</button><button class="btn" onclick="lim('up','limit_sell')">限价卖</button></div>
 </div>
 <div class="card side down">
  <button class="big" onclick="buy('down')"><span>买 DOWN <small>↓ · FOK · 按盘口模拟</small></span><span class="px" id="down_ask">—</span></button>
  <div class="posbox"><span>持仓 <strong id="down_pos">0</strong> sh</span><span>市值 <strong id="down_val">$0</strong></span><span>bid <strong id="down_bid">—</strong></span></div>
  <div class="est" id="down_est"></div>
  <div class="row"><button class="btn danger" onclick="act({action:'sell_all',side:'down'})">卖出全部 (W) FAK</button><button class="btn" onclick="act({action:'sell',side:'down',amount:amt})">卖 $<span class="amtlbl"></span></button></div>
  <div class="book" id="down_book"></div>
  <div class="row"><input id="down_lp" placeholder="限价 0.xx" style="width:90px"><button class="btn" onclick="lim('down','limit_buy')">限价买</button><button class="btn" onclick="lim('down','limit_sell')">限价卖</button></div>
 </div>
 <div class="right" style="display:flex;flex-direction:column;gap:12px">
  <div class="card"><div class="k" style="margin-bottom:6px">金额 (1-5)</div><div class="chips" id="chips"></div>
   <div class="row" style="margin-top:8px"><button class="btn danger" onclick="act({action:'cancel_all'})">撤销全部挂单 (Esc)</button></div></div>
  <div class="card"><div class="k" style="margin-bottom:6px">快捷键</div><div class="keys">
   <span><kbd>↑</kbd> 买 UP</span><span><kbd>↓</kbd> 买 DOWN</span><span><kbd>Q</kbd> 卖全部 UP</span><span><kbd>W</kbd> 卖全部 DOWN</span>
   <span><kbd>1-5</kbd> 金额</span><span><kbd>Esc</kbd> 撤单</span><span><kbd>K</kbd> 锁盘</span><span><kbd>Enter</kbd> 输入框限价</span></div></div>
  <div class="card"><div class="k" style="margin-bottom:6px">交易记录</div><div class="log" id="log"></div></div>
 </div>
</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN="__TOKEN__",PRESETS=__PRESETS__;let amt=PRESETS[2]||20,S=null,busy=false;
const $=id=>document.getElementById(id);
function chips(){$('chips').innerHTML=PRESETS.map((p,i)=>`<div class="chip ${p===amt?'active':''}" onclick="setAmt(${p})">$${p}<br><small style="font-weight:400;color:#9aa4b8">${i+1}</small></div>`).join('');document.querySelectorAll('.amtlbl').forEach(e=>e.textContent=amt)}
function setAmt(v){amt=v;chips();render()}
function toast(m,ok){const t=$('toast');t.textContent=m;t.className='toast show '+(ok?'ok':'bad');clearTimeout(t._h);t._h=setTimeout(()=>t.className='toast',2200)}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Auth-Token':TOKEN},body:JSON.stringify(body)});return r.json()}
async function act(b){if(busy)return;busy=true;try{const r=await post('/api/trade',b);toast(r.message+(r.elapsed_ms?` (${r.elapsed_ms}ms)`:''),r.success)}catch(e){toast('请求失败 '+e,false)}busy=false}
function buy(side){act({action:'buy',side,amount:amt})}
function lim(side,action){const p=parseFloat($(side+'_lp').value);if(!p)return toast('请输入限价',false);act({action,side,price:p,amount:amt})}
function sim(asks,usd){let left=usd,sh=0,worst=0;for(const l of asks){if(left<=0)break;const t=Math.min(left,l.p*l.s);sh+=t/l.p;left-=t;worst=l.p}return{sh,avg:sh?(usd-left)/sh:0,worst,filled:usd-left}}
function book(el,b){const mx=Math.max(1,...b.bids.map(x=>x.s),...b.asks.map(x=>x.s));const row=(l,c)=>`<div class="lvl ${c}"><b style="width:${Math.min(100,l.s/mx*100)}%"></b><span>${l.p.toFixed(2)}</span><span>${Math.round(l.s)}</span></div>`;
 el.innerHTML='<div>'+b.bids.slice(0,6).map(l=>row(l,'bid')).join('')+'</div><div>'+b.asks.slice(0,6).map(l=>row(l,'ask')).join('')+'</div>'}
function fmt(n,d=2){return n?Number(n).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d}):'—'}
function render(){if(!S)return;const s=S;
 $('d_rtds').className='dot'+(s.conn.rtds?' on':'');$('d_clob').className='dot'+(s.conn.clob_ws?' on':'');
 $('btc').textContent=fmt(s.btc);$('strike').textContent=fmt(s.strike);$('sk_tag').textContent=s.strike?(s.strike_confirmed?'✓':'(未确认)'):'';
 if(s.strike&&s.btc){const d=s.btc-s.strike,bps=d/s.strike*1e4;$('delta').innerHTML=`<span class="${d>=0?'pos':'neg'}">${d>=0?'▲':'▼'} ${fmt(Math.abs(d))} (${bps.toFixed(1)} bp)</span>`}else $('delta').textContent='';
 const left=Math.max(0,s.round_end-s.now),pct=s.round_end?100-left/300*100:0;$('left').textContent=s.round_end?`${Math.floor(left/60)}:${String(Math.floor(left%60)).padStart(2,'0')}`:'—';
 const pr=$('prog');pr.style.width=pct+'%';pr.className=left<15?'end':(left<60?'late':'');
 $('round').textContent=s.round_ts?new Date(s.round_ts*1000).toLocaleTimeString()+' 回合':'等待回合';$('bal').textContent=fmt(s.usdc_balance);
 $('slip').textContent=s.max_slip.toFixed(2);$('lim').textContent=s.limits.order;$('lock').className='lock'+(s.locked?' on':'');$('lock').textContent=s.locked?'🔓 已锁盘 (K)':'🔒 锁盘 (K)';
 for(const sd of['up','down']){$(sd+'_ask').textContent=s[sd+'_ask']?s[sd+'_ask'].toFixed(2):'—';$(sd+'_bid').textContent=s[sd+'_bid']?s[sd+'_bid'].toFixed(2):'—';
  $(sd+'_pos').textContent=s[sd+'_position'].toFixed(1);$(sd+'_val').textContent='$'+(s[sd+'_position']*(s[sd+'_bid']||0)).toFixed(2);
  const r=sim(s[sd+'_book'].asks,amt);const slip=r.avg-s[sd+'_ask'];
  $(sd+'_est').innerHTML=r.sh?`$${amt} ≈ <b>${r.sh.toFixed(1)} sh</b> 均价 <b>${r.avg.toFixed(3)}</b> 滑点 <b class="${slip>s.max_slip?'neg':''}">${slip.toFixed(3)}</b>${r.filled<amt-0.01?' <span class="neg">深度不足</span>':''}`:'无卖盘';
  book($(sd+'_book'),s[sd+'_book'])}
 const sel=$('acct');if(sel.options.length!==s.accounts.length){sel.innerHTML=s.accounts.map((a,i)=>`<option value="${i}">${a}</option>`).join('')}sel.value=s.account_idx;
 $('log').innerHTML=s.log.map(l=>`<div><span class="t">${l.time}</span><span class="${l.success?'ok':'bad'}">${l.message}</span><span class="ms">${l.elapsed_ms||''}${l.elapsed_ms?'ms':''}</span></div>`).join('')}
function connect(){const es=new EventSource('/api/stream?token='+TOKEN);es.onmessage=e=>{S=JSON.parse(e.data);render()};es.onerror=()=>{es.close();setTimeout(connect,1500)}}
$('acct').onchange=e=>post('/api/account',{idx:+e.target.value}).then(r=>toast(r.success?'已切换账号':'切换失败',r.success));
document.addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'){if(e.key==='Enter'){const id=e.target.id;const sd=id.split('_')[0];lim(sd,'limit_buy')}return}
 const k=e.key;if(k==='ArrowUp'){e.preventDefault();buy('up')}else if(k==='ArrowDown'){e.preventDefault();buy('down')}
 else if(k==='q'||k==='Q')act({action:'sell_all',side:'up'});else if(k==='w'||k==='W')act({action:'sell_all',side:'down'});
 else if(k==='k'||k==='K')act({action:'toggle_lock'});else if(k==='Escape')act({action:'cancel_all'});
 else if(/^[1-5]$/.test(k)&&PRESETS[+k-1])setAmt(PRESETS[+k-1])});
chips();connect();
</script></body></html>"""


def start_background():
    threading.Thread(target=round_worker, daemon=True, name="round").start()
    threading.Thread(target=ws_worker, daemon=True, name="ws").start()

def main():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/stream", stream)
    app.router.add_post("/api/trade", trade_api)
    app.router.add_post("/api/account", account_api)
    start_background()
    url = f"http://{HOST}:{PORT}/?token={API_TOKEN}"
    print("=" * 64)
    print(f"🚀 BTC 5m Quick Trade  →  {url}")
    print(f"   账号: {', '.join(a['label'] for a in ACCOUNTS) or '未配置'}   builder_code: {'已设置' if BUILDER_CODE else '无'}")
    print(f"   风控: 单笔≤${MAX_ORDER_USD:.0f}  单边敞口≤${MAX_SIDE_EXPOSURE_USD:.0f}  冷却 {HOTKEY_COOLDOWN_MS}ms")
    print("=" * 64)
    if not _embedded:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    web.run_app(app, host=HOST, port=PORT, print=None)

if __name__ == "__main__":
    main()
