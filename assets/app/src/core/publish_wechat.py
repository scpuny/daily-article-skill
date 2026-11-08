#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公众号原始接口发布器（绕过言序，保留自定义 HTML 设计感）。

流程：
  1. 解析文章 HTML（articles/<slug>.html，固定模板结构）-> 结构化 blocks
  2. 重排为「公众号安全的内联样式」HTML（去掉 <style>/class/渐变/阴影，仅保留内联样式）
     - h2 自动编号并加渐变徽标；加粗(strong)与有序/无序列表序号加高亮背景
  3. 配图上传为永久素材（cover -> thumb_media_id；正文图 -> url）
  4. 调草稿接口 /cgi-bin/draft/add 建草稿

用法：
  python3 publish_wechat.py --html articles/xxx.html --config articles/config.json [--title T] [--replace MID] [--dry-run]
"""
import argparse
import html
import json
import os
import re
import sys
import time
import threading
import urllib.request
from html.parser import HTMLParser

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

try:
    import requests
    HAVE_REQUESTS = True
except Exception:
    HAVE_REQUESTS = False

# ---------- 配色（主题化：默认与历史硬编码完全一致，可由 themes.json 注入覆盖） ----------
try:
    import theme_manager as _theme_manager
except Exception:
    _theme_manager = None

DEFAULT_TOKENS = {
    "accent":     "#5b6cff",  "accent2":   "#9a5bff",
    "strongBg":   "rgba(91,108,255,.13)", "strongFg": "#3a35c9",
    "badgeText":  "#ffffff",  "kickerText": "#ffffff",
    "heroBg":     "#0f1220",  "heroTitle": "#ffffff", "heroSub": "#dfe3ff",
    "ink":        "#1c2230",  "bodyText":  "#252b39", "muted": "#5b6472",
    "summaryBg":  "#f3f1ec",
    "codeBg":     "#1b2030",  "codeFg":    "#e7ecff", "codePlain": "#e7ecff",
    "codeComment": "#7a85a8", "codeKeyword": "#c792ea", "codeString": "#7fe0c0",
    "codeNumber":  "#f78c6c", "codeType": "#82aaff", "codeFunc": "#82aaff",
}

# 语法高亮（按语言分色；每个 token 均包 <span>，避免公众号丢失词间空格）
# 配色：深色背景上的高对比、低饱和、有层次
_PALETTE = {
    "comment": "#7a85a8", "keyword": "#c792ea", "string": "#7fe0c0",
    "number": "#f78c6c", "type": "#82aaff", "func": "#82aaff", "plain": "#e7ecff",
}

_T = dict(DEFAULT_TOKENS)
THEME_LOCK = threading.RLock()   # 主题是模块级状态，管理台预览与发布需串行化


def apply_theme(tokens=None, config_path=None):
    """把主题 token 注入渲染器。

    tokens=None 时自动读取 config 同目录 themes.json 的 active 主题；
    找不到主题文件时回落到默认配色。返回实际生效的 tokens。
    """
    global _T, ACCENT, ACCENT2, INK, MUTED, CODE_BG, CODE_FG, CODE_COLOR
    global STRONG_BG, STRONG_FG, HERO_BG, HERO_TITLE, HERO_SUB
    global KICKER_TEXT, BADGE_TEXT, BODY_TEXT, SUMMARY_BG
    raw = dict(DEFAULT_TOKENS)
    if tokens is None and _theme_manager is not None:
        p = paths.themes()
        if config_path:
            alt = os.path.join(os.path.dirname(os.path.abspath(config_path)), "themes.json")
            if os.path.isfile(alt):
                p = alt
        try:
            tokens = _theme_manager.active_tokens(path=p)
        except Exception:
            tokens = None
    if isinstance(tokens, dict):
        raw.update({k: v for k, v in tokens.items() if k in DEFAULT_TOKENS})
    if _theme_manager is not None:
        try:
            raw = _theme_manager.normalize_tokens(raw)
        except Exception:
            pass
    _T = raw
    ACCENT, ACCENT2 = raw["accent"], raw["accent2"]
    INK, MUTED = raw["ink"], raw["muted"]
    CODE_BG, CODE_FG = raw["codeBg"], raw["codeFg"]
    STRONG_BG, STRONG_FG = raw["strongBg"], raw["strongFg"]
    HERO_BG, HERO_TITLE, HERO_SUB = raw["heroBg"], raw["heroTitle"], raw["heroSub"]
    KICKER_TEXT, BADGE_TEXT = raw["kickerText"], raw["badgeText"]
    BODY_TEXT, SUMMARY_BG = raw["bodyText"], raw["summaryBg"]
    CODE_COLOR = {"c": raw["codeComment"], "k": raw["codeKeyword"],
                  "s": raw["codeString"]}
    _PALETTE.update({
        "comment": raw["codeComment"], "keyword": raw["codeKeyword"],
        "string": raw["codeString"], "number": raw["codeNumber"],
        "type": raw["codeType"], "func": raw["codeFunc"],
        "plain": raw["codePlain"],
    })
    return dict(_T)


# 模块导入时保持历史默认配色；publish_html / 管理台预览时按 active 主题重新注入
apply_theme(tokens=DEFAULT_TOKENS)

# 各语言：comment=注释起始符；kw=关键字；type_kw=类型关键字(蓝)；template=支持反引号模板串
_LANG = {
    "python": {
        "comment": ["#"],
        "kw": {"def","class","return","if","elif","else","for","while","import","from",
               "as","with","try","except","finally","raise","lambda","yield","global",
               "nonlocal","pass","break","continue","in","is","not","and","or","assert",
               "del","async","await","None","True","False","self","cls","print"},
        "type_kw": {"int","str","float","bool","list","dict","set","tuple","object","type"},
    },
    "java": {
        "comment": ["//","/*"],
        "kw": {"abstract","assert","boolean","break","byte","case","catch","char","class",
               "const","continue","default","do","double","else","enum","extends","final",
               "finally","float","for","if","goto","implements","import","instanceof","int",
               "interface","long","native","new","package","private","protected","public",
               "return","short","static","strictfp","super","switch","synchronized","this",
               "throw","throws","transient","try","void","volatile","while","var","record",
               "sealed","yield","null","true","false"},
        "type_kw": {"String","Integer","Long","Double","Boolean","Float","Short","Byte",
                    "Object","Class","List","Map","Set","Collection","Optional"},
    },
    "kotlin": {
        "comment": ["//","/*"],
        "kw": {"fun","val","var","class","interface","object","companion","data","sealed",
               "enum","when","if","else","for","while","return","break","continue","try",
               "catch","finally","throw","import","package","public","private","protected",
               "internal","override","abstract","open","final","suspend","lateinit","by",
               "is","as","out","null","true","false","this","super","let","also",
               "apply","run","with","use"},
        "type_kw": {"String","Int","Long","Double","Boolean","Float","Any","Unit","List",
                    "Map","Set","Pair","Triple"},
    },
    "js": {
        "comment": ["//","/*"],
        "kw": {"var","let","const","function","return","if","else","for","while","do",
               "switch","case","default","break","continue","try","catch","finally","throw",
               "new","class","extends","super","this","typeof","instanceof","in","of","void",
               "delete","yield","async","await","import","export","from","as","null","true",
               "false","undefined","NaN"},
        "type_kw": {"String","Number","Boolean","Object","Array","Symbol","Promise","Map",
                    "Set","JSON"},
    },
    "ts": {
        "comment": ["//","/*"],
        "kw": {"var","let","const","function","return","if","else","for","while","do",
               "switch","case","default","break","continue","try","catch","finally","throw",
               "new","class","extends","implements","super","this","typeof","instanceof","in",
               "of","void","delete","yield","async","await","import","export","from","as",
               "null","true","false","undefined","type","interface","enum","namespace",
               "public","private","protected","readonly","abstract","static","keyof","infer"},
        "type_kw": {"string","number","boolean","object","any","unknown","never","void",
                    "Array","Promise","Map","Set","Record","Partial","Readonly"},
        "template": True,
    },
    "go": {
        "comment": ["//","/*"],
        "kw": {"package","import","func","return","if","else","for","range","switch","case",
               "default","break","continue","go","defer","select","chan","map","struct","type",
               "interface","var","const","nil","true","false","fallthrough","goto"},
    },
    "rust": {
        "comment": ["//","/*"],
        "kw": {"fn","let","mut","pub","use","mod","struct","enum","trait","impl","for","while",
               "loop","if","else","match","return","break","continue","self","Self","super",
               "crate","as","in","where","async","await","move","ref","dyn","const","static",
               "unsafe","extern","type","true","false","Some","None","Ok","Err","Result","Option"},
    },
    "c": {
        "comment": ["//","/*"],
        "kw": {"auto","break","case","char","const","continue","default","do","double","else",
               "enum","extern","float","for","goto","if","inline","int","long","register",
               "restrict","return","short","signed","sizeof","static","struct","switch",
               "typedef","union","unsigned","void","volatile","while","NULL"},
        "type_kw": {"size_t","int8_t","int16_t","int32_t","int64_t","uint8_t","uint16_t",
                    "uint32_t","uint64_t","bool","FILE"},
    },
    "cpp": {
        "comment": ["//","/*"],
        "kw": {"auto","break","case","catch","char","class","const","constexpr","continue",
               "default","delete","do","double","dynamic_cast","else","enum","explicit","export",
               "extern","false","float","for","friend","goto","if","inline","int","long",
               "mutable","namespace","new","nullptr","operator","private","protected","public",
               "return","short","signed","sizeof","static","static_cast","struct","switch",
               "template","this","throw","true","try","typedef","typeid","typename","union",
               "unsigned","using","virtual","void","volatile","while","bool","string"},
        "type_kw": {"size_t","vector","map","shared_ptr","unique_ptr","std"},
    },
    "csharp": {
        "comment": ["//","/*"],
        "kw": {"abstract","as","base","bool","break","byte","case","catch","char","checked",
               "class","const","continue","decimal","default","delegate","do","double","else",
               "enum","event","explicit","extern","false","finally","fixed","float","for",
               "foreach","goto","if","implicit","in","int","interface","internal","is","lock",
               "long","namespace","new","null","object","operator","out","override","params",
               "private","protected","public","readonly","ref","return","sbyte","sealed",
               "short","sizeof","stackalloc","static","string","struct","switch","this","throw",
               "true","try","typeof","uint","ulong","unchecked","unsafe","ushort","using",
               "virtual","void","volatile","while","var","async","await","yield"},
        "type_kw": {"Task","List","Dictionary","IEnumerable","Func","Action"},
    },
    "sql": {
        "comment": ["--","/*"],
        "kw": {"select","from","where","insert","into","values","update","set","delete","create",
               "table","drop","alter","index","join","inner","left","right","outer","on","group",
               "by","order","having","limit","offset","distinct","as","and","or","not","null",
               "is","in","exists","between","like","union","all","with","primary","key","foreign",
               "references","default","constraint","unique","case","when","then","else","end",
               "count","sum","avg","min","max"},
    },
    "bash": {
        "comment": ["#"],
        "kw": {"if","then","else","elif","fi","for","while","do","done","case","esac","function",
               "return","exit","export","local","echo","cd","source"},
    },
    "yaml": {"comment": ["#"]},
    "yml": {"comment": ["#"]},
    "toml": {"comment": ["#"]},
    "dockerfile": {"comment": ["#"],
                   "kw": {"FROM","RUN","CMD","LABEL","EXPOSE","ENV","ADD","COPY","ENTRYPOINT",
                          "VOLUME","USER","WORKDIR","ARG","ONBUILD","HEALTHCHECK","SHELL"}},
    "properties": {"comment": ["#","!"]},
    "ini": {"comment": ["#",";"]},
    "json": {"comment": []},
    "html": {"comment": ["<!--"]},
    "xml": {"comment": ["<!--"]},
    "css": {"comment": ["/*"]},
}
_LANG["sh"] = _LANG["bash"]
_LANG["shell"] = _LANG["bash"]
_LANG["javascript"] = _LANG["js"]
_LANG["typescript"] = _LANG["ts"]
_LANG["py"] = _LANG["python"]
_LANG["c++"] = _LANG["cpp"]
_LANG["golang"] = _LANG["go"]
_LANG["rs"] = _LANG["rust"]

# 未标注语言时的通用兜底：注释/关键字取多语言并集，保证不漏染、不错染
_UNIVERSAL = {
    "comment": ["//", "/*", "#", "--", "<!--"],
    "kw": set().union(*[v.get("kw", set()) for k, v in _LANG.items()
                        if k not in ("yaml","yml","toml","dockerfile","properties","ini","json","html","xml","css")]),
    "type_kw": set().union(*[v.get("type_kw", set()) for k, v in _LANG.items() if v.get("type_kw")]),
}


def _esc(s):
    return html.escape(s, quote=True)


def highlight_code(text, lang=None):
    """整段高亮 HTML。每个 token 都包成 <span>，避免公众号渲染时吃掉词间空格。"""
    spec = _LANG.get((lang or "").lower(), _UNIVERSAL)
    comments = spec.get("comment", [])
    kw = spec.get("kw", set())
    type_kw = spec.get("type_kw", set())
    template = spec.get("template", False)
    P = _PALETTE

    def wrap(color, txt):
        if not txt:
            return ""
        t = _esc(txt)
        if color is None:
            return f'<span style="color:{P["plain"]}">{t}</span>'
        return f'<span style="color:{color}">{t}</span>'

    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        # 注释
        hit = None
        for cs in comments:
            if text[i:i + len(cs)] == cs:
                hit = cs
                break
        if hit:
            if hit in ("/*", "<!--"):
                close = "*/" if hit == "/*" else "-->"
                j = text.find(close, i + len(hit))
                end = (j + len(close)) if j >= 0 else n
            else:  # 行注释
                j = text.find("\n", i + len(hit))
                end = j if j >= 0 else n
            if end <= i:  # 兜底：任何情况下都推进指针，避免死循环
                end = i + 1
            out.append(wrap(P["comment"], text[i:end]))
            i = end
            continue
        # 字符串 / 模板串
        if c in "\"'`":
            if c == "`" and not template:
                out.append(wrap(None, c)); i += 1; continue
            q = c
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == q:
                    j += 1
                    break
                j += 1
            out.append(wrap(P["string"], text[i:j]))
            i = j
            continue
        # 数字
        if c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in "._" or
                             (j == i + 1 and text[i] == "0" and text[j] in "xXoObB")):
                j += 1
            out.append(wrap(P["number"], text[i:j]))
            i = j
            continue
        # 标识符 / 单词
        if c.isalpha() or c in "_$@.":
            j = i
            while j < n and (text[j].isalnum() or text[j] in "_$@."):
                j += 1
            word = text[i:j]
            if word in kw:
                out.append(wrap(P["keyword"], word))
            elif word in type_kw:
                out.append(wrap(P["type"], word))
            elif word and word[0].isupper() and word not in ("Self",):
                out.append(wrap(P["type"], word))
            elif text[j:j + 1] == "(":
                out.append(wrap(P["func"], word))
            else:
                out.append(wrap(None, word))
            i = j
            continue
        # 空白（含换行）：一律包 plain span，空格属于某个 span 内部，公众号不会丢
        if c in " \t\n":
            j = i
            while j < n and text[j] in " \t\n":
                j += 1
            out.append(wrap(None, text[i:j]))
            i = j
            continue
        out.append(wrap(None, c))
        i += 1
    return "".join(out)


FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"
MONO = "Consolas,Monaco,Menlo,monospace"


# ---------- 1. 解析文章 HTML ----------
class Extractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self.cover = None
        self.code_lang = None
        self.title = self.subtitle = self.kicker = self.summary = None
        self.header_open = False
        self.target = None          # kicker/title/subtitle/summary/figcaption
        self._buf = []
        self.cur = None             # 当前 block
        self.in_pre = False
        self.code_raw = []
        self.code_active = False
        self.segs = []              # 当前内联容器(p/quote/li)的文本段 [(kind,text)]
        self.kind = "t"             # t=普通文本 / s=加粗
        self.in_list = False
        self.list_ordered = False

    # ---- 内联文本段管理 ----
    def _push_seg(self):
        text = "".join(self._buf)
        if text:
            self.segs.append((self.kind, text))
        self._buf = []

    def _inline_data(self, data):
        self._buf.append(data)

    def _flush_inline(self):
        self._push_seg()
        segs = self.segs
        self.segs = []
        return segs

    # ---- 块结束 ----
    def _end_block(self):
        if self.cur is None:
            return
        t = self.cur["type"]
        if t in ("p", "quote"):
            self.cur["segs"] = self._flush_inline()
            if any(s[1].strip() for s in self.cur["segs"]):
                self.blocks.append(self.cur)
        elif t == "h2":
            if self.cur.get("text", "").strip():
                self.blocks.append(self.cur)
        elif t == "figure":
            if self.cur.get("img") or self.cur.get("caption"):
                self.blocks.append(self.cur)
        elif t == "list":
            if self.cur.get("items"):
                self.blocks.append(self.cur)
        self.cur = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = attrs.get("class", "")
        if tag == "header":
            self.header_open = True
        if tag == "img":
            if self.header_open:
                self.cover = attrs.get("src")
            elif self.cur and self.cur["type"] == "figure":
                self.cur["img"] = attrs.get("src")
            return
        if self.header_open:
            if tag == "span" and "kicker" in cls:
                self.target, self._buf = "kicker", []
            elif tag == "h1":
                self.target, self._buf = "title", []
            elif tag == "div" and "subtitle" in cls:
                self.target, self._buf = "subtitle", []
        if tag == "div" and "summary" in cls:
            self.target, self._buf = "summary", []
        if tag == "h2":
            self._end_block(); self.cur = {"type": "h2", "text": ""}; self._buf = []
        elif tag == "p":
            self._end_block(); self.cur = {"type": "p"}; self.segs = []; self.kind = "t"
        elif tag == "figure":
            self._end_block(); self.cur = {"type": "figure", "img": None, "caption": ""}
        elif tag == "figcaption" and self.cur and self.cur["type"] == "figure":
            self.target, self._buf = "figcaption", []
        elif tag == "blockquote":
            self._end_block(); self.cur = {"type": "quote"}; self.segs = []; self.kind = "t"
        elif tag in ("ul", "ol"):
            self._end_block(); self.cur = {"type": "list", "ordered": tag == "ol", "items": []}
            self.in_list = True
        elif tag == "li" and self.in_list:
            self.segs = []; self.kind = "t"
        elif tag == "pre":
            self._end_block(); self.in_pre = True; self.code_raw = []
        # 捕获代码语言（<pre class="language-python"> 或 <code class="language-xxx">）
        if self.in_pre and tag in ("pre", "code"):
            clz = attrs.get("class", "") or ""
            lm = re.search(r"(?:language|lang)-([\w+#.-]+)", clz)
            if lm:
                self.code_lang = lm.group(1).lower()
        if tag in ("strong", "b"):
            if not self.in_pre:
                self._push_seg(); self.kind = "s"; self._buf = []

    def handle_endtag(self, tag):
        if tag == "header":
            self.header_open = False
        if self.target and tag in ("span", "h1", "div", "figcaption"):
            text = "".join(self._buf).strip()
            if self.target == "kicker":
                self.kicker = text
            elif self.target == "title":
                self.title = text
            elif self.target == "subtitle":
                self.subtitle = text
            elif self.target == "summary":
                self.summary = text
            elif self.target == "figcaption" and self.cur:
                self.cur["caption"] = text
            self.target, self._buf = None, []
        if tag == "pre":
            self.in_pre = False
            self.code_active = False
            raw = "".join(self.code_raw)
            if raw.strip():
                self.blocks.append({"type": "code", "text": raw, "lang": self.code_lang})
            self.code_lang = None
        elif tag in ("strong", "b"):
            if not self.in_pre:
                self._push_seg(); self.kind = "t"
        elif tag == "li" and self.in_list:
            self.cur["items"].append(self._flush_inline())
        elif tag in ("ul", "ol"):
            self.in_list = False
            self._end_block()
        elif tag == "figure":
            self._end_block()
        elif tag in ("h2", "p", "blockquote"):
            if tag == "h2" and self.cur:
                self.cur["text"] = "".join(self._buf).strip()
            self._end_block()

    def handle_data(self, data):
        if self.target:
            self._buf.append(data)
        elif self.in_pre:
            self.code_raw.append(data)
        elif self.cur and (self.cur["type"] in ("p", "quote") or self.in_list
                           or self.cur["type"] == "h2"):
            if self.cur["type"] == "h2":
                self._buf.append(data)
            else:
                self._inline_data(data)


# ---------- 2. 渲染为公众号安全内联 HTML ----------
def esc(s):
    return html.escape(s, quote=True)


def inline_html(segs):
    out = []
    for kind, text in segs:
        t = esc(text)
        if kind == "s" and t.strip():
            out.append(f'<span style="background:{STRONG_BG};color:{STRONG_FG};'
                       f'padding:0 4px;border-radius:4px;font-weight:600">{t}</span>')
        else:
            out.append(t)
    return "".join(out)


def render(blocks, img_map):
    out = []
    # 注意：公众号正文不以封面图开头——封面仅作为草稿 thumb 与网页版 hero 使用，
    # 正文直接以标题区块（kicker/标题/副标题）开头，避免封面压在文章最前面。

    kicker = getattr(render, "_kicker", None)
    title = getattr(render, "_title", None)
    subtitle = getattr(render, "_subtitle", None)
    if title:
        hero = [f'<section style="background:{HERO_BG};padding:26px 20px 28px;">']
        if kicker:
            hero.append(
                f'<span style="display:inline-block;font-size:12px;letter-spacing:2px;'
                f'color:{KICKER_TEXT};background:{ACCENT};padding:4px 12px;border-radius:999px;">{esc(kicker)}</span>'
            )
        hero.append(
            f'<h1 style="color:{HERO_TITLE};font-size:22px;font-weight:800;line-height:1.4;'
            f'margin:14px 0 0;font-family:{FONT};">{esc(title)}</h1>'
        )
        if subtitle:
            hero.append(
                f'<p style="color:{HERO_SUB};font-size:15px;line-height:1.6;'
                f'margin:12px 0 0;font-family:{FONT};">{esc(subtitle)}</p>'
            )
        hero.append("</section>")
        out.append("".join(hero))

    sec = 0
    for b in blocks:
        t = b["type"]
        if t == "summary":
            out.append(
                f'<section style="background:{SUMMARY_BG};border-left:4px solid {ACCENT};'
                f'padding:16px 18px;margin:20px 0;font-size:15px;color:{MUTED};'
                f'font-family:{FONT};line-height:1.7;">{inline_html(b["segs"])}</section>'
            )
        elif t == "h2":
            sec += 1
            badge = (f'<span style="background:linear-gradient(135deg,{ACCENT},{ACCENT2});'
                     f'color:{BADGE_TEXT};font-size:13px;font-weight:700;padding:2px 9px;'
                     f'border-radius:6px;margin-right:10px;">{sec:02d}</span>')
            out.append(
                f'<h2 style="font-size:20px;font-weight:700;color:{INK};'
                f'margin:30px 0 12px;font-family:{FONT};">{badge}{esc(b["text"])}</h2>'
            )
        elif t == "p":
            out.append(
                f'<p style="font-size:16px;line-height:1.85;color:{BODY_TEXT};'
                f'margin:16px 0;font-family:{FONT};">{inline_html(b["segs"])}</p>'
            )
        elif t == "list":
            items = []
            for i, item in enumerate(b["items"], 1):
                if b["ordered"]:
                    mk = (f'<span style="display:inline-block;background:{ACCENT};'
                          f'color:{BADGE_TEXT};border-radius:4px;padding:0 6px;margin-right:6px;'
                          f'font-size:13px;font-weight:700;font-family:{FONT};'
                          f'vertical-align:middle">{i}</span>')
                else:
                    mk = (f'<span style="color:{ACCENT2};font-weight:700;'
                          f'margin-right:6px;font-size:15px;vertical-align:middle">•</span>')
                # 悬挂缩进用 text-indent：首行标记贴左缘，续行右缩进；无宽标记盒，不会留大空隙
                items.append(
                    f'<section style="margin:6px 0;padding-left:1.3em;'
                    f'text-indent:-1.3em;font-size:16px;line-height:1.85;'
                    f'color:{BODY_TEXT};font-family:{FONT};">'
                    f'{mk}{inline_html(item)}</section>'
                )
            out.append(f'<section style="margin:16px 0">{"".join(items)}</section>')
        elif t == "figure":
            src = img_map.get(b["img"], b["img"])
            out.append(
                f'<img src="{esc(src)}" style="width:100%;display:block;'
                f'border-radius:8px;margin:22px 0;">'
            )
            if b.get("caption"):
                out.append(
                    f'<p style="text-align:center;font-size:13px;color:{MUTED};'
                    f'margin:8px 0 0;font-family:{FONT};">{esc(b["caption"])}</p>'
                )
        elif t == "code":
            lang = b.get("lang")
            lines = (b.get("text", "") or "").split("\n")
            line_divs = []
            for raw in lines:
                m = re.match(r"^[ \t]+", raw)
                indent = ""
                body = raw
                if m:
                    # 行首缩进转 &nbsp;（保留缩进、不参与换行）
                    indent = "".join("&nbsp;" if ch == " " else "&nbsp;&nbsp;&nbsp;&nbsp;"
                                     for ch in m.group())
                    body = raw[m.end():]
                # 行内连续 2+ 空格转 &nbsp; 以保留对齐；单个空格保持普通（可作换行点）
                body = re.sub(r" {2,}", lambda x: "&nbsp;" * len(x.group()), body)
                hl = highlight_code(body, lang)
                line_divs.append(
                    f'<div style="font-family:{MONO};font-size:13px;'
                    f'line-height:1.55;color:{CODE_FG}">{indent}{hl}</div>'
                )
            inner = "".join(line_divs)
            out.append(
                f'<section style="background:{CODE_BG};padding:14px 16px;'
                f'margin:20px 0;border-radius:6px;'
                f'font-family:{MONO};overflow-wrap:break-word;">{inner}</section>'
            )
        elif t == "quote":
            out.append(
                f'<section style="border-left:3px solid {ACCENT2};padding:4px 16px;'
                f'color:{MUTED};font-style:italic;margin:20px 0;font-family:{FONT};'
                f'line-height:1.8;">{inline_html(b["segs"])}</section>'
            )
    return "".join(out)


# ---------- 主题预览（设置页用，不联网） ----------
SAMPLE_BLOCKS = [
    {"type": "summary", "segs": [
        ("", "这是一段文章简介，用来预览主题效果。"),
        ("s", "所见即所得"),
        ("", " —— 下方色板改动会立即反映到这里。"),
    ]},
    {"type": "h2", "text": "一、问题从哪里开始"},
    {"type": "p", "segs": [
        ("", "以前配色散落在渲染函数里十几处，换个色调要改半天。现在统一由 "),
        ("s", "主题 token"),
        ("", " 驱动，切换主题即可整体换肤。"),
    ]},
    {"type": "list", "ordered": False, "items": [
        [("", "标题区、"), ("s", "强调色"), ("", " 与正文分色独立可调")],
        [("", "代码块 9 种语法 token 可单独配色")],
    ]},
    {"type": "list", "ordered": True, "items": [
        [("", "选择或复制一套主题")], [("", "右侧实时预览")], [("", "保存后新文章自动生效")],
    ]},
    {"type": "code", "lang": "python",
     "text": "def apply_theme(tokens=None):\n    # 把主题注入渲染器\n    return render(blocks, img_map)"},
    {"type": "quote", "segs": [("", "这里展示的就是公众号正文的最终排版样式。")]},
]


def render_with_theme(blocks, img_map, tokens=None, config_path=None,
                      kicker=None, title=None, subtitle=None):
    """按指定主题渲染 blocks，返回 (html, applied_tokens)。线程安全。"""
    with THEME_LOCK:
        applied = apply_theme(tokens=tokens, config_path=config_path)
        render._kicker, render._title, render._subtitle = kicker, title, subtitle
        content = render(blocks, img_map)
    return content, applied


# ---------- 3. 微信 API ----------
def net(method, url, data=None, headers=None, timeout=30, retries=5):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method)
            if headers:
                for k, v in headers.items():
                    req.add_header(k, v)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(1 + i)
    raise RuntimeError(f"网络请求失败(重试{retries}次): {last}")


def get_access_token(appid, secret, cache_dir=None):
    cache = (paths.token_cache() if cache_dir is None
             else os.path.join(cache_dir, ".wx_token.json"))
    os.makedirs(os.path.dirname(os.path.abspath(cache)), exist_ok=True)
    if os.path.exists(cache):
        try:
            d = json.load(open(cache))
            if d.get("expires", 0) > time.time() + 120:
                return d["access_token"]
        except Exception:
            pass
    url = ("https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential"
           f"&appid={appid}&secret={secret}")
    resp = json.loads(net("GET", url, timeout=30))
    if "access_token" not in resp:
        raise RuntimeError(f"获取 access_token 失败: {resp}")
    with open(cache, "w") as f:
        json.dump({"access_token": resp["access_token"],
                   "expires": time.time() + resp.get("expires_in", 7200)}, f)
    return resp["access_token"]


def upload_image(access_token, path):
    url = ("https://api.weixin.qq.com/cgi-bin/material/add_material"
           f"?access_token={access_token}&type=image")
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
             "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
    if HAVE_REQUESTS:
        with open(path, "rb") as f:
            resp = requests.post(url, files={"media": (os.path.basename(path), f, ctype)},
                                timeout=60).json()
    else:
        boundary = "----wx" + os.urandom(8).hex()
        data = open(path, "rb").read()
        body = (f"--{boundary}\r\n").encode()
        body += (f'Content-Disposition: form-data; name="media"; '
                 f'filename="{os.path.basename(path)}"\r\n').encode()
        body += f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        resp = json.loads(net("POST", url, data=body, timeout=60))
    if "media_id" not in resp:
        raise RuntimeError(f"上传素材失败: {resp}")
    return resp


def create_draft(access_token, article):
    url = f"https://api.weixin.qq.com/cgi-bin/draft/add?access_token={access_token}"
    payload = json.dumps({"articles": [article]}, ensure_ascii=False).encode("utf-8")
    return json.loads(net("POST", url, data=payload, timeout=30,
                          headers={"Content-Type": "application/json"}))


def delete_draft(access_token, media_id):
    url = f"https://api.weixin.qq.com/cgi-bin/draft/delete?access_token={access_token}"
    payload = json.dumps({"media_id": media_id}).encode("utf-8")
    return json.loads(net("POST", url, data=payload, timeout=30,
                          headers={"Content-Type": "application/json"}))


# ---------- 主流程 ----------
def publish_html(html_path, config_path, replace=None, title_override=None,
                dry_run=False, out=None, theme=None):
    """核心发布逻辑。返回 dict：dry_run 时含 out/title/digest/image_count；
    真实发布时含 media_id/errcode/title/digest。供 CLI 与管理台共用。

    theme: 可选，显式 token dict（设置页预览用）；为 None 时自动读取
           config 同目录 themes.json 的 active 主题，找不到则用默认配色。
    """
    cfg = paths.load_config(config_path)
    appid = cfg.get("appId", "")
    secret = cfg.get("appSecret", "")
    author = cfg.get("author", "内容运营台")
    if not dry_run and (not appid or not secret or "YOUR" in str(appid)):
        raise RuntimeError("请在 config.json 填入有效的 appId / appSecret，或加 dry_run=True 仅验证排版。")

    raw = open(html_path, encoding="utf-8").read()
    ex = Extractor()
    ex.feed(raw)
    blocks = ex.blocks

    img_sources = []
    if ex.cover:
        img_sources.append(ex.cover)
    for b in blocks:
        if b["type"] == "figure" and b.get("img"):
            img_sources.append(b["img"])

    base = os.path.dirname(html_path)
    img_map = {}
    if dry_run:
        for i, src in enumerate(img_sources):
            img_map[src] = f"https://PLACEHOLDER/IMG{i}.jpg"
        if ex.cover:
            img_map["__cover__"] = "https://PLACEHOLDER/COVER.jpg"
    else:
        token = get_access_token(appid, secret)
        thumb = None
        for src in img_sources:
            local = src if os.path.isabs(src) else os.path.join(base, src)
            if not os.path.exists(local):
                print(f"WARN: 图片不存在，跳过: {local}")
                continue
            r = upload_image(token, local)
            img_map[src] = r.get("url", "")
            if src == ex.cover:
                img_map["__cover__"] = r.get("url", "")
                thumb = r["media_id"]
        thumb = img_map.get("__cover_thumb__") or thumb

    with THEME_LOCK:
        apply_theme(tokens=theme, config_path=config_path)
        render._kicker = ex.kicker
        render._title = ex.title
        render._subtitle = ex.subtitle
        content = render(blocks, img_map)

    title = (title_override or ex.title or "").strip()
    if len(title) > 32:
        title = title[:32]
    digest = (ex.summary or ex.subtitle or "")
    digest = re.sub(r"^简介[:：]\s*", "", digest)
    if len(digest) > 120:
        digest = digest[:120]

    if dry_run:
        out_path = out or (os.path.splitext(html_path)[0] + ".wechat.html")
        open(out_path, "w", encoding="utf-8").write(
            "<!DOCTYPE html><html><body>" + content + "</body></html>")
        return {"dry_run": True, "out": out_path, "title": title,
                "digest": digest, "image_count": len(img_sources)}

    if replace:
        dr = delete_draft(token, replace)
        print(f"旧草稿删除({replace}): errcode={dr.get('errcode')}")

    article = {
        "title": title,
        "author": author,
        "digest": digest,
        "content": content,
        "thumb_media_id": thumb,
        "need_open_comment": 0,
        "only_fans_can_comment": 0,
    }
    resp = create_draft(token, article)
    if resp.get("errcode", 0) != 0 and "media_id" not in resp:
        raise RuntimeError(f"建草稿失败: {resp}")
    media_id = resp.get("media_id")
    return {"media_id": media_id, "errcode": resp.get("errcode"),
            "title": title, "digest": digest}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--dry-run", action="store_true", help="仅本地验证排版，不联网")
    ap.add_argument("--out", default=None, help="dry-run 输出文件")
    ap.add_argument("--replace", default=None, help="先删除该 media_id 的旧草稿，再建新草稿")
    ap.add_argument("--title", default=None, help="覆盖文章标题（≤32字，避免硬截断）")
    ap.add_argument("--data-dir", default=None, help="显式指定数据目录（移植部署用）")
    args = ap.parse_args()
    if args.data_dir:
        _os.environ["YIZHIMA_DATA_DIR"] = _os.path.abspath(args.data_dir)
    try:
        res = publish_html(args.html, args.config, replace=args.replace,
                           title_override=args.title, dry_run=args.dry_run,
                           out=args.out)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    if args.dry_run:
        print(f"[dry-run] 公众号版 HTML 已写出: {res['out']}")
        print(f"[dry-run] 标题({len(res['title'])}字): {res['title']}")
        print(f"[dry-run] 摘要({len(res['digest'])}字): {res['digest']}")
        print(f"[dry-run] 图片数: {res['image_count']}（占位 URL）")
        return
    print(f"OK 草稿已创建: media_id={res['media_id']} errcode={res.get('errcode')}")


if __name__ == "__main__":
    main()
