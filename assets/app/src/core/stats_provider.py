#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内容运营台 大屏 · 统一数据层

数据来源（优先级从高到低，前端一律按 source 显示，绝不伪造 0）：
  api  微信 datacube 图文统计接口  —— 需「已通过微信认证」+ 文章已群发/发表
  csv  公众号后台「内容分析」导出的文件（.xls / .xlsx / .csv）—— 零成本，绕开接口权限
  none 仅本地数据 —— 远端指标显示「—」并给出接入指引

持久化：articles/stats_cache.json（只保存远端指标与权限探测结果）
本地指标：每次从 index.json / *.md / *.html 实时计算，无需缓存。
"""
import csv
import io
import json
import os
import re
import time
import urllib.request
import urllib.error
from collections import Counter
from datetime import date, datetime, timedelta

# --- 可移植引导：向上定位 src/，兼容旧平铺布局与 src/core 布局 ---
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
import paths

# 数据文件路径一律动态取，支持运行时切换 YIZHIMA_HOME / YIZHIMA_DATA_DIR

DATACUBE = "https://api.weixin.qq.com/datacube/"

# 微信公众平台「图文数据」接口清单（截图中的 10 个）
ENDPOINTS = [
    {"name": "获取图文群发每日数据", "path": "/datacube/getarticlesummary",
     "desc": "某天所有被阅读过的群发文章当天的阅读数据", "maxSpan": 1},
    {"name": "获取图文阅读分时数据", "path": "/datacube/getuserreadhour",
     "desc": "图文阅读分时数据", "maxSpan": 1},
    {"name": "获取图文转发分时数据", "path": "/datacube/getusersharehour",
     "desc": "图文转发分时数据", "maxSpan": 1},
    {"name": "获取图文阅读概况数据", "path": "/datacube/getuserread",
     "desc": "图文阅读概括数据", "maxSpan": 7},
    {"name": "获取图文群发总数据", "path": "/datacube/getarticletotal",
     "desc": "群发文章从群发日起的总量统计数据", "maxSpan": 1},
    {"name": "获取图文转发概况数据", "path": "/datacube/getusershare",
     "desc": "图文转发概况数据", "maxSpan": 7},
    {"name": "获取发表内容每日阅读数据", "path": "/datacube/getarticleread",
     "desc": "某天所有被阅读过的发表内容的阅读指标", "maxSpan": 1},
    {"name": "获取发表内容每日分享数据", "path": "/datacube/getarticleshare",
     "desc": "某天所有被分享过的发表内容的分享指标", "maxSpan": 1},
    {"name": "获取发表内容概况总数据", "path": "/datacube/getbizsummary",
     "desc": "圈选日期内账号发表内容汇总的概览数据", "maxSpan": 7},
    {"name": "获取发表内容发表详细数据", "path": "/datacube/getarticletotaldetail",
     "desc": "圈选日期内所有发表内容的详细数据", "maxSpan": 7},
]

# ---------------------------------------------------------------- 基础工具

def _read_json(path, default=None):
    if not os.path.exists(path):
        return {} if default is None else default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def _write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _cache():
    c = _read_json(paths.stats_cache(), {})
    c.setdefault("source", "none")
    c.setdefault("updatedAt", "")
    c.setdefault("daily", [])
    c.setdefault("articles", [])
    c.setdefault("apiStatus", {})
    return c


def _num(v):
    """把 '1,234' / '12人' / '' 安全转成整数。"""
    if v is None:
        return 0
    s = re.sub(r"[^\d.-]", "", str(v))
    if not s or s in ("-", "."):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _pick(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return 0


def _post(ep, token, payload, timeout=20):
    url = f"{DATACUBE}{ep}?access_token={token}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"__net_error__": str(e)}


def _date_span(days=7):
    end = date.today() - timedelta(days=1)   # 微信数据 T+1，当天无数据
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


# ---------------------------------------------------------------- 权限探测

def probe_permission(token, force=False, ttl=3600):
    """用最小代价探测 datacube 权限，结果缓存 ttl 秒。"""
    c = _cache()
    st = c.get("apiStatus") or {}
    if (not force and st.get("ts")
            and time.time() - st["ts"] < ttl
            and st.get("code") not in (None, "__net_error__")):
        return st

    begin, end = _date_span(1)
    r = _post("getarticlesummary", token,
              {"begin_date": begin, "end_date": end})
    if "__net_error__" in r:
        st = {"authorized": False, "code": "__net_error__",
              "message": "网络不可达：" + r["__net_error__"][:80], "ts": time.time()}
    else:
        code = r.get("errcode", 0)
        if code == 0 or "list" in r:
            st = {"authorized": True, "code": 0, "message": "接口权限正常",
                  "ts": time.time()}
        elif code == 48001:
            st = {"authorized": False, "code": 48001,
                  "message": "接口未授权（48001）：数据统计接口仅对已通过微信认证的订阅号/服务号开放",
                  "ts": time.time()}
        else:
            st = {"authorized": False, "code": code,
                  "message": f"{r.get('errmsg', '未知错误')}（errcode={code}）",
                  "ts": time.time()}
    st["checkedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c["apiStatus"] = st
    _write_json(paths.stats_cache(), c)
    return st


# ---------------------------------------------------------------- API 拉取

def fetch_datacube(token, days=7):
    """有权限时拉取并归一化。返回 {daily, articles} 或抛 RuntimeError。"""
    begin, end = _date_span(days)

    read = _post("getuserread", token,
                 {"begin_date": begin, "end_date": end})
    share = _post("getusershare", token,
                  {"begin_date": begin, "end_date": end})
    for name, r in (("getuserread", read), ("getusershare", share)):
        if r.get("errcode", 0) not in (0,):
            raise RuntimeError(f"{name} 失败: {r.get('errmsg')} (errcode={r.get('errcode')})")

    by_date = {}
    for row in read.get("list", []) or []:
        d = str(_pick(row, "ref_date", "stat_date", "date"))
        if not d:
            continue
        by_date.setdefault(d, {"date": d, "read": 0, "share": 0, "fav": 0, "like": 0})
        by_date[d]["read"] = _num(_pick(row, "int_page_read_user", "read_user", "read"))
    for row in share.get("list", []) or []:
        d = str(_pick(row, "ref_date", "stat_date", "date"))
        if not d:
            continue
        by_date.setdefault(d, {"date": d, "read": 0, "share": 0, "fav": 0, "like": 0})
        by_date[d]["share"] = _num(_pick(row, "share_user", "share"))
        by_date[d]["fav"] = _num(_pick(row, "add_to_fav_user", "fav_user"))

    # 文章级明细（TOP 榜）
    detail = _post("getarticletotaldetail", token,
                   {"begin_date": begin, "end_date": end,
                    "offset": 0, "count": 100})
    articles = []
    for row in (detail.get("list", []) or []):
        articles.append({
            "date": str(_pick(row, "stat_date", "ref_date", "date")),
            "title": str(_pick(row, "title", "article_title") or ""),
            "read": _num(_pick(row, "int_page_read_user", "read_user", "read")),
            "share": _num(_pick(row, "share_user", "share")),
            "fav": _num(_pick(row, "add_to_fav_user", "fav_user")),
            "like": _num(_pick(row, "like_user", "old_like_user", "like")),
        })
    return {"daily": sorted(by_date.values(), key=lambda x: x["date"]),
            "articles": articles}


def refresh(token, days=7, force=False):
    """刷新远端数据。页面打开时 force=False 走权限缓存；手动点刷新 force=True 重新探测。
    无权限时不覆盖已有 CSV 数据。"""
    st = probe_permission(token, force=force)
    if not st.get("authorized"):
        return {"ok": False, "apiStatus": st,
                "message": "接口未授权，已保留当前数据源。可改用公众号后台导出的 CSV。"}
    try:
        data = fetch_datacube(token, days)
    except RuntimeError as e:
        return {"ok": False, "apiStatus": st, "message": str(e)}
    c = _cache()
    c.update({"source": "api", "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "daily": data["daily"], "articles": data["articles"],
              "apiStatus": st})
    _write_json(paths.stats_cache(), c)
    return {"ok": True, "apiStatus": st, "source": "api",
            "daily": len(data["daily"]), "articles": len(data["articles"])}


# ---------------------------------------------------------------- CSV 导入

# 微信公众平台后台导出的真实列名非常杂，别名按「人数 > 次数」优先级排列
ALIASES = {
    "date":  ["日期", "统计日期", "数据日期", "发布时间", "发布日期", "发表日期",
              "发表时间", "群发日期", "stat_date", "ref_date", "date", "时间"],
    "title": ["文章标题", "内容标题", "图文标题", "标题", "图文名称", "title"],
    "read":  ["阅读人数", "图文页阅读人数", "阅读用户数", "阅读量", "阅读次数",
              "图文页阅读次数", "int_page_read_user", "read_user", "阅读", "read"],
    "share": ["分享人数", "分享转发人数", "转发人数", "分享次数", "转发次数",
              "share_user", "分享", "转发", "share"],
    "fav":   ["收藏人数", "微信收藏人数", "收藏次数", "收藏量",
              "add_to_fav_user", "收藏", "fav"],
    "like":  ["在看人数", "点赞人数", "在看数", "点赞数", "在看", "点赞", "like"],
    # 第五批：total_*.xls 的「内容url」是拼单篇明细下载链接的唯一线索
    "url":   ["内容url", "文章链接", "内容链接", "链接", "url"],
}


def msgid_from_url(url):
    """内容url 的 mid + '_' + idx 即下载链接的 msgid。idx 缺省按 1。"""
    m = re.search(r"[?&]mid=(\d+)", str(url or ""))
    if not m:
        return ""
    i = re.search(r"[?&]idx=(\d+)", str(url or ""))
    return m.group(1) + "_" + (i.group(1) if i else "1")


def _norm_header(s):
    return re.sub(r"[\s（）()%／/·、:：\-_]", "", str(s or "")).lower()


def _norm_date(v):
    """公众号导出日期五花八门：Excel 序列号 / 2026年11月4日 / 2026-11-4 / 20261104。"""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip().replace("\xa0", "")
    if not s:
        return ""
    if re.fullmatch(r"\d{5}(\.\d+)?", s):          # Excel 1900 日期序列号
        n = float(s)
        if 20000 <= n <= 80000:
            return (datetime(1899, 12, 30) + timedelta(days=n)).strftime("%Y-%m-%d")
    m = re.search(r"(\d{4})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    if re.fullmatch(r"\d{8}", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s[:10]


def _match_field(header, key):
    """保留给外部调用；实际列选择见 _pick_columns（按别名优先级，避免「阅读次数」抢走「阅读人数」）。"""
    h = _norm_header(header)
    aliases = [_norm_header(a) for a in ALIASES[key]]
    if h in aliases:
        return True
    return any(len(a) >= 2 and a in h for a in aliases)


def _pick_columns(headers):
    norm = {h: _norm_header(h) for h in headers}
    colmap = {}
    for key, aliases in ALIASES.items():
        na = [_norm_header(a) for a in aliases]
        hit = next((h for h in headers if norm[h] in na), None)      # 1) 精确
        if not hit:                                                   # 2) 包含，长别名优先
            for a in sorted(na, key=len, reverse=True):
                if len(a) < 2:
                    continue
                hit = next((h for h in headers if a in norm[h]), None)
                if hit:
                    break
        if hit:
            colmap[key] = hit
    return colmap


def parse_rows(text, fmt="csv"):
    """纯解析层：返回 (rows, daily, colmap)，**不写任何文件**。

    兼容逗号/分号/制表符、BOM、GBK 乱码、Excel 日期序列号。
    供 import_csv（管理台导入）与 strategy_engine.ingest_feedback（写作策略闭环）共用：
    后者只取带标题的行做归因，绝不覆盖 stats_cache.json。
    """
    if not text or not text.strip():
        raise ValueError("文件内容为空")
    text = text.lstrip("\ufeff")
    sample = "\n".join(text.splitlines()[:6])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("未识别到表头")
    headers = [h for h in reader.fieldnames if h]
    colmap = _pick_columns(headers)
    if "read" not in colmap and "share" not in colmap:
        raise ValueError("未识别到「阅读/分享」列，请检查导出内容。表头：" + " | ".join(headers[:12]))

    rows = []
    for r in reader:
        rec = {
            "date": _norm_date(r.get(colmap.get("date", ""), "")),
            "title": str(r.get(colmap.get("title", ""), "") or "").strip(),
            "read": _num(r.get(colmap.get("read", ""))),
            "share": _num(r.get(colmap.get("share", ""))),
            "fav": _num(r.get(colmap.get("fav", ""))),
            "like": _num(r.get(colmap.get("like", ""))),
            "url": str(r.get(colmap.get("url", ""), "") or "").strip(),
        }
        rec["msgid"] = msgid_from_url(rec["url"])
        if not rec["title"] and not rec["read"] and not rec["share"]:
            continue
        rows.append(rec)

    # 「渠道拆分」导出里一篇文会出现「全部 / 推荐 / 搜一搜」多行。按 (date,title)
    # 保留阅读最大的一行（通常就是「全部」），避免 TOP 榜与日汇总重复计数。
    seen, deduped = {}, []
    for rec in rows:
        if not rec["title"]:
            deduped.append(rec)
            continue
        key = (rec["date"], rec["title"])
        if key in seen:
            i = seen[key]
            if rec["read"] > deduped[i]["read"]:
                deduped[i] = rec
        else:
            seen[key] = len(deduped)
            deduped.append(rec)
    rows = deduped

    by_date = {}
    for rec in rows:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", rec["date"]):
            d = by_date.setdefault(rec["date"], {"date": rec["date"], "read": 0,
                                                 "share": 0, "fav": 0, "like": 0})
            for k in ("read", "share", "fav", "like"):
                d[k] += rec[k]
    if not rows:
        raise ValueError("未解析到有效数据行")
    return rows, sorted(by_date.values(), key=lambda x: x["date"]), colmap


def import_csv(text, fmt="csv"):
    """管理台导入入口：解析后覆盖 stats_cache.json（行为与拆分前完全一致）。

    第五批：total_*.xls 不含收藏/在看列，重新导入会把第四批补齐的 fav/like 冲回 0。
    这里按 (date, 归一化标题) 从旧 articles 继承这两个字段，并同步重算 daily，
    保证「累计收藏 / 在看·点赞」KPI 不因重导 total 表而丢失。
    """
    rows, daily, colmap = parse_rows(text, fmt)
    c = _cache()
    _key = lambda a: (a.get("date"), re.sub(r"[\s\W_]+", "", str(a.get("title") or "")).lower())
    old = {_key(a): a for a in (c.get("articles") or []) if a.get("title")}
    for r in rows:
        src = old.get(_key(r))
        if not src:
            continue
        for k in ("fav", "like"):
            if not int(r.get(k) or 0) and int(src.get(k) or 0):
                r[k] = int(src[k])
    for d in daily:
        same = [r for r in rows if r.get("date") == d.get("date")]
        if same:
            for k in ("fav", "like"):
                d[k] = sum(int(r.get(k) or 0) for r in same)
    c.update({"source": "csv", "format": fmt,
              "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "daily": daily, "articles": rows})
    _write_json(paths.stats_cache(), c)
    # 渠道拆分导出（含「渠道」列）只用于趋势，不参与单篇写作归因
    head = text.splitlines()[0] if text else ""
    return {"ok": True, "source": "csv", "format": fmt, "rows": len(rows),
            "daily": len(daily), "columns": list(colmap.keys()),
            "channelSplit": "渠道" in head}


def import_file(data_b64, filename=""):
    """前端把导出文件读成 base64 传进来，自动识别 .xls / .xlsx / 伪 xls / CSV。"""
    import base64
    import excel_import
    try:
        raw = base64.b64decode(data_b64 or "")
    except Exception as e:
        raise ValueError(f"文件编码无效：{e}")
    if not raw:
        raise ValueError("文件内容为空")
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("文件超过 20MB，请精简后重试")
    text, fmt = excel_import.parse(raw, filename)
    return import_csv(text, fmt=fmt)


def clear_remote():
    c = _cache()
    c.update({"source": "none", "format": "", "updatedAt": "", "daily": [], "articles": []})
    _write_json(paths.stats_cache(), c)
    return {"ok": True, "source": "none"}


# ---------------------------------------------------------------- 本地指标

def _md_stats(rel):
    """统计一篇 md 的正文字数、代码块数。"""
    if not rel:
        return 0, 0
    p = paths.resolve(rel, must_exist=True)
    if not p or not os.path.isfile(p):
        return 0, 0
    try:
        with open(p, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return 0, 0
    code = len(re.findall(r"^```", text, re.M)) // 2
    prose = re.sub(r"```[\s\S]*?```", "", text)
    cn = len(re.findall(r"[\u4e00-\u9fff]", prose))
    en = len(re.findall(r"[a-zA-Z]+", prose))
    return cn + en, code


def local_metrics():
    index = _read_json(paths.index(), [])
    series = _read_json(paths.series(), {})
    items = []
    for a in index:
        words, code = _md_stats(a.get("md"))
        items.append({
            "date": a.get("date", ""),
            "title": a.get("title", ""),
            "tags": a.get("tags") or [],
            "words": words,
            "codeBlocks": code,
            "hasCover": bool(a.get("cover")),
            "hasDraft": bool((a.get("publish") or {}).get("media_id")),
        })
    total = len(items)
    drafted = sum(1 for x in items if x["hasDraft"])
    month = sum(1 for x in items if x["date"].startswith(date.today().strftime("%Y-%m")))
    words_sum = sum(x["words"] for x in items)
    code_sum = sum(x["codeBlocks"] for x in items)
    cover = sum(1 for x in items if x["hasCover"])

    # 连续创作天数：从今天（或最近一天）往回数
    days = {x["date"] for x in items if x["date"]}
    streak = 0
    cur = date.today()
    if cur.isoformat() not in days:
        cur -= timedelta(days=1)
    while cur.isoformat() in days:
        streak += 1
        cur -= timedelta(days=1)

    # 近 14 日时间线（篇数 + 字数）
    timeline = []
    for i in range(13, -1, -1):
        d = (date.today() - timedelta(days=i)).isoformat()
        sub = [x for x in items if x["date"] == d]
        timeline.append({"date": d, "count": len(sub),
                         "words": sum(x["words"] for x in sub)})

    tags = Counter(t for x in items for t in x["tags"]).most_common(10)
    pillars = [{"key": k, "name": (v or {}).get("name", k),
                "count": (v or {}).get("count", 0)}
               for k, v in (series.get("pillars") or {}).items()]

    return {
        "total": total,
        "drafted": drafted,
        "pending": total - drafted,
        "month": month,
        "streak": streak,
        "totalWords": words_sum,
        "avgWords": round(words_sum / total) if total else 0,
        "codeBlocks": code_sum,
        "avgCodeBlocks": round(code_sum / total, 1) if total else 0,
        "coverRate": round(cover / total * 100) if total else 0,
        "timeline": timeline,
        "tags": [{"name": n, "count": c} for n, c in tags],
        "pillars": pillars,
        "articles": items,
    }


# ---------------------------------------------------------------- 对外快照

def snapshot(token=None):
    c = _cache()
    api = c.get("apiStatus") or {
        "authorized": False, "code": None,
        "message": "尚未探测接口权限，点击「刷新数据」可检测", "checkedAt": ""}
    return {
        "source": c.get("source", "none"),
        "format": c.get("format", ""),
        "updatedAt": c.get("updatedAt", ""),
        "apiStatus": {k: v for k, v in api.items() if k != "ts"},
        "daily": c.get("daily", []),
        "articles": c.get("articles", []),
        "endpoints": ENDPOINTS,
        "remoteSummary": _remote_summary(c.get("daily", []), c.get("articles", [])),
        "local": local_metrics(),
    }


def _remote_summary(daily, articles):
    if not daily and not articles:
        return None
    read = sum(_num(d.get("read")) for d in daily) or sum(_num(a.get("read")) for a in articles)
    share = sum(_num(d.get("share")) for d in daily) or sum(_num(a.get("share")) for a in articles)
    fav = sum(_num(d.get("fav")) for d in daily) or sum(_num(a.get("fav")) for a in articles)
    like = sum(_num(d.get("like")) for d in daily) or sum(_num(a.get("like")) for a in articles)
    n = max(1, len(articles) or len(daily))
    top = sorted(articles, key=lambda a: _num(a.get("read")), reverse=True)[:8]
    return {"read": read, "share": share, "fav": fav, "like": like,
            "avgRead": round(read / n), "top": top, "articleCount": len(articles)}
