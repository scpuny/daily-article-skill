#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公众号后台自助导出：登录态获取 + HTTP GET（单篇数据明细 / 图文总表）。

登录态两种来源，二选一，最终都落到同一份 state/mp_session.json：
  1) 扫码登录（默认）：scan_start() 建网页会话并取回二维码 → 后台线程轮询确认 →
     save_session() 自动写入 token / Cookie / UA。走 mp.weixin.qq.com 自有扫码链路，
     不依赖微信开放平台 AppID、企业认证、access_token 或 IP 白名单。
  2) Copy as cURL（兜底）：parse_curl() 提取 token / Cookie / UA，用于命中风控
     二次验证、或一个微信绑定多个公众号需在浏览器选账号的场景。

download_detail() / download_total() 共用 _get_export() 的凭证、代理与登录页判定，
两种登录态来源对下游完全透明。凭证权限 600；网络请求显式 ProxyHandler({})，
避免本机 HTTP_PROXY 干扰本地后台访问。

账号解耦：本模块不硬编码任何特定公众号。扫码的 login_appid 由 _login_appid()
读取 secrets.json 的 appId（换号只改 secrets.json）；数据归属 __biz 由 account
模块从导入的文章链接提取。两者都随配置/数据走，代码层面与具体账号无关。
"""
import base64
import json
import os
import random
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# --- 可移植引导：向上定位 src/，兼容从 scripts/ 或 core/ 直接运行 ---
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

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DETAIL_EP = "https://mp.weixin.qq.com/misc/appmsganalysis"


# ---------------------------------------------------------------- cURL 解析
def parse_curl(text):
    """Chrome / Edge 的 Copy as cURL(bash) 与 PowerShell 格式都兼容。

    返回 {token, cookie, ua, sourceUrl}；缺 token 或 Cookie 直接抛 ValueError，
    绝不把半截凭证写进 state（半截凭证比没有更危险，会让按钮看似可用却静默失败）。
    """
    t = str(text or "")
    # PowerShell 用反引号续行；bash 用反斜杠续行，统一压成空格
    t = t.replace("`\r\n", " ").replace("`\n", " ").replace("\\\r\n", " ").replace("\\\n", " ")
    t = t.replace("`", "")

    urls = re.findall(r"https?://[^\s'\"`<>]+", t)
    url = urls[0].rstrip(",)") if urls else ""
    m = re.search(r"[?&]token=(\d+)", url)
    if not m:
        m = re.search(r"token[=:]\s*['\"]?(\d+)", t)
    token = m.group(1) if m else ""

    # Cookie：-H 'cookie: ...' / --header "Cookie: ..." / -b '...' / PowerShell @{ "Cookie"="..." }
    cookie = ""
    m = re.search(r"(?:-H|--header)\s+['\"]?\s*cookie\s*:\s*([^'\"\n]+)", t, re.I)
    if m:
        cookie = m.group(1).strip()
    if not cookie:
        m = re.search(r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]", t, re.I)
        if m:
            cookie = m.group(1).strip()
    if not cookie:
        m = re.search(r"[\"']?cookie[\"']?\s*[=:]\s*[\"']([^\"'\n]+)", t, re.I)
        if m:
            cookie = m.group(1).strip()
    cookie = cookie.strip().strip(";").strip()

    ua = ""
    m = re.search(r"(?:-H|--header)\s+['\"]?\s*user-agent\s*:\s*([^'\"\n]+)", t, re.I)
    if m:
        ua = m.group(1).strip()
    if not ua:
        m = re.search(r"[\"']?user-agent[\"']?\s*[=:]\s*[\"']([^\"'\n]+)", t, re.I)
        if m:
            ua = m.group(1).strip()

    missing = [k for k, v in (("token", token), ("Cookie", cookie)) if not v]
    if missing:
        raise ValueError("cURL 里缺少 " + " / ".join(missing)
                         + "；请在公众号后台首页的请求上右键 Copy as cURL")
    return {"token": token, "cookie": cookie, "ua": ua or DEFAULT_UA, "sourceUrl": url}


def save_session(session):
    """写 state/mp_session.json，chmod 600，原子替换。session 可为 cURL 原文或已解析 dict。"""
    s = session if isinstance(session, dict) and "cookie" in session else parse_curl(session)
    s = {"token": str(s["token"]), "cookie": str(s["cookie"]),
         "ua": str(s.get("ua") or DEFAULT_UA), "sourceUrl": str(s.get("sourceUrl") or ""),
         "savedAt": datetime.now().isoformat(timespec="seconds")}
    p = paths.mp_session()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return status()


def load_session():
    try:
        with open(paths.mp_session(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def status():
    """脱敏状态：绝不回传 Cookie 明文，供前端与 CLI 使用。"""
    s = load_session()
    tok = str(s.get("token") or "")
    return {"valid": bool(tok and s.get("cookie")),
            "savedAt": s.get("savedAt", ""),
            "tokenTail": tok[-4:] if tok else "",
            "hasCookie": bool(s.get("cookie")),
            "sourceUrl": str(s.get("sourceUrl") or "")[:120]}


# ---------------------------------------------------------------- 两类后台导出
def detail_url(msgid, publish_date, token):
    """单篇数据明细：action=detailpage + msgid/发表日 + download=1。"""
    q = urllib.parse.urlencode({
        "action": "detailpage", "msgid": msgid,
        "publish_date": str(publish_date)[:10], "pageVersion": "1",
        "type": "int", "token": token, "lang": "zh_CN", "download": "1",
    })
    return DETAIL_EP + "?" + q


def total_url(begin_ts, end_ts, token, publish_list_type=0):
    """图文总表：action=download_article_total + 时间窗口，不需要 download=1。"""
    q = urllib.parse.urlencode({
        "action": "download_article_total",
        "begin_timestamp": int(begin_ts), "end_timestamp": int(end_ts),
        "publish_list_type": int(publish_list_type),
        "token": token, "lang": "zh_CN",
    })
    return DETAIL_EP + "?" + q


def _get_export(url, session, timeout=20):
    """两类导出共用的单次 GET：带凭证、禁用代理、识别登录页。

    返回 (raw_bytes, error)。微信不带 Cookie 也返回 200 + HTML，所以必须按
    「是不是表格文件」判定，不能只看状态码；失败一律给中文错误、不抛异常。
    """
    s = session or load_session()
    tok = str(s.get("token") or "")
    cookie = str(s.get("cookie") or "")
    if not tok or not cookie:
        return b"", "登录态未配置：请点「登录态」扫码登录（或兜底粘贴 cURL）"
    req = urllib.request.Request(url, headers={
        "Cookie": cookie,
        "User-Agent": str(s.get("ua") or DEFAULT_UA),
        "Referer": "https://mp.weixin.qq.com/cgi-bin/home?t=home/index&lang=zh_CN&token=" + tok,
        "Accept": "*/*",
    })
    try:
        # 显式禁用环境代理：本机 HTTP_PROXY 会把 mp.weixin.qq.com 带偏
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as r:
            raw = r.read()
            ctype = (r.headers.get("Content-Type") or "").lower()
    except Exception as e:
        return b"", "请求失败: " + str(e)
    low = raw[:8192].lstrip().lower()
    # 先认「这是不是表格文件」：OLE2 真 xls / xlsx zip / HTML table / SpreadsheetML。
    # 微信导出格式会变，不能因为 HTML 版伪 xls 以 <!doctype> 开头就误判成登录页。
    is_table = (raw.startswith(b"\xd0\xcf\x11\xe0") or raw.startswith(b"PK\x03\x04")
                or b"<table" in low or b"<?xml" in low or b"<workbook" in low)
    is_html = "text/html" in ctype or low.startswith((b"<!doctype html", b"<html"))
    login_like = (b"bizlogin" in low or b"qrcode" in low
                  or "\u767b\u5f55".encode("utf-8") in low
                  or (is_html and not is_table))
    if login_like:
        return b"", "登录态失效（返回登录页）：请重新扫码登录或粘贴 cURL，本次未写入任何台账"
    if len(raw) < 100:
        return b"", "响应内容过短（%d 字节），未写入任何台账" % len(raw)
    return raw, ""


def download_detail(msgid, publish_date, session=None):
    """单篇数据明细 → (raw_bytes, error)。行为与重构前完全一致。"""
    s = session or load_session()
    if not s.get("token") or not s.get("cookie"):
        return b"", "登录态未配置：请点「登录态」扫码登录（或兜底粘贴 cURL）"
    if not msgid:
        return b"", "缺少 msgid：请先导入 total_*.xls（含「内容url」列）"
    return _get_export(detail_url(msgid, publish_date, s["token"]), s, timeout=20)


def download_total(days=30, end_ts=None, session=None, publish_list_type=0):
    """图文总表 → (raw_bytes, error, filename)。窗口 = [end - days*86400, end]。

    文件名与后台手动导出一致（total_<begin>_<end>.xls），落盘 ~/Downloads 后
    可直接被 sync_msgids_from_downloads() 扫描。失败时返回空 bytes 与空文件名。
    """
    import time
    s = session or load_session()
    if not s.get("token") or not s.get("cookie"):
        return b"", "登录态未配置：请点「登录态」扫码登录（或兜底粘贴 cURL）", ""
    try:
        end = int(end_ts) if end_ts else int(time.time())
        span = max(1, min(int(days), 365)) * 86400
    except (TypeError, ValueError):
        return b"", "days / endTs 参数无效", ""
    begin = end - span
    fname = "total_%d_%d.xls" % (begin, end)
    raw, err = _get_export(total_url(begin, end, s["token"], publish_list_type),
                           s, timeout=30)
    if err:
        return b"", err, ""
    return raw, "", fname


def check(msgid, publish_date, session=None):
    """会话自检：只探测，不写任何台账。"""
    raw, err = download_detail(msgid, publish_date, session)
    if err:
        return {"ok": False, "error": err, "bytes": 0}
    try:
        import excel_import
        text, fmt = excel_import.parse(raw, "数据明细（自检）.xls")
        return {"ok": True, "bytes": len(raw), "format": fmt,
                "head": text.splitlines()[0][:120] if text else ""}
    except Exception as e:
        return {"ok": False, "error": "文件已下载但解析失败: " + str(e), "bytes": len(raw)}


# ---------------------------------------------------------------- 扫码登录（网页会话，非开放平台 API）
# 链路（2026-11-08 本机实测）：GET / → POST bizlogin?action=startlogin（拿 uuid 会话）
#   → GET scanloginqrcode?action=getqrcode（返回 image/jpg，缺上一步会得到 0 字节）
#   → 轮询 scanloginqrcode?action=ask（status=1 表示已确认）
#   → POST bizlogin?action=login（redirect_url 带 token，CookieJar 同时拿到登录 Cookie）
# 不调用 open.weixin.qq.com 的 qrconnect，不需要企业认证 / AppSecret / access_token。
MP_BASE = "https://mp.weixin.qq.com"
_SCAN_REDIRECT = MP_BASE + "/cgi-bin/home?t=home/index&lang=zh_CN"
_SCAN_TTL = 180                 # 二维码等待窗口（秒）
_SCAN_KEEP = 600                # 会话记录在内存保留时长（秒）
_SCAN_LOCK = threading.Lock()
_SCAN = {}                      # sid -> {op, cj, qr, state, error, ...}


def _scan_gc():
    """调用方持锁；清掉过期会话，二维码与 Cookie 不长期驻留内存。"""
    now = time.time()
    for k in [k for k, v in _SCAN.items() if now - v["created"] > _SCAN_KEEP]:
        _SCAN.pop(k, None)


def _scan_call(op, url, data=None, ajax=False):
    """扫码会话内的一次请求：同一 opener/CookieJar、禁代理、UA 与 DEFAULT_UA 一致。"""
    h = {"User-Agent": DEFAULT_UA, "Referer": MP_BASE + "/",
         "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}
    if ajax:
        h.update({"X-Requested-With": "XMLHttpRequest",
                  "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=h)
    with op.open(req, timeout=15) as r:
        return r.read()


def _scan_set(sid, **kw):
    with _SCAN_LOCK:
        if sid in _SCAN:
            _SCAN[sid].update(kw)


def _login_appid():
    """扫码登录的 login_appid 一律来自 secrets.json 的 appId（按账号凭据），
    读不到回退空串。不在代码里硬编码任何特定公众号，换号只改 secrets.json。"""
    try:
        return str((paths.load_config() or {}).get("appId") or "").strip()
    except Exception:
        return ""


def scan_start(timeout=_SCAN_TTL):
    """发起扫码登录 → (sid, jpeg_bytes, error)。成功由后台线程写 mp_session.json。"""
    import http.cookiejar
    with _SCAN_LOCK:
        _scan_gc()
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                     urllib.request.HTTPCookieProcessor(cj))
    sid = str(int(time.time() * 1000))
    session_id = sid + str(random.randrange(100))      # 与登录页一致：毫秒戳 + 两位随机数
    try:
        _scan_call(op, MP_BASE + "/")                                  # ① ua_id
        # 登录页 cgiData.redirectUrl = ""，浏览器提交的 redirect_url 就是空串；
        # 之前自作主张填完整 home URL，会让 login 拿不到期望的 redirect_url。
        d = json.loads(_scan_call(
            op, MP_BASE + "/cgi-bin/bizlogin?action=startlogin",
            {"userlang": "zh_CN", "redirect_url": "",
             "login_type": "3", "sessionid": session_id}, ajax=True
        ).decode("utf-8", "ignore"))                                   # ② uuid 会话
        if d.get("base_resp", {}).get("ret") != 0:
            return "", b"", "申请二维码失败：" + str(d.get("base_resp"))
        jpg = _scan_call(op, MP_BASE + "/cgi-bin/scanloginqrcode"
                         "?action=getqrcode&random=" + sid
                         + "&login_appid=" + _login_appid())
        if not jpg.startswith(b"\xff\xd8"):                            # ③ JPEG，不是 PNG
            return "", b"", "二维码返回异常（%d 字节）" % len(jpg)
    except Exception as e:
        return "", b"", "连接公众号后台失败：" + str(e)

    try:
        ttl = max(30, min(int(timeout or _SCAN_TTL), 600))
    except (TypeError, ValueError):
        ttl = _SCAN_TTL
    rec = {"sid": sid, "op": op, "cj": cj, "qr": jpg, "state": "waiting",
           "error": "", "tokenTail": "", "trace": [], "created": time.time(),
           "deadline": time.time() + ttl, "cancel": threading.Event()}
    with _SCAN_LOCK:
        _SCAN[sid] = rec
    threading.Thread(target=_scan_worker, args=(sid,), daemon=True).start()
    return sid, jpg, ""


def _scan_safe(obj):
    """诊断轨迹脱敏：token 数字打码、截断长度；绝不记录 Cookie。"""
    return re.sub(r"token=\d+", "token=***", str(obj))[:240]


def _scan_trace(sid, tag, payload):
    with _SCAN_LOCK:
        rec = _SCAN.get(sid)
        if not rec:
            return
        rec["trace"].append({"t": time.strftime("%H:%M:%S"), "tag": tag,
                             "raw": _scan_safe(payload)})
        if len(rec["trace"]) > 40:
            del rec["trace"][:-40]


def _scan_try_login(sid, rec):
    """提交 bizlogin?action=login（参数对齐登录页：redirect_url 为空）。

    成功 → 复用 save_session() 落盘并返回 (True, "")；失败只返回原因，
    由调用方决定是否继续轮询，不因为一次失败就终止会话。
    """
    _scan_set(sid, state="confirming", error="")
    try:
        d = json.loads(_scan_call(
            rec["op"], MP_BASE + "/cgi-bin/bizlogin?action=login",
            {"userlang": "zh_CN", "redirect_url": "", "cookie_forbidden": 0,
             "cookie_cleaned": 0, "plugin_used": 0, "login_type": 3}, ajax=True
        ).decode("utf-8", "ignore"))
    except Exception as e:
        return False, "login 请求失败：" + str(e)
    _scan_trace(sid, "login", d)
    m = re.search(r"token=(\d+)", str(d.get("redirect_url") or ""))
    if not m:
        return False, "未返回 token：" + _scan_safe(d)
    cookie = "; ".join("%s=%s" % (c.name, c.value) for c in rec["cj"])
    if not cookie:
        return False, "未取得登录 Cookie"
    try:
        st = save_session({"token": m.group(1), "cookie": cookie,
                           "ua": DEFAULT_UA, "sourceUrl": str(d["redirect_url"])})
    except Exception as e:
        return False, "写入登录态失败：" + str(e)
    _scan_set(sid, state="ok", tokenTail=st.get("tokenTail", ""), error="")
    return True, ""


def _scan_worker(sid):
    """后台轮询：扫码 / 手机选公众号 / 确认登录三条路径都要能走到写盘。

    微信有两条状态通道：
      scanloginqrcode?action=ask → status=1 已确认；4/6 已扫码待确认或选账号
      loginauth?action=ask       → 选账号后的确认，status=1 可提交 login
    手机端选完公众号显示「已登录」时主通道可能停在 4/6，所以这里在 4/6
    也乐观提交 login（限频），并同时轮询 loginauth，避免静默卡住。
    """
    with _SCAN_LOCK:
        rec = _SCAN.get(sid)
    if not rec:
        return
    fails = 0
    last_try = 0.0
    while not rec["cancel"].is_set() and time.time() < rec["deadline"]:
        now = time.time()
        # ① 主通道
        try:
            q = json.loads(_scan_call(
                rec["op"], MP_BASE + "/cgi-bin/scanloginqrcode?action=ask"
            ).decode("utf-8", "ignore"))
            fails = 0
            _scan_trace(sid, "ask", q)
            st = q.get("status")
            ret = q.get("base_resp", {}).get("ret")
            if ret == 0:
                if st == 1:                              # 已确认 → 立即提交登录
                    ok, why = _scan_try_login(sid, rec)
                    if ok:
                        return
                    _scan_set(sid, state="scanned", error="已确认，正在完成登录…")
                    _scan_trace(sid, "login-retry", why)
                elif st in (2, 3):
                    _scan_set(sid, state="expired", error="二维码已取消或过期，请重新发起")
                    return
                elif st in (4, 6) or int(q.get("acct_size") or 0) > 0:
                    # 已扫码 / 手机上选了公众号：不再判失败，乐观提交登录
                    _scan_set(sid, state="scanned",
                              error="已扫码，正在完成登录（手机已选公众号请稍候）…")
                    if now - last_try > 3:
                        last_try = now
                        ok, why = _scan_try_login(sid, rec)
                        if ok:
                            return
                        _scan_trace(sid, "login-retry", why)
                else:
                    _scan_set(sid, state="waiting", error="")
            else:
                _scan_trace(sid, "ask-ret", "ret=%s" % ret)
                fails += 1                               # 会话暂未就绪不立刻判死
                if fails >= 8:
                    _scan_set(sid, state="failed",
                              error="登录会话无响应（ret=%s），请重新发起扫码" % ret)
                    return
        except Exception as e:
            fails += 1
            _scan_trace(sid, "ask-error", str(e))
            if fails >= 8:
                _scan_set(sid, state="failed", error="轮询失败：" + str(e))
                return

        # ② 选账号 / 快捷登录通道
        try:
            a = json.loads(_scan_call(
                rec["op"], MP_BASE + "/cgi-bin/loginauth?action=ask"
            ).decode("utf-8", "ignore"))
            if a.get("base_resp", {}).get("ret") == 0:
                _scan_trace(sid, "loginauth", a)
                if a.get("status") == 1:
                    ok, why = _scan_try_login(sid, rec)
                    if ok:
                        return
                    _scan_trace(sid, "login-retry", why)
        except Exception:
            pass

        # ③ Cookie 已下发但 login 未给 token：用 home 重定向兜底取 token
        if now - last_try > 6 and ({c.name for c in rec["cj"]} &
                                   {"slave_sid", "data_ticket", "slave_user"}):
            last_try = now
            try:
                req = urllib.request.Request(
                    _SCAN_REDIRECT, headers={"User-Agent": DEFAULT_UA,
                                             "Referer": MP_BASE + "/"})
                with rec["op"].open(req, timeout=12) as r:
                    final = r.geturl()
                m = re.search(r"token=(\d+)", final)
                if m:
                    cookie = "; ".join("%s=%s" % (c.name, c.value) for c in rec["cj"])
                    stt = save_session({"token": m.group(1), "cookie": cookie,
                                        "ua": DEFAULT_UA, "sourceUrl": final})
                    _scan_set(sid, state="ok", tokenTail=stt.get("tokenTail", ""), error="")
                    return
                _scan_trace(sid, "home-probe", final.split("?")[0])
            except Exception as e:
                _scan_trace(sid, "home-probe-error", str(e))

        time.sleep(1.5)
    if not rec["cancel"].is_set():
        _scan_set(sid, state="expired", error="等待扫码超时，请重新发起")


def scan_qr_data_uri(sid):
    """给前端 <img> 用的 data URI；不落盘、不写日志。"""
    with _SCAN_LOCK:
        rec = _SCAN.get(str(sid or ""))
        jpg = rec.get("qr") if rec else None
    return "data:image/jpeg;base64," + base64.b64encode(jpg or b"").decode() if jpg else ""


def scan_status(sid):
    """脱敏状态：state = waiting|scanned|confirming|ok|failed|expired|cancelled。"""
    with _SCAN_LOCK:
        _scan_gc()
        rec = _SCAN.get(str(sid or ""))
        if not rec:
            return {"ok": False, "state": "expired",
                    "error": "扫码会话不存在或已过期，请重新发起"}
        return {"ok": True, "state": rec["state"], "error": rec["error"],
                "tokenTail": rec["tokenTail"],
                "remaining": max(0, int(rec["deadline"] - time.time())),
                "trace": list(rec.get("trace") or [])[-8:]}


def scan_cancel(sid):
    with _SCAN_LOCK:
        rec = _SCAN.get(str(sid or ""))
        if rec:
            rec["cancel"].set()
            if rec["state"] != "ok":
                rec["state"] = "cancelled"
    return {"ok": True}


def debug_sessions(limit=24):
    """只读诊断：所有扫码会话的状态 + 原始响应轨迹（token 已打码，无 Cookie / 二维码）。"""
    with _SCAN_LOCK:
        _scan_gc()
        out = []
        for sid, rec in sorted(_SCAN.items(), key=lambda x: -x[1]["created"]):
            out.append({"sid": sid, "state": rec["state"], "error": rec["error"],
                        "tokenTail": rec["tokenTail"],
                        "age": int(time.time() - rec["created"]),
                        "trace": list(rec.get("trace") or [])[-limit:]})
        return {"ok": True, "total": len(out), "sessions": out}
