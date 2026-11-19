from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SceneType = Literal["group", "private"]
TriggerType = Literal["mention", "quote", "keyword", "dice", "context_llm"]


@dataclass(slots=True)
class Segment:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class IncomingMessage:
    adapter_id: str
    platform: str
    self_id: str
    scene: SceneType
    channel_id: str
    message_id: str
    user_id: str
    sender_name: str
    text: str
    timestamp: float
    raw_segments: list[Segment] = field(default_factory=list)
    at_targets: list[str] = field(default_factory=list)
    reply_to_msg_id: str | None = None
    reply_to_user_id: str | None = None
    reply_to_text: str | None = None
    is_self: bool = False
    raw_event: dict[str, Any] = field(default_factory=dict)

    @property
    def session_key(self) -> str:
        return f"{self.adapter_id}:{self.scene}:{self.channel_id}"

    @property
    def is_quoted_bot(self) -> bool:
        return bool(
            self.reply_to_msg_id
            and self.reply_to_user_id
            and self.self_id
            and self.reply_to_user_id == self.self_id
        )

    @property
    def has_at_all(self) -> bool:
        return "all" in self.at_targets

    @property
    def at_bot_count(self) -> int:
        if not self.self_id:
            return 0
        return sum(1 for target in self.at_targets if target == self.self_id)

    @property
    def explicit_at_bot_count(self) -> int:
        """Number of `@bot` segments excluding the 1 auto-injected `@bot` from QQ quotes."""
        count = self.at_bot_count
        if self.is_quoted_bot and count > 0:
            return count - 1
        return count


@dataclass(slots=True)
class CachedMessage:
    message_id: str
    user_id: str
    sender_name: str
    text: str
    timestamp: float


@dataclass(slots=True)
class TriggerResult:
    triggered: bool
    trigger_type: TriggerType | None = None
    matched_triggers: list[TriggerType] = field(default_factory=list)
    reason: str = ""
    should_wait_trailing: bool = False


@dataclass(slots=True)
class ContextSnapshot:
    session_key: str
    adapter_id: str
    platform: str
    scene: SceneType
    channel_id: str
    primary_trigger: TriggerType
    matched_triggers: list[TriggerType]
    trigger_reason: str
    trigger_message: IncomingMessage
    tracked_user_ids: list[str]
    extra_messages_collected: int
    settled_at: float
    messages: list[IncomingMessage]
    trigger_message_ids: set[str] = field(default_factory=set)
    followup_message_ids: set[str] = field(default_factory=set)


@dataclass(slots=True)
class JudgeDecision:
    should_reply: bool
    reason: str = ""


@dataclass(slots=True)
class RunnerResult:
    ok: bool
    response: str = ""
    conversation_id: str = ""
    error: str = ""
    status: str = ""
    num_turns: int = 0
    is_rate_limited: bool = False
    is_fatal_auth_or_tos: bool = False
