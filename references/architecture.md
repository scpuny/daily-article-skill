# 架构与数据流（WeChat Article Studio）

本地优先（local-first）的微信公众号内容运营台。后端为纯标准库 `http.server` 多线程服务，
前端为内联 CSS/JS 的单页静态文件，全部数据落在本地 `data/` 与 `state/`。

## 启动与路由

- 入口 `src/app.py`：`--mode web` 起 HTTP 服务（默认 `127.0.0.1:8731`）；`--mode mcp` 起 stdio MCP。
- `src/paths.py`：可移植路径层，定位 `data/`（配置/文章/主题）与 `state/`（运行态/登录态/缓存）。
- `src/web/admin_server.py`：`ThreadingHTTPServer`，所有 `/api/*` 在此分发（`do_GET` / `do_POST`）。

## 三大页面

| 路由 | 文件 | 作用 |
|------|------|------|
| `/` | `admin_ui.html` | 内容运营大屏：累计指标、近 14 日产出、栏目矩阵、文章矩阵、策略面板 |
| `/manage` | `admin_manage.html` | 文章管理：左列表 + 右预览 + 主题切换 + 一键发布草稿 + 登录态弹窗 |
| `/settings` | `admin_settings.html` | 系统设置：选题/读者/篇幅/配图/发布开关/主题/凭据 |

## 核心模块（`src/core/`）

- `mp_session.py`：扫码登录（scan-login 三通道轮询 → 写盘 `state/mp_session.json`，chmod 600）、
  明细/总表 xls 下载。**登录态与具体账号解耦**：`login_appid` 读 `secrets.json` 的 `appId`。
- `publish_wechat.py`：把文章 HTML 整理成草稿并提交到公众号草稿箱（`media_id`），非直接群发。
- `stats_provider.py`：`import_csv()` 解析 `total_*.xls` 指标并 UPSERT 进 `state/stats_cache.json`；
  `snapshot()` 供大屏首屏；`local_metrics()` 从 `index.json` 计算本地指标。
- `strategy_engine.py`：写作策略闭环——从 `strategy_log.jsonl` / `feedback.jsonl` / `external_signals.jsonl`
  派生标题句式/钩子/栏目约束；`data_health()` + `repair_data()` 做台账一致性体检与自愈。纯标准库、确定性、**不调用任何模型 API**。
- `account.py`：账号身份解耦层——从文章链接提取 `__biz`、合并 `appId`，落 `state/account.json`；
  跨账号导入只告警、不覆盖原绑定。`/api/account` 返回脱敏快照。
- `theme_manager.py`：主题 token 体系（22 个），内置 + 自定义，`/settings` 可改。
- `build_gallery.py`：导出静态文章画廊 HTML。

## 公众号代码块空白策略（易踩坑）

微信正文编辑器保存草稿时会 trim 掉「独立的空白 `<span>`」。早期版本把词间空格包成
`<span> </span>`，发布后 `SELECT id` 变成 `SELECTid`、`FROM orders` 变成 `FROMorders`。
`publish_wechat.py` 现按三条规则渲染：

1. `highlight_code()` 把空白攒进 `pending`，并入相邻彩色 token，**绝不产生纯空白 span**；
2. `_esc_code()` 先 HTML 转义再插入实体，空格 / tab 一律输出 `&nbsp;`，不会被折叠，
   也不会被二次转义成字面文本 `&amp;nbsp;`；
3. 代码行 `<div>` 带 `white-space:pre-wrap;word-break:break-all` 作双保险，长行仍可断行。

正文 `inline_html()` 只把段首 / 段尾（紧贴标签边界）的空格转 `&nbsp;`，段中空格保持普通，
避免整段变成不可换行的一长串。

回归防线：`python3 scripts/check_code_spacing.py <文章.html>`（可再传一个参数指定已生成的
公众号版 HTML）。它把源 `<pre>` 与渲染产物逐行还原成纯文本比对，并静态检查 `&amp;nbsp;`
与纯空白 span；任何一行缺空格即 exit 1。改渲染器后建议对 `data/content/` 全量跑一遍。

## 关键数据流

1. **登录**：扫码 → `mp_session` 写 `state/mp_session.json` → 后续下载/发布复用同一份 token+Cookie。
2. **导入**：MP 后台下载 `total_*.xls` → `import_csv` → UPSERT `state/stats_cache.json`。
3. **发布**：`/manage` 选文章 → `publish_wechat` 生成草稿 → 回写 `index.json` 的 `media_id`。
4. **策略**：导入的反馈 → `strategy_engine` 现算/落盘 `writing_strategy.json` → 大屏/管理页展示。

## 隐私边界

- 凭据 `data/secrets.json`、登录态 `state/mp_session.json`、`__biz`、真实文章 `data/content/`、
  真实反馈/策略台账 —— **均不进包**，且被 `.gitignore` 排除。
- 包内品牌名统一泛化为「内容运营台」；`secrets.json` 仅以 `.example` 模板形式提供。
- 服务仅绑定 `127.0.0.1`，不对外暴露。
