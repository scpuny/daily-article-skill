#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文章主题数据层（多主题 / 可插拔）。

数据文件：articles/themes.json
{
  "active": "aurora",
  "themes": [
    {"id": "aurora", "name": "极光蓝紫", "builtin": true, "desc": "...", "tokens": {...}}
  ]
}

职责边界：
  - 只负责主题的存取 / 校验 / 补全，不 import publish_wechat（由调用方注入）
  - 颜色值会被直接写进公众号 style 属性，因此这里做严格白名单校验，防止 CSS 注入
"""
import json
import os
import re

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

THEMES_PATH = paths.themes()

# ---------- token 定义（顺序即设置页展示顺序） ----------
TOKEN_GROUPS = [
    {"key": "brand", "label": "品牌强调", "tokens": [
        {"key": "accent",    "label": "主强调色",   "type": "color"},
        {"key": "accent2",   "label": "辅助强调色", "type": "color"},
        {"key": "strongBg",  "label": "加粗底色",   "type": "text", "hint": "支持 rgba()，如 rgba(91,108,255,.13)"},
        {"key": "strongFg",  "label": "加粗文字色", "type": "color"},
        {"key": "badgeText", "label": "序号徽标字", "type": "color"},
        {"key": "kickerText", "label": "顶部标签字", "type": "color"},
    ]},
    {"key": "hero", "label": "标题区", "tokens": [
        {"key": "heroBg",    "label": "标题区背景", "type": "color"},
        {"key": "heroTitle", "label": "主标题文字", "type": "color"},
        {"key": "heroSub",   "label": "副标题文字", "type": "color"},
        {"key": "ink",       "label": "小标题文字", "type": "color"},
    ]},
    {"key": "body", "label": "正文", "tokens": [
        {"key": "bodyText",  "label": "正文文字",   "type": "color"},
        {"key": "muted",     "label": "次要文字",   "type": "color"},
        {"key": "summaryBg", "label": "简介底色",   "type": "color"},
    ]},
    {"key": "code", "label": "代码高亮", "tokens": [
        {"key": "codeBg",      "label": "代码块背景", "type": "color"},
        {"key": "codeFg",      "label": "代码默认字", "type": "color"},
        {"key": "codePlain",   "label": "普通 token", "type": "color"},
        {"key": "codeComment", "label": "注释",       "type": "color"},
        {"key": "codeKeyword", "label": "关键字",     "type": "color"},
        {"key": "codeString",  "label": "字符串",     "type": "color"},
        {"key": "codeNumber",  "label": "数字",       "type": "color"},
        {"key": "codeType",    "label": "类型",       "type": "color"},
        {"key": "codeFunc",    "label": "函数名",     "type": "color"},
    ]},
]

TOKEN_KEYS = [t["key"] for g in TOKEN_GROUPS for t in g["tokens"]]
COLOR_TOKEN_KEYS = [t["key"] for g in TOKEN_GROUPS for t in g["tokens"] if t["type"] == "color"]

# ---------- 默认（与改造前 publish_wechat.py 硬编码完全一致，保证零回归） ----------
DEFAULT_TOKENS = {
    "accent":     "#5b6cff",
    "accent2":    "#9a5bff",
    "strongBg":   "rgba(91,108,255,.13)",
    "strongFg":   "#3a35c9",
    "badgeText":  "#ffffff",
    "kickerText": "#ffffff",
    "heroBg":     "#0f1220",
    "heroTitle":  "#ffffff",
    "heroSub":    "#dfe3ff",
    "ink":        "#1c2230",
    "bodyText":   "#252b39",
    "muted":      "#5b6472",
    "summaryBg":  "#f3f1ec",
    "codeBg":     "#1b2030",
    "codeFg":     "#e7ecff",
    "codePlain":  "#e7ecff",
    "codeComment": "#7a85a8",
    "codeKeyword": "#c792ea",
    "codeString":  "#7fe0c0",
    "codeNumber":  "#f78c6c",
    "codeType":    "#82aaff",
    "codeFunc":    "#82aaff",
}


def _preset(pid, name, desc, **overrides):
    tokens = dict(DEFAULT_TOKENS)
    tokens.update(overrides)
    return {"id": pid, "name": name, "desc": desc, "builtin": True, "tokens": tokens}


PRESETS = [
    _preset("aurora", "极光蓝紫", "当前默认配色，蓝紫渐变 + 深色代码",
            ),
    _preset("cyber", "科技蓝", "呼应数据大屏，青蓝高对比",
            accent="#00a8ff", accent2="#00e5c0", strongBg="rgba(0,168,255,.14)",
            strongFg="#0069c9", heroBg="#06182e", heroSub="#a9d8ff",
            ink="#0d2b45", bodyText="#17324a", muted="#5b7a96", summaryBg="#eaf6ff",
            codeBg="#0a1b30", codeFg="#e2f2ff", codePlain="#e2f2ff",
            codeComment="#6f8ba8", codeKeyword="#5ad2ff", codeString="#7fe0c0",
            codeNumber="#ffb86b", codeType="#82aaff", codeFunc="#5ad2ff"),
    _preset("ink", "墨韵青灰", "低饱和商务风，适合长文与架构解析",
            accent="#2f6f6a", accent2="#8a7f6d", strongBg="rgba(47,111,106,.12)",
            strongFg="#1f5a55", heroBg="#1d2426", heroSub="#c9d6d2",
            ink="#20292b", bodyText="#2e383a", muted="#6c7878", summaryBg="#f2f3ef",
            codeBg="#20292c", codeFg="#e6ece9", codePlain="#e6ece9",
            codeComment="#88958f", codeKeyword="#a7c4b5", codeString="#d9c58a",
            codeNumber="#e09b7d", codeType="#9fc7d8", codeFunc="#9fc7d8"),
    _preset("amber", "暖橙琥珀", "暖色系，标题更有温度",
            accent="#e07b39", accent2="#d94f70", strongBg="rgba(224,123,57,.14)",
            strongFg="#b4551c", heroBg="#2b1b16", heroSub="#ffdcc0",
            ink="#3a2418", bodyText="#3d2a20", muted="#8a6a55", summaryBg="#fff5e9",
            codeBg="#2a1d1a", codeFg="#ffeede", codePlain="#ffeede",
            codeComment="#a98d7d", codeKeyword="#ffb37a", codeString="#b8d98b",
            codeNumber="#ff8f6b", codeType="#ffd08a", codeFunc="#ffd08a"),
    _preset("forest", "森野青绿", "清爽护眼，适合系列教程",
            accent="#2e9e6b", accent2="#7fb069", strongBg="rgba(46,158,107,.13)",
            strongFg="#1c7a4f", heroBg="#10241c", heroSub="#bfe6cf",
            ink="#153a29", bodyText="#22402f", muted="#62806c", summaryBg="#eef8f1",
            codeBg="#14261e", codeFg="#e4f5e9", codePlain="#e4f5e9",
            codeComment="#7e9a88", codeKeyword="#8fd6a8", codeString="#d6e08a",
            codeNumber="#f0a878", codeType="#8ecfd8", codeFunc="#8ecfd8"),
]

# 严格颜色白名单：#rgb / #rrggbb / #rrggbbaa / rgb(...) / rgba(...) / transparent
_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?(?:[0-9a-fA-F]{2})?"
    r"|(?:rgb|rgba)\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*[\d.]+\s*)?\)"
    r"|transparent)$"
)
_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,40}$")


def _check_color(key, value):
    if not isinstance(value, str) or not _COLOR_RE.match(value.strip()):
        raise ValueError(f"颜色「{key}」格式不合法：{value!r}（只允许 #hex / rgb() / rgba()）")
    return value.strip()


def normalize_tokens(tokens):
    """补全缺失 token + 校验颜色，返回干净 dict（只含 schema 内的键）。"""
    raw = tokens if isinstance(tokens, dict) else {}
    out = {}
    for key in TOKEN_KEYS:
        val = raw.get(key, DEFAULT_TOKENS[key])
        if key in COLOR_TOKEN_KEYS:
            out[key] = _check_color(key, val)
        else:
            # strongBg 允许 rgba()，同样走颜色白名单
            out[key] = _check_color(key, val)
    return out


def default_document():
    return {"active": "aurora",
            "themes": [dict(p, tokens=dict(p["tokens"])) for p in PRESETS]}


def load(path=None):
    """读 themes.json；不存在则用内置预设创建。自动补齐新增的内置主题。"""
    p = path or paths.themes()
    doc = default_document()
    if not os.path.exists(p):
        save(doc, p)
        return doc
    try:
        with open(p, encoding="utf-8") as f:
            cur = json.load(f)
    except Exception:
        save(doc, p)
        return doc
    themes = cur.get("themes") if isinstance(cur, dict) else None
    if not isinstance(themes, list) or not themes:
        save(doc, p)
        return doc
    # 合并内置主题（用户可能删过 / 版本升级新增），保留用户自定义主题与修改
    by_id = {t.get("id"): t for t in themes if isinstance(t, dict) and t.get("id")}
    merged = []
    for t in themes:
        if isinstance(t, dict) and t.get("id"):
            merged.append(t)
    for preset in PRESETS:
        if preset["id"] not in by_id:
            merged.append(dict(preset, tokens=dict(preset["tokens"])))
    doc = {"active": cur.get("active") or "aurora", "themes": merged}
    if doc["active"] not in {t["id"] for t in merged}:
        doc["active"] = merged[0]["id"]
    return doc


def save(doc, path=None):
    """校验并原子写入。返回规范化后的 doc。"""
    p = path or paths.themes()
    os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
    if not isinstance(doc, dict):
        raise ValueError("主题数据格式错误")
    themes = doc.get("themes")
    if not isinstance(themes, list) or not themes:
        raise ValueError("至少需要保留一个主题")
    seen, clean = set(), []
    for t in themes:
        if not isinstance(t, dict):
            raise ValueError("主题项格式错误")
        tid = str(t.get("id") or "").strip()
        name = str(t.get("name") or "").strip()
        if not _ID_RE.match(tid):
            raise ValueError(f"主题 id 不合法：{tid!r}（仅字母数字 _ -，≤40 字符）")
        if tid in seen:
            raise ValueError(f"主题 id 重复：{tid}")
        if not name or len(name) > 24:
            raise ValueError("主题名称需为 1-24 个字符")
        seen.add(tid)
        clean.append({
            "id": tid,
            "name": name,
            "desc": str(t.get("desc") or "")[:80],
            "builtin": bool(t.get("builtin")),
            "tokens": normalize_tokens(t.get("tokens")),
        })
    active = str(doc.get("active") or "").strip()
    if active not in seen:
        raise ValueError("当前启用的主题不存在")
    out = {"active": active, "themes": clean}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return out


def resolve(doc, theme_id=None):
    """取指定（或当前启用）主题的完整 tokens。"""
    tid = theme_id or doc.get("active")
    for t in doc.get("themes", []):
        if t.get("id") == tid:
            return normalize_tokens(t.get("tokens"))
    first = (doc.get("themes") or [{}])[0]
    return normalize_tokens(first.get("tokens"))


def active_tokens(theme_id=None, path=None):
    """便捷入口：直接读盘返回当前启用主题 tokens，供 publish_wechat 使用。"""
    return resolve(load(path), theme_id)


def css_vars(tokens):
    """供网页版文章生成 CSS 变量（供定时任务使用）。"""
    t = normalize_tokens(tokens)
    return "\n".join(f"  --wb-{k}: {v};" for k, v in t.items())


def schema():
    return {"groups": TOKEN_GROUPS, "defaults": DEFAULT_TOKENS,
            "tokenKeys": TOKEN_KEYS, "colorKeys": COLOR_TOKEN_KEYS}
