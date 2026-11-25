# AgyChat Bot 聊天网关插件开发指南（Python）

> **定位分工说明**
> - **让模型主动调用外部工具**（如画图、联网接口、数据库查询、脚本工具）→ 请直接使用 **`agy` 官方原生 Skill / MCP**（在 WebUI 的 **Skill & MCP** 页面管理，100% 遵循官方格式，可直接接入社区现成生态）。
> - **聊天网关外层逻辑**（如 0 Token 快捷命令、消息拦截过滤、进群/定时推送、模型调用前后上下文加工）→ 使用本目录（默认 `./plugins`）下的 **Python 插件**。

将任意 `.py` 文件（或包含 `main.py` / `plugin.py` 的文件夹）放入本目录，在 WebUI 的 **插件** 页面点击「重载」或在群内发送 `/plugins reload` 即可热加载。

## 最小插件模板

```python
from __future__ import annotations
from agychat_bot.plugins import (
    BotPlugin,
    PluginContext,
    PluginReply,
    command,
    on_message,
    on_before_llm,
    on_after_llm,
)

class MyPlugin(BotPlugin):
    id = "my_plugin"
    name = "我的插件"
    version = "1.0.0"
    author = "your_name"
    description = "一句话说明插件用途"
    default_config = {
        "keyword": "早安",
        "reply": "早安呀！",
    }

    async def on_load(self) -> None:
        self.ctx.logger.info("插件加载完成")

    async def on_unload(self) -> None:
        pass

    async def on_config_update(self, new_config: dict) -> None:
        # WebUI 修改插件配置时实时触发
        pass

    # 1. 注册聊天命令（前缀由 全局配置 -> 触发 -> 命令前缀 决定，默认 /）
    @command("hello", aliases=["hi"], description="打招呼", admin_only=False)
    async def cmd_hello(self, ctx: PluginContext, msg, args: str) -> str:
        return f"你好 {msg.sender_name}！参数: {args}"

    # 2. 拦截或旁听所有消息（priority 越小越先执行）
    @on_message(priority=50)
    async def watch_msg(self, ctx: PluginContext, msg):
        kw = self.config.get("keyword", "")
        if kw and msg.text.strip() == kw:
            return PluginReply(text=self.config.get("reply", "早安"), stop=True)
        return None  # 返回 None 继续走后续触发器与大模型

    # 3. 模型调用前钩子（可注入额外上下文/知识库检索结果）
    @on_before_llm(priority=50)
    async def before_llm(self, ctx: PluginContext, snapshot, prompt: str) -> str:
        return prompt

    # 4. 模型回复后钩子（可清洗/格式化/追加后缀）
    @on_after_llm(priority=50)
    async def after_llm(self, ctx: PluginContext, snapshot, reply_text: str) -> str:
        return reply_text
```

## `PluginContext` 常用 API

- `ctx.config`: 当前插件的实时配置字典（已与 `default_config` 深度合并，可在 WebUI 插件页在线修改）
- `ctx.bot_config`: 机器人完整 `BotConfig`
- `ctx.logger`: 专属日志器，输出会实时同步到 WebUI 日志流
- `ctx.data_dir`: 插件专属持久化数据目录（`data/plugins/<plugin_id>/`）
- `await ctx.send_reply(msg, text, quote=True, at=False)`: 主动发送消息
- `await ctx.ask_llm(session_key, prompt)`: 主动调用一次 Antigravity CLI 推理
- `ctx.get_recent_messages(session_key, limit=20)`: 获取当前群/会话最近上下文消息
