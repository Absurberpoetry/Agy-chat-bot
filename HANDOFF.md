# 本地接手开发交接清单（Cloud -> Local Antigravity Client）

本项目已从云端环境完成 MVP 架构搭建与离线单元测试，现交由**本地 Windows（后续迁移 Ubuntu）Antigravity 客户端（App / IDE / CLI）**继续联调与扩展。

---

## 1. 当前已完成状态

- **核心网关代码**（位于 `src/agy_bot/`）：
  - 多平台正向 WebSocket 并发接入与断线退避重连：`adapters/registry.py`、`adapters/onebot_v11.py`、`adapters/generic_ws.py`
  - 5 种触发器（严格区分 `@机器人` 与 `引用机器人消息`）：`triggers/engine.py`、`models.py`
  - 触发后防抖追发收集器（跟踪触发用户连发、旁观者不重置但保留在上下文窗口）：`collector/debouncer.py`、`collector/context_store.py`
  - 官方 Antigravity CLI 调度器（`--input-format stream-json --output-format stream-json`，全局并发锁 `Semaphore(1)`，429/403 熔断，结构化语境裁判 `--json-schema`）：`runner/agy_stream.py`
  - 主应用编排与 CLI 入口：`app.py`、`run.py`
- **隔离工作区与安全基线**：
  - `bot_workspace/GEMINI.md`：群聊回复风格与防提示词注入规则
  - `settings.antigravity-cli.example.json`：CLI 权限隔离示例
  - `bot_workspace/.agents/mcp_config.example.json`：后续接入第三方生图/脚本 MCP 的模板
- **自动化验证**：`python -m unittest discover -s tests -v`（14 项单元测试与 Mock 端到端链路全部通过）。

---

## 2. 本地待办联调清单（按顺序执行）

1. **安装本机环境依赖**：
   - 确认本地已安装并登录官方 `agy` CLI（可运行 `agy models` 查看可用模型）。
   - 创建虚拟环境并安装 `pip install -r requirements.txt`（主要是 `websockets>=12.0`）。
2. **配置 CLI 权限护栏**：
   - 参考 `settings.antigravity-cli.example.json` 配置 `~/.gemini/antigravity-cli/settings.json`（Windows 为 `%USERPROFILE%\.gemini\antigravity-cli\settings.json`）。
3. **填写真实配置并先跑离线模式**：
   - 复制 `config.example.json` -> `config.json`（已在 `.gitignore` 中排除），先只放 1 个测试群进 `group_allowlist`。
   - 先执行 `python run.py --dry-run`，验证：
     - NapCat 正向 WS 握手与 `get_login_info` 获取 `self_id`
     - 纯 `@机器人` vs 仅引用机器人消息（触发类型需严格区分 `mention` 与 `quote`）
     - 触发后连发 2~3 条消息的防抖追发与上下文截取
4. **切到真实 `agy` 模型联调**：
   - 执行 `python run.py`，观察真实 `agy stream-json` 首轮启动、第二轮暖进程复用、多群排队信号量、以及实际延迟/回复格式是否需要微调。
5. **下一步功能扩展（联调稳定后再做）**：
   - 按需求把生图或脚本工具封装成标准本地 MCP Server，接入 `bot_workspace/.agents/mcp_config.json` 并配好 `mcp(server/tool)` 权限白名单。
   - 按需新增其他平台（如 Discord / Telegram / 飞书等）的正向 WS 适配器。
