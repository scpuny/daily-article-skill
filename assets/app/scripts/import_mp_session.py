#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导入公众号后台登录态（Chrome DevTools → 请求右键 Copy as cURL）。

用法：
  python3 scripts/import_mp_session.py                     # 粘贴 cURL，Ctrl-D 结束
  python3 scripts/import_mp_session.py --file ~/curl.txt   # 从文件读
  python3 scripts/import_mp_session.py --status            # 只看脱敏状态
  python3 scripts/import_mp_session.py --check             # 用台账里第一篇探测是否返回 xls
  python3 scripts/import_mp_session.py --check 2247485682_1 2026-11-06

也可完全不用命令行：文章管理页 → 「🔑 登录态」按钮 → 粘贴保存。
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "src"), os.path.join(_ROOT, "src", "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import paths  # noqa: E402
import mp_session  # noqa: E402


def _first_msgid():
    try:
        with open(paths.detail_index(), encoding="utf-8") as f:
            items = (json.load(f).get("items") or {})
    except (OSError, ValueError):
        return "", ""
    for k, v in items.items():
        if k and v.get("date"):
            return k, v["date"]
    return "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", help="从文件读 cURL 文本")
    ap.add_argument("--status", action="store_true", help="只打印脱敏状态")
    ap.add_argument("--check", nargs="*", help="探测单篇明细下载，可跟 msgid 与发表日期")
    args = ap.parse_args()

    if args.status:
        print(json.dumps(mp_session.status(), ensure_ascii=False, indent=2))
        return 0

    if args.check is not None:
        msgid, pub = (args.check + ["", ""])[:2]
        if not msgid:
            msgid, pub = _first_msgid()
        if not msgid:
            print("没有可用的 msgid：请先导入 total_*.xls，或显式给 --check <msgid> <date>")
            return 1
        res = mp_session.check(msgid, pub)
        print(json.dumps({"msgid": msgid, "publishDate": pub, **res},
                         ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.file:
        with open(os.path.expanduser(args.file), encoding="utf-8") as f:
            text = f.read()
    else:
        print("请在公众号后台首页，DevTools → Network → 任意 mp.weixin.qq.com 请求",
              "→ 右键 Copy → Copy as cURL (bash)，粘贴到下面，然后 Ctrl-D：", sep="\n")
        text = sys.stdin.read()
    try:
        st = mp_session.save_session(text)
    except ValueError as e:
        print("导入失败：" + str(e))
        return 1
    print("已保存登录态：" + paths.mp_session())
    print(json.dumps(st, ensure_ascii=False, indent=2))
    print("可运行 --check 验证是否返回 xls；凭证不会进入 git（state/ 已忽略，权限 600）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
