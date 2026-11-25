# AGY BOT

基于**官方原生 Antigravity CLI（`agy --input-format stream-json --output-format stream-json`）**的多平台正向 WebSocket 聊天机器人，无需中间桥接层（如 ACP），直接将官方 `agy` CLI 接入 QQ（NapCat / OneBot v11）及其他自定义 WebSocket 聊天客户端，面向 Windows 与 Linux 跨平台部署设计。

---

## 核心特性

### 1. 100% 原生 `agy` CLI 执行与工具生态
- **官方原生运行**：由 Python 主控进程直接管理官方 `agy` 子进程，账号鉴权、模型请求、客户端标识、工具调用全部走官方原生链路。
- **原生 Skill & MCP 生态**：
  - **Skill**：遵循官方 `bot_workspace/.agents/skills/<name>/SKILL.md` 格式，自动发现并渐进式加载，支持 `.md` / `.zip` 导入与可视化启停。
  - **MCP**：遵循官方 `mcpServers` 标准，自动同步到主工作区与子代理独立工作区，并自动放行隔离权限白名单；支持 Stdio 本地命令、Streamable HTTP 远程服务与在线 `initialize` 握手探测。
- **隔离权限护栏**：通过 `--settings ./settings.antigravity-cli.bot.json` 隔离机器人的工具权限，不污染宿主机全局 `~/.gemini/antigravity-cli/settings.json`。

### 2. SQLite 持久化记忆、前缀缓存与双触发压缩
- **全量落库不丢记忆**：群聊与私聊消息、模型回复统一持久化至 `data/chat_history.db`（WAL 模式），重启服务上下文无感续接。
- **暖进程池 + 增量前缀缓存**：常驻 LRU 暖进程池（`max_warm_processes`）保留活跃 CLI 会话，同一压缩周期内仅增量投喂新增消息，稳定命中模型 KV 前缀缓存并实现 0ms 启动响应。
- **按条数 / Token / 字节双触发压缩**：支持全局及**单模型独立配置**压缩上限与丢弃条数，超限时将早期对话归档移出当前窗口并干净重建新进程；内置只读 **`chat_history` MCP** 供模型随时回查被压缩移出的历史消息。

### 3. 独立后台并发子代理（不阻塞群聊）
- 将生图等耗时 MCP 任务派发给独立的一次性后台子代理并发执行，期间群聊照常对话，完成后自动交回主代理按人设润色并发送图片。
- **独立平级工作区 (`data/subagent_workspace/`)**：避免子代理误加载主聊完整人设导致冷启误拒与格式冲突，并支持在 WebUI 中为子代理自由选择 **纯净工具模式 / 人格工坊专属人格 / 跟随主代理**。

### 4. 多客户端接入、回复编排与防打扰
- **多客户端正向 WS**：支持同时接入多个 NapCat（OneBot v11）实例与通用 `generic_ws` 客户端，WebUI 内置实时「测试连接」握手与 `get_login_info` 探测弹窗。
- **触发与回复控制**：支持 `@提及`、引用回复、关键词/正则、随机插话、可视化定时主动触发，配合「连发消息合并等待」、拟人分段气泡发送、回复频率控制与 429/403 自动熔断保护。
- **防打扰机制**：未显式 `@机器人` 激活的新群默认静默；若在群内被禁言自动恢复静默直至解禁后再次被 `@`。

### 5. 现代化 WebUI 控制台（支持电脑端与手机端）
- 内置完整控制台（默认端口 `8618`）：工作台实时对话与日志流、命名模型配置、人设工坊、插件管理、Skill & MCP 管理，以及**网页内置 `agy` CLI 交互终端**（支持远程 OAuth 登录链接自动捕获、手机端粘滞 `Ctrl` 修饰键与移动端全面适配）。

---

## 扩展体系分工

本项目严格区分 **AI 能力扩展** 与 **聊天网关扩展**，各司其职、互不耦合：

| 扩展类型 | 格式标准 | 运行层 | 适用场景 |
|---|---|---|---|
| **Skill & MCP** | **100% `agy` 官方原生格式** | `agy` CLI 内核 | 让**模型主动判断并调用**的能力：画图、查库、联网接口、脚本工具、社区现成 Skill / MCP |
| **Python 插件 (`./plugins/`)** | 本项目轻量 Python SDK | Python 聊天网关层 | **0 Token 快捷命令**（`/签到` `/菜单`）、消息前置拦截、主动推送、`before_llm` / `after_llm` 上下文加工 |

Python 插件开发指南见 [plugins/README.md](plugins/README.md)。

---

## 快速开始（Windows / Linux）

详细新手图文步骤请参阅 [docs/使用说明.md](docs/使用说明.md)。

### 1. 一键安装与初始化
- **Windows（纯 CMD / 双击即可，无需 PowerShell）**：
  ```cmd
  install.bat
  ```
- **Linux / Ubuntu**：
  ```bash
  bash scripts/linux/install.sh
  ```

脚本会自动创建 `.venv`、安装 `requirements.txt`，并从 `templates/` 生成 `config.json` 与 `settings.antigravity-cli.bot.json`。

### 2. 启动与可视化配置（无需手改 JSON）
- **Windows**：
  - 前台启动：`start.bat`（离线干跑验证：`start.bat --dry-run`）
  - 后台持久保活（SSH 断开不掉线，统一日志 `bot.log`）：`start_background.bat`
  - 停止后台服务：`stop.bat`
- **Linux / Ubuntu**：
  - 前台启动：`bash scripts/linux/start.sh`
  - 后台运行：`bash scripts/linux/start.sh -d`（停止：`bash scripts/linux/start.sh stop`）

启动后访问 `http://127.0.0.1:8618`（默认账号 `admin` / 密码 `admin123`，公网部署请及时改强密码），在 WebUI 的 **全局配置 → 核心引擎 / 平台** 中填写代理、启用 QQ（NapCat / LLBot）或 Telegram Bot 并点 **「测试连接」→「保存并热重载」** 即可上线。

---

## 运行测试

```bash
python -m unittest discover -s tests -p "test_*.py"
```

---

## 目录结构

```text
├── install.bat / start.bat / start_background.bat / stop.bat  # Windows 一键脚本
├── run.py                                                     # 主程序启动入口
├── requirements.txt                                           # Python 依赖清单
├── docs/使用说明.md                                            # 新手部署与排障文档
├── templates/                                                 # 默认配置与权限模板
│   ├── config.example.json
│   └── settings.antigravity-cli.example.json
├── scripts/linux/                                             # Linux/Ubuntu 一键脚本
│   ├── install.sh
│   └── start.sh
├── bot_workspace/                                             # 主代理 agy 工作区（GEMINI.md / .agents/）
├── data/                                                      # 运行期数据（SQLite 库 / 人设库 / 子代理工作区，不提交仓库）
├── plugins/                                                   # 聊天网关 Python 插件目录（含 SDK 示例）
├── src/agy_bot/
│   ├── app.py                                                 # 主控调度（消息流、防抖、压缩、子代理）
│   ├── adapters/                                              # OneBot v11 (NapCat/LLBot)、Telegram 与通用 WS 适配器
│   ├── collector/                                             # SQLite 群聊上下文存储、双触发压缩与防抖
│   ├── runner/                                                # 官方 agy CLI stream-json 暖进程池与子代理调度
│   ├── plugins/                                               # Python 聊天网关插件 SDK 与加载器
│   ├── webui/                                                 # WebUI 后端 API、CLI PTY 终端与前端控制台
│   └── mcp_history_server.py                                  # 内置只读 SQLite 历史消息检索 MCP 服务
└── tests/                                                     # 单元与集成测试套件
```

---

## 安全与使用提示
- 开源/提交代码前请勿提交 `config.json`、`data/`、`settings.antigravity-cli.bot.json`、`.media_cache/` 及 `*.log`（已加入 `.gitignore`）。
- 群聊场景下若开启 `allow_command_execution`，请务必结合 **身份与权限** 做好群友工具隔离，并保持 `settings.antigravity-cli.bot.json` 中的红线拦截规则。
