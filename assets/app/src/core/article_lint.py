#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文章质量闸门（纯本地静态检查，不联网、不调模型）。

定时任务在「写完 index.json 之后、建公众号草稿之前」调用；
errors 非空即阻断发布，最多修订 2 轮。

用法：
  PY article_lint.py --check-latest              # 检查最新一篇（默认写报告 + 回填 quality）
  PY article_lint.py --id 3 --no-write           # 只读检查指定文章
  PY article_lint.py --all                       # 体检全部历史文章（宽松模式，不回填）
"""
import argparse
import ast
import difflib
import html as html_lib
import json
import os
import re
import sys
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

CLICHES = ["首先", "其次", "值得注意的是", "总而言之", "综上所述",
           "众所周知", "不难看出", "在当今时代", "随着人工智能的快速发展"]
CONCRETE = re.compile(
    r"\b\d+\.\d+(?:\.\d+)?\b|\b\d{3,}\b|--[a-z][a-z-]{2,}|"
    r"Exception|Error|Timeout|timeout|\bms\b|\bQPS\b|\b\d+ms\b|0x[0-9a-fA-F]+")
WB_VARS = ["--wb-accent", "--wb-accent2", "--wb-hero-bg", "--wb-ink",
           "--wb-body-text", "--wb-strong-bg", "--wb-strong-fg",
           "--wb-code-bg", "--wb-code-fg"]
LEGACY_COLORS = ["#5b6cff", "#9a5bff", "#1c2230", "#1b2030"]
GROUP_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?/")
PRE_RE = re.compile(r"<pre([^>]*)>([\s\S]*?)</pre>", re.I)
CODE_RE = re.compile(r"<code([^>]*)>([\s\S]*?)</code>", re.I)


def _text_of(fragment):
    t = re.sub(r"<[^>]+>", "", fragment)
    return html_lib.unescape(t)


def _word_count(text):
    prose = re.sub(r"```[\s\S]*?```", "", text)
    cn = len(re.findall(r"[\u4e00-\u9fff]", prose))
    en = len(re.findall(r"[a-zA-Z]+", prose))
    return cn + en


def lint_article(art, index=None, strict=True):
    """检查单篇文章。返回 {ok, errors, warnings, score, checks, title, stats}。"""
    errors, warnings, checks = [], [], {}
    title = (art.get("title") or "").strip()
    summary = (art.get("summary") or "").strip()
    subtitle = (art.get("subtitle") or "").strip()

    def err(msg):
        (errors if strict else warnings).append(msg)

    def warn(msg):
        warnings.append(msg)

    # --- 元数据 ---
    if not title:
        err("标题为空")
    elif len(title) > 32:
        err(f"标题 {len(title)} 字，超过 32 字上限")
    if not subtitle:
        warn("缺少副标题")
    if len(summary) > 120:
        err(f"简介 {len(summary)} 字，超过 120 字上限")
    checks["title"] = len(title)
    checks["summary"] = len(summary)

    # --- 路径与归档 ---
    html_path = None
    for key in ("html", "md", "cover"):
        rel = art.get(key)
        if not rel:
            if key in ("html", "cover"):
                err(f"缺少 {key} 路径")
            continue
        p = paths.resolve(rel, must_exist=True)
        if not p:
            err(f"{key} 断链：{rel}")
        elif key == "html":
            html_path = p
        if key == "html" and not GROUP_RE.match(rel):
            err(f"html 未按月归档（缺 YYYY-MM/ 前缀）：{rel}")
    checks["pathsResolved"] = bool(html_path)

    raw = ""
    if html_path:
        try:
            raw = open(html_path, encoding="utf-8").read()
        except Exception as e:
            err(f"HTML 读取失败：{e}")

    # --- 封面不在正文最前 ---
    first_figure_is_cover = False
    if raw:
        try:
            import publish_wechat
            ex = publish_wechat.Extractor()
            ex.feed(raw)
            blocks = ex.blocks or []
            if blocks and blocks[0].get("type") == "figure":
                first_figure_is_cover = True
            checks["figures"] = sum(1 for b in blocks if b.get("type") == "figure")
            if checks["figures"] > 2:
                warn(f"文中插图 {checks['figures']} 张，超过 0-2 张的建议上限")
        except Exception as e:
            warn(f"正文结构解析失败（跳过结构检查）：{e}")
    if not art.get("cover"):
        err("缺少封面图")
    if first_figure_is_cover:
        err("封面图出现在公众号正文最前面")

    # --- 主题变量 / 旧色 / SVG ---
    if raw:
        missing_vars = [v for v in WB_VARS if v not in raw]
        if missing_vars:
            warn("网页版 :root 缺少主题变量：" + ", ".join(missing_vars[:4]))
        root_m = re.search(r":root\s*\{([\s\S]*?)\}", raw)
        outside = raw.replace(root_m.group(0), "") if root_m else raw
        hits = [c for c in LEGACY_COLORS if c.lower() in outside.lower()]
        if hits:
            warn("疑似写死旧默认色（应改用 var(--wb-…)）：" + ", ".join(hits))
        if re.search(r"<svg[\s>]", raw, re.I):
            err("HTML 内联了 <svg>，微信不支持（架构图须转 PNG）")
        if re.search(r"overflow-x\s*:\s*(auto|scroll)|white-space\s*:\s*nowrap", raw, re.I):
            warn("存在横向滚动样式，公众号可能变形")

    # --- 代码块 ---
    pres = PRE_RE.findall(raw)
    checks["codeBlocks"] = len(pres)
    if not pres:
        err("正文没有代码块")
    unlabeled, too_long, runnable_bad = 0, 0, []
    for attrs, inner in pres:
        m = CODE_RE.search(inner)
        cls = (m.group(1) if m else "") + attrs
        if "language-" not in cls:
            unlabeled += 1
        code = _text_of(m.group(2) if m else inner)
        long_lines = [ln for ln in code.splitlines() if len(ln) > 46]
        too_long += len(long_lines)
        if "runnable" in cls and re.search(r"language-(python|py)\b", cls):
            try:
                ast.parse(code)
            except SyntaxError as e:
                runnable_bad.append(str(e))
    if unlabeled:
        warn(f"{unlabeled} 个代码块缺少 language-xxx 标注，高亮走兜底")
    if too_long:
        warn(f"{too_long} 行代码超过 46 字符，手机端换行可能拥挤")
    for e in runnable_bad:
        err(f"标 runnable 的 Python 片段语法错误：{e}")

    # --- 结构 / 强调 / 字数 ---
    md_rel = art.get("md")
    md_path = paths.resolve(md_rel, must_exist=True) if md_rel else None
    body_text = ""
    if md_path:
        try:
            body_text = open(md_path, encoding="utf-8").read()
        except Exception:
            pass
    if not body_text and raw:
        # 无 md 时从 HTML 取正文；必须剔除 style/script/pre，否则 CSS 会被算进字数
        body_text = re.sub(
            r"<style[\s\S]*?</style>|<script[\s\S]*?</script>|<pre[\s\S]*?</pre>",
            "", raw, flags=re.I)
        body_text = _text_of(body_text)
    words = _word_count(body_text)
    checks["words"] = words
    cfg = paths.load_config()
    lo, hi = (cfg.get("wordRange") or [1200, 2500])[:2]
    if words and not (lo <= words <= hi):
        err(f"正文 {words} 字，不在 {lo}-{hi} 区间")
    if raw:
        h2 = len(re.findall(r"<h2[\s>]", raw, re.I))
        checks["h2"] = h2
        if h2 < 2:
            warn("小标题少于 2 个，层次偏弱")
        if not re.search(r"<(strong|b)[\s>]", raw, re.I):
            warn("没有 strong 强调")
        if not re.search(r"<(ol|ul)[\s>]", raw, re.I):
            warn("没有 ol/ul 要点列表")

    # --- 反 AI 味（规则词表，不调模型） ---
    cliche_hits = [c for c in CLICHES if c in body_text]
    concrete_matches = CONCRETE.findall(body_text)
    concrete_hits = len(concrete_matches)
    concrete_kinds = len(set(concrete_matches))
    checks["cliches"] = cliche_hits
    checks["concreteSignals"] = concrete_hits
    checks["concreteKinds"] = concrete_kinds
    if len(cliche_hits) >= 2:
        err("套话偏多（AI 味）：" + "、".join(cliche_hits))
    elif cliche_hits:
        warn("出现套话：" + "、".join(cliche_hits))
    if concrete_hits < 3:
        err("缺少具体版本号 / 参数值 / 报错信息 / 真实命令（不足 3 处）")

    # --- 撞题 ---
    if index and title:
        dup = []
        for other in index:
            t2 = (other.get("title") or "").strip()
            if not t2 or t2 == title:
                continue
            r = difflib.SequenceMatcher(None, title, t2).ratio()
            if r > 0.75:
                dup.append((round(r, 2), t2))
        if dup:
            err("与历史标题相似度过高（疑似撞题）：" + "; ".join(f"{r} {t}" for r, t in dup[:2]))

    score = max(0, 100 - 20 * len(errors) - 5 * len(warnings))
    return {"ok": len(errors) == 0, "errors": errors, "warnings": warnings,
            "score": score, "checks": checks, "title": title,
            "strict": strict, "checkedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S")}


def latest_index(idx):
    """index.json 按日期倒序（id=0 最新），但不依赖顺序：取 date 最大的一条。"""
    if not idx:
        return -1
    return max(range(len(idx)), key=lambda i: str(idx[i].get("date", "")))


def check_latest(write=True):
    idx = paths.read_json(paths.index(), [])
    if not idx:
        return {"ok": False, "errors": ["index.json 为空"], "warnings": [], "score": 0}
    li = latest_index(idx)
    others = [x for i, x in enumerate(idx) if i != li]
    result = lint_article(idx[li], index=others, strict=True)
    result["articleId"] = li
    if write:
        paths.write_json(paths.lint_report(), result)
        idx[li]["quality"] = {k: result[k] for k in ("ok", "score", "errors", "warnings", "checkedAt")}
        paths.write_json(paths.index(), idx)
        result["qualityWritten"] = True
        result["reportPath"] = paths.lint_report()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check-latest", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--id", type=int, default=None)
    ap.add_argument("--html", default=None, help="直接检查一个 HTML 文件（临时文章）")
    ap.add_argument("--no-write", action="store_true", help="完全只读，不写报告/不回填 quality")
    args = ap.parse_args()
    write = not args.no_write

    if args.all:
        idx = paths.read_json(paths.index(), [])
        out = []
        for i, a in enumerate(idx):
            r = lint_article(a, index=idx[:i] + idx[i + 1:], strict=False)
            out.append({"id": i, "title": r["title"], "ok": r["ok"],
                        "score": r["score"], "errors": r["errors"],
                        "warnings": r["warnings"][:3]})
        if write:
            paths.write_json(paths.lint_report(), {"mode": "all", "items": out,
                                                   "checkedAt": datetime.now().isoformat(timespec="seconds")})
        print(json.dumps({"mode": "all", "count": len(out),
                          "pass": sum(1 for x in out if x["ok"]), "items": out},
                         ensure_ascii=False, indent=2))
        return
    if args.html:
        art = {"title": os.path.basename(args.html), "summary": "", "subtitle": "",
               "html": args.html, "md": None, "cover": None}
        res = lint_article(art, strict=False)
    elif args.id is not None:
        idx = paths.read_json(paths.index(), [])
        if args.id < 0 or args.id >= len(idx):
            print(json.dumps({"error": "id out of range"}, ensure_ascii=False))
            sys.exit(1)
        res = lint_article(idx[args.id], index=idx, strict=True)
        res["articleId"] = args.id
    else:
        res = check_latest(write=write)

    print(json.dumps(res, ensure_ascii=False, indent=2))
    print(f"\n[LINT] {'PASS' if res['ok'] else 'BLOCK'}  score={res['score']}  "
          f"errors={len(res['errors'])}  warnings={len(res.get('warnings', []))}  "
          f"title={res.get('title', '')}", file=sys.stderr)
    if not res["ok"]:
        sys.exit(2)     # 非零退出码，便于 CI / 定时任务判断是否阻断


if __name__ == "__main__":
    main()
