"""Native Windows configuration wizard; browser never receives secrets."""
import json
import os
import socket
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
from windows_config import DEFAULTS, SECRET_FIELDS, Vault, load, save, validate, validate_secrets, positive


def launch(c, s, trading, home):
    import dotenv
    original_load = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    for k in list(os.environ):
        if k.startswith(("QUICK_", "CLOB_API_", "LB_")) or k in ("ALCHEMY_KEY","POLYGON_RPC_URL","CTF_ADDRESS"):
            os.environ.pop(k,None)
    os.environ.update(HOST="127.0.0.1",PORT="8890", SIGNATURE_TYPE=str(c["signature_type"]),
                      MAX_ORDER_USD=str(c["max_order"]),MAX_SIDE_EXPOSURE_USD=str(c["max_exposure"]),
                      SLIP_EARLY=str(c["slippage"]),SLIP_MID=str(c["slippage"]),SLIP_LATE=str(c["slippage"]),
                      HOTKEY_COOLDOWN_MS=str(c["cooldown"]),BLOCK_LAST_SECS=str(c["last_seconds"]),
                      TAKER_FEE_RATE="0.07",BUILDER_CODE="",WS_DEBUG="0")
    try:
        import btc5m_trade as p
    finally:
        dotenv.load_dotenv = original_load
    p.state["locked"] = True
    p.ACCOUNTS = ([dict(private_key=s["private_key"],funder=c["funder"],label=c["funder"][:8]+"…"+c["funder"][-4:])] if trading else [])
    p.AMOUNT_PRESETS = sorted(set(min(x,c["max_order"]) for x in (1,2,5,10,20)))
    p.TRADES_FILE = str(home / "trades.jsonl")
    client = None
    client_lock = threading.Lock()

    def get_client():
        nonlocal client
        if not trading:
            raise RuntimeError("只读模式")
        with client_lock:
            if client is None:
                from py_clob_client_v2.client import ClobClient
                from py_clob_client_v2.clob_types import ApiCreds
                client = ClobClient(host=p.CLOB_HTTP,key=s["private_key"],chain_id=137,
                                    signature_type=c["signature_type"],funder=c["funder"])
                client.set_api_creds(ApiCreds(api_key=s["api_key"],api_secret=s["api_secret"],api_passphrase=s["passphrase"]))
            return client
    p.get_clob_client = get_client
    p._save_creds = lambda *a: None
    p._load_creds = lambda *a: None
    # Never use simulated shares as available inventory.
    p._optimistic = lambda *a: None
    original_trade = p.handle_trade
    def guarded(d):
        if not trading:
            return dict(success=False,message="只读模式：请退出后在启动窗口配置交易")
        try:
            if not isinstance(d,dict):
                raise ValueError()
            action = d.get("action")
            if action in ("buy","sell","limit_buy","limit_sell"):
                amount = positive(d.get("amount"))
                if amount > c["max_order"]:
                    raise ValueError()
            if action in ("limit_buy","limit_sell"):
                price = positive(d.get("price"))
                if not .01 <= price <= .99:
                    raise ValueError()
            if action not in ("cancel_all","toggle_lock"):
                with p.state_lock:
                    if p.state["round_ts"] != p.round_ts_now() or time.time()-p.state["last_book_ts"]>2:
                        return dict(success=False,message="回合或盘口未就绪")
            result = original_trade(d)
            if not result.get("success"):
                result["message"] = "操作未确认成功：请核对订单与余额，勿盲目重试"
            return result
        except Exception:
            return dict(success=False,message="操作未完成：参数或连接异常；请先核对订单状态，勿盲目重试")
    p.handle_trade = guarded
    old_log = p._log
    def safe_log(ok,msg,ms,extra=None):
        old_log(ok,msg if ok else "请求异常：请核对订单状态",ms,extra)
    p._log = safe_log
    from aiohttp import web
    p._authorized = lambda req: (req.headers.get("X-Auth-Token")==p.API_TOKEN and req.headers.get("Origin")=="http://127.0.0.1:8890" and req.host=="127.0.0.1:8890")
    async def no_account(req):
        return web.json_response(dict(success=False,message="账户修改须退出后重新配置"),status=403)
    p.account_api = no_account
    @web.middleware
    async def local_only(req, handler):
        if req.host != "127.0.0.1:8890":
            raise web.HTTPForbidden()
        if req.method not in ("GET","HEAD") and not p._authorized(req):
            raise web.HTTPForbidden()
        try:
            response = await handler(req)
        except web.HTTPException:
            raise
        except Exception:
            return web.json_response(dict(success=False,message="内部错误，详情已隐藏"),status=500)
        if not response.prepared:
            response.headers.update({"Cache-Control":"no-store","Referrer-Policy":"no-referrer","X-Frame-Options":"DENY","X-Content-Type-Options":"nosniff"})
        return response
    original_app = p.web.Application
    def app(*a, **kw):
        kw["middlewares"] = [local_only]+list(kw.get("middlewares",[]))
        kw["client_max_size"] = 16384
        return original_app(*a,**kw)
    p.web.Application = app
    p.HTML = p.HTML.replace('chips();connect();', "history.replaceState(null,'', '/');chips();connect();")
    p.HTML = p.HTML.replace('const k=e.key;', 'if(e.repeat||e.target.closest("input,select,textarea,[contenteditable]"))return;const k=e.key;')
    p.HTML = p.HTML.replace('<div class="wrap">', '<p style="padding:8px 16px;color:#f59e0b">Windows配置模式：仅启动前可修改；凭证不写入项目文件。实盘执行核心仍为实验版。</p><div class="wrap">')
    if c["leaderboard"]:
        os.environ["ALCHEMY_KEY"] = s["alchemy_key"]
        os.environ["LB_DB"] = str(home / "leaderboard.sqlite3")
        import run_btc5m as combined
        threading.Thread(target=combined.board.run,daemon=True).start()
    # Old SDK errors may include request details. Do not echo them to console.
    import sys
    class Quiet:
        def write(self, text):
            if "BTC 5m Quick Trade" in text and "http://127.0.0.1:8890/" in text:
                sys.__stdout__.write(text + "\n")
            return len(text)
        def flush(self): pass
    sys.stdout = sys.stderr = Quiet()
    p.main()


def main():
    if os.name != "nt":
        raise SystemExit("Windows only")
    root = tk.Tk()
    root.title("BTC 5m · Windows 启动配置")
    root.geometry("760x760")
    home = Path(os.environ["LOCALAPPDATA"]) / "BTC5mTrade"
    home.mkdir(parents=True,exist_ok=True)
    import msvcrt
    lock = open(home / "instance.lock","a+b")
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        messagebox.showerror("已运行","请先退出已有窗口/终端再修改配置")
        root.destroy()
        return
    path = home / "config.json"
    try:
        existing = load(path)
    except Exception:
        messagebox.showerror("配置错误","配置文件损坏或不兼容；未自动覆盖，请先备份检查")
        root.destroy()
        return
    frame = ttk.Frame(root,padding=16)
    frame.pack(fill="both",expand=True)
    ttk.Label(frame,text="仅 BTC 5m · 配置修改后重新启动 · 默认锁盘",font=("Microsoft YaHei UI",13)).grid(row=0,column=0,columnspan=2,pady=8)
    fields = {}
    labels = {"funder":"资金地址（非签名私钥地址）","signature_type":"签名类型 0/1/2/3（按账户确认）","max_order":"单笔上限","max_exposure":"单边上限","slippage":"最大滑点（美元/股）","cooldown":"热键冷却 ms","last_seconds":"末尾禁止买入秒数"}
    row = 1
    for key,label in labels.items():
        ttk.Label(frame,text=label).grid(row=row,column=0,sticky="w",pady=4)
        v = tk.StringVar(value=str(existing[key]))
        ttk.Entry(frame,textvariable=v,width=48).grid(row=row,column=1,sticky="ew")
        fields[key]=v
        row += 1
    lb = tk.BooleanVar(value=existing["leaderboard"])
    ttk.Checkbutton(frame,text="启用链上研究榜（需要Alchemy key）",variable=lb).grid(row=row,column=0,columnspan=2,sticky="w")
    row += 1
    secrets = {}
    for key,label in zip(SECRET_FIELDS,("私钥","CLOB API key","CLOB API secret","CLOB passphrase","Alchemy key")):
        ttk.Label(frame,text=label).grid(row=row,column=0,sticky="w",pady=4)
        v=tk.StringVar()
        ttk.Entry(frame,textvariable=v,show="*",width=48).grid(row=row,column=1,sticky="ew")
        secrets[key]=v
        row += 1
    use_saved=tk.BooleanVar(value=False)
    persist=tk.BooleanVar(value=False)
    ttk.Checkbutton(frame,text="使用已有Windows凭证（仅同一账户可复用；不回显）",variable=use_saved).grid(row=row,column=0,columnspan=2,sticky="w");row+=1
    ttk.Checkbutton(frame,text="将本次非空凭证保存到Windows凭据管理器",variable=persist).grid(row=row,column=0,columnspan=2,sticky="w");row+=1
    status=tk.StringVar(value="未配置私钥也可启动只读行情。旧.env与JSON凭证缓存不会自动导入。")
    ttk.Label(frame,textvariable=status,wraplength=700).grid(row=row,column=0,columnspan=2,pady=8);row+=1
    result=[]
    def collect():
        raw={k:v.get() for k,v in fields.items()}
        raw.update(version=1,leaderboard=lb.get())
        raw["signature_type"]=int(raw["signature_type"])
        return validate(raw)
    def begin(trading):
        try:
            c=collect()
            values={k:v.get().strip() for k,v in secrets.items()}
            vault=Vault() if use_saved.get() or persist.get() else None
            if use_saved.get() and (c["funder"].lower()!=existing["funder"].lower() or c["signature_type"]!=existing["signature_type"]):
                raise ValueError("账户或签名类型已改变：取消复用并重新输入完整凭证")
            merged=vault.read() if use_saved.get() else {}
            merged.update({k:v for k,v in values.items() if v})
            s=validate_secrets(merged)
            if trading and (not c["funder"] or not all(s[k] for k in SECRET_FIELDS[:4])):
                raise ValueError("交易需资金地址、私钥和完整API凭证；不自动签名派生")
            if c["leaderboard"] and not s["alchemy_key"]:
                raise ValueError("排行榜需要Alchemy key")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1",8890))
            summary=json.dumps(c,ensure_ascii=False,indent=2)
            if not messagebox.askyesno("确认本次配置",summary+"\n"+("实验交易：尚未完成成交回执与费用校验。" if trading else "只读模式：禁止全部交易操作。")+"\n保存并启动？"):
                return
            if persist.get():
                vault.write({k:v for k,v in s.items() if v})
            save(path,c)
            if not trading:
                for k in SECRET_FIELDS[:4]: s[k]=""
            result.append((c,s,trading))
            for v in secrets.values(): v.set("")
            root.destroy()
        except ValueError as e:
            messagebox.showerror("校验失败",str(e) if len(str(e))<120 else "参数无效")
        except Exception:
            messagebox.showerror("无法启动","存储/端口/依赖检查失败。未启动交易；如已写入部分凭证，可用删除按钮清理。")
    def delete():
        if messagebox.askyesno("删除凭证","删除BTC5mTrade名下全部Windows凭证？普通配置不变。"):
            try:
                Vault().delete()
                status.set("已删除Windows凭证；输入框内容仍由你决定是否使用")
            except Exception:
                status.set("删除未全部完成，请在Windows凭据管理器中检查BTC5mTrade条目")
    def diagnose():
        status.set("检查公开行情连接中（不会签名、下单或发送你的凭证）…")
        def worker():
            try:
                import requests
                r=requests.get("https://clob.polymarket.com/time",timeout=5,allow_redirects=False)
                r.raise_for_status()
                positive(r.json())
                text="公开CLOB连接正常；尚未验证账户权限、余额或RPC。"
            except Exception:
                text="公开CLOB连接失败；错误详情已隐藏。"
            try: root.after(0,lambda:status.set(text))
            except RuntimeError: pass
        threading.Thread(target=worker,daemon=True).start()
    buttons=ttk.Frame(frame);buttons.grid(row=row,column=0,columnspan=2,pady=8)
    for label,fn in (("测试公开连接",diagnose),("保存并只读启动",lambda:begin(False)),("保存并锁盘启动交易",lambda:begin(True)),("删除保存凭证",delete)):
        ttk.Button(buttons,text=label,command=fn).pack(side="left",padx=3)
    ttk.Label(frame,text="Windows凭证不防同用户恶意程序。勿以管理员身份运行。关闭终端即停止服务；未成交挂单不会自动撤销。",wraplength=700).grid(row=row+1,column=0,columnspan=2,pady=8)
    root.mainloop()
    if result:
        try:
            launch(*result[0],home)
        except Exception:
            messagebox.showerror("启动失败","依赖或服务异常。未显示原始错误以避免泄露凭证。")
    lock.close()


if __name__ == "__main__":
    main()
