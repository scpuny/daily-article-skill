#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布后对账：草稿箱（draft/batchget）↔ index.json。

只读、不删草稿。标出：
  matched  本地有 media_id 且草稿箱仍在
  missing  本地有 media_id 但草稿箱已无（后台删了 / 失效）
  orphans  草稿箱里有但本地索引没记录
结果写 state/draft_reconcile.json，另需后台群发时手动勾选原创（接口不支持）。
"""
import argparse
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
paths.bootstrap()
import publish_wechat


def reconcile(write=True):
    cfg = paths.load_config()
    appid, secret = cfg.get("appId", ""), cfg.get("appSecret", "")
    if not appid or not secret:
        return {"ok": False, "error": "未配置 appId / appSecret（data/secrets.json）"}
    try:
        token = publish_wechat.get_access_token(appid, secret)
        url = ("https://api.weixin.qq.com/cgi-bin/draft/batchget"
               f"?access_token={token}")
        drafts, offset, total = [], 0, None
        while True:
            payload = json.dumps({"offset": offset, "count": 20,
                                  "no_content": 1}, ensure_ascii=False).encode()
            resp = json.loads(publish_wechat.net(
                "POST", url, data=payload, timeout=30,
                headers={"Content-Type": "application/json"}))
            if resp.get("errcode", 0) != 0:
                raise RuntimeError(f"draft/batchget 返回 {resp}")
            total = resp.get("total_count", total or 0)
            items = resp.get("item", []) or []
            for it in items:
                mid = it.get("media_id", "")
                news = ((it.get("content") or {}).get("news_item") or [{}])
                drafts.append({"media_id": mid,
                               "title": (news[0] or {}).get("title", ""),
                               "updateTime": it.get("update_time", "")})
            offset += len(items)
            if not items or offset >= (total or 0):
                break
    except Exception as e:
        return {"ok": False, "error": str(e)}

    idx = paths.read_json(paths.index(), [])
    local = [(i, a.get("title", ""), (a.get("publish") or {}).get("media_id", ""))
             for i, a in enumerate(idx)]
    draft_ids = {d["media_id"] for d in drafts}
    matched = [{"id": i, "title": t, "media_id": m} for i, t, m in local if m and m in draft_ids]
    missing = [{"id": i, "title": t, "media_id": m} for i, t, m in local if m and m not in draft_ids]
    local_ids = {m for _, _, m in local if m}
    orphans = [d for d in drafts if d["media_id"] not in local_ids]
    report = {
        "ok": True, "checkedAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "draftCount": len(drafts), "localCount": len(local),
        "matched": matched, "missing": missing, "orphans": orphans,
        "note": "只读对账，未删除任何草稿；原创声明仍需后台群发时手动勾选。",
    }
    if write:
        paths.write_json(os.path.join(paths.state_dir(), "draft_reconcile.json"), report)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()
    print(json.dumps(reconcile(write=not args.no_write), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
