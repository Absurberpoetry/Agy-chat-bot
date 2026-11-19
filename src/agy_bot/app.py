from __future__ import annotations

import datetime as dt
import logging

from .adapters import AdapterRegistry
from .collector import DebouncedReplyManager, GroupContextStore
from .config import BotConfig
from .models import ContextSnapshot, IncomingMessage, JudgeDecision
from .runner import BaseAgentRunner, build_runner
from .triggers import RateLimiter, TriggerEngine

logger = logging.getLogger(__name__)


def _format_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def format_context_prompt(snapshot: ContextSnapshot, bot_name: str) -> str:
    trigger_user = snapshot.trigger_message
    now_str = dt.datetime.fromtimestamp(snapshot.settled_at).strftime("%Y-%m-%d %H:%M:%S")
    trigger_labels = "+".join(snapshot.matched_triggers) if snapshot.matched_triggers else snapshot.primary_trigger

    header = [
        (
            f"[聊天场景] 平台={snapshot.platform} | 实例={snapshot.adapter_id} | "
            f"类型={'群聊' if snapshot.scene == 'group' else '私聊'} | 会话ID={snapshot.channel_id} | "
            f"触发方式={trigger_labels} ({snapshot.trigger_reason})"
        ),
        (
            f"[上下文说明] 主要触发用户={trigger_user.sender_name}({trigger_user.user_id}) | "
            f"追发补充条数={snapshot.extra_messages_collected} | 截止时间={now_str}。"
            f"请作为「{bot_name}」结合截至当前时间之前的群聊上下文直接给出自然、简洁的回复内容："
        ),
        "",
        "--- 最近消息上下文（按时间正序） ---",
    ]

    lines: list[str] = []
    for m in snapshot.messages:
        t_str = _format_ts(m.timestamp)
        speaker = f"{bot_name}(我)" if m.is_self else f"{m.sender_name}({m.user_id})"
        tags: list[str] = []
        if m.message_id and m.message_id in snapshot.trigger_message_ids:
            tags.append("触发消息")
        elif m.message_id and m.message_id in snapshot.followup_message_ids:
            tags.append("追发补充")
        tag_str = f" [{'/'.join(tags)}]" if tags else ""

        quote_part = ""
        if m.reply_to_msg_id:
            who = "我" if m.is_quoted_bot else (m.reply_to_user_id or "某人")
            snippet = (m.reply_to_text or "").replace("\n", " ")[:60]
            quote_part = f' [引用 {who}: "{snippet}"]' if snippet else f" [引用 {who}]"

        lines.append(f"[{t_str}] {speaker}{tag_str}{quote_part}: {m.text}")

    return "\n".join(header + lines)


def truncate_reply(text: str, max_chars: int) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max(1, max_chars - 3)].rstrip() + "..."


class BotApplication:
    """Wires forward-WS adapters, 5-mode trigger engine, trailing debouncer, and agy runner."""

    def __init__(
        self,
        config: BotConfig,
        *,
        registry: AdapterRegistry | None = None,
        runner: BaseAgentRunner | None = None,
        trigger_engine: TriggerEngine | None = None,
    ) -> None:
        self.config = config
        self.context_store = GroupContextStore(
            history_limit=config.collector.history_buffer_size,
        )
        self.registry = registry or AdapterRegistry()
        self.registry.build_adapters(
            config.platforms,
            message_lookup=self.context_store.lookup_message,
        )

        workspace_dir = config.resolve_path(config.agy.workspace_dir)
        data_dir = config.resolve_path(config.agy.data_dir)
        self.runner = runner or build_runner(
            config.agy,
            config.rate_limit,
            workspace_dir=workspace_dir,
            data_dir=data_dir,
        )
        self.rate_limiter = RateLimiter(config.rate_limit)
        self.triggers = trigger_engine or TriggerEngine(config.triggers)
        self.triggers.set_context_judge(self._judge_context)

        self.debouncer = DebouncedReplyManager(
            config.collector,
            self.context_store,
            self._on_snapshot_ready,
        )

    async def _judge_context(
        self,
        session_key: str,
        messages: list[IncomingMessage],
    ) -> JudgeDecision:
        return await self.runner.judge_context(session_key, messages)

    async def handle_message(self, msg: IncomingMessage) -> None:
        adapter = self.registry.get(msg.adapter_id)
        if adapter is not None and not adapter.is_channel_allowed(msg.scene, msg.channel_id, msg.user_id):
            return

        # Cache all messages (including self-posts if pushed by NapCat) for accurate reply/quote lookup.
        self.context_store.add_message(msg, count_ambient=False)
        if msg.is_self:
            return

        fast_trigger = self.triggers.evaluate_fast(msg)

        # Case 1: Active trailing collector is already tracking follow-up messages in this channel.
        if self.debouncer.has_pending(msg.session_key):
            if fast_trigger.triggered and fast_trigger.trigger_type in ("mention", "quote", "keyword"):
                self.debouncer.on_trigger(msg, fast_trigger)
            else:
                self.debouncer.handle_followup_message(msg, is_new_trigger=False)
            return

        # Case 2: No active collector; check triggers 1-4 first.
        if fast_trigger.triggered:
            allowed, rl_reason = self.rate_limiter.can_trigger(msg)
            if not allowed:
                logger.info("[%s] 用户 %s 触发 %s 被限流跳过 (%s)", msg.session_key, msg.user_id, fast_trigger.trigger_type, rl_reason)
                return

            circuit_open, rem = self.runner.is_circuit_open()
            if circuit_open:
                logger.warning("[%s] Antigravity 熔断冷却中 (%.0fs)，忽略触发", msg.session_key, rem)
                return

            self.rate_limiter.record_trigger(msg)
            self.context_store.reset_ambient_unread(msg.session_key)
            self.debouncer.on_trigger(msg, fast_trigger)
            return

        # Case 3: Ambient message; count towards Trigger #5 (context_llm) if enabled.
        unread = self.context_store.increment_ambient(msg.session_key)

        circuit_open, _ = self.runner.is_circuit_open()
        if circuit_open or not self.config.triggers.context_llm.enabled:
            return

        allowed, _ = self.rate_limiter.can_trigger(msg)
        if not allowed:
            return

        recent = self.context_store.get_recent(
            msg.session_key,
            limit=self.config.triggers.context_llm.judge_context_window,
        )
        ctx_trigger = await self.triggers.evaluate_context_llm(
            msg,
            unread_ambient_count=unread,
            recent_messages=recent,
        )
        if ctx_trigger.triggered:
            self.rate_limiter.record_trigger(msg)
            self.context_store.reset_ambient_unread(msg.session_key)
            self.debouncer.on_trigger(msg, ctx_trigger)

    async def _on_snapshot_ready(self, snapshot: ContextSnapshot) -> None:
        circuit_open, rem = self.runner.is_circuit_open()
        if circuit_open:
            logger.warning("[%s] 熔断冷却中 (%.0fs)，取消本次推理", snapshot.session_key, rem)
            return

        prompt = format_context_prompt(snapshot, self.config.bot_name)
        logger.info(
            "[%s] 开始调用 Antigravity (%s，上下文=%d 条，追发=%d 条)",
            snapshot.session_key,
            snapshot.primary_trigger,
            len(snapshot.messages),
            snapshot.extra_messages_collected,
        )
        result = await self.runner.ask(snapshot.session_key, prompt)
        if not result.ok:
            logger.error("[%s] Antigravity 推理失败: %s", snapshot.session_key, result.error)
            return

        if not result.response:
            logger.info("[%s] Antigravity 返回空内容，跳过发送", snapshot.session_key)
            return

        reply_text = truncate_reply(result.response, self.config.reply.max_reply_chars)
        adapter = self.registry.get(snapshot.adapter_id)
        if adapter is None:
            logger.error("[%s] 未找到适配器 %s，无法发送回复", snapshot.session_key, snapshot.adapter_id)
            return

        sent_id = await adapter.send_reply(
            snapshot.trigger_message,
            reply_text,
            quote_trigger=self.config.reply.quote_trigger_message,
            at_user=self.config.reply.at_trigger_user,
        )
        self.rate_limiter.record_reply_sent(snapshot.session_key)
        self.context_store.reset_ambient_unread(snapshot.session_key)

        self_id = getattr(adapter, "self_id", snapshot.trigger_message.self_id) or snapshot.trigger_message.self_id
        self_msg = IncomingMessage(
            adapter_id=snapshot.adapter_id,
            platform=snapshot.platform,
            self_id=self_id,
            scene=snapshot.scene,
            channel_id=snapshot.channel_id,
            message_id=sent_id or "",
            user_id=self_id,
            sender_name=self.config.bot_name,
            text=reply_text,
            timestamp=snapshot.settled_at,
            is_self=True,
        )
        self.context_store.add_message(self_msg, count_ambient=False)

    async def run(self) -> None:
        try:
            await self.registry.run_all(self.handle_message)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        await self.debouncer.shutdown()
        await self.registry.stop_all()
        await self.runner.shutdown()
