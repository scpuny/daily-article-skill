#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""根据 articles/index.json 重建文章画廊 index.html（卡片列表）。"""
import json
import os

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

INDEX = paths.index()
OUT = paths.gallery()


def href(rel):
    """画廊可能在 state/、文章在 data/content/，换算成可解析的相对 URL。"""
    if not rel:
        return ""
    target = paths.resolve(rel)
    base = os.path.dirname(paths.gallery())
    return os.path.relpath(target, base).replace(os.sep, "/")


def thumb(cover):
    if not cover:
        return ""
    return f'<img src="{href(cover)}" alt="cover" style="width:100%;height:170px;object-fit:cover;display:block">'


def tags_html(tags):
    return "".join(f'<span style="display:inline-block;background:#ece9ff;color:#4a3fd6;'
                   f'font-size:12px;padding:2px 10px;border-radius:999px;margin:0 6px 6px 0">{t}</span>'
                   for t in (tags or []))


def card(a):
    pub = a.get("publish") or {}
    status = ""
    if pub.get("media_id"):
        status = '<span style="color:#1a9d5a;font-size:12px;margin-left:8px">· 已发草稿</span>'
    return f'''
    <a class="card" href="{href(a["html"])}">
      {thumb(a.get("cover"))}
      <div class="body">
        <div class="date">{a["date"]}{status}</div>
        <div class="title">{a["title"]}</div>
        <div class="sum">{a.get("summary","").replace("简介：","")}</div>
        <div class="tags">{tags_html(a.get("tags"))}</div>
      </div>
    </a>'''


def main():
    data = json.load(open(INDEX, encoding="utf-8"))
    cards = "".join(card(a) for a in data)
    html = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>内容运营台 · 技术文章画廊</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
    background:#fbfaf7;color:#1c2230;padding:40px 20px}}
  h1{{font-size:26px;margin-bottom:6px}}
  .sub{{color:#5b6472;margin-bottom:30px}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:22px;max-width:1100px;margin:0 auto}}
  .card{{background:#fff;border:1px solid #ece8df;border-radius:16px;overflow:hidden;text-decoration:none;
    color:inherit;transition:transform .15s,box-shadow .15s;box-shadow:0 6px 20px rgba(20,24,48,.06)}}
  .card:hover{{transform:translateY(-4px);box-shadow:0 14px 36px rgba(20,24,48,.14)}}
  .body{{padding:16px 18px 20px}}
  .date{{font-size:12px;color:#9aa1ad;margin-bottom:8px}}
  .title{{font-size:17px;font-weight:700;line-height:1.4;margin-bottom:10px}}
  .sum{{font-size:13.5px;color:#5b6472;line-height:1.65;display:-webkit-box;-webkit-line-clamp:3;
    -webkit-box-orient:vertical;overflow:hidden}}
  .tags{{margin-top:12px}}
</style></head>
<body>
  <h1>内容运营台 · 技术文章画廊</h1>
  <div class="sub">实质性技术分享：Java / Spring / Vue / React 实践 · 架构与封装 · AI 工程化</div>
  <div class="grid">{cards}
  </div>
</body></html>'''
    os.makedirs(os.path.dirname(os.path.abspath(OUT)), exist_ok=True)
    open(OUT, "w", encoding="utf-8").write(html)
    print(f"画廊已生成: {OUT}（{len(data)} 篇）")


if __name__ == "__main__":
    main()
