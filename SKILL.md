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

## Operator guide (when the user asks "how do I use this")

**Two modes — the scheduled one is OPTIONAL.** Walk the user through `README.md`
chapters 1 → 2 → 3 (manual) or 4 (scheduled); do not start with architecture.

- **Manual (default)**: the bundle is a local console, **not a background service**.
  Installing this skill creates no buttons and runs nothing automatically. Start the web
  server (Quick start step 3), then on `/manage`: select an article → preview →
  「一键发布到公众号」 (creates a **draft only**, never a mass-send; originality flag must
  be ticked manually in the MP backend). The user writes the article, or asks the agent
  to write one on demand — that is a one-off run, not a scheduled task.
- **Data backfill** (optional, `/manage`): **no publish-age gate** — any article with a
  known msgid can be fetched the day it is published. A `settled` flag (< 7 days) is only
  a tooltip hint that numbers may still grow; use「↻ 重拉」to refresh. The first successful
  fetch marks the article as collected, so the default batch only picks up never-fetched
  ones. Already-collected articles stay re-fetchable via card「↻ 重拉」or sidebar
  「重拉定格」; both overwrite the detail/profile fields and require confirmation.
  **Large archives are capped server-side**: max 10 articles per HTTP request, max 60
  per task plan (newest first, overflow resumed on the next click), one batch at a time
  (mutex), and ≥1s between articles. The UI splits into chunks of 5 with a live progress
  bar, a stop control and a resumable queue in sessionStorage.
  The ledger is **one row per article**, with two coexisting sources: `read`/`share`
  from the total-table export, interaction / channel / profile fields from the
  per-article detail. The detail's own read count is kept as `detailRead` and never
  overwrites `read`; each row also records `detailFrom` + `detailFetchedAt`. The
  dashboard shows both source timestamps and the read-count drift count. A mismatch
  between the two counts is normal (different export times), not a bug.
  See README chapter A-2 and §七.
- **Scheduled**: a WorkBuddy recurring automation (e.g.
  `FREQ=DAILY;BYHOUR=9;BYMINUTE=0`) with `cwds` = the app working directory and a prompt
  that loads a daily authoring skill. A generic, credential-free template ships at
  `references/daily-workflow.md`; copy it to
  `<APP_HOME>/.workbuddy/skills/daily-article/SKILL.md` or
  `~/.workbuddy/skills/daily-article/SKILL.md` **before** creating the automation.
  It needs the machine awake and WorkBuddy running at that time. Optional stdio MCP
  (`src/app.py --mode mcp`) is convenient but not required — the template has full CLI
  fallbacks. `autoPublish=false` → only generate the article, operator publishes manually.
  Pausing the automation returns to manual mode with no data loss; both share `data/`.

**Facts to state up front**: Python 3.10+; the base console is stdlib-only, but publishing
a draft needs `requests` and real `.xls` import needs `xlrd`; `data/secrets.json` must be
created from `secrets.json.example`; the draft API checks the **server's egress IP**
(shown in the login modal), not the browser IP. Never put real credentials into articles,
logs, reports, or command output.

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
- `data/secrets.json` — **not bundled**; `appId` + `appSecret` only. Required for
  server-side draft APIs; QR login does not need `appSecret`. The legacy MP
  statistics API is no longer used; reach data comes from web-console exports.
- `data/themes.json` — theme definitions (safe to ship).

## Known pitfall: code-block spacing in drafts

WeChat's editor trims whitespace-only `<span>`s, which used to turn `SELECT id` into
`SELECTid` and `FROM orders` into `FROMorders`. `src/core/publish_wechat.py` now merges
whitespace into adjacent colored tokens and emits `&nbsp;` — never output a bare-space
span, and never pre-insert entities before `html.escape` (that produces `&amp;nbsp;`).

If a user reports lost spaces: re-publish from `/manage` (the endpoint deletes the old
draft and creates a new one); an already-broadcast article must be re-sent from the new
draft. Verify any article with `python3 scripts/check_code_spacing.py <article.html>`
(exit 1 = spacing regression).

## Privacy & distribution notes

- The package contains **zero** real credentials, tokens, Cookies, `__biz`, article
  content, or author identity. Brand strings were generalized to "内容运营台".
- `data/secrets.json`, `state/`, and all runtime JSON/JSONL caches are excluded and
  gitignored. A fresh install starts empty; the operator supplies their own account.
- To re-package after code changes: `python3 scripts/build_skill.py --src /path/to/articles`
  — regenerates the sanitized `assets/app/` and `dist/wechat-article-studio.zip`, and
  aborts if a privacy scan finds real credentials or article data.
