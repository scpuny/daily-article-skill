#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容运营台 · MCP 服务（stdio JSON-RPC，仅 Python 标准库，零第三方依赖）。

承载确定性本地能力：数据读写、写作策略计算、主题、质量闸门、渲染、发布、统计、台账、对账。
**不放写作 / 联网搜索 / 画图** —— 那些由 Agent 自带能力完成，避免依赖按量付费模型 API。

协议：MCP 2024-11-05，newline-delimited JSON-RPC 2.0（兼容 Content-Length 头）。
stdout 只输出协议消息；所有日志 / core 模块的 print 一律重定向到 stderr。

启动：
  PY articles/src/mcp/server.py [--home articles] [--data-dir ...] [--state-dir ...]
注册到 WorkBuddy（~/.workbuddy/mcp.json，需用户在连接器面板点「信任」后生效）：
  {"mcpServers":{"yizhima":{"command":"<PY>","args":["<绝对路径>/articles/src/mcp/server.py","--home","<绝对路径>/articles"]}}}
"""
import argparse
import contextlib
import io
import json
import os
import sys
import traceback
import urllib.parse
from datetime import datetime

# --- 可移植引导：向上定位 src/ ---
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

# core/ 里的确定性模块（含写作策略引擎）在任何入口下都可 import
_CORE = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
if _CORE not in sys.path:
    sys.path.insert(0, _CORE)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "yizhima-article", "version": "1.0.0"}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def _json(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- 工具实现
def t_get_config(a):
    cfg = paths.load_config()
    if cfg.get("appSecret"):
        cfg["appSecret"] = str(cfg["appSecret"])[:6] + "***"
    cfg["_paths"] = {"home": paths.home(), "data": paths.data_dir(),
                     "state": paths.state_dir(), "content": paths.content_dir()}
    return {"ok": True, "config": cfg}


def _article_brief(i, art):
    pub = art.get("publish") or {}
    return {"id": i, "date": art.get("date"), "title": art.get("title"),
            "subtitle": art.get("subtitle"), "tags": art.get("tags"),
            "html": art.get("html"), "md": art.get("md"), "cover": art.get("cover"),
            "published": bool(pub.get("media_id")), "mediaId": pub.get("media_id", ""),
            "quality": art.get("quality")}


def t_list_articles(a):
    idx = paths.read_json(paths.index(), [])
    limit = int(a.get("limit", 20))
    return {"ok": True, "count": len(idx),
            "articles": [_article_brief(i, x) for i, x in enumerate(idx)][-limit:]}


def t_get_article(a):
    idx = paths.read_json(paths.index(), [])
    aid = a.get("id")
    if aid is None and a.get("title"):
        aid = next((i for i, x in enumerate(idx) if x.get("title") == a["title"]), None)
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        return {"ok": False, "error": "需要 id 或 title"}
    if aid < 0 or aid >= len(idx):
        return {"ok": False, "error": f"id {aid} 越界（共 {len(idx)} 篇）"}
    art = idx[aid]
    out = {"ok": True, "article": dict(art),
           "htmlPath": paths.resolve(art.get("html"), True),
           "mdPath": paths.resolve(art.get("md"), True),
           "coverPath": paths.resolve(art.get("cover"), True)}
    if a.get("includeHtml") and out["htmlPath"]:
        out["htmlText"] = open(out["htmlPath"], encoding="utf-8").read()
    return out


def t_list_themes(a):
    doc = theme_manager.load()
    return {"ok": True, "active": doc["active"],
            "themes": [{"id": t["id"], "name": t["name"], "desc": t.get("desc", "")}
                       for t in doc["themes"]],
            "schema": theme_manager.schema()}


def t_get_theme(a):
    doc = theme_manager.load()
    tid = a.get("themeId") or doc["active"]
    tokens = theme_manager.resolve(doc, tid)
    name = next((t["name"] for t in doc["themes"] if t["id"] == tid), tid)
    return {"ok": True, "active": doc["active"], "themeId": tid, "name": name,
            "tokens": tokens, "cssVars": theme_manager.css_vars(tokens)}


def t_save_theme(a):
    if not a.get("themes"):
        return {"ok": False, "error": "缺少 themes"}
    doc = theme_manager.save({"active": a.get("active") or theme_manager.load()["active"],
                              "themes": a["themes"]})
    name = next((t["name"] for t in doc["themes"] if t["id"] == doc["active"]), "")
    cfg = paths.load_config()
    cfg["themeName"] = name
    paths.save_config(cfg)
    return {"ok": True, **doc, "themeName": name}


def t_next_topic(a):
    s = paths.read_json(paths.series(), {})
    pillars = s.get("pillars") or {}
    counts = {k: (v.get("count") if isinstance(v, dict) else v) or 0
              for k, v in pillars.items()}
    weakest = min(counts, key=counts.get) if counts else "A"
    return {"ok": True, "weakest": weakest, "counts": counts,
            "rotationRule": s.get("rotationRule", ""),
            "suggestion": s.get("nextSuggestion") or {}}


def t_lint_article(a):
    idx = paths.read_json(paths.index(), [])
    if a.get("articleId") is not None:
        try:
            aid = int(a["articleId"])
        except (TypeError, ValueError):
            return {"ok": False, "error": "articleId 非法"}
        if aid < 0 or aid >= len(idx):
            return {"ok": False, "error": "articleId 越界"}
        r = article_lint.lint_article(idx[aid], index=idx, strict=True)
        r["articleId"] = aid
    elif a.get("htmlPath"):
        p = a["htmlPath"]
        art = {"title": os.path.basename(p), "subtitle": "", "summary": "",
               "html": p, "md": None, "cover": None}
        r = article_lint.lint_article(art, strict=False)
    else:
        r = article_lint.check_latest(write=bool(a.get("write", True)))
    return r


def _extract(fp):
    ex = publish_wechat.Extractor()
    ex.feed(open(fp, encoding="utf-8").read())
    return ex


def t_render_preview(a):
    idx = paths.read_json(paths.index(), [])
    aid = int(a.get("articleId", article_lint.latest_index(idx)))
    if not idx or aid < 0 or aid >= len(idx):
        return {"ok": False, "error": "articleId 越界"}
    art = idx[aid]
    fp = paths.resolve(art.get("html"), True)
    if not fp:
        return {"ok": False, "error": "文章 HTML 不存在"}
    ex = _extract(fp)
    base = os.path.dirname(fp)
    image_base = a.get("imageBase")
    img_map = {}
    for b in ex.blocks:
        if b.get("type") == "figure" and b.get("img"):
            src = b["img"]
            if src in img_map:
                continue
            if image_base:
                rel = os.path.join(os.path.dirname(art["html"]), src)
                img_map[src] = image_base.rstrip("/") + "/" + urllib.parse.quote(rel)
            else:
                abs_path = os.path.abspath(os.path.join(base, src))
                img_map[src] = "file://" + urllib.parse.quote(abs_path)
    tokens = a.get("tokens")
    if not tokens:
        tokens = theme_manager.active_tokens(a.get("themeId"))
    content, applied = publish_wechat.render_with_theme(
        ex.blocks, img_map, tokens=tokens,
        kicker=ex.kicker, title=ex.title, subtitle=ex.subtitle)
    return {"ok": True, "html": content, "tokens": applied,
            "label": art.get("title", ""), "articleId": aid}


def t_publish_article(a):
    idx = paths.read_json(paths.index(), [])
    try:
        aid = int(a.get("articleId", article_lint.latest_index(idx)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "articleId 非法"}
    if aid < 0 or aid >= len(idx):
        return {"ok": False, "error": "articleId 越界"}
    art = idx[aid]
    lint = article_lint.lint_article(art, index=idx, strict=True)
    if not lint["ok"] and not a.get("force"):
        return {"ok": False, "blocked": True, "reason": "lint 未通过，按规则阻断建草稿",
                "lint": lint}
    fp = paths.resolve(art.get("html"), True)
    if not fp:
        return {"ok": False, "error": "文章 HTML 不存在"}
    dry = bool(a.get("dryRun"))
    # dry-run 产物落 state/（可丢弃），绝不往文章目录里写文件
    out = (os.path.join(paths.state_dir(),
                        os.path.splitext(os.path.basename(fp))[0] + ".wechat.html")
           if dry else None)
    try:
        res = publish_wechat.publish_html(
            fp, paths.config(),
            replace=(art.get("publish") or {}).get("media_id") or None,
            title_override=a.get("title"), dry_run=dry, out=out)
    except Exception as e:
        run_log.write_run({"pillar": a.get("pillar"), "title": art.get("title"),
                           "lint": {"ok": lint["ok"], "score": lint["score"]},
                           "error": str(e)})
        return {"ok": False, "error": str(e), "lint": lint}
    if dry:
        return {"ok": True, "dryRun": True, **res, "lint": lint}
    art["publish"] = {"target": "wechat",
                      "account": paths.load_config().get("author", "内容运营台"),
                      "media_id": res.get("media_id"),
                      "publishedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}
    art["quality"] = {k: lint[k] for k in ("ok", "score", "errors", "warnings", "checkedAt")}
    idx[aid] = art
    paths.write_json(paths.index(), idx)
    item, p = run_log.write_run({
        "pillar": a.get("pillar"), "theme": theme_manager.load()["active"],
        "title": art.get("title"), "articleId": aid,
        "lint": {"ok": lint["ok"], "score": lint["score"]},
        "media_id": res.get("media_id")})
    return {"ok": True, **res, "articleId": aid, "lint": lint,
            "runId": item["runId"], "runPath": p,
            "note": "原创声明需在公众号后台群发时手动勾选（draft/add 不支持）"}


def t_build_gallery(a):
    import build_gallery
    build_gallery.main()
    idx = paths.read_json(paths.index(), [])
    return {"ok": True, "path": paths.gallery(), "count": len(idx)}


def t_write_run(a):
    rec = a.get("record") or {k: a[k] for k in a if k not in ("record",)}
    item, p = run_log.write_run(rec)
    return {"ok": True, "runId": item["runId"], "path": p, "record": item}


def t_list_runs(a):
    return {"ok": True, "count": len(run_log.list_runs(1000)),
            "runs": run_log.list_runs(int(a.get("limit", 10)))}


def t_reconcile_drafts(a):
    return draft_reconcile.reconcile(write=bool(a.get("write", True)))


def t_get_stats(a):
    return {"ok": True, "stats": stats_provider.snapshot()}


# ---------------------------------------------------------------- 写作策略反馈闭环
def t_get_strategy(a):
    """写作首步：先吸收下载目录里新增的图文分析导出，再产出 constraints[]。"""
    import strategy_engine
    st = strategy_engine.build(a.get("downloadDir"), do_ingest=bool(a.get("ingest", True)))
    return {"ok": True, "strategy": st}


def t_ingest_feedback(a):
    import strategy_engine
    return strategy_engine.ingest_feedback(a.get("downloadDir"))


def t_record_features(a):
    import strategy_engine
    payload = a.get("features") if isinstance(a.get("features"), dict) else \
        {k: v for k, v in a.items() if k != "features"}
    return strategy_engine.record_features(payload)


def t_record_signals(a):
    import strategy_engine
    return strategy_engine.record_signals(a.get("items") or [])


HANDLERS = {
    "wb_get_config": t_get_config,
    "wb_list_articles": t_list_articles,
    "wb_get_article": t_get_article,
    "wb_list_themes": t_list_themes,
    "wb_get_theme": t_get_theme,
    "wb_save_theme": t_save_theme,
    "wb_next_topic": t_next_topic,
    "wb_lint_article": t_lint_article,
    "wb_render_preview": t_render_preview,
    "wb_publish_article": t_publish_article,
    "wb_build_gallery": t_build_gallery,
    "wb_write_run": t_write_run,
    "wb_list_runs": t_list_runs,
    "wb_reconcile_drafts": t_reconcile_drafts,
    "wb_get_stats": t_get_stats,
    "wb_get_strategy": t_get_strategy,
    "wb_ingest_feedback": t_ingest_feedback,
    "wb_record_features": t_record_features,
    "wb_record_signals": t_record_signals,
}


def _schema(props, required=None):
    return {"type": "object", "properties": props,
            **({"required": required} if required else {})}


S = {"type": "string"}
N = {"type": "number"}
B = {"type": "boolean"}
O = {"type": "object"}
L = {"type": "array", "items": {"type": "object"}}

TOOLS = [
    {"name": "wb_get_config", "description": "读取全部配置（appSecret 脱敏）与数据目录", "inputSchema": _schema({})},
    {"name": "wb_list_articles", "description": "列出文章索引与发布状态", "inputSchema": _schema({"limit": N})},
    {"name": "wb_get_article", "description": "取单篇文章元数据与绝对路径", "inputSchema": _schema({"id": N, "title": S, "includeHtml": B})},
    {"name": "wb_list_themes", "description": "列出全部主题与 token schema", "inputSchema": _schema({})},
    {"name": "wb_get_theme", "description": "取指定/启用主题的 tokens 与可直接粘贴的 cssVars", "inputSchema": _schema({"themeId": S})},
    {"name": "wb_save_theme", "description": "保存主题库并同步 config.themeName", "inputSchema": _schema({"active": S, "themes": L}, ["themes"])},
    {"name": "wb_next_topic", "description": "按四大支柱轮转给出下一篇建议", "inputSchema": _schema({})},
    {"name": "wb_lint_article", "description": "发布前质量闸门；errors 非空必须阻断建草稿", "inputSchema": _schema({"articleId": N, "htmlPath": S, "write": B})},
    {"name": "wb_render_preview", "description": "按主题渲染公众号版 HTML（不联网、不写文件）", "inputSchema": _schema({"articleId": N, "themeId": S, "tokens": O, "imageBase": S})},
    {"name": "wb_publish_article", "description": "lint 通过后建公众号草稿并回写 index.json + 运行台账；dryRun 只验证排版", "inputSchema": _schema({"articleId": N, "dryRun": B, "force": B, "title": S, "pillar": S})},
    {"name": "wb_build_gallery", "description": "按 index.json 重建画廊 state/index.html", "inputSchema": _schema({})},
    {"name": "wb_write_run", "description": "写一条运行台账（成功与失败都要记）", "inputSchema": _schema({"record": O})},
    {"name": "wb_list_runs", "description": "读最近运行台账", "inputSchema": _schema({"limit": N})},
    {"name": "wb_reconcile_drafts", "description": "草稿箱与 index.json 只读对账（不删草稿）", "inputSchema": _schema({"write": B})},
    {"name": "wb_get_stats", "description": "本地指标 + 已导入的公众号传播数据快照", "inputSchema": _schema({})},
    {"name": "wb_get_strategy", "description": "写作首步必调：吸收下载目录新增的图文分析导出并产出 writing_strategy.json；返回 constraints[]、confidence、自有/外部句式权重", "inputSchema": _schema({"downloadDir": S, "ingest": B})},
    {"name": "wb_ingest_feedback", "description": "只扫描下载目录里新增的公众号导出文件（带标题才收）；不覆盖 stats_cache.json", "inputSchema": _schema({"downloadDir": S})},
    {"name": "wb_record_features", "description": "写作完成后登记本轮文章特征与假设：articleId/date/title/pillar/titlePattern/hookType/wordCount/codeBlocks/strategyVersion/hypothesis", "inputSchema": _schema({"features": O})},
    {"name": "wb_record_signals", "description": "登记 WebSearch 得到的外部高互动样本 titlePattern/hookType/engagementLevel（30 天半衰，冷启动主力）", "inputSchema": _schema({"items": L})},
]


# ---------------------------------------------------------------- JSON-RPC
def dispatch(req):
    rid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def fail(code, message):
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({"protocolVersion": PROTOCOL_VERSION,
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": SERVER_INFO})
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return ok({})
    if method == "resources/list":
        return ok({"resources": []})
    if method == "prompts/list":
        return ok({"prompts": []})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = HANDLERS.get(name)
        if not handler:
            return fail(-32602, f"未知工具: {name}")
        try:
            with contextlib.redirect_stdout(sys.stderr):   # core 的 print 不污染协议
                result = handler(args)
            text = _json(result)
            is_error = False
        except Exception as e:
            log(traceback.format_exc())
            text = _json({"ok": False, "error": f"{type(e).__name__}: {e}"})
            is_error = True
        return ok({"content": [{"type": "text", "text": text}],
                   "isError": is_error})
    if method in ("shutdown", "exit"):
        return ok({})
    return fail(-32601, f"Method not found: {method}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", default=None, help="项目根（articles/）")
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--state-dir", default=None)
    args = ap.parse_args()
    if args.home:
        os.environ["YIZHIMA_HOME"] = os.path.abspath(args.home)
    if args.data_dir:
        os.environ["YIZHIMA_DATA_DIR"] = os.path.abspath(args.data_dir)
    if args.state_dir:
        os.environ["YIZHIMA_STATE_DIR"] = os.path.abspath(args.state_dir)

    paths.bootstrap()
    global publish_wechat, theme_manager, stats_provider, article_lint, run_log, draft_reconcile
    import publish_wechat, theme_manager, stats_provider
    import article_lint, run_log, draft_reconcile

    log(f"[yizhima-mcp] home={paths.home()} data={paths.data_dir()} state={paths.state_dir()}")
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    stream = sys.stdin
    while True:
        line = stream.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("content-length:"):
            try:
                n = int(line.split(":", 1)[1].strip())
            except ValueError:
                continue
            while True:
                h = stream.readline()
                if not h or h.strip() == "":
                    break
            line = stream.read(n)
        try:
            req = json.loads(line)
        except Exception as e:
            print(_json({"jsonrpc": "2.0", "id": None,
                         "error": {"code": -32700, "message": f"parse error: {e}"}}),
                  flush=True)
            continue
        if not isinstance(req, dict):
            continue
        resp = dispatch(req)
        if resp is not None:
            print(_json(resp), flush=True)


if __name__ == "__main__":
    main()
