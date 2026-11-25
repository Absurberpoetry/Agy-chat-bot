from __future__ import annotations

import datetime as dt

from agy_bot.plugins import BotPlugin, PluginContext, command


class ExampleHelperPlugin(BotPlugin):
    id = "example_helper"
    name = "基础工具示例插件"
    version = "1.0.0"
    author = "Antigravity Bot"
    description = "提供 /ping 与 /time 快捷命令，可作为编写自定义外部插件的参考模板"
    default_config = {
        "ping_reply": "pong!",
        "time_format": "%Y-%m-%d %H:%M:%S",
    }
    config_schema = [
        {
            "key": "ping_reply",
            "label": "/ping 回复文本",
            "description": "群友发送 /ping 时机器人回复的固定前缀",
            "type": "string",
            "group": "基础回复设置",
            "placeholder": "pong!",
        },
        {
            "key": "time_format",
            "label": "时间显示格式",
            "description": "Python strftime 格式，如 %Y-%m-%d %H:%M:%S",
            "type": "string",
            "group": "基础回复设置",
            "placeholder": "%Y-%m-%d %H:%M:%S",
        },
    ]

    async def on_load(self) -> None:
        self.ctx.logger.info("示例插件 example_helper 已就绪")

    @command("ping", aliases=["测活"], description="测试机器人是否在线")
    async def cmd_ping(self, ctx: PluginContext, msg, args: str) -> str:
        base = str(self.config.get("ping_reply", "pong!"))
        return f"{base} {args}".strip()

    @command("time", aliases=["时间"], description="查看服务器当前时间")
    async def cmd_time(self, ctx: PluginContext, msg, args: str) -> str:
        fmt = str(self.config.get("time_format", "%Y-%m-%d %H:%M:%S"))
        return dt.datetime.now().strftime(fmt)
