# 内容运营台 · 微信公众号本地运营控制台

> 一套**本地优先（local-first）**的微信公众号内容运营工具：文章管理、本地预览、一键发布草稿、扫码登录、后台数据导入、写作策略闭环、主题定制。纯 Python 标准库实现，**零第三方依赖**，开箱即用。

---

## 一、这个项目是做什么的

它是一个跑在你自己机器上的 Web 控制台，用来打理一个微信公众号的内容生产链路：

- **文章管理 & 预览**：左侧文章列表、右侧实时预览（公众号版式）、随时切换主题。
- **一键发布草稿**：把文章整理成公众号草稿并提交到草稿箱（不是直接群发，群发仍需在公众号后台确认）。
- **扫码登录**：用公众号管理员/运营者的微信扫码登录，自动保存登录态，免去手动复制 token。
- **后台数据导入**：导入公众号后台导出的 `total_*.xls`，把阅读/分享/收藏等指标汇入本地看板。
- **写作策略闭环**：从导入的阅读反馈里**自动**归纳标题句式、开头钩子、栏目配比等规律，纯本地计算、不调用任何模型 API。
- **主题定制**：内置多套配色主题，支持自定义 22 个设计 token。
- **出网 IP 展示**：在登录弹窗里直接显示服务器当前出口公网 IPv4，方便你把该 IP 加入公众号**草稿推送 IP 白名单**。

> 整个服务仅监听 `127.0.0.1`，数据全部落在本地，不上传任何第三方。

---

## 二、功能特性一览

| 模块 | 说明 |
|------|------|
| 运营大屏 `/` | 累计指标、近 14 日产出、栏目矩阵、文章矩阵、策略面板 |
| 文章管理 `/manage` | 左列表 + 右预览 + 主题切换 + 一键发布草稿 + 登录态弹窗 |
| 系统设置 `/settings` | 选题/读者/篇幅/配图/发布开关/主题/凭据 可视化配置 |
| 扫码登录 | 走公众号后台自有扫码链路，无需企业认证、无需 `access_token` 交换 |
| 数据导入 | `total_*.xls` 解析后 UPSERT 进本地缓存，不覆盖既有数据 |
| 策略闭环 | 反馈 → 规则，纯标准库确定性计算，可体检/自愈台账一致性 |
| 主题系统 | 内置 + 自定义，22 个 token 可调 |
| MCP 服务 | 可选 stdio MCP，把同样的能力暴露给 Agent |

---

## 三、运行页面截图

> 以下截图均使用**通用示例数据**生成，不含任何真实账号信息。

### 运营大屏（首页）
![运营大屏](assets/screenshots/dashboard.png)

### 文章管理（预览 + 发布）
![文章管理](assets/screenshots/manage.png)

### 系统设置
![系统设置](assets/screenshots/settings.png)

### 配置登录态弹窗（扫码 + 出网 IP）
![登录态弹窗](assets/screenshots/login.png)

---

## 四、系统架构

```
wechat-article-studio/            # 解压后的工作目录
├── src/
│   ├── app.py                    # 入口：--mode web | mcp，默认端口 8731
│   ├── paths.py                  # 可移植路径层（data/ 与 state/ 定位）
│   ├── web/
│   │   ├── admin_server.py       # 多线程 HTTP 服务，分发所有 /api/*
│   │   ├── admin_ui.html         # 运营大屏
│   │   ├── admin_manage.html     # 文章管理
│   │   └── admin_settings.html   # 系统设置
│   ├── core/
│   │   ├── mp_session.py         # 扫码登录 / token 持久化 / xls 下载
│   │   ├── publish_wechat.py     # 文章 HTML → 公众号草稿
│   │   ├── stats_provider.py     # import_csv / snapshot / local_metrics
│   │   ├── strategy_engine.py    # 反馈→规则，data_health / repair_data
│   │   ├── account.py            # 账号身份解耦层（appId / __biz）
│   │   ├── theme_manager.py      # 主题 token
│   │   └── build_gallery.py      # 静态画廊导出
│   └── mcp/server.py             # 可选 stdio MCP
├── data/                        # 配置与内容（见下方「配置」）
└── state/                       # 运行态（自动生成，已被 .gitignore 排除）
```

**关键解耦点**：账号身份（`login_appid`、`__biz`）完全由配置/数据驱动，不写死在任何代码里——
换一个公众号，只改 `data/secrets.json` 和 `data/config.json` 即可。

---

## 五、安装与运行

### 环境要求
- Python **3.10+**（仅用标准库，无需 `pip install` 任何包）。

### 步骤
```bash
# 1) 把包内的 app 目录复制到你的工作目录（建议独立目录，保持包本身干净）
cp -R <skill>/assets/app ~/wechat-studio
cd ~/wechat-studio

# 2) 准备凭据（从公众号后台「设置 → 公众号开发 → 基本配置」获取 appId / appSecret）
cp data/secrets.json.example data/secrets.json
#   然后编辑 data/secrets.json，填入你自己的 appId 与 appSecret

# 3) 启动服务（显式清除代理环境变量，确保本地与公众号请求都不被代理干扰）
env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY python3 src/app.py --mode web --port 8731

# 4) 浏览器打开
#    http://127.0.0.1:8731/manage     文章管理（日常入口）
#    http://127.0.0.1:8731/           运营大屏
#    http://127.0.0.1:8731/settings   系统设置
```

服务启动后 `state/` 会自动生成（登录态、缓存等），它已被 `.gitignore` 排除，**不要**纳入版本库或随包分发。

---

## 六、配置

### `data/secrets.json`（**不会**随包提供，需自行创建）
```json
{
  "appId": "wxYOUR_APPID",
  "appSecret": "YOUR_APPSECRET"
}
```
- `appId`：登录扫码时作为 `login_appid` 使用，也用于身份展示。
- `appSecret`：仅在使用公众号**数据分析接口（datacube）**时需要；扫码登录本身不需要它。

### `data/config.json`（已随包提供，可改）
| 字段 | 含义 |
|------|------|
| `topics` | 选题方向（数组） |
| `audience` | 目标读者 |
| `author` | 署名（发布到草稿时的作者名） |
| `wordRange` | 字数下限/上限 `[1200, 2500]` |
| `themeName` | 默认主题 |
| `autoPublish` / `publishTarget` | 发布开关与目标（如 `wechat`） |

---

## 七、扫码登录与 IP 白名单

公众号草稿推送受 **IP 白名单**限制。白名单认的是**后端 Python 向 mp.weixin.qq.com 出网**的 IP，
不是你浏览器的 IP——所以必须在登录弹窗里查看「**当前出网 IP**」并把它加入公众号后台的白名单。

1. 打开文章管理页 → 右上角「配置登录态」→ 弹窗。
2. 点「**扫码登录**」生成二维码 → 用公众号管理员/运营者微信扫码并确认。
3. 弹窗内「当前出网 IP（草稿白名单用）」即需加白的 IP；换网络/重启后点「刷新」重取。
4. 若遇短信/管理员二次验证，或多微信绑定多个公众号需浏览器选账号，展开「粘贴 cURL」兜底：
   登录后台 → F12 → Network → 任选一条 mp.weixin.qq.com 请求 → 右键 **Copy as cURL (bash)**
   → 粘进文本框 → 保存。

> 登录态落在 `state/mp_session.json`（权限 600），30 天内可复用；接口不回传 Cookie 明文。

---

## 八、数据导入

在公众号后台「数据分析 → 图文数据 → 导出」拿到 `total_*.xls`，到文章管理页导入：

- 解析阅读/分享/收藏/在看等指标，UPSERT 进 `state/stats_cache.json`（按日期+标题去重，**不覆盖**既有行）。
- 首次导入含「内容 url」的表格后，系统会自动识别数据归属 `__biz`，并在账号卡片展示。
- 跨账号导入只会**告警**、不会改写原账号绑定。

---

## 九、写作策略闭环

`src/core/strategy_engine.py` 从导入的阅读反馈里归纳写作规律：

- 输入：`strategy_log.jsonl`（文章特征）、`feedback.jsonl`（指标）、`external_signals.jsonl`（外部选题信号）。
- 输出：`writing_strategy.json` —— 标题句式、开头钩子、栏目配比、约束与置信度。
- 纯本地确定性计算，**不调用任何大模型 API**，可在运营大屏与文章管理页查看。
- 附带 `data_health()` / `repair_data()`：台账错位、数值分叉、定格溯源等一致性体检与自愈。

---

## 十、主题定制

`/settings` →「文章主题」可切换内置主题，或基于 22 个设计 token 自定义并保存。
主题定义集中在 `data/themes.json`。

---

## 十一、隐私与分发声明（重要）

本仓库/包**刻意不包含**任何真实隐私信息：

- ❌ 不含 `appId` / `appSecret` / 登录 token / Cookie（`secrets.json`、`state/` 均被 `.gitignore` 排除）。
- ❌ 不含真实文章内容与真实阅读数据（`data/content/`、`state/stats_cache.json` 等运行态不进包）。
- ❌ 不含真实账号标识（`__biz`、真实微信号）。
- ✅ 包内品牌名统一泛化为「**内容运营台**」，你可按需改为自己的品牌。
- ✅ `secrets.json` 仅以 `.example` 模板形式提供，提醒使用者自行填写。

**因此可以直接把本包分享给他人安装，不会泄露你的公众号隐私。**

---

## 十二、常见问题（FAQ）

**Q：扫码后电脑没反应？**
A：多半是登录态弹窗把提示 toast 挡住了（已修复，toast 层级提到遮罩之上）；若仍失败，展开「诊断信息」看轮询原始响应，或用「粘贴 cURL」兜底。

**Q：点「登录态自检」没反应？**
A：它只做自检、弹一条短暂提示（不下载文件）。确认提示是否一闪而过；本质是验证登录态能否拉到数据。

**Q：为什么大屏首屏是空的？**
A：运营大屏的指标来自数据导入（`total_*.xls`）。首次未导入时本地指标仍会展示（文章数、连续创作天数等），远程指标为空属正常。

**Q：需要公网/域名吗？**
A：不需要。服务仅监听 `127.0.0.1`，纯本地使用。

**Q：能多账号吗？**
A：账号身份由 `secrets.json` + `config.json` 驱动，单实例单账号；换账号改这两个文件即可。跨账号导入会告警但不串号。

---

## 十三、从 GitHub 获取与重新打包

### 方式一：作为 WorkBuddy Skill 安装（推荐）
把本仓库克隆 / 下载到 WorkBuddy 的 skills 目录即可被识别为 Skill：
```bash
# 用户级（所有项目可用）
git clone git@github.com:scpuny/daily-article-skill.git ~/.workbuddy/skills/wechat-article-studio
# 或：下载 Release / zip 解压到上述目录
```
重启 WorkBuddy 后，在对话中即可调用「内容运营台」Skill。

### 方式二：仅取运行所需的 app 目录
不需要 Skill 机制时，只需 `assets/app/`：
```bash
cp -R wechat-article-studio/assets/app ~/wechat-studio
cd ~/wechat-studio
# 然后按「五、安装与运行」启动服务
```

### 重新打包（维护者）
仓库自带一键打包脚本，从真实工程重建脱敏 `assets/app/` 并产出可分发的 zip：
```bash
cd wechat-article-studio
python3 scripts/build_skill.py                              # 重建 + 打包到 dist/
python3 scripts/build_skill.py --src /path/to/your/articles # 指定源工程
python3 scripts/build_skill.py --no-zip                     # 仅重建 assets/app
```
脚本会：① 复制源工程 `src/` 与 `data/` 白名单；② 把原品牌名替换为中性「内容运营台」；③ 剔除真实凭据 / 登录态 / 业务数据；④ 打包后做**隐私兜底扫描**，一旦检测到真实 `appId` / `__biz` / `token` / 原品牌名残留立即报错、绝不产出泄露包。

> 注意：分发前请确认你本地填入的 `data/secrets.json`（真实 `appId`/`appSecret`）**未被读入**——脚本只读取白名单文件，真实凭据文件始终被排除。

---

## 许可

本工具按「自用 / 内部分发」提供，凭据与账号数据由使用者自行负责。
