# AGY CLI BOT

基于官方原生 Antigravity CLI（`agy`）的轻量多平台聊天机器人。

它直接调用官方 `agy` CLI 的 `stream-json` 协议，无需中间桥接层，可将 Antigravity 接入 QQ（NapCat / LLBot OneBot v11）、Telegram 及自定义 WebSocket 客户端，并自带可视化 WebUI 控制台，支持 Windows 部署，linux部署尚待验证。

## 主要功能

- 原生接入官方 `agy` CLI，支持 Skill、MCP 与本地权限隔离
- 支持 QQ（OneBot v11）、Telegram 与通用 WebSocket 多平台同时接入
- SQLite 持久化聊天记录、上下文缓存复用与自动压缩
- 支持后台子代理并发执行耗时工具任务，不阻塞正常对话
- 内置 WebUI 控制台（人设工坊、模型配置、实时日志与内置 CLI 终端）
- 支持轻量 Python 聊天网关插件，详见 [plugins/README.md](plugins/README.md)

## 快速开始

详细安装、登录与接入教程见 [docs/使用说明.md](docs/使用说明.md)。

Windows（双击即可）：

```cmd
install.bat
start.bat
```

需要后台持续运行可使用 `start_background.bat`，停止使用 `stop.bat`。

Linux / Ubuntu：

```bash
bash scripts/linux/install.sh
bash scripts/linux/start.sh
```

首次运行启动脚本时会提示设置 WebUI 登录账号和密码。启动后访问 `http://127.0.0.1:8618`，在 WebUI 中完成登录与平台配置即可使用。
