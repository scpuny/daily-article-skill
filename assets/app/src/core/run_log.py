#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行台账：每次定时任务写一条，回答「昨天为什么没发」。

存储：articles/state/runs/<YYYY-MM-DD>.json（当天多条追加）
用法：
  PY run_log.py --write '{"pillar":"D","title":"...","lint":{"ok":true},"media_id":"..."}'
  PY run_log.py --list --limit 10
  PY run_log.py --latest
"""
import argparse
import glob
import json
import os
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

FIELDS = ("topic", "pillar", "theme", "title", "words", "images",
          "lint", "media_id", "durationSec", "error", "articleId")


def write_run(record):
    """追加一条运行记录，返回 (记录, 文件路径)。"""
    if not isinstance(record, dict):
        raise ValueError("record 必须是 JSON 对象")
    now = datetime.now()
    item = {k: record.get(k) for k in FIELDS if record.get(k) is not None}
    item["ts"] = record.get("ts") or now.strftime("%Y-%m-%dT%H:%M:%S")
    item["date"] = item["ts"][:10]
    item["runId"] = record.get("runId") or now.strftime("%Y%m%d-%H%M%S-%f")[:21]
    os.makedirs(paths.runs_dir(), exist_ok=True)
    p = os.path.join(paths.runs_dir(), item["date"] + ".json")
    items = paths.read_json(p, []) or []
    items.append(item)
    paths.write_json(p, items)
    return item, p


def list_runs(limit=10):
    files = sorted(glob.glob(os.path.join(paths.runs_dir(), "*.json")), reverse=True)
    out = []
    for p in files:
        v = paths.read_json(p, []) or []
        out.extend(v if isinstance(v, list) else [v])
    out.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return out[:max(0, limit)]


def latest():
    rows = list_runs(1)
    return rows[0] if rows else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", default=None, help="单行 JSON 字符串")
    ap.add_argument("--stdin", action="store_true", help="从标准输入读 JSON")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    raw = None
    if args.stdin:
        raw = sys.stdin.read()
    elif args.write:
        raw = args.write
    if raw:
        try:
            rec = json.loads(raw)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"JSON 解析失败: {e}"}, ensure_ascii=False))
            sys.exit(1)
        item, p = write_run(rec)
        print(json.dumps({"ok": True, "runId": item["runId"], "path": p,
                          "record": item}, ensure_ascii=False, indent=2))
        return
    if args.latest:
        print(json.dumps(latest(), ensure_ascii=False, indent=2))
        return
    rows = list_runs(args.limit)
    print(json.dumps({"count": len(rows), "runs": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
