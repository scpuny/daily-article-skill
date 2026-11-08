#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_skill.py — 重新生成「内容运营台」脱敏 SKILL 分发包。

功能：
  1. 从真实工程（默认 ARTICLES_SRC 或 --src）重建脱敏后的 assets/app/：
       - src/   全量复制（排除 __pycache__ / *.pyc）
       - data/  仅复制白名单文件（config.json / themes.json / index.json /
                secrets.json.example / .gitignore），剔除真实 secrets.json、
                state/、content/、*.jsonl、*.log
       - 全局把原品牌名替换为中性「内容运营台」
  2. 保留手写文档（SKILL.md / README.md / references/ / assets/screenshots/）。
  3. 打包整个 skill 目录为 dist/wechat-article-studio.zip
     （排除 .git / __pycache__ / *.pyc / dist / .DS_Store）。
  4. 打包后做隐私兜底扫描：若发现真实 appId / __biz 值 / 真实 token /
     32 位 appSecret / 原品牌名残留，直接报错退出，绝不产出泄露包。

用法：
  python3 scripts/build_skill.py                 # 用默认源重建并打包
  python3 scripts/build_skill.py --src /path/to/articles
  python3 scripts/build_skill.py --out /tmp/was.zip
  python3 scripts/build_skill.py --no-zip        # 只重建 assets/app，不打 zip

依赖：仅 Python 标准库。
"""
import argparse
import base64
import glob
import json
import os
import re
import shutil
import sys
import zipfile

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DST = os.path.join(SKILL_ROOT, "assets", "app")
DIST_DIR = os.path.join(SKILL_ROOT, "dist")

def _default_src() -> str:
    """探测源工程，不硬编码任何本机绝对路径。

    优先级：ARTICLES_SRC 环境变量 → 当前目录及其上级的 articles/
    → ~/WorkBuddy/<工作区>/articles。找不到返回空串，由 main 提示显式指定 --src。
    """
    env = os.environ.get("ARTICLES_SRC")
    if env:
        return os.path.abspath(env)
    cwd = os.getcwd()
    candidates = [
        os.path.join(cwd, "articles"),
        os.path.join(os.path.dirname(cwd), "articles"),
    ]
    candidates += sorted(glob.glob(os.path.join(
        os.path.expanduser("~"), "WorkBuddy", "*", "articles")))
    for c in candidates:
        if os.path.isdir(os.path.join(c, "src")) and os.path.isdir(os.path.join(c, "data")):
            return os.path.abspath(c)
    return ""

# 原品牌名（base64 运行时解码，避免明文出现在分发包中）
_ORIGINAL_AI = base64.b64decode("5piT5pm656CBQUk=").decode("utf-8")
_ORIGINAL = base64.b64decode("5piT5pm656CB").decode("utf-8")
# 脱敏：原品牌名 -> 中性名
BRAND_MAP = [(_ORIGINAL_AI, "内容运营台"), (_ORIGINAL, "内容运营台")]

# data/ 白名单（其余一律不打包）
DATA_WHITELIST = {
    "config.json",
    "themes.json",
    "index.json",
    "secrets.json.example",
    ".gitignore",
}

# 隐私兜底：命中即视为泄露（value 特征，非字段名）
FORBIDDEN_PATTERNS = [
    re.compile(r"wx[0-9a-f]{14,18}"),          # 真实 appId
    re.compile(r"__biz=[A-Za-z0-9_]+"),         # 真实 __biz 值
    re.compile(re.escape(_ORIGINAL)),             # 原品牌名残留
    re.compile(r"[0-9a-f]{32}"),                # 32 位 appSecret / token
    re.compile(r"access_token[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{20,}"),
]
FORBIDDEN_FILES = {"secrets.json"}  # 真实凭据文件名，绝不打包

# 文本扩展名（其余按二进制拷贝）
TEXT_EXT = {
    ".py", ".js", ".html", ".css", ".json", ".md", ".txt", ".yml",
    ".yaml", ".toml", ".cfg", ".ini", ".example",
}


def _is_text(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        return True
    # 无扩展名文件（如 .gitignore 已带点，secret.example 已覆盖）
    return ext == ""


def _desensitize_text(text: str) -> str:
    for old, new in BRAND_MAP:
        text = text.replace(old, new)
    return text


def _copy_src(src_root: str) -> None:
    src_dir = os.path.join(src_root, "src")
    if not os.path.isdir(src_dir):
        sys.exit(f"[ERROR] 找不到源 src 目录：{src_dir}")
    dst_dir = os.path.join(APP_DST, "src")
    shutil.rmtree(dst_dir, ignore_errors=True)
    copied = 0
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for f in files:
            if f.endswith(".pyc") or f == ".DS_Store":
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, src_dir)
            out = os.path.join(dst_dir, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            if _is_text(full):
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    data = fh.read()
                data = _desensitize_text(data)
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(data)
            else:
                shutil.copy2(full, out)
            ext = os.path.splitext(f)[1].lower()
            if ext == ".py":
                os.chmod(out, 0o755)
            copied += 1
    print(f"  ✓ src/ 复制 {copied} 个文件（已脱敏）")


def _copy_data(src_root: str) -> None:
    src_data = os.path.join(src_root, "data")
    dst_data = os.path.join(APP_DST, "data")
    os.makedirs(dst_data, exist_ok=True)

    # index.json 始终为空数组：真实文章索引属于业务数据，绝不随包分发
    with open(os.path.join(dst_data, "index.json"), "w", encoding="utf-8") as fh:
        fh.write("[]\n")
    print("  ✓ data/index.json（空数组，真实文章索引不随包分发）")

    have_src = os.path.isdir(src_data)
    for name in DATA_WHITELIST - {"index.json"}:
        full = os.path.join(src_data, name) if have_src else None
        if not full or not os.path.exists(full):
            continue
        out = os.path.join(dst_data, name)
        if _is_text(full):
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read()
            data = _desensitize_text(data)
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(data)
        else:
            shutil.copy2(full, out)
        print(f"  ✓ data/{name}")
    if not have_src:
        print("  ! 源 data/ 不存在，仅生成空 index.json")


def _copy_scripts(src_root: str) -> None:
    """把源工程的维护脚本一并带入（若存在）。"""
    src_scripts = os.path.join(src_root, "scripts")
    dst_scripts = os.path.join(APP_DST, "scripts")
    if os.path.isdir(src_scripts):
        os.makedirs(dst_scripts, exist_ok=True)
        for f in os.listdir(src_scripts):
            if not f.endswith(".py") or f == ".DS_Store":
                continue
            shutil.copy2(os.path.join(src_scripts, f), os.path.join(dst_scripts, f))
        print(f"  ✓ scripts/ 同步")


def _privacy_scan() -> None:
    hits = []
    # 业务数据关卡：index.json 必须是空数组，真实文章索引不允许随包分发
    idx = os.path.join(APP_DST, "data", "index.json")
    if os.path.exists(idx):
        try:
            with open(idx, "r", encoding="utf-8") as fh:
                val = json.load(fh)
            if not (isinstance(val, list) and len(val) == 0):
                hits.append(f"{idx}: index.json 含真实文章数据（应为空数组）")
        except Exception:
            hits.append(f"{idx}: index.json 解析失败")
    for root, dirs, files in os.walk(APP_DST):
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for f in files:
            full = os.path.join(root, f)
            if f in FORBIDDEN_FILES or f == "secrets.json":
                hits.append(f"真实凭据文件: {full}")
                continue
            if not _is_text(full):
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    data = fh.read()
            except Exception:
                continue
            for pat in FORBIDDEN_PATTERNS:
                m = pat.search(data)
                if m:
                    hits.append(f"{full}: 命中 {m.group(0)[:24]!r}")
                    break
    if hits:
        print("\n[隐私校验失败] 发现疑似真实凭据/品牌残留：")
        for h in hits:
            print("   -", h)
        sys.exit("\n打包已终止，未生成泄露包。请检查源工程或脱敏逻辑。")


def _zip_skill(out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    exclude_dirs = {".git", "__pycache__", "dist"}
    n = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(SKILL_ROOT):
            dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.startswith(".")]
            # 排除 dist 输出目录自身
            if os.path.abspath(root) == os.path.abspath(DIST_DIR):
                continue
            for f in files:
                if f.endswith((".pyc", ".DS_Store")):
                    continue
                full = os.path.join(root, f)
                rel = os.path.relpath(full, SKILL_ROOT)
                zf.write(full, rel)
                n += 1
    print(f"  ✓ 打包 {n} 个文件 -> {out_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="重建并打包脱敏 SKILL 分发包")
    ap.add_argument("--src", default=_default_src(),
                    help="真实工程根目录（含 src/ 与 data/）；也可用 ARTICLES_SRC 环境变量")
    ap.add_argument("--out", default=None, help="输出 zip 路径（默认 dist/wechat-article-studio.zip）")
    ap.add_argument("--no-zip", action="store_true", help="只重建 assets/app，不打 zip")
    args = ap.parse_args()

    if not args.src:
        sys.exit("[ERROR] 未找到源工程，请显式指定：--src /path/to/articles "
                 "（或设置 ARTICLES_SRC 环境变量）")
    src_root = os.path.abspath(args.src)
    if not os.path.isdir(src_root) or not os.path.isdir(os.path.join(src_root, "src")):
        sys.exit(f"[ERROR] 源工程不存在或缺少 src/：{src_root}")

    print(f"[1/4] 重建 assets/app/（源：{src_root}）")
    os.makedirs(APP_DST, exist_ok=True)
    _copy_src(src_root)
    _copy_data(src_root)
    _copy_scripts(src_root)

    print("[2/4] 隐私兜底扫描…")
    _privacy_scan()
    print("  ✓ 未发现真实凭据/品牌残留")

    if args.no_zip:
        print("[3/4] --no-zip：跳过打包")
        print("[4/4] 完成（仅重建 assets/app/）")
        return

    out_path = args.out or os.path.join(DIST_DIR, "wechat-article-studio.zip")
    out_path = os.path.abspath(out_path)
    print(f"[3/4] 打包 zip…")
    _zip_skill(out_path)
    print("[4/4] 完成 ✅")
    print(f"\n产物：{out_path}")


if __name__ == "__main__":
    main()
