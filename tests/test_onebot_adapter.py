from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_chat_bot.adapters.onebot_v11 import OneBotV11Adapter, parse_onebot_segments  # noqa: E402
from agy_chat_bot.collector.context_store import GroupContextStore  # noqa: E402
from agy_chat_bot.config import PlatformConfig  # noqa: E402
from agy_chat_bot.models import IncomingMessage  # noqa: E402


class FakeWebSocket:
    def __init__(self, inbound_frames: list[str], on_send=None) -> None:
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()
        for f in inbound_frames:
            self._inbound.put_nowait(f)
        self.sent: list[dict] = []
        self._on_send = on_send
        self.closed = False

    def push_frame(self, payload: dict) -> None:
        self._inbound.put_nowait(json.dumps(payload, ensure_ascii=False))

    def close_stream(self) -> None:
        self._inbound.put_nowait(None)

    async def send(self, data: str) -> None:
        obj = json.loads(data)
        self.sent.append(obj)
        if self._on_send is not None:
            self._on_send(self, obj)

    async def close(self) -> None:
        self.closed = True
        self.close_stream()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._inbound.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class OneBotAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_cq_and_array_segments(self) -> None:
        segs_cq = parse_onebot_segments("[CQ:reply,id=100][CQ:at,qq=999] hello &#91;world&#93;")
        self.assertEqual([s.type for s in segs_cq], ["reply", "at", "text"])
        self.assertEqual(segs_cq[0].data["id"], "100")
        self.assertEqual(segs_cq[1].data["qq"], "999")
        self.assertEqual(segs_cq[2].data["text"], " hello [world]")

        segs_arr = parse_onebot_segments([
            {"type": "at", "data": {"qq": 999}},
            {"type": "text", "data": {"text": " hi"}},
        ])
        self.assertEqual(len(segs_arr), 2)
        self.assertEqual(segs_arr[0].data["qq"], 999)

    async def test_quote_bot_vs_mention_bot_with_rpc_lookup(self) -> None:
        store = GroupContextStore()
        cfg = PlatformConfig(id="qq_1", type="onebot_v11", ws_url="ws://localhost:3001", self_id="999999")

        ws_holder: dict[str, FakeWebSocket] = {}

        def on_send(ws: FakeWebSocket, pkt: dict) -> None:
            if pkt.get("action") == "get_msg":
                msg_id = str(pkt["params"]["message_id"])
                if msg_id == "bot_msg_1":
                    ws.push_frame({
                        "status": "ok",
                        "retcode": 0,
                        "echo": pkt["echo"],
                        "data": {
                            "message_id": "bot_msg_1",
                            "sender": {"user_id": 999999, "nickname": "Bot"},
                            "message": [{"type": "text", "data": {"text": "我之前的回答"}}],
                        },
                    })
                elif msg_id == "other_msg_1":
                    ws.push_frame({
                        "status": "ok",
                        "retcode": 0,
                        "echo": pkt["echo"],
                        "data": {
                            "message_id": "other_msg_1",
                            "sender": {"user_id": 777777, "nickname": "路人"},
                            "message": [{"type": "text", "data": {"text": "路人的观点"}}],
                        },
                    })
            elif pkt.get("action") == "send_group_msg":
                ws.push_frame({
                    "status": "ok",
                    "retcode": 0,
                    "echo": pkt["echo"],
                    "data": {"message_id": 888001},
                })

        adapter = OneBotV11Adapter(
            cfg,
            message_lookup=store.lookup_message,
            ws_connect=lambda url, **kw: ws_holder["ws"],
        )

        # Simulate:
        # 1) Pure @bot
        # 2) Quote bot message (QQ injects reply + @bot automatically)
        # 3) Quote someone else + manually @bot
        # 4) Quote bot + ALSO manually @bot a second time
        events = [
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 999999,
                "group_id": 10001,
                "user_id": 11111,
                "message_id": "m1",
                "sender": {"card": "小明"},
                "message": [
                    {"type": "at", "data": {"qq": "999999"}},
                    {"type": "text", "data": {"text": " 帮我看下"}},
                ],
            }),
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 999999,
                "group_id": 10001,
                "user_id": 11111,
                "message_id": "m2",
                "sender": {"card": "小明"},
                "message": [
                    {"type": "reply", "data": {"id": "bot_msg_1"}},
                    {"type": "at", "data": {"qq": "999999"}},
                    {"type": "text", "data": {"text": " 展开说说"}},
                ],
            }),
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 999999,
                "group_id": 10001,
                "user_id": 22222,
                "message_id": "m3",
                "sender": {"nickname": "小红"},
                "message": [
                    {"type": "reply", "data": {"id": "other_msg_1"}},
                    {"type": "at", "data": {"qq": "999999"}},
                    {"type": "text", "data": {"text": " 你怎么看"}},
                ],
            }),
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 999999,
                "group_id": 10001,
                "user_id": 11111,
                "message_id": "m4",
                "sender": {"card": "小明"},
                "message": [
                    {"type": "reply", "data": {"id": "bot_msg_1"}},
                    {"type": "at", "data": {"qq": "999999"}},
                    {"type": "at", "data": {"qq": "999999"}},
                    {"type": "text", "data": {"text": " 双重提醒"}},
                ],
            }),
        ]

        ws = FakeWebSocket(events, on_send=on_send)
        ws_holder["ws"] = ws

        received: list[IncomingMessage] = []
        done = asyncio.Event()

        async def on_msg(msg: IncomingMessage) -> None:
            received.append(msg)
            if len(received) == 4:
                done.set()

        run_task = asyncio.create_task(adapter.start(on_msg))
        await asyncio.wait_for(done.wait(), timeout=3.0)

        sent_id = await adapter.send_reply(received[0], "测试回复", quote_trigger=True)
        self.assertEqual(sent_id, "888001")

        await adapter.stop()
        await asyncio.wait_for(run_task, timeout=2.0)

        # Verify m1: pure @bot
        self.assertFalse(received[0].is_quoted_bot)
        self.assertEqual(received[0].explicit_at_bot_count, 1)

        # Verify m2: quote bot -> QQ's auto @bot is excluded from explicit_at_bot_count!
        self.assertTrue(received[1].is_quoted_bot)
        self.assertEqual(received[1].at_bot_count, 1)
        self.assertEqual(received[1].explicit_at_bot_count, 0)
        self.assertEqual(received[1].reply_to_text, "我之前的回答")
        self.assertEqual(received[1].text, "展开说说")

        # Verify m3: quote other + @bot -> is_quoted_bot=False, explicit_at_bot_count=1!
        self.assertFalse(received[2].is_quoted_bot)
        self.assertEqual(received[2].reply_to_user_id, "777777")
        self.assertEqual(received[2].explicit_at_bot_count, 1)

        # Verify m4: quote bot + extra manual @bot -> both true!
        self.assertTrue(received[3].is_quoted_bot)
        self.assertEqual(received[3].explicit_at_bot_count, 1)


if __name__ == "__main__":
    unittest.main()
