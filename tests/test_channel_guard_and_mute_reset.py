from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_bot.adapters.base import PlatformAdapter  # noqa: E402
from agy_bot.adapters.onebot_v11 import OneBotV11Adapter  # noqa: E402
from agy_bot.app import BotApplication  # noqa: E402
from agy_bot.config import (  # noqa: E402
    BotConfig,
    PlatformConfig,
    load_config_from_dict,
)
from agy_bot.models import IncomingMessage, JudgeDecision, RunnerResult  # noqa: E402


class _CaptureAdapter(PlatformAdapter):
    def __init__(self, cfg: PlatformConfig) -> None:
        super().__init__(cfg)
        self.self_id = cfg.self_id or "99999"
        self.sent: list[dict[str, Any]] = []

    async def start(self, on_message) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_reply(self, target, text: str, *, quote_trigger: bool = True, at_user: bool = False):
        self.sent.append({"scene": target.scene, "channel": target.channel_id, "text": text})
        return f"m-{len(self.sent)}"


class _AlwaysReplyRunner:
    def __init__(self) -> None:
        self.ask_count = 0
        self.judge_count = 0

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    async def ask(self, session_key: str, prompt: str, **kwargs) -> RunnerResult:
        self.ask_count += 1
        return RunnerResult(ok=True, response=f"reply-{self.ask_count}", status="SUCCESS")

    async def judge_context(self, session_key: str, messages) -> JudgeDecision:
        self.judge_count += 1
        return JudgeDecision(should_reply=True, reason="judge_yes")

    async def shutdown(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        return {"model": "test", "warm_sessions": [], "circuit_open": False}


class ChannelGuardAndMuteResetTests(unittest.IsolatedAsyncioTestCase):
    def _build_app(self, base: Path) -> tuple[BotApplication, _CaptureAdapter, _AlwaysReplyRunner]:
        cfg = load_config_from_dict(
            {
                "bot_name": "小安",
                "platforms": [
                    {
                        "id": "qq_1",
                        "ws_url": "ws://127.0.0.1:3001",
                        "self_id": "99999",
                        "user_blocklist": ["bad_u"],
                        "private_blocklist": ["priv_bad"],
                    }
                ],
                "triggers": {
                    "mention": {"enabled": True, "allow_at_all": True},
                    "quote": {"enabled": True},
                    "keyword": {"enabled": True, "keywords": ["小助手"]},
                    "dice": {"enabled": True, "probability": 1.0, "min_message_length": 1, "cooldown_seconds": 0},
                    "context_llm": {
                        "enabled": True,
                        "min_unread_messages": 1,
                        "min_interval_seconds": 0,
                        "cooldown_after_reply_seconds": 0,
                    },
                },
                "collector": {"quiet_wait_seconds": 0, "max_wait_seconds": 0},
                "rate_limit": {"user_cooldown_seconds": 0, "channel_min_interval_seconds": 0, "max_replies_per_channel_per_minute": 50},
                "agy": {"mode": "mock", "workspace_dir": "./ws", "data_dir": "./data"},
            },
            base_dir=base,
        )
        runner = _AlwaysReplyRunner()
        app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
        adapter = _CaptureAdapter(cfg.platforms[0])
        app.registry.adapters["qq_1"] = adapter
        return app, adapter, runner

    async def test_unactivated_group_and_private_stay_completely_silent_until_woken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app, adapter, runner = self._build_app(Path(tmp))

            # 1) Proactive scheduled/script push MUST NOT fire into unactivated group or unactivated private!
            r_grp = await app.trigger_proactive(scene="group", channel_id="88888", mode="text", prompt="早安")
            self.assertFalse(r_grp["ok"])
            r_priv = await app.trigger_proactive(scene="private", channel_id="12345", mode="text", prompt="你好陌生人")
            self.assertFalse(r_priv["ok"])
            self.assertEqual(len(adapter.sent), 0)

            # 2) In an unactivated group, keyword / dice(p=1.0) / context_llm / @all / pure quote MUST NOT trigger!
            for mid, text, extra in [
                ("m1", "小助手在吗", {}),
                ("m2", "随便水一句触发100%骰子", {}),
                ("m3", "全体成员注意", {"at_targets": ["all"]}),
                (
                    "m4",
                    "引用机器人旧消息也不行",
                    {"at_targets": ["99999"], "reply_to_msg_id": "old_bot_msg", "reply_to_user_id": "99999"},
                ),
                (
                    "m5",
                    "未解析来源的引用带1个at也不行",
                    {"at_targets": ["99999"], "reply_to_msg_id": "unknown_msg", "reply_to_user_id": None},
                ),
            ]:
                await app.handle_message(
                    IncomingMessage(
                        adapter_id="qq_1",
                        platform="onebot_v11",
                        self_id="99999",
                        scene="group",
                        channel_id="88888",
                        message_id=mid,
                        user_id="10001",
                        sender_name="群友",
                        text=text,
                        timestamp=time.time(),
                        **extra,
                    )
                )
            await asyncio.sleep(0.05)
            self.assertEqual(len(adapter.sent), 0)
            self.assertEqual(runner.ask_count, 0)
            self.assertEqual(runner.judge_count, 0)
            self.assertFalse(app.channel_guard.is_activated("qq_1:group:88888"))

            # 3) Explicit @bot in the group wakes & activates the group!
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="88888",
                    message_id="m6",
                    user_id="10001",
                    sender_name="群友",
                    text="你好呀",
                    at_targets=["99999"],
                    timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            self.assertTrue(app.channel_guard.is_activated("qq_1:group:88888"))
            self.assertEqual(len(adapter.sent), 1)

            # Now subsequent keyword / quote / proactive push works in that activated group!
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="88888",
                    message_id="m7",
                    user_id="10001",
                    sender_name="群友",
                    text="小助手再回一次",
                    timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(adapter.sent), 2)

            # 4) Friend private chat activates private session; non-friend temp private chat is ignored!
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="private",
                    channel_id="77777",
                    message_id="p_tmp",
                    user_id="77777",
                    sender_name="陌生临时会话",
                    text="你好",
                    timestamp=time.time(),
                    raw_event={"sub_type": "group"},
                )
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(adapter.sent), 2)
            self.assertFalse(app.channel_guard.is_activated("qq_1:private:77777"))

            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="private",
                    channel_id="12345",
                    message_id="p_friend",
                    user_id="12345",
                    sender_name="好友",
                    text="私聊你好",
                    timestamp=time.time(),
                    raw_event={"sub_type": "friend"},
                )
            )
            await asyncio.sleep(0.05)
            self.assertTrue(app.channel_guard.is_activated("qq_1:private:12345"))
            self.assertEqual(len(adapter.sent), 3)

            await app.shutdown()

    async def test_group_mute_resets_group_back_to_silent_until_explicit_at_after_unmute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            app, adapter, _ = self._build_app(base)

            # Wire a real OneBotV11Adapter notice handler to verify end-to-end OneBot group_ban event parsing
            ob = OneBotV11Adapter(app.config.platforms[0])
            ob.set_group_ban_handler(app.handle_group_ban)

            # Activate group 88888 by explicit @bot
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="88888",
                    message_id="m1",
                    user_id="10001",
                    sender_name="群友",
                    text="唤醒",
                    at_targets=["99999"],
                    timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            self.assertTrue(app.channel_guard.is_activated("qq_1:group:88888"))
            self.assertEqual(len(adapter.sent), 1)

            # Simulate OneBot v11 notice: bot is muted in group 88888 for 600s!
            await ob._handle_notice_event(
                {
                    "post_type": "notice",
                    "notice_type": "group_ban",
                    "sub_type": "ban",
                    "self_id": 99999,
                    "group_id": 88888,
                    "operator_id": 11111,
                    "user_id": 99999,
                    "duration": 600,
                }
            )
            self.assertFalse(app.channel_guard.is_activated("qq_1:group:88888"))
            self.assertTrue(app.channel_guard.is_muted("qq_1:group:88888"))

            # While muted, even explicit @bot does NOT reply or re-activate
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="88888",
                    message_id="m2",
                    user_id="10001",
                    sender_name="群友",
                    text="禁言期间at",
                    at_targets=["99999"],
                    timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(adapter.sent), 1)
            self.assertFalse(app.channel_guard.is_activated("qq_1:group:88888"))

            # Simulate OneBot v11 notice: lift_ban!
            await ob._handle_notice_event(
                {
                    "post_type": "notice",
                    "notice_type": "group_ban",
                    "sub_type": "lift_ban",
                    "self_id": 99999,
                    "group_id": 88888,
                    "operator_id": 11111,
                    "user_id": 99999,
                    "duration": 0,
                }
            )
            # Unmuted, BUT still deactivated (Point 1 state)!
            self.assertFalse(app.channel_guard.is_muted("qq_1:group:88888"))
            self.assertFalse(app.channel_guard.is_activated("qq_1:group:88888"))

            # After unmute: keyword / dice / quote / proactive STILL do NOT fire!
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="88888",
                    message_id="m3",
                    user_id="10001",
                    sender_name="群友",
                    text="小助手解禁了说话呀",
                    timestamp=time.time(),
                )
            )
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="88888",
                    message_id="m4",
                    user_id="10001",
                    sender_name="群友",
                    text="引用也不行",
                    at_targets=["99999"],
                    reply_to_msg_id="m-1",
                    reply_to_user_id="99999",
                    timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(adapter.sent), 1)

            # Only a fresh explicit @bot after unmute re-activates the group!
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="88888",
                    message_id="m5",
                    user_id="10001",
                    sender_name="群友",
                    text="回来聊吧",
                    at_targets=["99999"],
                    timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            self.assertTrue(app.channel_guard.is_activated("qq_1:group:88888"))
            self.assertEqual(len(adapter.sent), 2)

            await app.shutdown()


if __name__ == "__main__":
    unittest.main()
