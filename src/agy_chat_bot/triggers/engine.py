from __future__ import annotations

import logging
import random
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from ..config import RateLimitConfig, TriggersConfig
from ..models import IncomingMessage, JudgeDecision, TriggerResult, TriggerType

logger = logging.getLogger(__name__)

ContextJudgeFn = Callable[[str, list[IncomingMessage]], Awaitable[JudgeDecision]]


class RateLimiter:
    """Enforces per-user cooldown and per-channel reply budget."""

    def __init__(self, config: RateLimitConfig) -> None:
        self._config = config
        self._last_user_trigger: dict[tuple[str, str], float] = {}
        self._last_channel_reply: dict[str, float] = {}
        self._channel_minute_window: dict[str, deque[float]] = defaultdict(deque)

    def can_trigger(self, msg: IncomingMessage, *, now_mono: float | None = None) -> tuple[bool, str]:
        now = time.monotonic() if now_mono is None else now_mono
        user_key = (msg.session_key, msg.user_id)

        last_user = self._last_user_trigger.get(user_key)
        if last_user is not None and (now - last_user) < self._config.user_cooldown_seconds:
            return False, "user_cooldown"

        last_chan = self._last_channel_reply.get(msg.session_key)
        if last_chan is not None and (now - last_chan) < self._config.channel_min_interval_seconds:
            return False, "channel_min_interval"

        win = self._channel_minute_window[msg.session_key]
        while win and (now - win[0]) > 60.0:
            win.popleft()
        if len(win) >= self._config.max_replies_per_channel_per_minute:
            return False, "channel_minute_quota"

        return True, "ok"

    def record_trigger(self, msg: IncomingMessage, *, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        self._last_user_trigger[(msg.session_key, msg.user_id)] = now

    def record_reply_sent(self, session_key: str, *, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else now_mono
        self._last_channel_reply[session_key] = now
        win = self._channel_minute_window[session_key]
        while win and (now - win[0]) > 60.0:
            win.popleft()
        win.append(now)


class TriggerEngine:
    """Evaluates the 5 trigger strategies with strict separation between @ and quote."""

    def __init__(
        self,
        config: TriggersConfig,
        *,
        rng: random.Random | None = None,
        context_judge: ContextJudgeFn | None = None,
    ) -> None:
        self._config = config
        self._rng = rng or random.Random()
        self._context_judge = context_judge
        self._compiled_regex: list[re.Pattern[str]] = []
        flags = 0 if config.keyword.case_sensitive else re.IGNORECASE
        for pattern in config.keyword.regex_patterns:
            try:
                self._compiled_regex.append(re.compile(pattern, flags))
            except re.error as exc:
                logging.getLogger(__name__).warning("忽略无效的关键词触发正则 (%s): %s", pattern, exc)

        self._last_dice_trigger: dict[str, float] = {}
        self._last_context_judge: dict[str, float] = {}
        self._last_context_reply: dict[str, float] = {}

    def set_context_judge(self, judge: ContextJudgeFn | None) -> None:
        self._context_judge = judge

    def evaluate_fast(
        self,
        msg: IncomingMessage,
        *,
        now_mono: float | None = None,
    ) -> TriggerResult:
        """Evaluate triggers 1-4 (mention, quote, keyword, dice) without LLM network calls."""
        now = time.monotonic() if now_mono is None else now_mono

        if msg.is_self:
            return TriggerResult(triggered=False, reason="self_message")

        # Private messages: direct reply unless private_needs_wake is enabled
        if msg.scene == "private":
            if self._config.private_needs_wake:
                if self._config.keyword.enabled and self._match_keyword(msg.text):
                    return TriggerResult(
                        triggered=True,
                        trigger_type="keyword",
                        matched_triggers=["keyword"],
                        reason="private_wake_keyword",
                        should_wait_trailing=True,
                    )
                return TriggerResult(triggered=False, reason="private_needs_wake")
            return TriggerResult(
                triggered=True,
                trigger_type="mention",
                matched_triggers=["mention"],
                reason="private_message",
                should_wait_trailing=True,
            )

        matched: list[TriggerType] = []
        reasons: list[str] = []

        # 1 & 2: Distinguish quote/reply vs @ mention carefully.
        # In QQ / OneBot v11, quoting the bot's message injects an `@bot` segment automatically.
        is_quoted_bot = msg.is_quoted_bot
        quote_matched = self._config.quote.enabled and is_quoted_bot
        if quote_matched:
            matched.append("quote")
            reasons.append("reply_to_bot")

        mention_cfg = self._config.mention
        mention_matched = False
        if mention_cfg.enabled:
            if mention_cfg.include_quote_at and msg.at_bot_count > 0:
                mention_matched = True
                reasons.append("at_bot_including_quote")
            elif msg.explicit_at_bot_count > 0:
                mention_matched = True
                reasons.append("explicit_at_bot")
            elif mention_cfg.allow_at_all and msg.has_at_all:
                mention_matched = True
                reasons.append("at_all")

        if (
            not quote_matched
            and not mention_matched
            and msg.reply_to_msg_id
            and msg.reply_to_user_id is None
            and msg.at_bot_count > 0
        ):
            if mention_cfg.unknown_reply_at_behavior == "quote" and self._config.quote.enabled:
                matched.append("quote")
                reasons.append("unknown_reply_fallback_quote")
            elif mention_cfg.unknown_reply_at_behavior == "mention" and mention_cfg.enabled:
                mention_matched = True
                reasons.append("unknown_reply_fallback_at")

        if mention_matched and "mention" not in matched:
            matched.append("mention")

        # 3: Keyword / regex trigger
        if self._config.keyword.enabled and self._match_keyword(msg.text):
            matched.append("keyword")
            reasons.append("keyword_matched")

        if matched:
            # Explicit ordering: if user ONLY quoted bot -> "quote"; if explicit `@` -> "mention"
            # (when both quote + extra explicit `@` occurred, primary is "quote" or "mention" deterministically).
            if "mention" in matched and "quote" not in matched:
                primary: TriggerType = "mention"
            elif "quote" in matched and "mention" not in matched:
                primary = "quote"
            elif "quote" in matched and "mention" in matched:
                primary = "quote"
            else:
                primary = matched[0]
            return TriggerResult(
                triggered=True,
                trigger_type=primary,
                matched_triggers=matched,
                reason="+".join(reasons),
                should_wait_trailing=True,
            )

        # 4: Random dice trigger (strictly gated by length, ignore prefixes, and group cooldown)
        if self._config.dice.enabled and self._match_dice(msg, now):
            self._last_dice_trigger[msg.session_key] = now
            return TriggerResult(
                triggered=True,
                trigger_type="dice",
                matched_triggers=["dice"],
                reason=f"dice_roll(p={self._config.dice.probability})",
                should_wait_trailing=True,
            )

        return TriggerResult(triggered=False, reason="no_fast_trigger")

    async def evaluate_context_llm(
        self,
        msg: IncomingMessage,
        *,
        unread_ambient_count: int,
        recent_messages: list[IncomingMessage],
        now_mono: float | None = None,
    ) -> TriggerResult:
        """Evaluate Trigger 5 (model decides from group context) under strict rate gates."""
        cfg = self._config.context_llm
        if not cfg.enabled or self._context_judge is None or msg.scene != "group":
            return TriggerResult(triggered=False, reason="context_llm_disabled")

        now = time.monotonic() if now_mono is None else now_mono
        if unread_ambient_count < cfg.min_unread_messages:
            return TriggerResult(triggered=False, reason="insufficient_ambient_messages")

        last_judge = self._last_context_judge.get(msg.session_key)
        if last_judge is not None and (now - last_judge) < cfg.min_interval_seconds:
            return TriggerResult(triggered=False, reason="context_judge_interval")

        last_ctx_reply = self._last_context_reply.get(msg.session_key)
        if last_ctx_reply is not None and (now - last_ctx_reply) < cfg.cooldown_after_reply_seconds:
            return TriggerResult(triggered=False, reason="context_reply_cooldown")

        self._last_context_judge[msg.session_key] = now
        win_n = max(1, int(cfg.judge_context_window or 1))
        window = recent_messages[-win_n:]
        decision = await self._context_judge(msg.session_key, window)
        if decision.should_reply:
            self._last_context_reply[msg.session_key] = now if now_mono is not None else time.monotonic()
            return TriggerResult(
                triggered=True,
                trigger_type="context_llm",
                matched_triggers=["context_llm"],
                reason=f"context_llm:{decision.reason}",
                should_wait_trailing=True,
            )

        return TriggerResult(triggered=False, reason=f"context_llm_skip:{decision.reason}")

    def _match_keyword(self, text: str) -> bool:
        if not text:
            return False
        cfg = self._config.keyword
        haystack = text if cfg.case_sensitive else text.lower()

        for excl in cfg.exclude_keywords:
            needle = excl if cfg.case_sensitive else excl.lower()
            if needle and needle in haystack:
                return False

        for kw in cfg.keywords:
            needle = kw if cfg.case_sensitive else kw.lower()
            if needle and needle in haystack:
                return True

        for pattern in self._compiled_regex:
            if pattern.search(text):
                return True

        return False

    def _match_dice(self, msg: IncomingMessage, now_mono: float) -> bool:
        cfg = self._config.dice
        if msg.scene != "group" or cfg.probability <= 0:
            return False
        text = msg.text.strip()
        if len(text) < cfg.min_message_length:
            return False
        for prefix in cfg.ignore_prefixes:
            if prefix and text.startswith(prefix):
                return False
        last = self._last_dice_trigger.get(msg.session_key)
        if last is not None and (now_mono - last) < cfg.cooldown_seconds:
            return False
        return self._rng.random() < cfg.probability
