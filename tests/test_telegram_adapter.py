from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

from agy_bot.adapters.registry import AdapterRegistry
from agy_bot.adapters.telegram import (
    TelegramAdapter,
    normalize_telegram_api_base,
    probe_telegram_bot,
    resolve_telegram_proxy,
)
from agy_bot.channel_guard import is_strict_explicit_at_bot
from agy_bot.config import PlatformConfig, config_to_dict, load_config_from_dict
from agy_bot.models import IncomingMessage, MediaAttachment
from agy_bot.webui.cli_pty import parse_agy_quota_output


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)
_TINY_GIF = b"GIF89a\x01\x00\x01\x00\x00\x00\x00;"


class TelegramHelpersTest(unittest.TestCase):
    def test_normalize_api_base(self) -> None:
        self.assertEqual(normalize_telegram_api_base(""), "https://api.telegram.org")
        self.assertEqual(normalize_telegram_api_base("ws://127.0.0.1:3001"), "https://api.telegram.org")
        self.assertEqual(normalize_telegram_api_base("https://tg.example.com/"), "https://tg.example.com")
        self.assertEqual(normalize_telegram_api_base("tg.example.com"), "https://tg.example.com")

    def test_resolve_proxy_precedence(self) -> None:
        cfg_inherit = PlatformConfig(id="tg1", type="telegram", extra={})
        self.assertEqual(resolve_telegram_proxy(cfg_inherit, "http://127.0.0.1:7890"), "http://127.0.0.1:7890")

        cfg_explicit = PlatformConfig(
            id="tg1",
            type="telegram",
            extra={"proxy": "http://127.0.0.1:10809", "use_agy_proxy": False},
        )
        self.assertEqual(resolve_telegram_proxy(cfg_explicit, "http://127.0.0.1:7890"), "http://127.0.0.1:10809")

        cfg_direct = PlatformConfig(id="tg1", type="telegram", extra={"use_agy_proxy": False})
        self.assertEqual(resolve_telegram_proxy(cfg_direct, "http://127.0.0.1:7890"), "")

    def test_platform_extra_roundtrip_and_unwrap(self) -> None:
        raw = {
            "platforms": [
                {
                    "id": "tg_1",
                    "type": "telegram",
                    "ws_url": "https://api.telegram.org",
                    "access_token": "tok",
                    "extra": {"proxy": "http://127.0.0.1:10809", "use_agy_proxy": False},
                },
                {
                    "id": "tg_legacy_nested",
                    "type": "telegram",
                    "ws_url": "https://api.telegram.org",
                    "access_token": "tok2",
                    "extra": {"extra": {"proxy": "", "use_agy_proxy": True}},
                },
            ]
        }
        cfg = load_config_from_dict(raw, base_dir=Path("."))
        self.assertEqual(cfg.platforms[0].extra, {"proxy": "http://127.0.0.1:10809", "use_agy_proxy": False})
        self.assertEqual(cfg.platforms[1].extra, {"proxy": "", "use_agy_proxy": True})
        dumped = config_to_dict(cfg)
        self.assertEqual(dumped["platforms"][0]["extra"], {"proxy": "http://127.0.0.1:10809", "use_agy_proxy": False})
        reloaded = load_config_from_dict(dumped, base_dir=Path("."))
        self.assertEqual(reloaded.platforms[0].extra, {"proxy": "http://127.0.0.1:10809", "use_agy_proxy": False})

    def test_quota_parser_ignores_slash_login_autocomplete_hint(self) -> None:
        raw = """
  /login  Sign in to Antigravity
  /usage  View model quota
└ Models & Quota

  Account: demo@example.com

GEMINI MODELS
  Models within this group: Gemini 4 Neon
  Weekly Limit Remaining
    [██████░░░░] 53.95%
    Refreshes in 144h 16m
  Five Hour Limit Remaining
    [█████████░] 90.95%
    Refreshes in 4h 49m
"""
        parsed = parse_agy_quota_output(raw)
        self.assertTrue(parsed["ok"])
        self.assertTrue(parsed["signed_in"])
        self.assertEqual(parsed["account"], "demo@example.com")
        self.assertEqual(len(parsed["groups"]), 1)
        self.assertAlmostEqual(parsed["groups"][0]["weekly"]["remaining_pct"], 53.95)


class TelegramParseMessageTest(unittest.TestCase):
    def _make_adapter(self) -> TelegramAdapter:
        cfg = PlatformConfig(id="tg_main", type="telegram", access_token="12345:TEST_TOKEN")
        ad = TelegramAdapter(cfg)
        ad.self_id = "999000"
        ad.bot_username = "MyAgyTestBot"
        return ad

    def test_parse_private_photo_message(self) -> None:
        ad = self._make_adapter()
        raw = {
            "message_id": 101,
            "date": 1700000000,
            "chat": {"id": 424242, "type": "private"},
            "from": {"id": 424242, "is_bot": False, "first_name": "Star", "last_name": "Lin"},
            "caption": "帮我看看这张图",
            "photo": [
                {"file_id": "small_id", "width": 90, "height": 90, "file_size": 1024},
                {"file_id": "large_id", "width": 1280, "height": 720, "file_size": 88000},
            ],
        }
        msg = ad.parse_message(raw)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.scene, "private")
        self.assertEqual(msg.channel_id, "424242")
        self.assertEqual(msg.user_id, "424242")
        self.assertEqual(msg.sender_name, "Star Lin")
        self.assertIn("帮我看看这张图", msg.text)
        self.assertIn("[图片]", msg.text)
        self.assertEqual(len(msg.attachments), 1)
        self.assertEqual(msg.attachments[0].file_id, "large_id")
        self.assertEqual(msg.attachments[0].source, "message")

    def test_parse_group_mention_and_reply(self) -> None:
        ad = self._make_adapter()
        raw = {
            "message_id": 202,
            "date": 1700000010,
            "chat": {"id": -1001234567890, "type": "supergroup", "title": "Test Group"},
            "from": {"id": 778899, "is_bot": False, "first_name": "Alice"},
            "text": "@MyAgyTestBot 解释一下刚才那张图",
            "entities": [{"type": "mention", "offset": 0, "length": 13}],
            "reply_to_message": {
                "message_id": 199,
                "from": {"id": 999000, "is_bot": True, "username": "MyAgyTestBot"},
                "caption": "这是原图说明",
                "photo": [{"file_id": "quoted_pic_id", "width": 800, "height": 600, "file_size": 45000}],
            },
        }
        msg = ad.parse_message(raw)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.scene, "group")
        self.assertEqual(msg.channel_id, "-1001234567890")
        self.assertEqual(msg.at_targets, ["999000"])
        self.assertTrue(is_strict_explicit_at_bot(msg))
        self.assertEqual(msg.reply_to_msg_id, "199")
        self.assertEqual(msg.reply_to_user_id, "999000")
        self.assertIn("这是原图说明", msg.reply_to_text or "")
        self.assertEqual(len(msg.attachments), 1)
        self.assertEqual(msg.attachments[0].file_id, "quoted_pic_id")
        self.assertEqual(msg.attachments[0].source, "reply")

        # Pure reply to bot WITHOUT explicit @bot should count as quote, NOT explicit @bot
        pure_reply = {
            "message_id": 203,
            "date": 1700000011,
            "chat": {"id": -1001234567890, "type": "supergroup"},
            "from": {"id": 778899, "is_bot": False, "first_name": "Alice"},
            "text": "谢谢",
            "reply_to_message": {
                "message_id": 199,
                "from": {"id": 999000, "is_bot": True, "username": "MyAgyTestBot"},
                "text": "不客气",
            },
        }
        qmsg = ad.parse_message(pure_reply)
        self.assertIsNotNone(qmsg)
        assert qmsg is not None
        self.assertTrue(qmsg.is_quoted_bot)
        self.assertEqual(qmsg.explicit_at_bot_count, 0)
        self.assertFalse(is_strict_explicit_at_bot(qmsg))

    def test_ignore_other_bots_and_parse_sticker_voice(self) -> None:
        ad = self._make_adapter()
        other_bot = {
            "message_id": 300,
            "chat": {"id": -1001, "type": "group"},
            "from": {"id": 555, "is_bot": True, "first_name": "OtherBot"},
            "text": "hello",
        }
        self.assertIsNone(ad.parse_message(other_bot))

        sticker_voice = {
            "message_id": 301,
            "chat": {"id": 111, "type": "private"},
            "from": {"id": 111, "is_bot": False, "username": "solo_user"},
            "sticker": {"file_id": "stk_001", "emoji": "😄", "is_animated": False, "is_video": False},
            "voice": {"file_id": "voc_001"},
        }
        msg = ad.parse_message(sticker_voice)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.sender_name, "@solo_user")
        kinds = [a.kind for a in msg.attachments]
        self.assertEqual(kinds, ["image", "audio"])


class TelegramMockServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.recorded_calls: list[tuple[str, Any]] = []
        self.updates_served = False

        async def handle_bot_api(request: web.Request) -> web.Response:
            token = request.match_info["token"]
            method = request.match_info["method"]
            if token != "123456:VALID_TOKEN":
                return web.json_response({"ok": False, "description": "Unauthorized"}, status=401)

            if request.content_type.startswith("multipart/"):
                reader = await request.multipart()
                fields: dict[str, Any] = {}
                while True:
                    part = await reader.next()
                    if part is None:
                        break
                    data = await part.read()
                    fields[part.name or ""] = data if part.filename else data.decode("utf-8", errors="replace")
                self.recorded_calls.append((method, fields))
                return web.json_response({"ok": True, "result": {"message_id": 888}})

            body: dict[str, Any] = {}
            if request.can_read_body:
                try:
                    body = await request.json()
                except Exception:
                    body = {}
            self.recorded_calls.append((method, body))

            if method == "getMe":
                return web.json_response(
                    {
                        "ok": True,
                        "result": {
                            "id": 123456,
                            "is_bot": True,
                            "first_name": "Agy TG Bot",
                            "username": "agy_local_test_bot",
                            "can_read_all_group_messages": True,
                        },
                    }
                )
            if method == "deleteWebhook":
                return web.json_response({"ok": True, "result": True})
            if method == "getFile":
                return web.json_response({"ok": True, "result": {"file_id": body.get("file_id"), "file_path": "photos/test.png"}})
            if method == "sendMessage":
                return web.json_response({"ok": True, "result": {"message_id": 777}})
            if method == "getUpdates":
                if not self.updates_served:
                    self.updates_served = True
                    return web.json_response(
                        {
                            "ok": True,
                            "result": [
                                {
                                    "update_id": 5001,
                                    "message": {
                                        "message_id": 10,
                                        "date": 1700000100,
                                        "chat": {"id": 654321, "type": "private"},
                                        "from": {"id": 654321, "is_bot": False, "first_name": "SoloTester"},
                                        "text": "你好 Telegram 机器人",
                                    },
                                }
                            ],
                        }
                    )
                await asyncio.sleep(0.05)
                return web.json_response({"ok": True, "result": []})
            return web.json_response({"ok": False, "description": f"unknown method {method}"}, status=400)

        async def handle_file_download(request: web.Request) -> web.Response:
            return web.Response(body=_TINY_PNG, content_type="image/png")

        app = web.Application()
        app.router.add_route("*", "/bot{token}/{method}", handle_bot_api)
        app.router.add_get("/file/bot{token}/{tail:.*}", handle_file_download)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base_url = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def test_probe_telegram_bot_success_and_error(self) -> None:
        ok_res = await probe_telegram_bot("123456:VALID_TOKEN", api_base=self.base_url, timeout_seconds=5.0)
        self.assertTrue(ok_res["ok"])
        self.assertEqual(ok_res["user_id"], "123456")
        self.assertEqual(ok_res["username"], "agy_local_test_bot")
        self.assertTrue(ok_res["can_read_all_group_messages"])

        bad_res = await probe_telegram_bot("123456:BAD", api_base=self.base_url, timeout_seconds=5.0)
        self.assertFalse(bad_res["ok"])
        self.assertIn("401", bad_res["error"])

    async def test_resolve_image_send_reply_and_send_image(self) -> None:
        cfg = PlatformConfig(
            id="tg_test",
            type="telegram",
            ws_url=self.base_url,
            access_token="123456:VALID_TOKEN",
            extra={"use_agy_proxy": False},
        )
        ad = TelegramAdapter(cfg)
        try:
            resolved = await ad.resolve_image_source(MediaAttachment(kind="image", file_id="fid_999"))
            self.assertIn("base64", resolved)
            self.assertEqual(base64.b64decode(resolved["base64"]), _TINY_PNG)
            self.assertTrue(resolved["url"].endswith("/file/bot123456:VALID_TOKEN/photos/test.png"))

            # Long message should chunk cleanly and attach reply_parameters on first chunk
            priv_target = IncomingMessage(
                adapter_id="tg_test",
                platform="telegram",
                self_id="123456",
                scene="private",
                channel_id="654321",
                message_id="10",
                user_id="654321",
                sender_name="SoloTester",
                text="hi",
                timestamp=1700000100.0,
            )
            grp_target = IncomingMessage(
                adapter_id="tg_test",
                platform="telegram",
                self_id="123456",
                scene="group",
                channel_id="-10099",
                message_id="12",
                user_id="654321",
                sender_name="SoloTester",
                text="hi",
                timestamp=1700000101.0,
            )
            long_text = "A" * 4500
            mid = await ad.send_reply(priv_target, long_text, quote_trigger=True)
            self.assertEqual(mid, "777")
            send_calls = [body for m, body in self.recorded_calls if m == "sendMessage"]
            self.assertEqual(len(send_calls), 2)
            self.assertEqual(send_calls[0]["reply_parameters"]["message_id"], 10)
            self.assertNotIn("reply_parameters", send_calls[1])

            with tempfile.TemporaryDirectory() as td:
                png_path = Path(td) / "sample.png"
                gif_path = Path(td) / "anim.gif"
                png_path.write_bytes(_TINY_PNG)
                gif_path.write_bytes(_TINY_GIF)

                img_mid = await ad.send_image(grp_target, str(png_path), quote_trigger=True)
                gif_mid = await ad.send_image(grp_target, str(gif_path), quote_trigger=False)
                self.assertEqual(img_mid, "888")
                self.assertEqual(gif_mid, "888")

            methods = [m for m, _ in self.recorded_calls]
            self.assertIn("sendPhoto", methods)
            self.assertIn("sendAnimation", methods)
        finally:
            await ad.stop()

    async def test_polling_loop_and_registry_hot_sync(self) -> None:
        received: list[Any] = []
        done = asyncio.Event()

        async def on_msg(msg: Any) -> None:
            received.append(msg)
            done.set()

        reg = AdapterRegistry()
        # Start with one disabled dummy onebot client
        onebot_cfg = PlatformConfig(id="qq_1", type="onebot_v11", enabled=False)
        tg_cfg = PlatformConfig(
            id="tg_hot",
            type="telegram",
            enabled=True,
            ws_url=self.base_url,
            access_token="123456:VALID_TOKEN",
            extra={"use_agy_proxy": False, "poll_timeout_seconds": 5},
        )
        reg.build_adapters([onebot_cfg])
        runner_task = asyncio.create_task(reg.run_all(on_msg))
        try:
            await asyncio.sleep(0.05)
            await reg.sync_adapters([onebot_cfg, tg_cfg])
            await asyncio.wait_for(done.wait(), timeout=3.0)
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].platform, "telegram")
            self.assertEqual(received[0].text, "你好 Telegram 机器人")
            st = reg.get_status()
            self.assertEqual(len(st), 1)
            self.assertTrue(st[0]["connected"])
            self.assertEqual(st[0]["bot_username"], "agy_local_test_bot")

            # Hot-remove Telegram adapter
            await reg.sync_adapters([onebot_cfg])
            self.assertEqual(len(reg.adapters), 0)
        finally:
            await reg.stop_all()
            runner_task.cancel()
            try:
                await runner_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    unittest.main()
