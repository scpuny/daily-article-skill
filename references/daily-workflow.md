# 每日创作编排 · 通用模板（给定时任务 / Agent 读）

> 这份是**通用执行清单**，不含任何账号、品牌与真实路径。
> 把它复制成一份 Skill，定时任务就能照着跑：
>
> ```bash
> # 方式一：放到工作目录（项目级）
> mkdir -p <APP_HOME>/.workbuddy/skills/daily-article
> cp references/daily-workflow.md <APP_HOME>/.workbuddy/skills/daily-article/SKILL.md
>
> # 方式二：放到用户级 skills（所有项目可用）
> mkdir -p ~/.workbuddy/skills/daily-article
> cp references/daily-workflow.md ~/.workbuddy/skills/daily-article/SKILL.md
> ```
>
> 占位符：
> - `APP_HOME` = 你放 `assets/app` 的工作目录（内含 `src/`、`data/`、`state/`）
> - `PY` = `python3`；若要导入真 `.xls` 或发布草稿，换成装了 `requests`、`xlrd` 的解释器
> - 所有命令都在 `APP_HOME` 下执行，`--mode cli` 必须写在子命令**前面**

---

## 0. 职责边界（先看清，别找错工具）

本工具**不写文章、不联网检索、不画图**——这些由 Agent 自身能力完成。
`src/` 只提供确定性能力：策略计算、渲染、质量闸门、建公众号草稿、画廊、统计、运行台账。

所谓「定时任务」就是**到点把下面第 2 节跑一遍的 Agent**，不是常驻进程，也不是包自带的后台服务。

## 1. 每次运行前检查

| 检查项 | 不通过时 |
|---|---|
| `data/secrets.json` 已填 `appId` / `appSecret` | 跳过「建草稿」，其余步骤照跑并如实汇报 |
| `data/config.json` → `autoPublish` | `true` = 写完自动建草稿；`false` = 只生成文章，由人在管理台点「一键发布到公众号」 |
| 公众号登录态有效 | 草稿接口会失败，按第 4 节写失败台账 |
| 凭据只从 `data/secrets.json` 读 | **绝不**写进文章、日志、汇报或命令行回显 |

## 2. 执行顺序（不可跳步）

1. **取写作策略（首步）**
   ```bash
   $PY src/app.py --mode cli strategy_engine.py get
   ```
   内部自动吸收 `~/Downloads` 里新增的公众号导出文件，返回 `version`、`confidence`、`constraints[]`、句式权重。
   - `constraints[]` 必须**逐条落实**到本轮选题、标题句式、开头钩子、收尾。
   - `confidence=cold`（样本 < 8 篇）按返回的冷启动基线执行，**不臆造数据、不假装已有样本**。
   - 记下 `version`，第 9 步要用。
   - 只想算策略、不想扫下载目录：追加 `--no-ingest`。

2. **读配置与主题**：`data/config.json`、`data/themes.json`（active tokens）、`data/series_state.json`。
   四个内容支柱轮转，选已写篇数最少的一支，且不与上一篇同句式。

3. **外部信号（可选但推荐）**：用 Agent 的检索能力看近 30 天同领域高互动文章 3–5 篇，
   只提炼标题句式 / 开头钩子 / 选题角度，**不照搬内容、不编造互动数**，然后登记：
   ```bash
   $PY src/app.py --mode cli strategy_engine.py signals --json '[{"date":"2026-11-08","titlePattern":"坑点清单+可运行解法","hookType":"线上故障现场","engagementLevel":1.0,"source":"web"}]'
   ```

4. **写作 + 配图**：由 Agent 完成。正文 1200–2500 字，标题 ≤32 字，含 ≥1 个可运行代码示例；
   代码块写 `<pre><code class="language-xxx">`；封面图必须有，但**不放正文最前面**。
   网页版 `:root` 用主题 tokens 对应的 `--wb-*` 变量，图片用 `assets/xxx.png` 相对路径。

5. **归档 + 登记台账**
   ```
   data/content/<YYYY-MM>/<YYYY-MM-DD>-<slug>.html
   data/content/<YYYY-MM>/<YYYY-MM-DD>-<slug>.md
   data/content/<YYYY-MM>/assets/<slug>-*.png
   ```
   把新记录**插入 `data/index.json` 列表最前面**（按日期倒序，id=0 为最新；管理台、画廊、统计都依赖这个顺序）：
   ```json
   {"date":"2026-11-08","title":"…","subtitle":"…","summary":"…","tags":[],
    "html":"2026-11/2026-11-08-xxx.html","md":"2026-11/2026-11-08-xxx.md",
    "cover":"2026-11/assets/xxx-cover.png","publish":null,"quality":null}
   ```
   `html` / `md` / `cover` 路径都必须带 `<YYYY-MM>/` 前缀；只写实际存在的文件，缺失字段留 `null`。
   已存在的图片直接复用、不覆盖；不改动历史文章。

6. **重建画廊**
   ```bash
   $PY src/app.py --mode cli gallery
   ```

7. **质量闸门（发布前必跑）**
   ```bash
   $PY src/app.py --mode cli lint --check-latest
   ```
   检查标题/简介长度、字数区间、封面、路径断链、`--wb-*` 变量、代码块语言标注、反 AI 套话、撞题相似度等。
   - `errors` 非空 → 修订后重检（≤2 轮）；仍不通过则**不建草稿**，写一条失败台账并如实汇报。
   - 只想看结果不改文件：追加 `--no-write`。

8. **建公众号草稿**（仅当 `autoPublish=true` 且 `publishTarget="wechat"`）
   ```bash
   # 先本地预览排版（不联网，可选）
   $PY src/app.py --mode cli publish --html "<APP_HOME>/data/content/<YYYY-MM>/xxx.html" \
     --config data/config.json --dry-run --out /tmp/preview.html

   # 正式建草稿
   $PY src/app.py --mode cli publish --html "<APP_HOME>/data/content/<YYYY-MM>/xxx.html" \
     --config data/config.json --title "<≤32字完整标题>"
   ```
   - `--html` 写**绝对路径**，脚本以 HTML 所在目录为基准解析 `assets/`。
   - 只调 `draft/add` 建草稿，**不群发**；原创声明接口不支持，需后台群发时手动勾选。
   - 成功把 `{target, media_id, publishedAt}` 写回 `index.json` 对应记录的 `publish` 字段。
   - 发布器自动读 active 主题，不要传硬编码颜色。

9. **写运行台账（成功与失败都要写）**
   ```bash
   $PY src/app.py --mode cli runs --write '{"ts":"2026-11-08 09:12:00","title":"…","pillar":"A","lint":{"ok":true,"errors":0,"warnings":1,"score":88},"media_id":"…","durationSec":180,"error":null}'
   $PY src/app.py --mode cli runs --list        # 回看「昨天为什么没发」
   ```
   台账落到 `state/runs/<YYYY-MM-DD>.json`，同日追加。

10. **登记本轮特征（末步，lint 失败也要记并加 `lintFailed:true`）**
    ```bash
    $PY src/app.py --mode cli strategy_engine.py features --json '{"articleId":0,"date":"2026-11-08","title":"…","pillar":"A","titlePattern":"坑点清单+可运行解法","hookType":"线上故障现场","wordCount":1800,"codeBlocks":3,"strategyVersion":"<第1步的 version>","hypothesis":"故障现场开头应提升分享率"}'
    ```
    `strategyVersion` 取第 1 步返回值，`hypothesis` 写一句可验证的假设——下一轮策略是否变好就靠它对齐。

11. **汇报**：本轮支柱、采用的 `version` 与 constraints 落实情况、文件路径、lint 结果、
    `media_id` / 失败原因、台账路径、下一篇建议；提醒原创需在后台手动勾选。

## 3. 命令速查（MCP 不可用时的等价回退）

| 动作 | 命令 |
|---|---|
| 取策略 | `--mode cli strategy_engine.py get` |
| 吸收下载目录导出 | `--mode cli strategy_engine.py ingest` |
| 登记外部信号 | `--mode cli strategy_engine.py signals --json '<JSON 数组>'` |
| 登记文章特征 | `--mode cli strategy_engine.py features --json '<单行 JSON>'` |
| 质检 | `--mode cli lint --check-latest` |
| 本地排版预览 | `--mode cli publish --html <路径> --config data/config.json --dry-run --out /tmp/p.html` |
| 建草稿 | `--mode cli publish --html <路径> --config data/config.json --title "<标题>"` |
| 画廊 | `--mode cli gallery` |
| 台账写 / 读 | `--mode cli runs --write '<JSON>'` / `--mode cli runs --list` |
| 草稿对账 | `--mode cli reconcile` |
| 冷启动回填 | `--mode cli strategy_engine.py backfill` |

> 前缀一律 `$PY src/app.py`。`--mode cli` 也支持直接写脚本名，例如
> `--mode cli article_lint.py --all --no-write`。

## 4. 可选：注册 MCP 走工具调用

不配也能跑（上面 CLI 已覆盖全流程）。若希望定时任务用 `wb_*` 工具，把下面条目**合并**
进 `~/.workbuddy/mcp.json` 的 `mcpServers`（不要覆盖已有条目），然后在连接器管理里信任并启用：

```json
{
  "mcpServers": {
    "wechat-studio": {
      "type": "stdio",
      "command": "<PY 解释器的绝对路径>",
      "args": ["<APP_HOME>/src/app.py", "--mode", "mcp", "--home", "<APP_HOME>"]
    }
  }
}
```

工具与 CLI 共用同一份 `src/core` 代码，产物一致。常用：`wb_get_strategy` / `wb_get_theme` /
`wb_lint_article` / `wb_publish_article` / `wb_write_run` / `wb_record_features`。

## 5. 硬约束

1. 不改动历史文章与已有配图；不覆盖任何已存在图片。
2. 封面必须有，但绝不放正文最前面；文中插图不凑数（0–2 张）。
3. **不群发、不自动勾原创**（接口不支持）；不伪造统计指标、不臆造句式效果。
4. `lint` 有 error 不建草稿。
5. 数据路径只走 `src/paths.py` 解析，不在文章或脚本里写死绝对路径。
6. `constraints[]` 逐条落实；无真实样本时按冷启动基线，不编造阅读数据。

## 6. 失败排查

| 现象 | 先看哪里 |
|---|---|
| 到点没跑 | 电脑是否休眠 / WorkBuddy 是否在运行 / 定时任务是否 ACTIVE |
| 文章生成了但没草稿 | `autoPublish` 是否为 `true`、`secrets.json` 是否填、登录态是否过期、出网 IP 是否在白名单 |
| 草稿接口报错 | 管理台「登录态自检」、出网 IP 是否变化（换网络后要刷新白名单） |
| lint 一直不过 | `state/lint_report.json` 的 `errors`；多为路径缺 `<YYYY-MM>/` 前缀或封面缺失 |
| 大屏指标为空 | 未导入后台导出文件；把 `total_*.xls` 放进 `~/Downloads` 或在管理台「导入数据」 |
