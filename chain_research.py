"""Read-only CTF leaderboard. No signing keys or order methods."""
import json
import math
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests
from web3 import Web3


def lower_bound(w3, timestamp, head):
    lo, hi = 0, head + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if int(w3.eth.get_block(mid)["timestamp"]) < timestamp:
            lo = mid + 1
        else:
            hi = mid
    return lo


def fetch_logs(w3, address, topics, lo, hi):
    try:
        return list(w3.eth.get_logs(dict(address=address, topics=[topics], fromBlock=lo, toBlock=hi)))
    except Exception:
        if lo == hi:
            raise
        mid = (lo + hi) // 2
        return fetch_logs(w3, address, topics, lo, mid) + fetch_logs(w3, address, topics, mid + 1, hi)


def wilson(wins, n):
    if not n:
        return 0.0
    p, z = wins / n, 1.96
    return (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1+z*z/n)


class Leaderboard:
    def __init__(self, panel):
        self.panel = panel
        self.lock = threading.Lock()
        self.view = {"status": "starting", "rows": [], "signals": []}
        self.dbpath = Path(os.getenv("LB_DB", "data/leaderboard.sqlite3"))
        self.dbpath.parent.mkdir(parents=True, exist_ok=True)
        self.confirmations = max(1, int(os.getenv("LB_CONFIRMATIONS", "8")))
        self.min_rounds = max(50, int(os.getenv("LB_MIN_ROUNDS", "100")))
        self.min_change = max(1, float(os.getenv("LB_MIN_CHANGE_SHARES", "500")))
        self.excluded = {a.strip().lower() for a in os.getenv("LB_EXCLUDE", "").split(",") if a.strip()}
        self.rts, self.cursor, self.anchor, self.saved = 0, 0, None, False
        self.bal = defaultdict(lambda: [0, 0])
        self.previous = {}

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.view))

    def market(self, rts):
        r = requests.get("https://gamma-api.polymarket.com/events", params={"slug": f"btc-updown-5m-{rts}", "closed": "any"}, timeout=10)
        r.raise_for_status()
        return r.json()[0]["markets"][0]

    def initialize(self, rts, head):
        m = self.market(rts)
        if m.get("version") not in (None, "v1", 1):
            raise RuntimeError("Only legacy CTF markets supported; Protocol V2 disabled")
        def array(x):
            return json.loads(x) if isinstance(x, str) else x
        names = [str(x).lower() for x in array(m["outcomes"])]
        tids = array(m["clobTokenIds"])
        self.ids = {int(tids[names.index(s)]): i for i, s in enumerate(("up", "down"))}
        self.condition = m["conditionId"]
        created = datetime.fromisoformat(m["createdAt"].replace("Z", "+00:00")).timestamp()
        self.start = lower_bound(self.w3, int(created)-3600, head)
        self.cursor, self.rts, self.anchor, self.saved = self.start, rts, None, False
        self.bal.clear()
        self.previous.clear()

    def apply(self, logs):
        for e in sorted(logs, key=lambda x: (x["blockNumber"], x["transactionIndex"], x["logIndex"])):
            src, dst = ["0x" + bytes(t)[-20:].hex() for t in e["topics"][2:4]]
            raw = bytes(e["data"])
            if bytes(e["topics"][0]) == bytes(self.topics[0]):
                ids, values = [int.from_bytes(raw[:32], "big")], [int.from_bytes(raw[32:64], "big")]
            else:
                ids, values = self.w3.codec.decode(["uint256[]", "uint256[]"], raw)
            for tid, value in zip(ids, values):
                if tid not in self.ids:
                    continue
                i = self.ids[tid]
                if int(src, 16):
                    self.bal[src][i] -= value
                if int(dst, 16):
                    self.bal[dst][i] += value

    def rows(self):
        result = []
        for a, (u, d) in self.bal.items():
            if a in self.excluded or max(u, d) < 500000:
                continue
            gross, net = u+d, u-d
            candidate = u > 0 and d > 0 and abs(net)/max(gross, 1) < .2
            result.append(dict(address=a, up=u/1e6, down=d/1e6, net=net/1e6,
                               hedged_candidate=candidate, change=(net-self.previous.get(a, net))/1e6))
        return sorted(result, key=lambda x: abs(x["net"]), reverse=True)

    def score(self, db, address):
        q = db.execute("SELECT o.side,r.winner FROM observations o JOIN rounds r ON r.ts=o.round_ts WHERE o.address=? AND o.candidate=0 AND r.winner IS NOT NULL AND o.round_ts<? ORDER BY o.round_ts DESC LIMIT 200", (address, self.rts)).fetchall()
        n, wins = len(q), sum(a == b for a, b in q)
        return n, wins, wilson(wins, n)

    def resolve(self, db):
        for rts, condition in db.execute("SELECT ts,condition FROM rounds WHERE winner IS NULL AND ts<? ORDER BY ts LIMIT 10", (int(time.time())-300,)).fetchall():
            c = self.contract
            cid = bytes.fromhex(condition.removeprefix("0x"))
            if c.functions.payoutDenominator(cid).call() == 0:
                continue
            vals = [c.functions.payoutNumerators(cid, i).call() for i in (0, 1)]
            mapping = json.loads(db.execute("SELECT outcomes FROM rounds WHERE ts=?", (rts,)).fetchone()[0])
            if vals[0] != vals[1]:
                db.execute("UPDATE rounds SET winner=? WHERE ts=?", (mapping[0 if vals[0] > vals[1] else 1], rts))
        db.commit()

    def run(self):
        try:
            rpc = os.getenv("POLYGON_RPC_URL") or ("https://polygon-mainnet.g.alchemy.com/v2/" + os.getenv("ALCHEMY_KEY", ""))
            self.w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
            try:
                from web3.middleware import ExtraDataToPOAMiddleware
                self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            except ImportError:
                from web3.middleware import geth_poa_middleware
                self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            if self.w3.eth.chain_id != 137:
                raise RuntimeError("RPC must be Polygon chain 137")
            address = self.w3.to_checksum_address(os.getenv("CTF_ADDRESS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"))
            self.topics = [Web3.keccak(text=s) for s in ("TransferSingle(address,address,address,uint256,uint256)", "TransferBatch(address,address,address,uint256[],uint256[])")]
            abi = [dict(type="function", name=name, stateMutability="view", inputs=[dict(name="conditionId", type="bytes32")] + ([dict(name="index", type="uint256")] if name == "payoutNumerators" else []), outputs=[dict(name="", type="uint256")]) for name in ("payoutDenominator", "payoutNumerators")]
            self.contract = self.w3.eth.contract(address=address, abi=abi)
            db = sqlite3.connect(self.dbpath)
            db.executescript("CREATE TABLE IF NOT EXISTS rounds(ts INTEGER PRIMARY KEY,condition TEXT,outcomes TEXT,winner TEXT); CREATE TABLE IF NOT EXISTS observations(round_ts INTEGER,address TEXT,side TEXT,net REAL,candidate INTEGER,observed REAL,PRIMARY KEY(round_ts,address)); CREATE TABLE IF NOT EXISTS signals(round_ts INTEGER,address TEXT,observed REAL,side TEXT,ask REAL,net_change REAL,samples INTEGER,lower_bound REAL,PRIMARY KEY(round_ts,address));")
        except Exception as e:
            with self.lock:
                self.view = {"status": str(e)[:160], "rows": [], "signals": []}
            return
        tick = 0
        while True:
            try:
                head = self.w3.eth.block_number-self.confirmations
                rts = int(time.time())//300*300
                if rts != self.rts:
                    self.initialize(rts, head)
                if self.anchor and self.w3.eth.get_block(self.cursor-1)["hash"].hex() != self.anchor:
                    db.execute("DELETE FROM observations WHERE round_ts=?", (rts,))
                    db.execute("DELETE FROM signals WHERE round_ts=?", (rts,))
                    db.commit()
                    self.initialize(rts, head)
                while self.cursor <= head:
                    end = min(self.cursor+29, head)
                    logs = fetch_logs(self.w3, address, self.topics, self.cursor, end)
                    self.apply(logs)
                    self.cursor = end+1
                    self.anchor = self.w3.eth.get_block(end)["hash"].hex()
                    if not self.saved and int(self.w3.eth.get_block(end)["timestamp"]) >= rts+120:
                        break
                rows = self.rows()
                negative = any(min(v)<0 for v in self.bal.values())
                complete = self.cursor > head
                if not self.saved and complete and not negative and time.time() >= rts+120 and time.time() < rts+180:
                    m = self.market(rts)
                    outs = m["outcomes"]
                    outs = json.loads(outs) if isinstance(outs, str) else outs
                    db.execute("INSERT OR IGNORE INTO rounds VALUES(?,?,?,NULL)", (rts, self.condition, json.dumps([str(s).lower() for s in outs])))
                    db.executemany("INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?,?)", [(rts, r["address"], "up" if r["net"]>0 else "down", r["net"], r["hedged_candidate"], time.time()) for r in rows if abs(r["net"])>=1])
                    db.commit()
                    self.saved = True
                signals = []
                for row in rows[:100]:
                    n, wins, lb = self.score(db, row["address"])
                    row.update(samples=n, wins=wins, lower_bound=lb)
                    if complete and not negative and self.saved and rts+120 <= time.time() < rts+210 and n>=self.min_rounds and lb>.55 and not row["hedged_candidate"] and abs(row["change"])>=self.min_change and row["change"]*row["net"]>0:
                        side = "up" if row["net"]>0 else "down"
                        with self.panel.state_lock:
                            ask = self.panel.state[side+"_ask"]
                            ready = self.panel.state["round_ts"] == rts and time.time()-self.panel.state["last_book_ts"] < 2
                        if ready and ask > 0:
                            db.execute("INSERT OR IGNORE INTO signals VALUES(?,?,?,?,?,?,?,?)", (rts, row["address"], time.time(), side, ask, row["change"], n, lb))
                            signals.append(dict(address=row["address"], side=side, ask=ask, lower_bound=lb))
                db.commit()
                if complete:
                    self.previous = {a: u-d for a,(u,d) in self.bal.items()}
                tick += 1
                if tick % 30 == 0:
                    self.resolve(db)
                    watch = db.execute("SELECT DISTINCT address FROM observations").fetchall()
                    qualified = [a for (a,) in watch if (lambda s: s[0]>=self.min_rounds and s[2]>.55)(self.score(db,a))]
                    dest = self.dbpath.parent / "research_watchlist.json"
                    temp = dest.with_suffix(".tmp")
                    temp.write_text(json.dumps(qualified), encoding="utf-8")
                    temp.replace(dest)
                with self.lock:
                    self.view = dict(status="negative balances: incomplete history" if negative else ("live (unverified opening balance)" if complete else "catching up"), rows=rows[:100], signals=signals, block=self.cursor-1, round_ts=rts, updated=time.time(), candidate_skew=sum(r["net"] for r in rows if r["hedged_candidate"]))
            except Exception as e:
                with self.lock:
                    self.view.update(status="scan paused: "+str(e)[:140], signals=[])
            time.sleep(2)
