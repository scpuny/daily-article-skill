#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文章归档：把 articles/ 下平铺的文章按「月份」或「日期」分组到子目录。

默认按月份，结果：
  articles/
    2026-10/
      assets/                       当月配图（与 HTML 同层）
      2026-10-27-mybatis-....html
      2026-10-27-mybatis-....md
    2026-11/
      assets/
      2026-11-05-java-....html
      ...
    index.json                      html/md/cover 路径带分组前缀
    index.html                     画廊（归档后运行 build_gallery.py 重建）

为什么配图也要跟着走：
  文章 HTML 内是 src="assets/xxx.png" 这种相对引用。只要把配图放到
  <group>/assets/、HTML 放到 <group>/ 下，引用就依然成立 —— 已生成
  的文章内容一个字都不用改。index.json 的路径改成 "<group>/..." 后，
  发布脚本（base=dirname(html)）、管理台（/files/<rel>）、统计模块
  （os.path.join(HERE, md)）都天然兼容，无需改动。

用法：
  python3 archive_articles.py --dry-run          # 只看计划，不动文件
  python3 archive_articles.py --by month         # 执行（默认）
  python3 archive_articles.py --by date          # 改成按天分组
  python3 archive_articles.py --rollback         # 按 .archive_map.json 完整还原
"""
import argparse
import json
import os
import re
import shutil
import sys
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

# 文章根目录（新布局 data/content/，旧布局项目根），index.json 相对路径都基于它
HERE = paths.content_dir()
INDEX = paths.index()
ASSETS = os.path.join(HERE, "assets")
MAP = paths.archive_map()
BACKUP_DIR = paths.archive_backup()

DATE_IN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
ASSET_REF = re.compile(r"assets/([A-Za-z0-9._\-]+\.(?:png|jpe?g|gif|webp|svg|bmp))", re.I)
ARTICLE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}-.*\.(?:html|md)$", re.I)


def group_of(date_str, by):
    """'2026-11-05' -> '2026-11' (month) / '2026-11-05' (date)"""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str or ""):
        return None
    return date_str[:7] if by == "month" else date_str[:10]


def group_of_filename(name, by):
    m = DATE_IN_NAME.match(name)
    return group_of(m.group(1), by) if m else None


def infer_group(name, bound):
    """未被 HTML 直接引用的图片（多为 SVG 源图/旧版图）按文件名推断归属。
    先找同名不同扩展名的已绑定图，再退化为最长公共前缀匹配（至少 8 字符）。
    """
    stem = os.path.splitext(name)[0]
    for other, group in bound.items():
        if os.path.splitext(other)[0] == stem:
            return group
    best_len, best_group = 0, None
    for other, group in bound.items():
        n = 0
        for x, y in zip(stem, os.path.splitext(other)[0]):
            if x != y:
                break
            n += 1
        if n > best_len:
            best_len, best_group = n, group
    return best_group if best_len >= 8 else None


def load_index():
    with open(INDEX, encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return ""


def build_plan(by):
    """返回 (moves, index_new, warnings)
    moves: [(src_rel, dst_rel), ...]，全部相对 articles/ 根。
    """
    index = load_index()
    moves, warnings = [], []
    seen_dst = set()

    # 1) 建立「配图文件名 -> 分组」映射：从每篇文章 HTML 的真实引用 + index.cover 推导
    asset_group = {}
    asset_conflict = set()

    def bind(asset_name, group, origin):
        if not asset_name or not group:
            return
        prev = asset_group.get(asset_name)
        if prev and prev != group:
            asset_conflict.add(asset_name)
            warnings.append(f"配图被不同分组共用，保留在根 assets/：{asset_name} ({prev} / {group})")
        else:
            asset_group[asset_name] = group

    for art in index:
        group = group_of(art.get("date", ""), by)
        if not group:
            warnings.append(f"日期无法识别，跳过：{art.get('title', '')!r} date={art.get('date')!r}")
            continue
        html_rel = art.get("html") or ""
        html_path = os.path.join(HERE, html_rel)
        for ref in ASSET_REF.findall(read_text(html_path)):
            bind(os.path.basename(ref), group, html_rel)
        cover = art.get("cover") or ""
        if cover:
            bind(os.path.basename(cover), group, "cover")

    # 2) 文章文件（html / md / *.wechat.html）成组移动
    article_names = sorted(
        n for n in os.listdir(HERE)
        if ARTICLE_FILE.match(n) and os.path.isfile(os.path.join(HERE, n))
    )
    for name in article_names:
        group = group_of_filename(name, by)
        if not group:
            warnings.append(f"文件名无日期前缀，跳过：{name}")
            continue
        src, dst = name, f"{group}/{name}"
        if os.path.exists(os.path.join(HERE, dst)):
            warnings.append(f"目标已存在，跳过：{dst}")
            continue
        moves.append((src, dst))
        seen_dst.add(dst)

    # 3) 配图移动到 <group>/assets/（HTML 里的 "assets/xxx" 引用因此继续有效）
    if os.path.isdir(ASSETS):
        for name in sorted(os.listdir(ASSETS)):
            src_path = os.path.join(ASSETS, name)
            if not os.path.isfile(src_path):
                continue
            if name in asset_conflict:
                continue
            group = asset_group.get(name)
            if not group:
                group = infer_group(name, asset_group)
                if group:
                    warnings.append(f"配图按文件名推断归入 {group}/：{name}")
            if not group:
                warnings.append(f"配图无归属文章，保留在根 assets/：{name}")
                continue
            src, dst = f"assets/{name}", f"{group}/assets/{name}"
            if os.path.exists(os.path.join(HERE, dst)):
                warnings.append(f"目标已存在，跳过：{dst}")
                continue
            moves.append((src, dst))
            seen_dst.add(dst)

    # 4) index.json 路径加分组前缀；顺带清掉指向不存在文件的历史幽灵路径
    move_pairs = {(s, d) for s, d in moves}
    index_new = json.loads(json.dumps(index, ensure_ascii=False))
    for art in index_new:
        group = group_of(art.get("date", ""), by)
        if not group:
            continue
        for key in ("html", "md", "cover"):
            rel = art.get(key)
            if not rel or re.match(r"^\d{4}-\d{2}(-\d{2})?/", rel):
                continue                    # 已带分组前缀，幂等
            expected = f"{group}/{rel}"
            if (rel, expected) in move_pairs:
                art[key] = expected
            elif not os.path.isfile(os.path.join(HERE, rel)):
                warnings.append(f"索引指向不存在的文件，已清空 {key}: {rel}")
                art[key] = None

    return moves, index_new, warnings


def apply(moves, index_new, by):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup = os.path.join(BACKUP_DIR, f"index-{stamp}.json")
    shutil.copy2(INDEX, backup)

    # 先建目录
    for _, dst in moves:
        os.makedirs(os.path.dirname(os.path.join(HERE, dst)), exist_ok=True)

    done = []
    try:
        for src, dst in moves:
            s, d = os.path.join(HERE, src), os.path.join(HERE, dst)
            if not os.path.isfile(s):
                continue
            shutil.move(s, d)
            done.append([src, dst])
        atomic_write_json(INDEX, index_new)
    except Exception:
        # 中途失败立即回滚已移动的文件，index.json 尚未写入则天然保持原样
        for src, dst in reversed(done):
            s, d = os.path.join(HERE, src), os.path.join(HERE, dst)
            if os.path.isfile(d) and not os.path.exists(s):
                shutil.move(d, s)
        raise

    with open(MAP, "w", encoding="utf-8") as f:
        json.dump({
            "by": by,
            "createdAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "indexBackup": os.path.relpath(backup, paths.home()),
            "files": done,
        }, f, ensure_ascii=False, indent=2)
    return backup


def rollback():
    if not os.path.isfile(MAP):
        print("没有 .archive_map.json，无需回滚。")
        return 1
    with open(MAP, encoding="utf-8") as f:
        m = json.load(f)

    moved_back = 0
    for src, dst in reversed(m.get("files", [])):
        s, d = os.path.join(HERE, src), os.path.join(HERE, dst)
        if os.path.isfile(d) and not os.path.exists(s):
            os.makedirs(os.path.dirname(s), exist_ok=True)
            shutil.move(d, s)
            moved_back += 1

    backup = paths.resolve(m.get("indexBackup", ""), must_exist=True) or ""
    if os.path.isfile(backup):
        shutil.copy2(backup, INDEX)
        print(f"index.json 已从备份恢复：{m['indexBackup']}")
    else:
        print("WARN: 备份不存在，index.json 未恢复", file=sys.stderr)

    # 只清理自己创建且已空的分组目录
    groups = {dst.split("/", 1)[0] for _, dst in m.get("files", []) if "/" in dst}
    for g in sorted(groups):
        for sub in (os.path.join(HERE, g, "assets"), os.path.join(HERE, g)):
            try:
                if os.path.isdir(sub) and not os.listdir(sub):
                    os.rmdir(sub)
            except OSError:
                pass            # 空目录清理失败不影响回滚结果，留着即可

    stamped = MAP.replace(".json", f".rolledback-{datetime.now():%Y%m%d-%H%M%S}.json")
    if os.path.exists(stamped):
        os.replace(MAP, stamped)
    else:
        os.rename(MAP, stamped)
    print(f"已回滚 {moved_back} 个文件，分组方式={m.get('by')}")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", choices=["month", "date"], default="month")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不改动任何文件")
    ap.add_argument("--rollback", action="store_true", help="按 .archive_map.json 还原")
    args = ap.parse_args()

    if args.rollback:
        return rollback()
    if os.path.isfile(MAP):
        print("检测到 .archive_map.json（已归档过）。如需重新分组请先 --rollback。")
        return 1

    moves, index_new, warnings = build_plan(args.by)
    groups = sorted({dst.split("/", 1)[0] for _, dst in moves})
    print(f"分组方式: {args.by}  |  目标目录: {', '.join(groups) or '(无)'}")
    print(f"待移动文件: {len(moves)} 个  |  索引记录: {len(index_new)} 篇")

    by_kind = {}
    for src, dst in moves:
        kind = "配图" if src.startswith("assets/") else "文章"
        by_kind[kind] = by_kind.get(kind, 0) + 1
    print("构成:", ", ".join(f"{k} {v}" for k, v in by_kind.items()) or "无")

    for g in groups:
        names = [os.path.basename(d) for _, d in moves if d.startswith(g + "/") and "/assets/" not in d]
        print(f"  {g}/  ({len(names)} 个文章文件)  {names[0] if names else ''}{' ...' if len(names) > 1 else ''}")
    for w in warnings:
        print("  ! " + w)

    changed = sum(
        1 for old, new in zip(load_index(), index_new)
        if any(old.get(k) != new.get(k) for k in ("html", "md", "cover"))
    )
    print(f"index.json 待更新路径: {changed} 篇")

    if args.dry_run:
        print("\n[dry-run] 未改动任何文件。")
        return 0

    backup = apply(moves, index_new, args.by)
    print(f"\n归档完成：{len(moves)} 个文件已移动，index.json 已更新。")
    print(f"备份: {os.path.relpath(backup, HERE)}  |  映射: {os.path.basename(MAP)}")
    print("下一步: python3 build_gallery.py   （重建画廊链接）")
    print("回滚:   python3 archive_articles.py --rollback")
    return 0


if __name__ == "__main__":
    sys.exit(main())
