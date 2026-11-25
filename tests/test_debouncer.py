from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_bot.adapters import AdapterRegistry  # noqa: E402
from agy_bot.adapters.base import PlatformAdapter  # noqa: E402
from agy_bot.adapters.generic_ws import GenericWSAdapter  # noqa: E402
from agy_bot.app import BotApplication  # noqa: E402
from agy_bot.collector.context_store import GroupContextStore  # noqa: E402
from agy_bot.collector.debouncer import DebouncedReplyManager  # noqa: E402
from agy_bot.config import (  # noqa: E402
    AgyConfig,
    BotConfig,
    CollectorConfig,
    PlatformConfig,
    RateLimitConfig,
    ReplyConfig,
    TriggersConfig,
    load_config,
)
from agy_bot.models import ContextSnapshot, IncomingMessage, TriggerResult  # noqa: E402
from agy_bot.runner import MockAgentRunner  # noqa: E402


def _make_msg(
    *,
    adapter_id: str = "qq_1",
    self_id: str = "999",
    msg_id: str,
    user_id: str,
    text: str,
    ts: float = 1000.0,
    at_targets: list[str] | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        adapter_id=adapter_id,
        platform="onebot_v11",
        self_id=self_id,
        scene="group",
        channel_id="10001",
        message_id=msg_id,
        user_id=user_id,
        sender_name=f"U{user_id}",
        text=text,
        timestamp=ts,
        at_targets=at_targets or [],
    )


class DummyAdapter(PlatformAdapter):
    def __init__(self, config: PlatformConfig, **kwargs) -> None:
        super().__init__(config, **kwargs)
        self.replies: list[tuple[IncomingMessage, str, bool]] = []
        self._counter = 0

    async def start(self, on_message) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_reply(
        self,
        target: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        self._counter += 1
        self.replies.append((target, text, quote_trigger))
        return f"bot_sent_{self._counter}"


class DebouncerAndIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_trailing_collection_resets_only_for_tracked_user_and_captures_bystanders(self) -> None:
        store = GroupContextStore()
        snapshots: list[ContextSnapshot] = []
        done = asyncio.Event()

        async def on_ready(snap: ContextSnapshot) -> None:
            snapshots.append(snap)
            done.set()

        debouncer = DebouncedReplyManager(
            CollectorConfig(
                wait_triggers=["mention", "quote", "keyword"],
                quiet_wait_seconds=0.15,
                max_wait_seconds=1.0,
                max_extra_messages=5,
                track_only_trigger_user=True,
                context_message_limit=20,
            ),
            store,
            on_ready,
        )

        m0 = _make_msg(msg_id="m0", user_id="bystander", text="前面聊的内容", ts=999.0)
        store.add_message(m0)

        m1 = _make_msg(msg_id="m1", user_id="alice", text="帮我看看", ts=1000.0, at_targets=["999"])
        store.add_message(m1)
        debouncer.on_trigger(
            m1,
            TriggerResult(
                triggered=True,
                trigger_type="mention",
                matched_triggers=["mention"],
                reason="explicit_at_bot",
                should_wait_trailing=True,
            ),
        )

        # Wait 80ms (< 150ms), Alice sends follow-up #1 -> should reset idle timer!
        await asyncio.sleep(0.08)
        self.assertEqual(len(snapshots), 0)
        m2 = _make_msg(msg_id="m2", user_id="alice", text="运行报错 KeyError", ts=1001.0)
        store.add_message(m2)
        debouncer.handle_followup_message(m2)

        # Wait 40ms, Bystander sends message -> stored in context, does NOT reset or block
        await asyncio.sleep(0.04)
        m_other = _make_msg(msg_id="m_bystander", user_id="bob", text="我也遇到过", ts=1002.0)
        store.add_message(m_other)
        self.assertFalse(debouncer.handle_followup_message(m_other))

        # Wait 40ms (now 80ms since m2 < 150ms), Alice sends follow-up #2 -> resets timer again!
        await asyncio.sleep(0.04)
        self.assertEqual(len(snapshots), 0)
        m3 = _make_msg(msg_id="m3", user_id="alice", text="就是 config.py 第 10 行", ts=1003.0)
        store.add_message(m3)
        debouncer.handle_followup_message(m3)

        # Now Alice stops typing -> settles after quiet_wait_seconds (~0.15s)
        await asyncio.wait_for(done.wait(), timeout=1.0)
        await debouncer.shutdown()

        self.assertEqual(len(snapshots), 1)
        snap = snapshots[0]
        self.assertEqual(snap.extra_messages_collected, 2)
        self.assertEqual([m.message_id for m in snap.messages], ["m0", "m1", "m2", "m_bystander", "m3"])
        self.assertIn("m1", snap.trigger_message_ids)
        self.assertEqual(snap.followup_message_ids, {"m2", "m3"})

    async def test_max_extra_messages_flushes_immediately(self) -> None:
        store = GroupContextStore()
        snapshots: list[ContextSnapshot] = []
        done = asyncio.Event()

        async def on_ready(snap: ContextSnapshot) -> None:
            snapshots.append(snap)
            done.set()

        debouncer = DebouncedReplyManager(
            CollectorConfig(
                wait_triggers=["mention"],
                quiet_wait_seconds=5.0,
                max_wait_seconds=10.0,
                max_extra_messages=2,
            ),
            store,
            on_ready,
        )

        m1 = _make_msg(msg_id="m1", user_id="alice", text="第一句", at_targets=["999"])
        store.add_message(m1)
        debouncer.on_trigger(
            m1,
            TriggerResult(triggered=True, trigger_type="mention", matched_triggers=["mention"], should_wait_trailing=True),
        )

        m2 = _make_msg(msg_id="m2", user_id="alice", text="第二句")
        store.add_message(m2)
        debouncer.handle_followup_message(m2)
        self.assertEqual(len(snapshots), 0)

        m3 = _make_msg(msg_id="m3", user_id="alice", text="第三句到达上限")
        store.add_message(m3)
        debouncer.handle_followup_message(m3)

        await asyncio.wait_for(done.wait(), timeout=0.5)
        await debouncer.shutdown()
        self.assertEqual(snapshots[0].extra_messages_collected, 2)

    async def test_max_wait_seconds_deadline_caps_continuous_followups(self) -> None:
        store = GroupContextStore()
        snapshots: list[ContextSnapshot] = []
        done = asyncio.Event()

        async def on_ready(snap: ContextSnapshot) -> None:
            snapshots.append(snap)
            done.set()

        debouncer = DebouncedReplyManager(
            CollectorConfig(
                wait_triggers=["mention"],
                quiet_wait_seconds=0.14,
                max_wait_seconds=0.20,
                max_extra_messages=10,
            ),
            store,
            on_ready,
        )

        m1 = _make_msg(msg_id="m1", user_id="alice", text="第1句", at_targets=["999"])
        store.add_message(m1)
        debouncer.on_trigger(
            m1,
            TriggerResult(triggered=True, trigger_type="mention", matched_triggers=["mention"], should_wait_trailing=True),
        )

        for i in range(2, 6):
            await asyncio.sleep(0.06)
            if done.is_set():
                break
            mi = _make_msg(msg_id=f"m{i}", user_id="alice", text=f"第{i}句")
            store.add_message(mi)
            debouncer.handle_followup_message(mi)

        await asyncio.wait_for(done.wait(), timeout=0.6)
        await debouncer.shutdown()
        self.assertEqual(len(snapshots), 1)
        self.assertGreaterEqual(snapshots[0].extra_messages_collected, 1)

    async def test_bot_application_end_to_end_multi_platform_and_quote_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp_root = Path(td)
            registry = AdapterRegistry()
            registry.register_type("dummy", DummyAdapter)

            cfg = BotConfig(
                bot_name="测试机器人",
                platforms=[
                    PlatformConfig(id="p1", type="dummy", ws_url="ws://1", self_id="999"),
                    PlatformConfig(id="p2", type="dummy", ws_url="ws://2", self_id="888"),
                ],
                triggers=TriggersConfig(),
                collector=CollectorConfig(
                    wait_triggers=["mention", "quote", "keyword"],
                    quiet_wait_seconds=0.1,
                    max_wait_seconds=1.0,
                    max_extra_messages=4,
                ),
                rate_limit=RateLimitConfig(
                    max_concurrent_requests=1,
                    user_cooldown_seconds=10.0,
                    channel_min_interval_seconds=0.0,
                ),
                reply=ReplyConfig(quote_trigger_message=True, max_reply_chars=500),
                agy=AgyConfig(
                    mode="mock",
                    workspace_dir=str(tmp_root / "ws"),
                    data_dir=str(tmp_root / "data"),
                ),
                base_dir=tmp_root,
            )
            runner = MockAgentRunner()
            app = BotApplication(cfg, registry=registry, runner=runner)

            # User triggers on p1, then immediately sends a follow-up within cooldown window.
            # Cooldown MUST NOT block the follow-up message from extending/updating the pending batch.
            await app.handle_message(_make_msg(adapter_id="p1", self_id="999", msg_id="p1_m1", user_id="u1", text="在吗", at_targets=["999"]))
            await asyncio.sleep(0.03)
            await app.handle_message(_make_msg(adapter_id="p1", self_id="999", msg_id="p1_m2", user_id="u1", text="我补充第二句"))

            # Simultaneously, platform p2 triggers independently!
            await app.handle_message(_make_msg(adapter_id="p2", self_id="888", msg_id="p2_m1", user_id="u2", text="p2测试", at_targets=["888"]))

            await asyncio.sleep(0.25)

            p1_adapter = registry.get("p1")
            p2_adapter = registry.get("p2")
            assert isinstance(p1_adapter, DummyAdapter)
            assert isinstance(p2_adapter, DummyAdapter)

            self.assertEqual(len(p1_adapter.replies), 1)
            self.assertEqual(len(p2_adapter.replies), 1)
            # Bot's prompt for p1 must include the follow-up text!
            p1_calls = [p for sk, p in runner.calls if sk.startswith("p1:")]
            self.assertEqual(len(p1_calls), 1)
            self.assertIn("我补充第二句", p1_calls[0])

            # Bot's own sent reply ID (`bot_sent_1`) should now be cached in ContextStore for future quote detection!
            cached = app.context_store.lookup_message("p1", "bot_sent_1")
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.user_id, "999")

            # Duplicate echoed message_sent with same message_id should NOT duplicate channel history!
            echo = _make_msg(adapter_id="p1", self_id="999", msg_id="bot_sent_1", user_id="999", text="echo")
            echo.is_self = True
            await app.handle_message(echo)
            recent_ids = [m.message_id for m in app.context_store.get_recent("p1:group:10001", limit=20)]
            self.assertEqual(recent_ids.count("bot_sent_1"), 1)

            await app.shutdown()

    def test_load_example_config_and_generic_ws_normalize(self) -> None:
        cfg = load_config(ROOT / "config.example.json")
        self.assertEqual(cfg.rate_limit.max_concurrent_requests, 1)
        self.assertEqual(len(cfg.platforms), 3)

        store = GroupContextStore()
        store.cache_message(
            adapter_id="custom",
            message_id="orig_1",
            user_id="bot_01",
            sender_name="Bot",
            text="上一句回复",
            timestamp=100.0,
        )
        g_adapter = GenericWSAdapter(
            PlatformConfig(id="custom", type="generic_ws", ws_url="ws://127.0.0.1:4001/ws", self_id="bot_01"),
            message_lookup=store.lookup_message,
        )
        msg = g_adapter.normalize_event({
            "type": "message",
            "scene": "group",
            "channel_id": "room_9",
            "message_id": "m_2",
            "user_id": "u_alice",
            "sender_name": "Alice",
            "text": "展开说说",
            "reply_to_msg_id": "orig_1",
        })
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertTrue(msg.is_quoted_bot)
        self.assertEqual(msg.reply_to_text, "上一句回复")


if __name__ == "__main__":
    unittest.main()
