# Antigravity 多平台正向 WebSocket 群聊机器人

基于**官方未修改 Antigravity CLI（`agy --input-format stream-json --output-format stream-json`）**的多平台正向 WebSocket 对话机器人，面向 Windows 与 Ubuntu 跨平台部署设计。

## 特性亮点
1. **100% 官方原生工具与客户端身份**：请求由官方 `agy` 二进制直接发出；机器人人设与防注入规则写在隔离工作区 [bot_workspace/GEMINI.md](bot_workspace/GEMINI.md)，后续可扩展至 `bot_workspace/.agents/mcp_config.json`。
2. **多平台正向 WebSocket 并发接入**：通过 `platforms` 数组可同时连入多个 NapCat（OneBot v11）实例或其他自定义 `generic_ws` 平台，断线指数退避重连，统一按 `(adapter_id, scene, channel_id)` 隔离会话。
3. **5 种触发机制（严格区分 `@` 与 `引用`）**：
   - `1. mention`（消息被 @）：精确识别显式 `@机器人`，**排除 QQ 引用机器人消息时自动附加的那 1 个 `@机器人`**。
   - `2. quote`（消息被引用）：单独识别“引用/回复机器人消息”（通过本地缓存 + OneBot `get_msg` 查询原发送者）。
   - `3. keyword`（关键词 / 正则触发）。
   - `4. dice`（随机投骰子触发，带最小长度、指令前缀过滤与群冷却）。
   - `5. context_llm`（模型根据上下文语境选择是否插话，使用独立 `agy -p --output-format json --json-schema` 结构化判断，不污染主群聊记忆，带未读条数与最小时间间隔双重限流）。
4. **触发后延迟追发收集（防抖窗口）**：
   - 命中 `wait_triggers`（默认 `mention` / `quote` / `keyword`）后，开始跟踪触发用户在群内的后续连发。
   - 每次该用户再发消息就重置静默等待计时器（`quiet_wait_seconds`），直到他停下超过设定时间（或达到 `max_extra_messages` / `max_wait_seconds` 上限），再截取**截至当前时间之前的群聊上下文**一次性送入 `agy` 思考回复（期间其他群友的插话同样会进入上下文缓存，但不会重置触发用户的静默计时）。
5. **群聊并发限制与防封熔断**：
   - 全局 `max_concurrent_requests = 1` 信号量串行调度 + 排队上限；
   - 用户级冷却 CD + 单群每分钟回复配额；
   - 检测到 `429` 限流或 `403/ToS` 报错自动开启熔断冷却，避免循环轰炸。

---

## 快速开始（Windows / Ubuntu 通用）

### 1. 前置准备
1. 安装并登录官方 **Antigravity CLI (`agy`)**：
   - 终端运行一次 `agy` 完成登录（如果要在活跃公开群使用，建议在登录时选择 **Google Cloud Project → Pay As You Go** 按量计费，避免消费级订阅风控）。
   - 运行 `agy models` 确认你账号可用的模型 slug。
2. 配置安全权限，防止群友提示词注入操控本机：
   - 参考 [settings.antigravity-cli.example.json](settings.antigravity-cli.example.json)，在以下路径配置拒绝高危终端命令与越权读写规则：
     - Windows: `%USERPROFILE%\.gemini\antigravity-cli\settings.json`
     - Ubuntu: `~/.gemini/antigravity-cli/settings.json`
   - > 注意：`~/.gemini/antigravity-cli/settings.json` 对当前系统用户的所有 `agy` 会话生效；如果这台电脑同时还要自己用 `agy` 交互式写代码，可以把 `command(*)` 放进 `"ask"`（交互时人工点确认，headless 群聊下自动拒批）或只在专用机器人账号/用户下写死 `"deny"`。
3. 在 NapCat WebUI 中新增一个 **WebSocket 服务器（正向 WS）**，例如监听 `ws://127.0.0.1:3001`，记下 `Token`。

### 2. 安装 Python 依赖（建议 Python 3.11+）
```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
# Ubuntu: source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 配置并启动
```bash
cp config.example.json config.json
```
编辑 `config.json`：
- 填写 `platforms[0].ws_url`、`access_token` 和 `group_allowlist`（**强烈建议先填 1 个测试群**）。
- 可选：`self_id` 留空时会在连接 NapCat 后通过 `get_login_info` 自动获取。
- `agy.model` 填 `agy models` 里存在的模型名（留空则用 CLI 默认模型）。

#### 先用 `--dry-run` 离线验证流程（不消耗任何额度）
```bash
python run.py --dry-run
```
在测试群里依次测试：纯 `@机器人`、仅引用机器人消息（看日志 `trigger_type=quote` 而非 `mention`）、关键词、连发 3 条等几秒后回复，确认无误后再正式启动：

```bash
python run.py
```

---

## 下一步接入第三方插件（生图 / 脚本）
对话与限流稳定后，把生图接口或固定脚本封装为本地 MCP Server，填入 [bot_workspace/.agents/mcp_config.example.json](bot_workspace/.agents/mcp_config.example.json) 并重命名为 `mcp_config.json`，同时在 `settings.json` 的 `permissions.allow` 中精确放行对应 `mcp(server/tool)` 白名单即可，依旧保持 `command(*)` 禁用。
