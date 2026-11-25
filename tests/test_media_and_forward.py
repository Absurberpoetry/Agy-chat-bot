from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_bot.adapters.onebot_v11 import OneBotV11Adapter  # noqa: E402
from agy_bot.app import BotApplication, format_context_prompt  # noqa: E402
from agy_bot.collector.context_store import GroupContextStore  # noqa: E402
from agy_bot.config import BotConfig, ModelProfileConfig, PlatformConfig  # noqa: E402
from agy_bot.media import detect_image_ext, materialize_image, materialize_snapshot_images  # noqa: E402
from agy_bot.models import ContextSnapshot, IncomingMessage, MediaAttachment, RunnerResult  # noqa: E402
from agy_bot.runner.agy_stream import AgyRunner  # noqa: E402

# Tiny valid 1x1 PNG & 1x1 GIF & fake JPEG header bytes
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
_GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
_JPG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
_WEBP_BYTES = b"RIFF\x14\x00\x00\x00WEBPVP8 \x08\x00\x00\x00\x00\x00\x00\x00"


class _FakeWS:
    def __init__(self, inbound_frames: list[str], on_send=None) -> None:
        self._inbound: asyncio.Queue[str | None] = asyncio.Queue()
        for f in inbound_frames:
            self._inbound.put_nowait(f)
        self.sent: list[dict[str, Any]] = []
        self._on_send = on_send

    def push_frame(self, payload: dict[str, Any]) -> None:
        self._inbound.put_nowait(json.dumps(payload, ensure_ascii=False))

    def close_stream(self) -> None:
        self._inbound.put_nowait(None)

    async def send(self, data: str) -> None:
        obj = json.loads(data)
        self.sent.append(obj)
        if self._on_send is not None:
            self._on_send(self, obj)

    async def close(self) -> None:
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


class _FakeRunner:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    async def ask(self, session_key: str, prompt: str, **kwargs) -> RunnerResult:
        self.prompts.append(prompt)
        return RunnerResult(ok=True, response="收到图片和聊天记录了")

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]):
        from agy_bot.models import JudgeDecision

        return JudgeDecision(should_reply=False)

    async def reset_sessions(self, session_key: str | None = None) -> int:
        return 0

    async def update_config(self, *args, **kwargs) -> None:
        return None

    async def close(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        return {"model": "test-model", "warm_sessions": [], "circuit_open": False}


class MediaAndForwardTests(unittest.IsolatedAsyncioTestCase):
    def test_detect_image_ext_magic_bytes(self) -> None:
        self.assertEqual(detect_image_ext(_PNG_BYTES), ".png")
        self.assertEqual(detect_image_ext(_GIF_BYTES), ".gif")
        self.assertEqual(detect_image_ext(_JPG_BYTES), ".jpg")
        self.assertEqual(detect_image_ext(_WEBP_BYTES), ".webp")
        self.assertEqual(detect_image_ext(b"random", content_type="image/png"), ".png")
        self.assertEqual(detect_image_ext(b"random", hint_name="photo.jpeg?rkey=1"), ".jpg")

    async def test_unrestricted_nested_and_large_forward_with_cycle_guard(self) -> None:
        store = GroupContextStore()
        cfg = PlatformConfig(id="qq_1", type="onebot_v11", ws_url="ws://localhost:3001", self_id="999999")
        ws_holder: dict[str, _FakeWS] = {}

        # 120 nodes inside fwd_big to prove ZERO count cap!
        big_nodes = [
            {
                "type": "node",
                "data": {
                    "uin": str(10000 + i),
                    "name": f"群友{i}",
                    "time": 1700000000 + i,
                    "content": [{"type": "text", "data": {"text": f"第{i}条转发消息"}}],
                },
            }
            for i in range(1, 121)
        ]

        # Multi-level nested forward (level1 -> level2 -> level3 + image + cycle back to level1)
        level3_nodes = [
            {
                "sender": {"user_id": 3001, "card": "深层发言人"},
                "time": 1700000300,
                "message": [
                    {"type": "text", "data": {"text": "最底层消息 "}},
                    {"type": "image", "data": {"file": "deep.png", "url": "https://example.invalid/deep.png"}},
                    {"type": "forward", "data": {"id": "fwd_level1"}},  # cycle back!
                ],
            }
        ]
        level2_nodes = [
            {
                "type": "node",
                "data": {
                    "user_id": 2001,
                    "nickname": "中层群友",
                    "content": [
                        {"type": "text", "data": {"text": "看看里面："}},
                        {"type": "forward", "data": {"id": "fwd_level3"}},
                    ],
                },
            }
        ]
        level1_nodes = [
            {
                "sender": {"user_id": 1001, "nickname": "外层群友"},
                "message": [
                    {"type": "text", "data": {"text": "第一层附带图片 "}},
                    {"type": "image", "data": {"file": "outer.jpg", "url": "https://example.invalid/outer.jpg"}},
                    {"type": "forward", "data": {"id": "fwd_level2"}},
                ],
            }
        ]

        def on_send(ws: _FakeWS, pkt: dict[str, Any]) -> None:
            action = pkt.get("action")
            params = pkt.get("params", {})
            if action == "get_forward_msg":
                # Test fallback to message_id for fwd_big
                if params.get("id") == "fwd_big":
                    ws.push_frame({"status": "failed", "retcode": 100, "echo": pkt["echo"], "data": None})
                    return
                fid = str(params.get("id") or params.get("message_id") or "")
                mapping = {
                    "fwd_big": big_nodes,
                    "fwd_level1": level1_nodes,
                    "fwd_level2": level2_nodes,
                    "fwd_level3": level3_nodes,
                }
                if fid in mapping:
                    ws.push_frame({
                        "status": "ok",
                        "retcode": 0,
                        "echo": pkt["echo"],
                        "data": {"messages": mapping[fid]},
                    })
                    return
            elif action == "get_msg":
                if str(params.get("message_id")) == "quoted_fwd_msg":
                    ws.push_frame({
                        "status": "ok",
                        "retcode": 0,
                        "echo": pkt["echo"],
                        "data": {
                            "message_id": "quoted_fwd_msg",
                            "sender": {"user_id": 5555, "nickname": "原消息作者"},
                            "message": [
                                {"type": "forward", "data": {"id": "fwd_level1"}},
                            ],
                        },
                    })
                    return
            ws.push_frame({"status": "failed", "retcode": -1, "echo": pkt["echo"], "data": None})

        events = [
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 999999,
                "group_id": 10001,
                "user_id": 11111,
                "message_id": "m_big",
                "sender": {"card": "小明"},
                "message": [{"type": "forward", "data": {"id": "fwd_big"}}],
            }),
            json.dumps({
                "post_type": "message",
                "message_type": "group",
                "self_id": 999999,
                "group_id": 10001,
                "user_id": 11111,
                "message_id": "m_nested",
                "sender": {"card": "小明"},
                "message": [
                    {"type": "reply", "data": {"id": "quoted_fwd_msg"}},
                    {"type": "at", "data": {"qq": "999999"}},
                    {"type": "text", "data": {"text": " 帮我总结这段转发记录和图片"}},
                ],
            }),
        ]

        ws = _FakeWS(events, on_send=on_send)
        ws_holder["ws"] = ws
        adapter = OneBotV11Adapter(
            cfg,
            message_lookup=store.lookup_message,
            ws_connect=lambda url, **kw: ws_holder["ws"],
        )

        received: list[IncomingMessage] = []
        done = asyncio.Event()

        async def on_msg(msg: IncomingMessage) -> None:
            store.add_message(msg)
            received.append(msg)
            if len(received) == 2:
                done.set()

        task = asyncio.create_task(adapter.start(on_msg))
        await asyncio.wait_for(done.wait(), timeout=5.0)
        await adapter.stop()
        await asyncio.wait_for(task, timeout=2.0)

        # 1) Verify ALL 120 messages in fwd_big were expanded without truncation
        big_msg = received[0]
        self.assertIn("[合并转发聊天记录 开始]", big_msg.text)
        self.assertIn("第1条转发消息", big_msg.text)
        self.assertIn("第60条转发消息", big_msg.text)
        self.assertIn("第120条转发消息", big_msg.text)
        self.assertIn("[合并转发聊天记录 结束]", big_msg.text)

        # 2) Verify quoted nested 3-level forward + cycle guard + images inside quoted forward
        nested_msg = received[1]
        self.assertEqual(nested_msg.reply_to_user_id, "5555")
        self.assertIsNotNone(nested_msg.reply_to_text)
        assert nested_msg.reply_to_text is not None
        self.assertIn("外层群友(1001)", nested_msg.reply_to_text)
        self.assertIn("中层群友(2001)", nested_msg.reply_to_text)
        self.assertIn("深层发言人(3001)", nested_msg.reply_to_text)
        self.assertIn("最底层消息", nested_msg.reply_to_text)
        self.assertIn("[合并转发消息(循环引用已跳过)]", nested_msg.reply_to_text)
        # Both images from level 1 and level 3 extracted as reply attachments
        self.assertEqual(len(nested_msg.attachments), 2)
        self.assertEqual([a.file_id for a in nested_msg.attachments], ["outer.jpg", "deep.png"])
        self.assertTrue(all(a.source == "reply" for a in nested_msg.attachments))

    async def test_media_download_http_local_base64_rpc_and_prompt_injection(self) -> None:
        # Spin up a tiny local HTTP server serving extensionless PNG/JPG endpoints
        async def handle_img(request: web.Request) -> web.Response:
            idx = request.match_info["idx"]
            payload = _PNG_BYTES if int(idx) % 2 == 0 else _JPG_BYTES
            return web.Response(body=payload, content_type="application/octet-stream")

        http_app = web.Application()
        http_app.router.add_get("/download/{idx}", handle_img)
        runner = web.AppRunner(http_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        try:
            with tempfile.TemporaryDirectory() as tmp:
                ws_dir = Path(tmp) / "bot_workspace"
                ws_dir.mkdir(parents=True, exist_ok=True)

                # Local file source (simulating NapCat local cache)
                napcat_local = Path(tmp) / "napcat_cached.gif"
                napcat_local.write_bytes(_GIF_BYTES)

                # Adapter stub with get_image RPC fallback
                class _StubAdapter:
                    async def resolve_image_source(self, att: MediaAttachment) -> dict[str, str]:
                        if att.file_id == "rpc_only_file":
                            return {"url": f"http://127.0.0.1:{port}/download/2"}
                        return {}

                # Build 15 images in one message (proving NO count limit!) covering HTTP, local, base64, RPC
                atts: list[MediaAttachment] = [
                    MediaAttachment(kind="image", url=f"http://127.0.0.1:{port}/download/{i}", source="message")
                    for i in range(12)
                ]
                atts.append(MediaAttachment(kind="image", source_path=str(napcat_local), source="message"))
                atts.append(
                    MediaAttachment(
                        kind="image",
                        file_id="base64://" + base64.b64encode(_WEBP_BYTES).decode("ascii"),
                        source="message",
                    )
                )
                atts.append(MediaAttachment(kind="image", file_id="rpc_only_file", source="message"))

                msg = IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="999",
                    scene="group",
                    channel_id="10001",
                    message_id="m_15imgs",
                    user_id="12345",
                    sender_name="阿星",
                    text=" ".join(["[图片]"] * 15),
                    timestamp=1700000500.0,
                    reply_to_msg_id="m_prev",
                    reply_to_user_id="88888",
                    reply_to_text="[合并转发聊天记录 开始]\n张三(1): " + ("这是一段很长不会被截断的引用内容" * 8) + " [图片]\n[合并转发聊天记录 结束]",
                    attachments=[
                        MediaAttachment(kind="image", url=f"http://127.0.0.1:{port}/download/0", source="reply"),
                        *atts,
                    ],
                )

                paths = await materialize_snapshot_images(
                    [msg],
                    workspace_dir=ws_dir,
                    adapter=_StubAdapter(),
                )
                # All 16 images (1 reply + 15 message) materialized!
                self.assertEqual(len(paths), 16)
                for rel in paths:
                    self.assertTrue(rel.startswith(".media_cache/img_"))
                    self.assertTrue((ws_dir / rel).is_file())

                # Verify extensions detected accurately from magic bytes
                self.assertTrue(msg.attachments[-3].local_path.endswith(".gif"))
                self.assertTrue(msg.attachments[-2].local_path.endswith(".webp"))
                self.assertTrue(msg.attachments[-1].local_path.endswith(".png"))

                snap = ContextSnapshot(
                    session_key=msg.session_key,
                    adapter_id=msg.adapter_id,
                    platform=msg.platform,
                    scene=msg.scene,
                    channel_id=msg.channel_id,
                    primary_trigger="mention",
                    matched_triggers=["mention"],
                    trigger_reason="at",
                    trigger_message=msg,
                    tracked_user_ids=[msg.user_id],
                    extra_messages_collected=0,
                    settled_at=1700000501.0,
                    messages=[msg],
                    trigger_message_ids={msg.message_id},
                )

                # Prompt with vision enabled: includes [图片附件说明], .media_cache paths, and full untruncated quote
                prompt_on = format_context_prompt(snap, "安挽行")
                self.assertIn("[图片附件说明]", prompt_on)
                self.assertIn("read_file", prompt_on)
                self.assertIn(".media_cache/img_", prompt_on)
                self.assertIn("这是一段很长不会被截断的引用内容" * 8, prompt_on)
                self.assertNotIn("[图片已忽略]", prompt_on)

                # Prompt with vision disabled: replaces [图片] with [图片已忽略] and omits [图片附件说明]
                profile_off = ModelProfileConfig(enable_vision=False)
                prompt_off = format_context_prompt(snap, "安挽行", model_profile=profile_off)
                self.assertNotIn("[图片附件说明]", prompt_off)
                self.assertIn("[图片已忽略]", prompt_off)
                self.assertNotIn(".media_cache/img_", prompt_off)
        finally:
            await runner.cleanup()

    async def test_agy_runner_settings_allow_media_cache_when_vision_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "bot_workspace"
            data = base / "data"
            ws.mkdir()
            data.mkdir()
            base_settings = base / "settings.json"
            base_settings.write_text(
                json.dumps({"permissions": {"deny": ["command(*)", "read_file(*)"], "allow": []}}),
                encoding="utf-8",
            )
            cfg = BotConfig()
            runner = AgyRunner(
                cfg.agy,
                cfg.rate_limit,
                workspace_dir=ws,
                data_dir=data,
                settings_file=base_settings,
            )

            p_on = ModelProfileConfig(enable_vision=True)
            f_on = runner._resolve_model_settings_file("gemini-3.8-flash", p_on)
            assert f_on is not None
            d_on = json.loads(f_on.read_text(encoding="utf-8"))
            self.assertNotIn("read_file(*)", d_on["permissions"]["deny"])
            self.assertIn("read_file(.media_cache/*)", d_on["permissions"]["allow"])

            p_off = ModelProfileConfig(enable_vision=False)
            f_off = runner._resolve_model_settings_file("gemini-3.8-flash", p_off)
            assert f_off is not None
            d_off = json.loads(f_off.read_text(encoding="utf-8"))
            self.assertIn("read_file(*)", d_off["permissions"]["deny"])
            self.assertNotIn("read_file(.media_cache/*)", d_off["permissions"]["allow"])

    async def test_bot_application_end_to_end_downloads_images_before_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(base / "bot_workspace")
            cfg.agy.data_dir = str(base / "data")
            cfg.collector.quiet_wait_seconds = 0.05
            cfg.collector.max_wait_seconds = 0.1
            cfg.platforms = [PlatformConfig(id="qq_1", type="onebot_v11", ws_url="ws://127.0.0.1:3001", self_id="999")]

            fake_runner = _FakeRunner()
            app = BotApplication(cfg, runner=fake_runner)  # type: ignore[arg-type]

            sent_replies: list[str] = []

            class _MockAdapter:
                adapter_id = "qq_1"
                platform_name = "onebot_v11"
                self_id = "999"
                config = cfg.platforms[0]

                def is_channel_allowed(self, scene, cid, uid):
                    return True

                async def resolve_image_source(self, att):
                    return {"base64": base64.b64encode(_PNG_BYTES).decode("ascii")}

                async def send_reply(self, target, text, *, quote_trigger=True, at_user=False):
                    sent_replies.append(text)
                    return "sent_1"

            app.registry._adapters["qq_1"] = _MockAdapter()  # type: ignore[assignment]

            msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="999",
                scene="group",
                channel_id="10001",
                message_id="m_e2e",
                user_id="12345",
                sender_name="阿星",
                text="[@机器人] 帮我看下这张图 [图片]",
                timestamp=1700000900.0,
                at_targets=["999"],
                attachments=[
                    MediaAttachment(kind="image", file_id="napcat_img_001", source="message"),
                ],
            )
            await app.handle_message(msg)
            await asyncio.sleep(0.2)

            self.assertEqual(len(fake_runner.prompts), 1)
            prompt = fake_runner.prompts[0]
            self.assertIn("[图片附件说明]", prompt)
            self.assertIn(".media_cache/img_", prompt)
            self.assertTrue((base / "bot_workspace" / msg.attachments[0].local_path).is_file())
            self.assertEqual(sent_replies, ["收到图片和聊天记录了"])

    def test_sanitize_reference_image_strips_android_exif_and_iccp_chunks(self) -> None:
        import struct
        from agy_bot.media import sanitize_reference_image

        def _chunk(ctype: bytes, data: bytes) -> bytes:
            return struct.pack(">I", len(data)) + ctype + data + b"\x00\x00\x00\x00"

        dirty_png = (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", b"\x00" * 13)
            + _chunk(b"eXIf", b"MM\x00*AndroidSkia")
            + _chunk(b"iCCP", b"SkiaProfile")
            + _chunk(b"sBIT", b"\x08\x08\x08")
            + _chunk(b"IDAT", b"compressed")
            + _chunk(b"IEND", b"")
        )
        clean_png, ext_png = sanitize_reference_image(dirty_png, hint_name="fake.jpg")
        self.assertEqual(ext_png, ".png")
        self.assertNotIn(b"eXIf", clean_png)
        self.assertNotIn(b"iCCP", clean_png)
        self.assertNotIn(b"sBIT", clean_png)
        self.assertIn(b"IHDR", clean_png)
        self.assertIn(b"IDAT", clean_png)
        self.assertIn(b"IEND", clean_png)

        exif_payload = b"Exif\x00\x00MM\x00*"
        dirty_jpg = (
            b"\xff\xd8"
            + b"\xff\xe1" + struct.pack(">H", len(exif_payload) + 2) + exif_payload
            + b"\xff\xda\x00\x08\x01\x02\x03\xff\xd9"
        )
        clean_jpg, ext_jpg = sanitize_reference_image(dirty_jpg, hint_name="photo.jpg")
        self.assertEqual(ext_jpg, ".jpg")
        self.assertNotIn(b"Exif", clean_jpg)
        self.assertIn(b"JFIF", clean_jpg)
        self.assertTrue(clean_jpg.endswith(b"\xff\xd9"))


if __name__ == "__main__":
    unittest.main()
