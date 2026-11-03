# -*- coding: utf-8 -*-
"""运行时设置（存数据库，可在界面改）。

和金牌系统一样：环境变量只是「首次默认值」，之后以库里为准。
"""
import config
import db

_DEFAULTS = {
    "public_base": config.PUBLIC_BASE,   # 写进 .strm 的地址
    "api_key": config.API_KEY,           # 苹果CMS 接口密码
    "last_finish": "0",                  # 上次自动更新完成时间戳
}


def get(key, default=None):
    v = db.get_setting(key, "")
    if v != "":
        return v
    if key in _DEFAULTS:
        return _DEFAULTS[key]
    return "" if default is None else default


def set_many(pairs):
    for k, v in pairs.items():
        db.set_setting(k, v)


def public_base():
    """写进 .strm 的服务地址。"""
    return (get("public_base") or "").rstrip("/")


def api_key():
    return str(get("api_key") or "").strip()


def last_finish():
    try:
        return float(get("last_finish") or 0)
    except Exception:
        return 0.0


def mark_finished():
    import time
    set_many({"last_finish": str(int(time.time()))})
