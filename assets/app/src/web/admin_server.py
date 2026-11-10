#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内容运营台 · 文章管理台（本地，仅 127.0.0.1）。

与 WorkBuddy 定时任务共享同一批文件：
  - config.json       读写（主题/选题/凭据/发布开关）
  - index.json        只读（文章清单）
  - articles/*.html   只读（预览/发布源）
  - publish_wechat    发布逻辑（import 复用，两端共用）

启动：
  python3 admin_server.py [--port 8731]
访问：
  http://127.0.0.1:8731/          内容运营大屏（文章矩阵跑马灯）
  http://127.0.0.1:8731/manage   文章管理页（左列表 + 右预览 + 一键发布）
  http://127.0.0.1:8731/settings 系统设置页（全部配置可视化 + 多主题定制）

主题接口：
  GET  /api/themes            主题列表 + token schema
  PUT  /api/themes            保存主题（含 active 切换）
  POST /api/themes/preview    按 token 实时渲染公众号版预览（纯本地）
"""
import argparse
import json
import os
import sys
import shutil
import mimetypes
import re
import time
import ssl
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

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
paths.bootstrap()      # 把 src/core 加入 sys.path（新旧布局都能 import core 模块）

import publish_wechat   # 复用发布逻辑（含主题渲染）
import stats_provider   # 本地指标 + 网页端导出快照
import theme_manager    # 多主题数据层

UI_PATH = paths.web_file("admin_ui.html")
MANAGE_PATH = paths.web_file("admin_manage.html")
SETTINGS_PATH = paths.web_file("admin_settings.html")

# 允许前端修改的配置键（白名单，避免写入无关字段）
# 与 config.json 现有的全部键一一对应，设置页可完整可视化编辑
CONFIG_WRITABLE = {
    "topics", "audience", "wordRange", "imageStyle", "skipImages",
    "autoPublish", "publishTarget", "author", "appId", "appSecret",
    "wechatAccountId", "themeName",
}


def _read_json(path, default=None):
    if not os.path.exists(path):
        return default if default is not None else []
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else []


# ---------- 服务器出网公网 IPv4（公众号 IP 白名单用） ----------
# 白名单限制的是「后端 Python 向 mp.weixin.qq.com 出网」的 IP，因此必须从服务端探测，
# 而非读取浏览器 IP。多源容错：任一可达即返回；带 5 分钟缓存，避免每次打开弹窗都出网。
_MYIP_SOURCES = [
    "https://api.ipify.org?format=text",   # 海外，IPv4 only
    "https://ipv4.icanhazip.com",          # 海外，纯文本
    "https://ip.3322.net",                 # 国内，纯文本
    "https://myip.ipip.net",               # 国内，含地理，正则取首个 IPv4
]
_MYIP_CACHE = {"ip": None, "src": None, "ts": 0.0}
_MYIP_TTL = 300.0


def _extract_ipv4(text):
    m = re.search(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", text or "")
    if not m:
        return None
    ip = m.group(1)
    if all(0 <= int(p) <= 255 for p in ip.split(".")):
        return ip
    return None


def _get_myip(force=False):
    now = time.time()
    if not force and _MYIP_CACHE["ip"] and now - _MYIP_CACHE["ts"] < _MYIP_TTL:
        return {"ok": True, "ip": _MYIP_CACHE["ip"], "source": _MYIP_CACHE["src"], "cached": True}
    # 显式禁用代理：服务端若处于内网代理环境，需直连公网 echo 服务
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_err = None
    for url in _MYIP_SOURCES:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with opener.open(req, timeout=6) as r:
                text = r.read().decode("utf-8", "ignore").strip()
            ip = _extract_ipv4(text)
            if ip:
                _MYIP_CACHE.update(ip=ip, src=url, ts=now)
                return {"ok": True, "ip": ip, "source": url, "cached": False}
        except Exception as e:
            last_err = str(e)
            continue
    return {"ok": False, "error": last_err or "所有公网 IP 探测源均不可达（请检查服务器出网）"}


def _atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _safe_join(base, rel):
    """防目录穿越：rel 必须落在 base 内。"""
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, rel))
    if target != base_real and not target.startswith(base_real + os.sep):
        return None
    return target


def _resolve_article_path(rel):
    """index.json 相对路径 → 磁盘绝对路径（paths 兼容新旧布局 + 防穿越）。"""
    return paths.resolve(rel)


def _articles_summary():
    index = _read_json(paths.index(), [])
    out = []
    for i, a in enumerate(index):
        cover = a.get("cover")
        html = a.get("html")
        pub = a.get("publish") or {}
        out.append({
            "id": i,
            "date": a.get("date", ""),
            "title": a.get("title", ""),
            "subtitle": a.get("subtitle", ""),
            "summary": a.get("summary", ""),
            "tags": a.get("tags", []),
            "coverUrl": ("/files/" + urllib.parse.quote(cover)) if cover else "",
            "htmlUrl": ("/files/" + urllib.parse.quote(html)) if html else "",
            "mdUrl": ("/files/" + urllib.parse.quote(a["md"])) if a.get("md") else "",
            "published": bool(pub.get("media_id")),
            "mediaId": pub.get("media_id", ""),
            "publishedAt": pub.get("publishedAt", ""),
        })
    return out


def _theme_preview(tokens, article_id=None):
    """按给定 token 渲染公众号版预览 HTML。纯本地、不联网。

    article_id 有效时用真实文章（图片走 /files/ 本地路由），否则用内置示例。
    """
    if article_id is not None:
        try:
            art = _read_json(paths.index(), [])[int(article_id)]
        except Exception:
            art = None
        if art and art.get("html"):
            fp = _resolve_article_path(art["html"])
            if fp and os.path.isfile(fp):
                ex = publish_wechat.Extractor()
                ex.feed(open(fp, encoding="utf-8").read())
                base = os.path.dirname(art["html"])
                img_map = {}
                for b in ex.blocks:
                    if b.get("type") == "figure" and b.get("img"):
                        src = b["img"]
                        rel = os.path.join(base, src) if not os.path.isabs(src) else src
                        img_map[src] = "/files/" + urllib.parse.quote(rel)
                content, applied = publish_wechat.render_with_theme(
                    ex.blocks, img_map, tokens=tokens,
                    kicker=ex.kicker, title=ex.title, subtitle=ex.subtitle)
                return content, applied, art.get("title", "")
    content, applied = publish_wechat.render_with_theme(
        publish_wechat.SAMPLE_BLOCKS, {}, tokens=tokens,
        kicker="TECH SHARE", title="主题定制 · 实时预览",
        subtitle="下方色板改动会立即反映到这段公众号排版")
    return content, applied, "示例文章"


_PREVIEW_DOC = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 html,body{margin:0;padding:0;background:#fff}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}
 .wb-page{max-width:640px;margin:0 auto;padding:0 14px 40px;background:#fff}
 ::-webkit-scrollbar{display:none!important}
</style></head><body><div class="wb-page">__WB_CONTENT__</div></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "ArticleAdmin/1.0"

    # ---------- 工具 ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path):
        if not path or not os.path.isfile(path):
            self._send(404, {"error": "file not found"})
            return
        ctype, _ = mimetypes.guess_type(path)
        ctype = ctype or "application/octet-stream"
        try:
            with open(path, "rb") as f:
                data = f.read()
        except Exception as e:
            self._send(500, {"error": str(e)})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._send_file(UI_PATH)
            return
        if path in ("/manage", "/manage.html"):
            self._send_file(MANAGE_PATH)
            return
        if path in ("/settings", "/settings.html"):
            self._send_file(SETTINGS_PATH)
            return
        if path.startswith("/files/"):
            # 以文章所在目录为基准地址提供文件，使 HTML 内 assets/ 相对引用可解析
            rel = urllib.parse.unquote(path[len("/files/"):])
            fp = _resolve_article_path(rel)
            if not fp:
                self._send(403, {"error": "forbidden path"})
                return
            self._send_file(fp)
            return
        if path == "/api/config":
            self._send(200, paths.load_config())
            return
        if path == "/api/account":
            # 账号身份（解耦层）：当前绑定公众号 appId / __biz 脱敏快照
            try:
                import account
                self._send(200, {"ok": True, **account.state()})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return

        if path == "/api/myip":
            # 服务器出网公网 IPv4：公众号草稿推送受 IP 白名单限制，需展示以便加白。
            # 取的是「本机向微信出网」的出口 IP（非浏览器 IP），并带短缓存避免频繁出网。
            try:
                self._send(200, _get_myip(force=bool(qs.get("force"))))
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/series":
            self._send(200, _read_json(paths.series(), {}))
            return
        if path == "/api/themes":
            doc = theme_manager.load()
            self._send(200, {**doc, "schema": theme_manager.schema()})
            return
        if path == "/api/system":
            cfg = paths.load_config()
            st = stats_provider.snapshot()
            self._send(200, {
                "articles": len(_read_json(paths.index(), [])),
                "series": _read_json(paths.series(), {}),
                "statsSource": st.get("source", "none"),
                "activeTheme": theme_manager.load().get("active"),
                "configKeys": sorted(cfg.keys()),
                "serverDir": paths.home(),
                "dataDir": paths.data_dir(),
                "stateDir": paths.state_dir(),
            })
            return
        if path == "/api/stats":
            # 纯本地快照，不打网络，保证首屏秒开
            snap = stats_provider.snapshot()
            # 用别名导入：do_GET 后续分支有局部 import strategy_engine，
            # 直接同名 import 会把整个函数的名字变成局部变量（历史踩过坑）。
            try:
                import strategy_engine as _se
                snap["profile"] = _se.profile_summary()
            except Exception:
                snap["profile"] = None
            self._send(200, snap)
            return
        if path == "/api/data/health":
            # 全链路只读体检：台账错位 / 数值分叉 / 日汇总 / 定格溯源 / 策略过期
            try:
                import strategy_engine
                self._send(200, strategy_engine.data_health())
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e), "issues": []})
            return
        if path == "/api/strategy":
            # 写作策略面板：只读。优先展示定时任务生成的同一份策略文件；
            # 文件缺失时现算一份但不落盘，不扫下载目录、不改动任何数据。
            try:
                import strategy_engine
                st = paths.read_json(os.path.join(paths.data_dir(), "writing_strategy.json"), None)
                computed = False
                if not st:
                    st = strategy_engine.build(do_ingest=False, write=False)
                    computed = True
                else:
                    # 台账可能在管理台之外被更新（CLI / 定时任务 / 手工导入）。
                    # 文件快照落后时按当前台账现算派生字段返回，避免面板读旧权重、
                    # Skill 注入新权重；版本号保持文件原值，不伪造一版策略。
                    fresh = strategy_engine.build(do_ingest=False, write=False)
                    if (st.get("sampleSize") != fresh.get("sampleSize")
                            or st.get("own") != fresh.get("own")):
                        st = {**st, **{k: fresh[k] for k in (
                            "sampleSize", "confidence", "scoreDef", "own", "external",
                            "coverage", "constraints", "constraintMeta", "insights")},
                              "stale": True}
                counts = {
                    "features": strategy_engine.count_lines("strategy_log.jsonl"),
                    "feedback": strategy_engine.count_lines("feedback.jsonl"),
                    "signals": strategy_engine.count_lines("external_signals.jsonl"),
                }
                # 闭环扩展（第一批 R2/R3/R4）：版本时间线 / 约束有效性 / 假设验证 / 待写入规则
                def _safe(fn, d):
                    try:
                        return fn()
                    except Exception:
                        return d
                history = _safe(lambda: strategy_engine.list_history(12), [])
                eff = _safe(strategy_engine.effectiveness, {"globalMean": 0, "versions": []})
                hyps = _safe(strategy_engine.verify_hypotheses, [])
                pending = _safe(
                    lambda: [r for r in strategy_engine._jsonl("verified_rules.jsonl")
                             if r.get("status") == "pending"], [])
                self._send(200, {"ok": True, "strategy": st, "counts": counts,
                                 "recent": strategy_engine.recent_features(5),
                                 "computed": computed, "source": "writing_strategy.json",
                                 "history": history, "effectiveness": eff,
                                 "hypotheses": hyps, "verifiedRules": pending})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/skill/rules":
            # 策略演进：待写入 SKILL 的规则（预览，只读），带 evidence / directiveId
            try:
                import strategy_engine
                pending = [r for r in strategy_engine._jsonl("verified_rules.jsonl")
                           if r.get("status") == "pending"]
                self._send(200, {"ok": True, "rules": pending, "total": len(pending)})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/strategy/rules":
            # R4 规则池四态全量（pending/written/stale/rejected），只读，供生命周期面板
            try:
                import strategy_engine
                rows = strategy_engine._jsonl("verified_rules.jsonl")
                groups = {k: [r for r in rows if r.get("status") == k]
                          for k in ("pending", "written", "stale", "rejected")}
                self._send(200, {"ok": True, "rules": rows, "groups": groups,
                                 "counts": {k: len(v) for k, v in groups.items()},
                                 "total": len(rows)})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/strategy/attribution":
            # R1 约束归因矩阵（只读派生，不触发 build、不扫下载目录、不写文件）
            try:
                import strategy_engine
                res = strategy_engine.attribution_matrix()
                self._send(200, {"ok": True, **res})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/strategy/pairing":
            # R7 配对诊断（只读派生，零写入）
            try:
                import strategy_engine
                res = strategy_engine.pairing_stats()
                self._send(200, {"ok": True, **res})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/strategy/samples":
            # 策略演进 / 归因矩阵补内容：配对成功的真实样本（只读派生，零写入、不触发 build）
            try:
                import strategy_engine
                rows = strategy_engine._samples()
                keys = ("title", "date", "pillar", "titlePattern", "hookType",
                        "strategyVersion", "published")
                out = []
                for r in rows:
                    rec = {k: r.get(k) for k in keys}
                    for k in ("read", "share", "fav", "like", "watch", "thumb"):
                        rec[k] = r.get(k, 0) or 0
                    rec["score"] = round(float(r.get("_score") or 0), 4)
                    out.append(rec)
                out.sort(key=lambda x: x["score"], reverse=True)
                self._send(200, {"ok": True, "samples": out, "total": len(out)})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e), "samples": [], "total": 0})
            return
        if path == "/api/strategy/detail-status":
            # 第五批 v4：文章管理页逐篇「是否已收敛 / 已采集 / 可拉取 / 可重拉」状态（不联网）
            try:
                import strategy_engine
                self._send(200, strategy_engine.detail_status())
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e),
                                 "session": {"valid": False}, "items": {}, "summary": {}})
            return
        if path == "/api/strategy/session":
            # 登录态脱敏状态：只回 token 尾号，绝不回传 Cookie 明文
            try:
                import mp_session
                self._send(200, {"ok": True, **mp_session.status()})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e), "valid": False})
            return
        if path == "/api/strategy/session/scan-status":
            # 扫码登录轮询：只读内存状态，不回传二维码、Cookie 或 token 全文
            try:
                import mp_session
                self._send(200, mp_session.scan_status(qs.get("sid", [""])[0]))
            except Exception as e:
                self._send(200, {"ok": False, "state": "failed", "error": str(e)})
            return
        if path == "/api/strategy/session/scan-debug":
            # 排障用：列出所有扫码会话的脱敏轮询轨迹，不含 Cookie / 二维码
            try:
                import mp_session
                self._send(200, mp_session.debug_sessions())
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return
        if path == "/api/articles":
            self._send(200, _articles_summary())
            return
        if path == "/api/file":
            rel = qs.get("path", [""])[0]
            fp = _resolve_article_path(rel)
            if not fp:
                self._send(403, {"error": "forbidden path"})
                return
            self._send_file(fp)
            return
        self._send(404, {"error": "not found"})

    def do_PUT(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/config":
            incoming = self._read_body()
            cur = paths.load_config()
            for k in CONFIG_WRITABLE:
                if k in incoming:
                    cur[k] = incoming[k]
            cur = paths.save_config(cur)      # 凭据单独落 secrets.json
            self._send(200, {"ok": True, "config": cur})
            return
        if parsed.path == "/api/themes":
            incoming = self._read_body()
            try:
                saved = theme_manager.save(incoming)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            except Exception as e:
                self._send(500, {"error": str(e)})
                return
            self._send(200, {"ok": True, **saved, "schema": theme_manager.schema()})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        # ---------- 主题实时预览（纯本地渲染，不联网、不写文件） ----------
        if parsed.path == "/api/themes/preview":
            body = self._read_body()
            try:
                tokens = theme_manager.normalize_tokens(body.get("tokens"))
                content, applied, label = _theme_preview(tokens, body.get("articleId"))
                self._send(200, {"ok": True,
                                 "html": _PREVIEW_DOC.replace("__WB_CONTENT__", content),
                                 "tokens": applied, "label": label},
                           "application/json; charset=utf-8")
            except ValueError as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": str(e)})
            return

        # ---------- 运营数据 ----------
        if parsed.path == "/api/stats/import":
            body = self._read_body()
            fn = body.get("filename", "") or ""
            # 单篇「下载数据明细」xls：只补收藏/在看/点赞等互动字段，
            # 不能走 import_csv（会抛「未解析到有效数据行」且覆盖 stats_cache.json）。
            import strategy_engine
            if strategy_engine.is_single_detail(fn):
                try:
                    if body.get("data"):
                        res = strategy_engine.absorb_single_file(body.get("data", ""), fn)
                    else:
                        res = strategy_engine.absorb_single_detail(body.get("csv", ""), fn)
                    res = dict(res)
                    res["detailImport"] = True
                    # updated=0 但 ok=true 属于幂等成功（数据已最新），不能当失败报红
                    res["detailOk"] = bool(res.get("ok"))
                    res["detailIdempotent"] = bool(res.get("idempotent"))
                    res["detailUpdated"] = int(bool(res.get("updated")))
                    res["detailTitle"] = res.get("title", "")
                    # 幂等成功不是错误：detailError 只在真正失败时给值
                    res["detailError"] = ("" if res.get("ok")
                                          else (res.get("reason") or res.get("error") or ""))
                    res.update({"rows": 0, "daily": 0, "format": "single-detail"})
                except Exception as e:
                    res = {"ok": False, "detailImport": True, "detailOk": False,
                           "detailIdempotent": False, "detailUpdated": 0,
                           "detailTitle": "", "detailError": str(e),
                           "rows": 0, "daily": 0, "format": "single-detail"}
                if res.get("detailOk"):
                    # 互动数据变了，策略快照必须同步，否则面板 / Skill 口径分叉
                    try:
                        res["strategySynced"] = strategy_engine.refresh_strategy_snapshot()
                    except Exception:
                        pass
                res["stats"] = stats_provider.snapshot()
                self._send(200, res)
                return
            try:
                # 新：Excel/CSV 二进制（base64）；旧：纯 CSV 文本，保持兼容
                if body.get("data"):
                    res = stats_provider.import_file(
                        body.get("data", ""), body.get("filename", ""))
                else:
                    res = stats_provider.import_csv(body.get("csv", ""))
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            except Exception as e:
                self._send(500, {"error": str(e)})
                return
            # 带标题的「图文分析 → 单篇」行同步进写作策略反馈台账；
            # 只追加去重，不覆盖 stats_cache.json，渠道日报（无标题）自然跳过。
            try:
                import strategy_engine
                # 渠道拆分导出的阅读是「全部 / 推荐 / 搜一搜」子集，不能当单篇表现
                if not res.get("channelSplit"):
                    cache = _read_json(paths.stats_cache(), {})
                    rows = cache.get("articles") or []
                    absorbed = strategy_engine.absorb_rows(rows)
                    res["strategyFeedbackRows"] = absorbed.get("added", 0)
                    res["strategyFeedbackUpdated"] = absorbed.get("updated", 0)
                    # 第五批：total 表的「内容url」→ msgid 台账，供卡片按钮拼下载链接
                    synced = strategy_engine.sync_msgids(rows)
                    res["msgids"] = synced.get("total", 0)
                    # 解耦：从导入的文章链接提取 __biz，记录当前绑定账号身份；
                    # 跨账号导入仅警告、不阻断（防止不同公众号数据混在一份台账）。
                    try:
                        import account
                        acc = account.detect(rows)
                        res["account"] = acc
                        if acc.get("crossAccount"):
                            res["crossAccount"] = True
                    except Exception:
                        pass
                    try:
                        res["strategySynced"] = strategy_engine.refresh_strategy_snapshot()
                    except Exception:
                        pass
            except Exception:
                pass
            res["stats"] = stats_provider.snapshot()
            self._send(200, res)
            return

        if parsed.path == "/api/stats/clear":
            res = stats_provider.clear_remote()
            res["stats"] = stats_provider.snapshot()
            self._send(200, res)
            return

        if parsed.path == "/api/publish":
            body = self._read_body()
            aid = body.get("id")
            index = _read_json(paths.index(), [])
            try:
                aid = int(aid)
            except (TypeError, ValueError):
                self._send(400, {"error": "invalid id"})
                return
            if aid < 0 or aid >= len(index):
                self._send(400, {"error": "article not found"})
                return
            art = index[aid]
            html_rel = art.get("html")
            html_path = _resolve_article_path(html_rel)
            if not html_path or not os.path.isfile(html_path):
                self._send(400, {"error": f"html not found: {html_rel}"})
                return
            old_mid = (art.get("publish") or {}).get("media_id")
            try:
                res = publish_wechat.publish_html(
                    html_path, paths.config(),
                    replace=old_mid if old_mid else None,
                )
            except RuntimeError as e:
                self._send(500, {"error": str(e)})
                return
            media_id = res.get("media_id")
            art["publish"] = {
                "target": "wechat",
                "account": _read_json(paths.config(), {}).get("author", "内容运营台"),
                "media_id": media_id,
                "publishedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }
            index[aid] = art
            _atomic_write_json(paths.index(), index)
            self._send(200, {"ok": True, "media_id": media_id,
                             "title": res.get("title"), "digest": res.get("digest")})
            return

        # ---------- 策略演进：回滚至指定版本 ----------
        if parsed.path == "/api/strategy/rollback":
            body = self._read_body()
            try:
                import strategy_engine
                res = strategy_engine.rollback(body.get("version"))
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self._send(200, res)
            return

        # ---------- 策略演进：确认写入 SKILL.md 已验证规则段 ----------
        if parsed.path == "/api/skill/apply-rules":
            try:
                import strategy_engine
                skill_path = os.path.join(os.path.dirname(paths.data_dir()),
                                         "skills", "daily-article", "SKILL.md")
                res = strategy_engine.apply_verified_rules(skill_path)
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self._send(200, res)
            return

        # ---------- 第五批：单篇明细手动拉取（无定时任务，只有点按钮才联网） ----------
        if parsed.path == "/api/strategy/session":
            body = self._read_body()
            try:
                import mp_session
                st = mp_session.save_session(body.get("curl") or "")
                self._send(200, {"ok": True, **st})
            except ValueError as e:
                self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return

        # ---------- 扫码登录：拿二维码 / 轮询状态 / 取消（cURL 粘贴仍在下方保留兜底） ----------
        if parsed.path == "/api/strategy/session/scan-start":
            body = self._read_body()
            try:
                import mp_session
                ttl = body.get("timeout") or 180
                sid, jpg, err = mp_session.scan_start(timeout=ttl)
                if err:
                    self._send(200, {"ok": False, "error": err})
                else:
                    self._send(200, {"ok": True, "sid": sid,
                                     "qr": mp_session.scan_qr_data_uri(sid),
                                     "timeout": ttl})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return

        if parsed.path == "/api/strategy/session/scan-cancel":
            body = self._read_body()
            try:
                import mp_session
                self._send(200, mp_session.scan_cancel(body.get("sid")))
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})
            return

        if parsed.path == "/api/strategy/session/check":
            # 显式自检：用台账里第一篇探测是否返回 xls，绝不写任何台账
            try:
                import mp_session
                items = _read_json(paths.detail_index(), {}).get("items") or {}
                mid, pub = "", ""
                for k, v in items.items():
                    if k and v.get("date"):
                        mid, pub = k, v.get("date")
                        break
                if not mid:
                    res = {"ok": False, "error": "暂无 msgid：请先导入含「内容url」的 total_*.xls"}
                else:
                    res = mp_session.check(mid, pub)
                    res.update({"msgid": mid, "publishDate": pub})
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self._send(200, res)
            return

        if parsed.path == "/api/strategy/fetch-detail":
            body = self._read_body()
            try:
                import strategy_engine
                res = strategy_engine.fetch_detail(body.get("articleId"),
                                                   dry_run=bool(body.get("dryRun")))
                if res.get("ok") and not body.get("dryRun"):
                    try:
                        res["strategySynced"] = strategy_engine.refresh_strategy_snapshot()
                    except Exception:
                        pass
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self._send(200, res)
            return

        if parsed.path == "/api/strategy/fetch-details":
            body = self._read_body()
            try:
                import strategy_engine
                # v4：不再按发表天数拦截，任何有 msgid 的文章都能拉。
                # scope: missing(默认，没采过) / frozen(已采过，显式重拉) / all。
                # 防大批量：服务端单次请求/计划上限 + 全局互斥锁 + pause 下限；
                # 前端先 dryRun 拿固定队列，再按 5 篇分片串行调用。
                try:
                    limit = max(0, int(body.get("limit", 0) or 0))
                except (TypeError, ValueError):
                    limit = 0
                try:
                    pause = float(body.get("pause", 1.5) or 1.5)
                except (TypeError, ValueError):
                    pause = 1.5
                res = strategy_engine.fetch_detail_batch(
                    article_ids=body.get("articleIds"),
                    dry_run=bool(body.get("dryRun")),
                    pause=pause,
                    scope=str(body.get("scope") or "missing"),
                    limit=limit)
                if res.get("okCount") and not body.get("dryRun"):
                    try:
                        res["strategySynced"] = strategy_engine.refresh_strategy_snapshot()
                    except Exception:
                        pass
            except Exception as e:
                res = {"ok": False, "error": str(e), "results": []}
            self._send(200, res)
            return

        # ---------- 第五批补遗：图文总表 total_*.xls 一键拉取（同一份登录态） ----------
        if parsed.path == "/api/strategy/fetch-total":
            body = self._read_body()
            try:
                import base64
                import mp_session
                # stats_provider 已在模块级导入：此处不可再局部 import，否则会把 do_POST
                # 内的 stats_provider 变成局部变量，令其它分支 UnboundLocalError
                import strategy_engine
                days = int(body.get("days", 30) or 30)
                end_ts = body.get("endTs") or None
                if body.get("dryRun"):
                    import time as _time
                    end = int(end_ts) if end_ts else int(_time.time())
                    begin = end - max(1, min(days, 365)) * 86400
                    res = {"ok": True, "dryRun": True, "days": days,
                           "beginTimestamp": begin, "endTimestamp": end,
                           "filename": "total_%d_%d.xls" % (begin, end),
                           "session": mp_session.status()}
                else:
                    raw, err, fname = mp_session.download_total(days=days, end_ts=end_ts)
                    if err:
                        res = {"ok": False, "error": err}
                    else:
                        # 与 /api/stats/import 的 total 分支完全同一条链路：
                        # 刷新 KPI 快照（继承已有 fav/like）→ feedback 追加 → msgid 台账
                        res = dict(stats_provider.import_file(
                            base64.b64encode(raw).decode(), fname))
                        rows = (_read_json(paths.stats_cache(), {}).get("articles") or [])
                        if not res.get("channelSplit"):
                            absorbed = strategy_engine.absorb_rows(rows)
                            res["strategyFeedbackRows"] = absorbed.get("added", 0)
                            res["strategyFeedbackUpdated"] = absorbed.get("updated", 0)
                            res["msgids"] = strategy_engine.sync_msgids(rows).get("total", 0)
                        else:
                            res["msgids"] = 0
                        try:
                            res["strategySynced"] = strategy_engine.refresh_strategy_snapshot()
                        except Exception:
                            pass
                        # 落盘一份：可追溯，也能被 sync_msgids_from_downloads() 扫描兜底
                        try:
                            dl = os.path.join(os.path.expanduser("~"), "Downloads")
                            os.makedirs(dl, exist_ok=True)
                            with open(os.path.join(dl, fname), "wb") as f:
                                f.write(raw)
                            res["savedTo"] = "~/Downloads/" + fname
                        except OSError as e:
                            res["savedTo"] = "落盘失败: " + str(e)
                        res.update({"ok": True, "bytes": len(raw), "filename": fname,
                                    "stats": stats_provider.snapshot()})
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self._send(200, res)
            return

        if parsed.path == "/api/data/repair":
            # 显式一键自愈：重锚特征台账 → 对齐反馈数值 → 补明细溯源 → 刷新策略
            try:
                import strategy_engine
                res = strategy_engine.repair_data()
            except Exception as e:
                res = {"ok": False, "error": str(e)}
            self._send(200, res)
            return
        self._send(404, {"error": "not found"})

    def do_DELETE(self):
        """删除文章：从 index.json 移除记录，关联文件移入 .trash/<时间戳>-<id>/ 便于恢复。
        不触碰公众号已发布草稿（不可逆操作，交由用户在公众号后台处理）。"""
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/api/article":
            self._send(404, {"error": "not found"})
            return
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            aid = int(qs.get("id", [""])[0])
        except (TypeError, ValueError):
            self._send(400, {"error": "invalid id"})
            return
        index = _read_json(paths.index(), [])
        if aid < 0 or aid >= len(index):
            self._send(400, {"error": "article not found"})
            return

        art = index.pop(aid)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        trash = os.path.join(paths.trash_dir(), f"{stamp}-{aid}")
        os.makedirs(trash, exist_ok=True)
        moved = []
        for key in ("html", "md", "cover"):
            rel = art.get(key)
            if not rel:
                continue
            fp = _resolve_article_path(rel)
            if not fp or not os.path.isfile(fp):
                continue
            dst = os.path.join(trash, os.path.basename(fp))
            try:
                shutil.move(fp, dst)
                moved.append(rel)
            except OSError:
                pass
        with open(os.path.join(trash, "_meta.json"), "w", encoding="utf-8") as f:
            json.dump({
                "article": art,
                "moved": moved,
                "deletedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }, f, ensure_ascii=False, indent=2)
        _atomic_write_json(paths.index(), index)
        # index.json 是数组：删掉中间一篇，后面所有 articleId 会整体左移。
        # 立即按标题重锚特征台账并刷新策略快照，否则归因会把上一篇的特征
        # 安到这一篇上（历史上 16:55 / 16:57 两次删除已造成过一次漂移）。
        repair = None
        try:
            import strategy_engine
            repair = strategy_engine.reanchor_features()
            strategy_engine.refresh_strategy_snapshot()
        except Exception:
            pass
        self._send(200, {"ok": True, "title": art.get("title", ""),
                         "trash": os.path.relpath(trash, paths.home()), "moved": moved,
                         "reanchor": repair})

    # 静默日志，避免刷屏
    def log_message(self, fmt, *args):
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=None, help="显式指定数据目录（移植部署用）")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["YIZHIMA_DATA_DIR"] = os.path.abspath(args.data_dir)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"文章管理台已启动: http://{args.host}:{args.port}/")
    print(f"数据目录: {paths.data_dir()}  |  运行态: {paths.state_dir()}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        httpd.shutdown()


if __name__ == "__main__":
    main()
