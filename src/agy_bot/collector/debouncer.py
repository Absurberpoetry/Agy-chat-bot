from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from ..config import CollectorConfig
from ..models import ContextSnapshot, IncomingMessage, TriggerResult, TriggerType
from .context_store import GroupContextStore

logger = logging.getLogger(__name__)

OnSnapshotReadyFn = Callable[[ContextSnapshot], Awaitable[None]]


@dataclass
class _PendingCollection:
    session_key: str
    adapter_id: str
    platform: str
    scene: str
    channel_id: str
    primary_trigger: TriggerType
    matched_triggers: list[TriggerType]
    trigger_reason: str
    trigger_message: IncomingMessage
    started_mono: float
    deadline_mono: float
    tracked_user_ids: set[str] = field(default_factory=set)
    trigger_message_ids: set[str] = field(default_factory=set)
    followup_message_ids: set[str] = field(default_factory=set)
    extra_count: int = 0
    generation: int = 0
    timer_handle: asyncio.TimerHandle | None = None
    prompt_override: str = ""


class DebouncedReplyManager:
    """Tracks post-trigger follow-up messages before emitting a context snapshot."""

    def __init__(
        self,
        config: CollectorConfig,
        context_store: GroupContextStore,
        on_ready: OnSnapshotReadyFn,
    ) -> None:
        self._config = config
        self._store = context_store
        self._on_ready = on_ready
        self._pending: dict[str, _PendingCollection] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._session_dispatch_tasks: dict[str, set[asyncio.Task[None]]] = {}

    def has_pending(self, session_key: str) -> bool:
        return session_key in self._pending

    def is_tracked_followup(self, msg: IncomingMessage) -> bool:
        pending = self._pending.get(msg.session_key)
        if pending is None:
            return False
        if not self._config.track_only_trigger_user:
            return True
        return msg.user_id in pending.tracked_user_ids

    def handle_followup_message(self, msg: IncomingMessage, *, is_new_trigger: bool = False) -> bool:
        """Observe a message arriving while a pending session exists.

        Returns True if the message belonged to tracked user(s) and updated/flushed the timer.
        Messages from bystander users still sit in GroupContextStore and do not reset the timer.
        """
        pending = self._pending.get(msg.session_key)
        if pending is None:
            return False

        tracked = (not self._config.track_only_trigger_user) or (msg.user_id in pending.tracked_user_ids)
        if not tracked:
            return False

        if is_new_trigger:
            if msg.message_id:
                pending.trigger_message_ids.add(msg.message_id)
        else:
            if msg.message_id:
                pending.followup_message_ids.add(msg.message_id)

        pending.extra_count += 1
        now_mono = time.monotonic()

        if (
            pending.extra_count >= self._config.max_extra_messages
            or now_mono >= pending.deadline_mono
            or self._config.quiet_wait_seconds <= 0
        ):
            logger.debug(
                "[%s] 追发收集达到上限或截止时间 (extra=%d)，立即快照思考",
                msg.session_key,
                pending.extra_count,
            )
            self._flush(msg.session_key)
            return True

        self._reschedule(pending, now_mono)
        return True

    def on_trigger(self, msg: IncomingMessage, trigger: TriggerResult) -> None:
        assert trigger.trigger_type is not None
        session_key = msg.session_key
        should_wait = (
            trigger.should_wait_trailing
            and trigger.trigger_type in self._config.wait_triggers
            and self._config.quiet_wait_seconds > 0
            and self._config.max_wait_seconds > 0
        )

        existing = self._pending.get(session_key)
        if existing is not None:
            existing.tracked_user_ids.add(msg.user_id)
            for t in trigger.matched_triggers:
                if t not in existing.matched_triggers:
                    existing.matched_triggers.append(t)
            # Re-trigger from another user or same user updates tracking and respects existing deadline.
            self.handle_followup_message(msg, is_new_trigger=True)
            return

        if not should_wait:
            snapshot = self._build_snapshot(
                session_key=session_key,
                adapter_id=msg.adapter_id,
                platform=msg.platform,
                scene=msg.scene,
                channel_id=msg.channel_id,
                primary_trigger=trigger.trigger_type,
                matched_triggers=list(trigger.matched_triggers or [trigger.trigger_type]),
                trigger_reason=trigger.reason,
                trigger_message=msg,
                tracked_user_ids=[msg.user_id],
                extra_count=0,
                trigger_message_ids={msg.message_id} if msg.message_id else set(),
                followup_message_ids=set(),
                prompt_override=trigger.prompt_override,
            )
            self._emit_snapshot(snapshot)
            return

        now_mono = time.monotonic()
        pending = _PendingCollection(
            session_key=session_key,
            adapter_id=msg.adapter_id,
            platform=msg.platform,
            scene=msg.scene,
            channel_id=msg.channel_id,
            primary_trigger=trigger.trigger_type,
            matched_triggers=list(trigger.matched_triggers or [trigger.trigger_type]),
            trigger_reason=trigger.reason,
            trigger_message=msg,
            started_mono=now_mono,
            deadline_mono=now_mono + self._config.max_wait_seconds,
            tracked_user_ids={msg.user_id},
            trigger_message_ids={msg.message_id} if msg.message_id else set(),
            prompt_override=trigger.prompt_override,
        )
        self._pending[session_key] = pending
        self._reschedule(pending, now_mono)
        logger.info(
            "[%s] 触发(%s)，开始跟踪用户 %s 连发 (静默等待 %.1fs，最长 %.1fs，最多追 %d 条)",
            session_key,
            trigger.trigger_type,
            msg.user_id,
            self._config.quiet_wait_seconds,
            self._config.max_wait_seconds,
            self._config.max_extra_messages,
        )

    def _reschedule(self, pending: _PendingCollection, now_mono: float) -> None:
        if pending.timer_handle is not None:
            pending.timer_handle.cancel()
            pending.timer_handle = None

        remaining_deadline = max(0.0, pending.deadline_mono - now_mono)
        delay = min(self._config.quiet_wait_seconds, remaining_deadline)
        pending.generation += 1
        gen = pending.generation

        if delay <= 0:
            self._flush(pending.session_key)
            return

        loop = asyncio.get_running_loop()
        pending.timer_handle = loop.call_later(
            delay,
            self._on_timer_fired,
            pending.session_key,
            gen,
        )

    def _on_timer_fired(self, session_key: str, generation: int) -> None:
        pending = self._pending.get(session_key)
        if pending is None or pending.generation != generation:
            return
        pending.timer_handle = None
        self._flush(session_key)

    def _flush(self, session_key: str) -> None:
        pending = self._pending.pop(session_key, None)
        if pending is None:
            return
        if pending.timer_handle is not None:
            pending.timer_handle.cancel()
            pending.timer_handle = None

        snapshot = self._build_snapshot(
            session_key=pending.session_key,
            adapter_id=pending.adapter_id,
            platform=pending.platform,
            scene=pending.scene,  # type: ignore[arg-type]
            channel_id=pending.channel_id,
            primary_trigger=pending.primary_trigger,
            matched_triggers=pending.matched_triggers,
            trigger_reason=pending.trigger_reason,
            trigger_message=pending.trigger_message,
            tracked_user_ids=sorted(pending.tracked_user_ids),
            extra_count=pending.extra_count,
            trigger_message_ids=pending.trigger_message_ids,
            followup_message_ids=pending.followup_message_ids,
            prompt_override=pending.prompt_override,
        )
        self._emit_snapshot(snapshot)

    def _build_snapshot(
        self,
        *,
        session_key: str,
        adapter_id: str,
        platform: str,
        scene: str,
        channel_id: str,
        primary_trigger: TriggerType,
        matched_triggers: list[TriggerType],
        trigger_reason: str,
        trigger_message: IncomingMessage,
        tracked_user_ids: list[str],
        extra_count: int,
        trigger_message_ids: set[str],
        followup_message_ids: set[str],
        prompt_override: str = "",
    ) -> ContextSnapshot:
        recent = self._store.get_recent(session_key, limit=self._config.context_message_limit)
        if trigger_message not in recent:
            recent = [*recent, trigger_message]
        return ContextSnapshot(
            session_key=session_key,
            adapter_id=adapter_id,
            platform=platform,
            scene=scene,  # type: ignore[arg-type]
            channel_id=channel_id,
            primary_trigger=primary_trigger,
            matched_triggers=matched_triggers,
            trigger_reason=trigger_reason,
            trigger_message=trigger_message,
            tracked_user_ids=tracked_user_ids,
            extra_messages_collected=extra_count,
            settled_at=time.time(),
            messages=recent,
            trigger_message_ids=trigger_message_ids,
            followup_message_ids=followup_message_ids,
            prompt_override=prompt_override,
        )

    def _emit_snapshot(self, snapshot: ContextSnapshot) -> None:
        skey = snapshot.session_key
        task = asyncio.create_task(
            self._safe_dispatch(snapshot),
            name=f"dispatch-{skey}",
        )
        self._dispatch_tasks.add(task)
        bucket = self._session_dispatch_tasks.setdefault(skey, set())
        bucket.add(task)

        def _cleanup(t: asyncio.Task[None]) -> None:
            self._dispatch_tasks.discard(t)
            b = self._session_dispatch_tasks.get(skey)
            if b is not None:
                b.discard(t)
                if not b:
                    self._session_dispatch_tasks.pop(skey, None)

        task.add_done_callback(_cleanup)

    async def _safe_dispatch(self, snapshot: ContextSnapshot) -> None:
        try:
            await self._on_ready(snapshot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[%s] 快照触发回复任务失败", snapshot.session_key)

    def cancel_session(self, session_key: str) -> bool:
        """Cancel any pending debounced collection AND active dispatch tasks for a session (e.g., when muted)."""
        cancelled_any = False
        pending = self._pending.pop(session_key, None)
        if pending is not None:
            if pending.timer_handle is not None:
                pending.timer_handle.cancel()
                pending.timer_handle = None
            cancelled_any = True
        active_tasks = list(self._session_dispatch_tasks.get(session_key, ()))
        for t in active_tasks:
            if not t.done():
                t.cancel()
                cancelled_any = True
        return cancelled_any

    async def shutdown(self) -> None:
        for pending in list(self._pending.values()):
            if pending.timer_handle is not None:
                pending.timer_handle.cancel()
        self._pending.clear()
        for task in list(self._dispatch_tasks):
            task.cancel()
        if self._dispatch_tasks:
            await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
            self._dispatch_tasks.clear()
