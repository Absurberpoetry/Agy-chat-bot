"""示例：本地 Python 脚本触发器（在 WebUI「全局配置 -> 触发 -> 脚本触发」中引用 scripts/example_trigger.py）。

当群里或私聊收到消息时，机器人会调用 check(ctx)（支持同步或 async def check(ctx)）。
ctx 包含字段：
  - text: 消息纯文本
  - user_id: 发送者 QQ
  - sender_name: 发送者昵称
  - channel_id: 群号或私聊 QQ
  - scene: "group" 或 "private"
  - is_admin: 是否管理员
  - length: 消息字数
  - at_bot / has_at_all / is_quoted_bot: 布尔标记
  - hour / minute / weekday / recent_count

返回值约定：
  - False / None: 不触发
  - True: 触发 AI 正常回复
  - dict:
      {"trigger": True, "reply": "直接发送的固定文本（不消耗 AI）"}
      {"trigger": True, "prompt": "给 AI 追加的本轮指令", "reason": "日志标注原因"}
"""

from __future__ import annotations

from typing import Any


def check(ctx: dict[str, Any]) -> bool | dict[str, Any]:
    text = str(ctx.get("text", "")).strip()
    # 示例：如果群友发送了超过 120 字的长文且包含“总结”，自动让 AI 帮忙提炼要点
    if len(text) >= 120 and ("总结" in text or "tl;dr" in text.lower()):
        return {
            "trigger": True,
            "prompt": "请用 3 条以内简短要点帮群友总结上面这段长消息的核心意思。",
            "reason": "长文自动总结脚本",
        }
    return False
