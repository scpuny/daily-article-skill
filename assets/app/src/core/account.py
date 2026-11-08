#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""账号身份（解耦层）：当前绑定公众号的 appId 与数据归属 __biz。

系统不再在代码里硬编码任何特定公众号：
  - appId  一律来自 secrets.json（按账号凭据层），经 paths.load_config() 合并读取；
  - __biz  从导入的文章链接中提取，作为数据归属标识；跨账号导入仅警告、不阻断。

两条身份落 state/account.json（chmod 600），与登录态 mp_session.json 分离，
做到「谁的数据」与「登录凭证」解耦：换号只改 secrets.json + 重新导入即可。
"""
import json
import os
import re
import time

# --- 可移植引导：向上定位 src/，兼容从 core/ 直接运行 ---
import os as _os, sys as _sys
_d = _os.path.dirname(_os.path.abspath(__file__))
while _d != _os.path.dirname(_d):
    if _os.path.isfile(_os.path.join(_d, "paths.py")):
        break
    _s = _os.path.join(_d, "src")
    if _os.path.isfile(_os.path.join(_s, "paths.py")):
        _d = _s
        break
    _d = _os.path.dirname(_d)
for _p in (_d, _os.path.dirname(_os.path.abspath(__file__))):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
import paths  # noqa: E402


def _path():
    return os.path.join(os.path.dirname(paths.mp_session()), "account.json")


def load():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data):
    p = _path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return data


def biz_from_url(url):
    """从 mp 文章链接提取 __biz（数据归属标识），取不到返回空串。

    __biz 形如 MzIxNDA5NjY0Mw==，同一公众号的所有文章链接共享同一个值，
    是区分「这是哪个账号的数据」最可靠的字段。
    """
    m = re.search(r"[?&]__biz=([^&\s\"']+)", str(url or ""))
    return m.group(1) if m else ""


def app_id():
    """appId 一律来自 secrets.json（按账号凭据），读不到返回空串。"""
    try:
        return str((paths.load_config() or {}).get("appId") or "").strip()
    except Exception:
        return ""


def detect(rows=None, app_id_value=None):
    """从导入行里提取 __biz，并与已记录的 appId 一并写回 account.json。

    返回 {biz, appId, changed, crossAccount, prevBiz}：
      - changed      : 本次提取到的 biz 与历史记录不同（仅首次或同账号刷新）
      - crossAccount : 历史已有 biz 且本次 biz 不同 → 跨账号导入（仅警告，不阻断，
                       且不改写已绑定的 biz，避免来回横跳）
    """
    biz = ""
    for r in (rows or []):
        u = r.get("url") if isinstance(r, dict) else ""
        b = biz_from_url(u)
        if b:
            biz = b
            break
    cur = load()
    prev_biz = str(cur.get("biz") or "")
    cross = bool(prev_biz) and bool(biz) and prev_biz != biz
    changed = bool(biz) and biz != prev_biz and not cross
    app = app_id_value or app_id()
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if biz and not cross:
        # 首次或同账号：写入 / 刷新绑定
        data = {"appId": app or cur.get("appId") or "",
                "biz": biz, "detectedAt": now}
        save(data)
        stored_biz = biz
    else:
        # 跨账号：保留原绑定，仅告警；无 biz：保持现状
        stored_biz = prev_biz if biz else cur.get("biz") or ""
        if not cur and app:
            save({"appId": app, "biz": "", "detectedAt": now})
    return {"biz": stored_biz, "appId": app or cur.get("appId") or "",
            "changed": changed, "crossAccount": cross, "prevBiz": prev_biz}


def state():
    """脱敏后的账号身份快照：appId/biz 只露尾号，绝不回传 Cookie/token。"""
    cur = load()
    app = str(cur.get("appId") or app_id() or "")
    biz = str(cur.get("biz") or "")
    return {
        "configured": bool(app),
        "appIdTail": app[-6:] if app else "",
        "bizTail": biz[-6:] if biz else "",
        "hasBiz": bool(biz),
        "detectedAt": cur.get("detectedAt", ""),
    }
