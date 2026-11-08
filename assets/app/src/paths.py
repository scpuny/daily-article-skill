#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容运营台 · 唯一路径来源（可移植层）。

所有模块只通过本文件找数据，不再各自拼 `dirname(__file__)`。这样
`src/` 拷到任何机器都能跑，`data/` 换一份就是另一个公众号。

优先级：
  YIZHIMA_HOME          项目根（含 data/ 与 state/）
  YIZHIMA_DATA_DIR      单独覆盖数据目录
  YIZHIMA_STATE_DIR     单独覆盖运行态目录
  自动推断              src/paths.py 的上两级 = 项目根

兼容两种布局（迁移期与迁移后都可用）：
  新: <home>/data/{config.json,themes.json,index.json,series_state.json,secrets.json}
      <home>/data/content/<YYYY-MM>/{*.html,*.md,assets/}
      <home>/state/{stats_cache.json,.wx_token.json,index.html,.trash/,runs/}
      <home>/src/{core,web,mcp}/
  旧: <home>/ 直接平铺全部文件

index.json 里的相对路径统一走 resolve()：先 data/content/，再 data/，
再 home/，最后 state/。因此 16 篇历史记录一条都不用改。
"""
import json
import os

__all__ = [
    "home", "src_dir", "core_dir", "web_dir", "mcp_dir",
    "data_dir", "state_dir", "content_dir",
    "config", "themes", "index", "series", "secrets",
    "stats_cache", "token_cache", "gallery", "lint_report",
    "trash_dir", "archive_backup", "archive_map", "runs_dir", "web_file",
    "resolve", "read_json", "write_json", "load_config", "save_config",
    "bootstrap",
]

_AUTO_HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def home():
    """项目根（articles/）。"""
    return os.path.abspath(os.environ.get("YIZHIMA_HOME") or _AUTO_HOME)


def src_dir():
    return os.path.join(home(), "src")


def core_dir():
    return os.path.join(src_dir(), "core")


def web_dir():
    return os.path.join(src_dir(), "web")


def mcp_dir():
    return os.path.join(src_dir(), "mcp")


def data_dir():
    env = os.environ.get("YIZHIMA_DATA_DIR")
    if env:
        return os.path.abspath(env)
    p = os.path.join(home(), "data")
    return p if os.path.isdir(p) else home()


def state_dir():
    env = os.environ.get("YIZHIMA_STATE_DIR")
    if env:
        return os.path.abspath(env)
    p = os.path.join(home(), "state")
    return p if os.path.isdir(p) else data_dir()


def content_dir():
    p = os.path.join(data_dir(), "content")
    return p if os.path.isdir(p) else data_dir()


# ---------------------------------------------------------------- 具体文件
def config():
    return os.path.join(data_dir(), "config.json")


def themes():
    return os.path.join(data_dir(), "themes.json")


def index():
    return os.path.join(data_dir(), "index.json")


def series():
    return os.path.join(data_dir(), "series_state.json")


def secrets():
    return os.path.join(data_dir(), "secrets.json")


def stats_cache():
    return os.path.join(state_dir(), "stats_cache.json")


def token_cache():
    return os.path.join(state_dir(), ".wx_token.json")


def mp_session():
    """公众号后台登录态（token/Cookie/UA），含敏感凭证，state/ 已 gitignore。"""
    return os.path.join(state_dir(), "mp_session.json")


def detail_index():
    """msgid 台账 + 单篇明细 7 天定格状态。"""
    return os.path.join(state_dir(), "detail_index.json")


def gallery():
    return os.path.join(state_dir(), "index.html")


def lint_report():
    return os.path.join(state_dir(), "lint_report.json")


def trash_dir():
    return os.path.join(state_dir(), ".trash")


def archive_backup():
    return os.path.join(state_dir(), ".archive_backup")


def archive_map():
    return os.path.join(state_dir(), ".archive_map.json")


def runs_dir():
    return os.path.join(state_dir(), "runs")


def web_file(name):
    """管理台 HTML：新布局在 src/web/，旧布局在项目根。"""
    for d in (web_dir(), home()):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return os.path.join(web_dir(), name)


# ---------------------------------------------------------------- 路径解析
def resolve(rel, must_exist=False):
    """把 index.json 里的相对路径解析为磁盘绝对路径，带目录穿越防护。

    按 content → data → home → state 依次查找；都不存在时返回 content 下的
    候选路径（供写入或生成错误信息），must_exist=True 则返回 None。
    """
    if not rel:
        return None
    if os.path.isabs(rel):
        return rel if (os.path.exists(rel) or not must_exist) else None
    bases, seen = [], set()
    for b in (content_dir(), data_dir(), home(), state_dir()):
        rb = os.path.realpath(b)
        if rb not in seen:
            seen.add(rb)
            bases.append(b)
    for b in bases:
        cand = os.path.join(b, rel)
        real = os.path.realpath(cand)
        rbase = os.path.realpath(b)
        if real != rbase and not real.startswith(rbase + os.sep):
            continue                                  # 拒绝 ../ 穿越
        if os.path.exists(cand):
            return cand
    if must_exist:
        return None
    fallback = os.path.join(content_dir(), rel)
    rf, rb = os.path.realpath(fallback), os.path.realpath(content_dir())
    if rf != rb and not rf.startswith(rb + os.sep):
        return None                                   #  fallback 同样拒绝穿越
    return fallback


# ---------------------------------------------------------------- JSON 读写
def read_json(path, default=None):
    if not path or not os.path.exists(path):
        return {} if default is None else default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {} if default is None else default


def write_json(path, data):
    """原子写，自动建父目录。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 配置/密钥
_SECRET_KEYS = ("appId", "appSecret")
_ENV_KEYS = {"appId": "YIZHIMA_APP_ID", "appSecret": "YIZHIMA_APP_SECRET"}


def load_config(config_path=None):
    """读配置并按 secrets.json → 环境变量依次覆盖凭据。

    兼容旧布局：secrets.json 不存在时，config.json 里的 appId/appSecret 仍有效。
    """
    p = config_path or config()
    cfg = read_json(p, {}) or {}
    sec = read_json(os.path.join(os.path.dirname(os.path.abspath(p)), "secrets.json"), {}) or {}
    for k in _SECRET_KEYS:
        if sec.get(k):
            cfg[k] = sec[k]
    for k, env in _ENV_KEYS.items():
        if os.environ.get(env):
            cfg[k] = os.environ[env]
    return cfg


def save_config(cfg, config_path=None):
    """写配置：凭据单独落 secrets.json（可 gitignore），其余写 config.json。"""
    p = config_path or config()
    d = os.path.dirname(os.path.abspath(p))
    sec_path = os.path.join(d, "secrets.json")
    sec = read_json(sec_path, {}) or {}
    for k in _SECRET_KEYS:
        if k in cfg:
            sec[k] = cfg[k]
    if sec:
        write_json(sec_path, sec)
    write_json(p, {k: v for k, v in cfg.items() if k not in _SECRET_KEYS})
    return load_config(p)


# ---------------------------------------------------------------- 导入引导
def bootstrap():
    """把 src/ 与 core/ 放进 sys.path，返回 src 目录。

    供 MCP / 管理台等入口调用；core 模块自身用文件头部的极简引导。
    """
    import sys
    for d in (src_dir(), core_dir()):
        if os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
    return src_dir()
