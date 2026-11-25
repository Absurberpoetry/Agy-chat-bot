from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agychat_bot.config import (  # noqa: E402
    ContextLLMTriggerConfig,
    DiceTriggerConfig,
    KeywordTriggerConfig,
    MentionTriggerConfig,
    QuoteTriggerConfig,
    TriggersConfig,
)
from agychat_bot.models import IncomingMessage, JudgeDecision  # noqa: E402
from agychat_bot.triggers.engine import TriggerEngine  # noqa: E402


def _make_msg(
    *,
    msg_id: str = "m1",
    user_id: str = "111",
    text: str = "hello",
    at_targets: list[str] | None = None,
    reply_to_msg_id: str | None = None,
    reply_to_user_id: str | None = None,
    is_self: bool = False,
) -> IncomingMessage:
    return IncomingMessage(
        adapter_id="qq_1",
        platform="onebot_v11",
        self_id="999",
        scene="group",
        channel_id="10001",
        message_id=msg_id,
        user_id=user_id,
        sender_name=f"User{user_id}",
        text=text,
        timestamp=1000.0,
        at_targets=at_targets or [],
        reply_to_msg_id=reply_to_msg_id,
        reply_to_user_id=reply_to_user_id,
        is_self=is_self,
    )


class TriggerEngineTests(unittest.IsolatedAsyncioTestCase):
    def test_mention_vs_quote_strict_distinction(self) -> None:
        # Case A: Quote enabled, Mention disabled -> quoting bot (which carries QQ auto-@bot) MUST still trigger `quote`,
        # whereas pure @bot MUST NOT trigger!
        engine_quote_only = TriggerEngine(
            TriggersConfig(
                mention=MentionTriggerConfig(enabled=False),
                quote=QuoteTriggerConfig(enabled=True),
                keyword=KeywordTriggerConfig(enabled=False),
                dice=DiceTriggerConfig(enabled=False),
            )
        )
        pure_at = _make_msg(text="hi", at_targets=["999"])
        self.assertFalse(engine_quote_only.evaluate_fast(pure_at).triggered)

        quoted_bot = _make_msg(
            text="真的吗",
            at_targets=["999"],  # QQ auto-injects @bot on quote!
            reply_to_msg_id="bot_0",
            reply_to_user_id="999",
        )
        res_q = engine_quote_only.evaluate_fast(quoted_bot)
        self.assertTrue(res_q.triggered)
        self.assertEqual(res_q.trigger_type, "quote")

        # Case B: Mention enabled, Quote disabled -> pure quote of bot MUST NOT trigger `mention`!
        engine_mention_only = TriggerEngine(
            TriggersConfig(
                mention=MentionTriggerConfig(enabled=True, include_quote_at=False),
                quote=QuoteTriggerConfig(enabled=False),
                keyword=KeywordTriggerConfig(enabled=False),
                dice=DiceTriggerConfig(enabled=False),
            )
        )
        self.assertFalse(engine_mention_only.evaluate_fast(quoted_bot).triggered)
        self.assertEqual(engine_mention_only.evaluate_fast(pure_at).trigger_type, "mention")

        # Quoting another user + manually @bot -> triggers `mention`!
        quote_other_plus_at = _make_msg(
            text="你看他说得对吗",
            at_targets=["999"],
            reply_to_msg_id="u_other",
            reply_to_user_id="777",
        )
        res_m = engine_mention_only.evaluate_fast(quote_other_plus_at)
        self.assertTrue(res_m.triggered)
        self.assertEqual(res_m.trigger_type, "mention")

    def test_keyword_and_exclude_and_regex(self) -> None:
        engine = TriggerEngine(
            TriggersConfig(
                mention=MentionTriggerConfig(enabled=False),
                quote=QuoteTriggerConfig(enabled=False),
                keyword=KeywordTriggerConfig(
                    enabled=True,
                    keywords=["小助手"],
                    regex_patterns=[r"^#ask\b"],
                    exclude_keywords=["别理我"],
                ),
                dice=DiceTriggerConfig(enabled=False),
            )
        )
        self.assertEqual(engine.evaluate_fast(_make_msg(text="呼叫小助手")).trigger_type, "keyword")
        self.assertEqual(engine.evaluate_fast(_make_msg(text="#ASK 帮我查一下")).trigger_type, "keyword")
        self.assertFalse(engine.evaluate_fast(_make_msg(text="小助手别理我")).triggered)

    def test_dice_probability_and_cooldown(self) -> None:
        class FixedRng(random.Random):
            def random(self) -> float:
                return 0.01

        engine = TriggerEngine(
            TriggersConfig(
                mention=MentionTriggerConfig(enabled=False),
                quote=QuoteTriggerConfig(enabled=False),
                keyword=KeywordTriggerConfig(enabled=False),
                dice=DiceTriggerConfig(
                    enabled=True,
                    probability=0.05,
                    min_message_length=4,
                    cooldown_seconds=60.0,
                    ignore_prefixes=["/"],
                ),
            ),
            rng=FixedRng(),
        )
        # Too short or command prefix ignored
        self.assertFalse(engine.evaluate_fast(_make_msg(text="hi"), now_mono=10.0).triggered)
        self.assertFalse(engine.evaluate_fast(_make_msg(text="/cmd abc"), now_mono=10.0).triggered)

        # Eligible message triggers dice
        res1 = engine.evaluate_fast(_make_msg(text="今天吃啥好呢"), now_mono=100.0)
        self.assertTrue(res1.triggered)
        self.assertEqual(res1.trigger_type, "dice")

        # Within cooldown -> suppressed
        self.assertFalse(engine.evaluate_fast(_make_msg(text="再来一条看看"), now_mono=120.0).triggered)

        # Past cooldown -> triggers again
        self.assertTrue(engine.evaluate_fast(_make_msg(text="过了冷却期"), now_mono=165.0).triggered)

    async def test_context_llm_gating(self) -> None:
        judge_calls = 0

        async def fake_judge(session_key: str, msgs: list[IncomingMessage]) -> JudgeDecision:
            nonlocal judge_calls
            judge_calls += 1
            return JudgeDecision(should_reply=True, reason="有人在等解答")

        engine = TriggerEngine(
            TriggersConfig(
                context_llm=ContextLLMTriggerConfig(
                    enabled=True,
                    min_unread_messages=3,
                    min_interval_seconds=60.0,
                    judge_context_window=5,
                    cooldown_after_reply_seconds=60.0,
                )
            ),
            context_judge=fake_judge,
        )
        msg = _make_msg(text="没人回答吗")
        recent = [_make_msg(msg_id=f"m{i}", text=f"msg {i}") for i in range(4)]

        # Insufficient unread count
        r_low = await engine.evaluate_context_llm(msg, unread_ambient_count=2, recent_messages=recent, now_mono=100.0)
        self.assertFalse(r_low.triggered)
        self.assertEqual(judge_calls, 0)

        # Meets unread threshold
        r_ok = await engine.evaluate_context_llm(msg, unread_ambient_count=3, recent_messages=recent, now_mono=100.0)
        self.assertTrue(r_ok.triggered)
        self.assertEqual(r_ok.trigger_type, "context_llm")
        self.assertEqual(judge_calls, 1)

        # Respects interval cooldown
        r_cd = await engine.evaluate_context_llm(msg, unread_ambient_count=10, recent_messages=recent, now_mono=110.0)
        self.assertFalse(r_cd.triggered)
        self.assertEqual(judge_calls, 1)


if __name__ == "__main__":
    unittest.main()
