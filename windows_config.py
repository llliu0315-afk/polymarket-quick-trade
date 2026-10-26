"""Windows startup settings. Never serialize credentials to config.json."""
import json
import math
import os
import re
import tempfile
from pathlib import Path

DEFAULTS = dict(version=1, funder="", signature_type=3, max_order=5.0,
                max_exposure=25.0, slippage=0.02, cooldown=1000, last_seconds=5,
                leaderboard=False)
SECRET_FIELDS = ("private_key", "api_key", "api_secret", "passphrase", "alchemy_key")
SERVICE = "BTC5mTrade"


def validate(raw):
    if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
        raise ValueError("未知配置字段")
    c = dict(DEFAULTS, **raw)
    if type(c["version"]) is not int or c["version"] != 1:
        raise ValueError("配置版本不支持")
    if type(c["leaderboard"]) is not bool:
        raise ValueError("排行榜开关无效")
    if type(c["signature_type"]) is not int or c["signature_type"] not in (0, 1, 2, 3):
        raise ValueError("钱包类型须为0/1/2/3，需与你的账户匹配")
    if not isinstance(c["funder"], str):
        raise ValueError("资金地址无效")
    c["funder"] = c["funder"].strip()
    if c["funder"] and (not re.fullmatch(r"0x[0-9a-fA-F]{40}", c["funder"]) or int(c["funder"],16)==0):
        raise ValueError("资金地址须为非零20字节地址")
    for key, lo, hi in (("max_order",.01,10000),("max_exposure",.01,100000),
                        ("slippage",0,.1),("cooldown",100,60000),("last_seconds",1,60)):
        if isinstance(c[key], bool):
            raise ValueError(key+" 类型错误")
        try:
            v = float(c[key])
        except (ValueError, TypeError):
            raise ValueError(key+" 须为数字") from None
        if not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError(key+" 超出允许范围")
        if key in ("cooldown","last_seconds"):
            if not v.is_integer():
                raise ValueError(key+" 须为整数")
            v = int(v)
        c[key] = v
    if c["max_order"] > c["max_exposure"]:
        raise ValueError("单笔上限不可超过单边上限")
    return c


def validate_secrets(values):
    if set(values)-set(SECRET_FIELDS):
        raise ValueError("未知凭证字段")
    s = {k: str(values.get(k, "")).strip() for k in SECRET_FIELDS}
    if s["private_key"]:
        from eth_account import Account
        try:
            Account.from_key(s["private_key"])
        except Exception:
            raise ValueError("私钥格式或取值无效") from None
    if s["alchemy_key"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", s["alchemy_key"]):
        raise ValueError("Alchemy key格式无效；这里不接收URL")
    if any(len(v)>1024 for v in s.values()):
        raise ValueError("凭证过长")
    return s


def load(path):
    if not path.exists():
        return dict(DEFAULTS)
    return validate(json.loads(path.read_text(encoding="utf-8")))


def save(path, config):
    c = validate(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="config-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f, ensure_ascii=False, allow_nan=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class Vault:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("仅支持Windows原生凭据管理器")
        from keyring.backends.Windows import WinVaultKeyring
        self.backend = WinVaultKeyring()

    def read(self):
        return {k: self.backend.get_password(SERVICE,k) or "" for k in SECRET_FIELDS}

    def write(self, values):
        for k,v in values.items():
            if k not in SECRET_FIELDS:
                raise ValueError("未知凭证字段")
            if v:
                self.backend.set_password(SERVICE,k,v)
                if self.backend.get_password(SERVICE,k) != v:
                    raise RuntimeError("凭据写入验证失败")

    def delete(self):
        for k in SECRET_FIELDS:
            if self.backend.get_password(SERVICE,k) is not None:
                self.backend.delete_password(SERVICE,k)


def positive(value):
    if isinstance(value,bool):
        raise ValueError("金额无效")
    v = float(value)
    if not math.isfinite(v) or v <= 0:
        raise ValueError("金额须为有限正数")
    return v
