#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公众号代码块空格自检（回归防线）。

微信正文编辑器会 trim 掉「独立的空白 <span>」，历史上导致 `SELECT id`
发布后变成 `SELECTid`、`FROM orders` 变成 `FROMorders`。渲染器必须：
  1. 空白并入相邻彩色 token，不产生纯空白 span；
  2. 所有空格 / tab 输出 &nbsp;，且不能被二次转义成 &amp;nbsp;。

本脚本把源文章每个 <pre> 与公众号版 HTML 的代码块逐行还原成纯文本并比对，
任何一行少一个空格都会报错退出（exit 1）。

用法（工作目录为应用根目录）：
  python3 scripts/check_code_spacing.py data/content/2026-11/xxx.html
  python3 scripts/check_code_spacing.py <源文章.html> <公众号版.html>
不传第二个参数时，自动跑一次 dry-run 生成到临时文件（不联网、不建草稿）。
"""
import argparse
import html
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUB = os.path.join(ROOT, "src", "core", "publish_wechat.py")
CONFIG = os.path.join(ROOT, "data", "config.json")

PRE_RE = re.compile(r"<pre[^>]*>([\s\S]*?)</pre>", re.I)
SECTION_RE = re.compile(r"<section\b[^>]*>([\s\S]*?)</section>", re.I)
DIV_RE = re.compile(r"(<div\b[^>]*>)([\s\S]*?)</div>", re.I)
TAG_RE = re.compile(r"<[^>]+>")


def source_blocks(src_html):
    """源文章 <pre> -> [[每行纯文本], ...]"""
    blocks = []
    for m in PRE_RE.finditer(src_html):
        body = re.sub(r"</?code[^>]*>", "", m.group(1), flags=re.I)
        # 早期文章的 <pre> 里有手写高亮 <span class="k">，渲染器会剥掉标签只留文本；
        # 先去标签再 unescape，避免 &lt; / &gt; 被当成标签边界
        body = html.unescape(TAG_RE.sub("", body))
        body = body.rstrip("\n").replace("\t", "    ")
        blocks.append(body.split("\n"))
    return blocks


def rendered_blocks(wx_html):
    """公众号版 HTML 代码块 section -> [[每行纯文本], ...]"""
    blocks = []
    for sec in SECTION_RE.findall(wx_html):
        rows = []
        for tag, body in DIV_RE.findall(sec):
            if "line-height:1.55" not in tag:
                continue
            txt = re.sub(r"<br\s*/?>", "\n", body)
            txt = html.unescape(TAG_RE.sub("", txt)).replace("\xa0", " ")
            if "<span" not in body and txt == " ":  # 空行的 &nbsp; 占位
                txt = ""
            rows.append(txt)
        if rows:
            blocks.append(rows)
    return blocks


def static_checks(wx_html):
    errs = []
    if "&amp;nbsp;" in wx_html:
        errs.append("&nbsp; 被二次转义成 &amp;nbsp;（会显示成字面文本）")
    if re.search(r"<span[^>]*> +</span>", wx_html):
        errs.append("存在纯空格 span，微信编辑器会 trim 掉")
    return errs


def make_render(src_path):
    """自动跑 dry-run，返回临时渲染文件路径。"""
    fd, tmp = tempfile.mkstemp(prefix="code-spacing-", suffix=".wechat.html")
    os.close(fd)
    cmd = [sys.executable, PUB, "--html", os.path.abspath(src_path),
           "--config", CONFIG, "--dry-run", "--out", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"dry-run 失败：{r.stdout}{r.stderr}")
    return tmp


def main():
    ap = argparse.ArgumentParser(description="公众号代码块空格自检")
    ap.add_argument("src", help="源文章 HTML（含 <pre> 代码块）")
    ap.add_argument("rendered", nargs="?", help="公众号版 HTML；省略则自动 dry-run 生成")
    args = ap.parse_args()

    tmp = None
    try:
        wx_path = args.rendered or (tmp := make_render(args.src))
        src = open(args.src, encoding="utf-8").read()
        wx = open(wx_path, encoding="utf-8").read()

        errs = static_checks(wx)
        a, b = source_blocks(src), rendered_blocks(wx)
        if len(a) != len(b):
            errs.append(f"代码块数量不一致：源 {len(a)} / 渲染 {len(b)}")
        else:
            for i, (want, got) in enumerate(zip(a, b), 1):
                if len(want) != len(got):
                    errs.append(f"代码块 {i} 行数不一致：源 {len(want)} / 渲染 {len(got)}")
                    continue
                for j, (x, y) in enumerate(zip(want, got), 1):
                    if x != y:
                        errs.append(f"代码块 {i} 第 {j} 行空格/文本不一致：\n"
                                    f"    源   {x!r}\n    渲染 {y!r}")
        if errs:
            print("FAIL 代码块空格自检未通过：")
            for e in errs:
                print("  -", e)
            return 1
        total = sum(len(x) for x in a)
        print(f"PASS {len(a)} 个代码块 / {total} 行逐字一致，空格完整保留")
        return 0
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


if __name__ == "__main__":
    sys.exit(main())
