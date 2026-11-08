---
name: wechat-article-studio
description: "This skill bundles a self-hosted WeChat Official Account content operations console. Use it when a user wants to set up, run, or operate a local tool for managing WeChat articles, including article preview, one-click draft publishing, QR-code login, MP backend data import, a writing-strategy feedback loop, and theme customization, or when packaging or deploying such a tool. The full application ships under assets/app and runs on Python 3.10 plus standard library only with no third-party dependencies."
agent_created: true
---

# WeChat Article Studio（微信公众号内容运营台）

## Overview

A **local-first** web console for operating a WeChat Official Account content pipeline.
It is built around three pages (`/`, `/manage`, `/settings`) plus a backend that talks to
the WeChat MP backend (mp.weixin.qq.com) through the operator's own browser session.

The skill packages the entire application under `assets/app/`. On install, copy that
folder to a working directory, drop in credentials, and run it. **No credentials or real
account data are bundled** — `secrets.json`, `state/`, and all runtime caches are excluded
and gitignored by design.

## When to use

- Set up or run a self-hosted WeChat article management / publishing console.
- Explain how the QR-code login, IP-whitelist display, data import, or strategy loop works.
- Package, redeploy, or hand off this tool to another operator.
- Troubleshoot the local server (`python3 src/app.py --mode web --port 8731`).

## Quick start (for the agent)

1. Copy the bundle to a clean working dir:
   `cp -R assets/app ~/wechat-studio && cd ~/wechat-studio`
2. Create `data/secrets.json` from the template:
   `cp data/secrets.json.example data/secrets.json`, then fill `appId` / `appSecret`
   from the MP admin console (设置 → 公众号开发 → 基本配置).
3. Launch (the server disables proxies so local + MP calls work):
   `env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY python3 src/app.py --mode web --port 8731`
4. Open `http://127.0.0.1:8731/manage`. Bind login via **扫码登录** in the login modal,
   import `total_*.xls` from the MP backend to populate metrics, then publish drafts.

The server binds `127.0.0.1` only — it is not exposed to the network.

## Core capabilities

1. **Article management & preview** (`/manage`): left list + right live preview +
   theme switcher; one-click "发布到公众号" creates an MP draft (not a direct publish).
2. **QR-code login** (login modal): uses the MP backend's own scan-login flow — no
   enterprise certification, no `access_token` exchange; token + Cookie persist to
   `state/mp_session.json` (chmod 600). A `cURL` fallback covers multi-account / SMS
   second-factor cases.
3. **Egress IP display**: the modal shows the server's **outbound public IPv4** so the
   operator can whitelist it in the MP admin (草稿推送受 IP 白名单限制).
4. **Data import** (`total_*.xls`): parses MP reach/engagement metrics; merges into
   `state/stats_cache.json` without overwriting existing rows (UPSERT by date+title).
5. **Writing-strategy feedback loop** (`src/core/strategy_engine.py`): derives title /
   hook / pillar rules from imported feedback; pure stdlib, deterministic, **no model API
   calls**. Surfaced on `/` and `/manage`.
6. **Theme customization** (`src/core/theme_manager.py`): built-in + custom themes with
   22 design tokens, editable in `/settings`.
7. **MCP server** (`src/mcp/server.py`): optional stdlib MCP (stdio) exposing the same
   operations to an agent.

## Architecture (see references/architecture.md for detail)

```
assets/app/
├── src/app.py            # entrypoint: --mode web | mcp ; port 8731
├── src/paths.py         # portable path resolution (data/ + state/)
├── src/web/admin_server.py   # ThreadingHTTPServer, all /api/* endpoints
├── src/web/admin_*.html       # dashboard / manage / settings UIs
├── src/core/
│   ├── mp_session.py    # scan-login, token persistence, xls download
│   ├── publish_wechat.py# draft creation from article HTML
│   ├── stats_provider.py# import_csv / snapshot / local_metrics
│   ├── strategy_engine.py# feedback → rules, data_health / repair
│   ├── account.py       # appId / __biz identity (decoupled, config-driven)
│   ├── theme_manager.py # theme tokens
│   └── build_gallery.py # static gallery export
└── data/                # config.json, themes.json, index.json([]), secrets.json.example
```

Key decoupling point: account identity (`login_appid`, `__biz`) is **config/data driven**,
never hardcoded — swap accounts by editing `data/secrets.json` + `data/config.json`.

## Configuration

- `data/config.json` — topics, audience, wordRange, author, themeName, publishTarget,
  autoPublish. `author` is the byline shown on published drafts.
- `data/secrets.json` — **not bundled**; `appId` + `appSecret` only. Required for the
  MP datacube API; login itself does not need `appSecret`.
- `data/themes.json` — theme definitions (safe to ship).

## Privacy & distribution notes

- The package contains **zero** real credentials, tokens, Cookies, `__biz`, article
  content, or author identity. Brand strings were generalized to "内容运营台".
- `data/secrets.json`, `state/`, and all runtime JSON/JSONL caches are excluded and
  gitignored. A fresh install starts empty; the operator supplies their own account.
- To re-package after changes: `python3 <skill-creator>/scripts/package_skill.py <this-dir>`.
