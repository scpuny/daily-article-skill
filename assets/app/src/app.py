#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""内容运营台 · 唯一入口（方案 B：统一入口，不合并进程）。

    python3 src/app.py --mode web    [--port 8731] [--host 127.0.0.1]
    python3 src/app.py --mode mcp    [--home articles]     # stdio，由 MCP 客户端拉起
    python3 src/app.py --mode cli lint --check-latest
    python3 src/app.py --mode cli publish --html ... --title "..."
    python3 src/app.py --mode cli gallery

解释器：本文件已 chmod +x，可直接 ./src/app.py 执行（shebang 走系统 python3）。
但真 .xls 导入依赖 xlrd，系统 python3 通常没有；发布链路建议统一用装了
xlrd / requests 的 venv python3。PY 变量必须与命令在同一个 shell 里定义：
    PY=/path/to/venv/bin/python3
    $PY src/app.py --mode web --port 8731
最稳妥是写全解释器绝对路径 —— 变量未展开时 shell 会把脚本当可执行文件，
在没 +x 的情况下直接报 permission denied。

设计约束：
  1. 不改任何现有脚本 —— 用 runpy 以 __main__ 执行原文件，原有 argparse /
     __main__ 逻辑完全保留，旧路径命令仍然有效（CLI 回退链路零风险）。
  2. --mode mcp 时本文件绝不向 stdout 写任何东西，stdin/stdout 是 MCP 协议
     通道，只有目标脚本能写协议消息；所有诊断信息一律走 stderr。
  3. MCP 必须由客户端直接拉起，不提供 --mode all：stdio 经父进程代理会
     破坏协议，且会把管理台的生命周期绑到客户端会话上。

全局参数必须写在最前面；遇到第一个非全局参数，其后全部原样透传给目标脚本。
"""
import os
import runpy
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# CLI 子命令 → core 下的脚本；也支持直接写 xxx.py
CLI = {
    "lint": "article_lint.py",
    "publish": "publish_wechat.py",
    "gallery": "build_gallery.py",
    "archive": "archive_articles.py",
    "runs": "run_log.py",
    "reconcile": "draft_reconcile.py",
}

GLOBAL = {
    "--mode": "mode", "--home": "home", "--data-dir": "data_dir",
    "--state-dir": "state_dir", "--host": "host", "--port": "port",
}

ENV = {
    "home": "YIZHIMA_HOME",
    "data_dir": "YIZHIMA_DATA_DIR",
    "state_dir": "YIZHIMA_STATE_DIR",
}

HELP = """内容运营台 · 统一入口

  --mode web      文章管理台 HTTP 服务（默认）
  --mode mcp      MCP stdio 服务，由 WorkBuddy 连接器拉起
  --mode cli     透传执行 core 脚本

全局参数（须写在最前）：--home / --data-dir / --state-dir / --host / --port

CLI 子命令：lint, publish, gallery, archive, runs, reconcile
           也可直接写脚本名，如 article_lint.py --all --no-write

示例（推荐写全解释器路径，不要依赖上一条命令里定义的变量）：
  /path/to/venv/bin/python3 src/app.py --mode web --port 8731
  /path/to/venv/bin/python3 src/app.py --mode mcp --home articles
  /path/to/venv/bin/python3 src/app.py --mode cli lint --check-latest
  /path/to/venv/bin/python3 src/app.py --mode cli gallery

脚本可直接执行（+x）；系统 python3 缺 xlrd 时真 .xls 导入会提示安装。
"""


def parse(argv):
    """解析前段全局参数，遇到第一个非全局参数起全部原样透传。"""
    opts, rest, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a in ("-h", "--help"):
            opts["help"] = True
            i += 1
            continue
        key = GLOBAL.get(a)
        if key:
            if i + 1 >= len(argv):
                raise SystemExit(f"参数 {a} 缺少值")
            opts[key] = argv[i + 1]
            i += 2
            continue
        if a.startswith("--") and "=" in a:
            flag, val = a.split("=", 1)
            key = GLOBAL.get(flag)
            if key:
                opts[key] = val
                i += 1
                continue
        rest = argv[i:]
        break
    return opts, rest


def execute(target, child_argv):
    if not os.path.isfile(target):
        print(f"入口不存在: {target}", file=sys.stderr, flush=True)
        raise SystemExit(2)
    sys.argv = [target] + list(child_argv)
    runpy.run_path(target, run_name="__main__")


def main():
    opts, rest = parse(sys.argv[1:])
    if opts.get("help"):
        sys.stdout.write(HELP)
        return 0

    mode = opts.get("mode", "web")
    for key, env in ENV.items():
        if opts.get(key):
            os.environ[env] = os.path.abspath(opts[key])

    import paths
    paths.bootstrap()

    if mode == "mcp":
        # stdout 保持纯净：本分支不 print 任何东西
        passthru = []
        for flag, key in (("--home", "home"), ("--data-dir", "data_dir"),
                          ("--state-dir", "state_dir")):
            if opts.get(key):
                passthru += [flag, opts[key]]
        passthru += rest
        execute(os.path.join(paths.mcp_dir(), "server.py"), passthru)

    elif mode == "web":
        passthru = []
        for flag, key in (("--host", "host"), ("--port", "port"),
                          ("--data-dir", "data_dir")):
            if opts.get(key):
                passthru += [flag, str(opts[key])]
        passthru += rest
        execute(os.path.join(paths.web_dir(), "admin_server.py"), passthru)

    elif mode == "cli":
        if not rest:
            print("缺少 CLI 子命令", file=sys.stderr)
            print(HELP, file=sys.stderr)
            return 2
        name = rest[0]
        fname = CLI.get(name)
        if not fname and name.endswith(".py"):
            fname = name
        if not fname:
            print(f"未知 CLI 子命令: {name}", file=sys.stderr)
            print(HELP, file=sys.stderr)
            return 2
        execute(os.path.join(paths.core_dir(), fname), rest[1:])

    else:
        print(f"未知 --mode: {mode}（可选 web / mcp / cli）", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
