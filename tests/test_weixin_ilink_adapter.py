from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

from agy_chat_bot.adapters.registry import AdapterRegistry
from agy_chat_bot.adapters.weixin_ilink import (
    WeixinILinkAdapter,
    _aes128_decrypt_block,
    _aes128_encrypt_block,
    _aes128_expand_key,
    aes128_ecb_decrypt,
    aes128_ecb_encrypt,
    encode_qr_matrix,
    fetch_weixin_qr_code,
    normalize_weixin_api_base,
    parse_ilink_aes_key,
    poll_weixin_qr_status,
    probe_weixin_ilink,
    render_qr_svg_data_uri,
    resolve_weixin_proxy,
)
from agy_chat_bot.app import BotApplication
from agy_chat_bot.config import PlatformConfig, WebUIConfig, load_config_from_dict
from agy_chat_bot.models import IncomingMessage, MediaAttachment
from agy_chat_bot.webui.server import WebUIServer


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


class WeixinCryptoAndQrTest(unittest.TestCase):
    def test_aes128_fips197_vector_and_pkcs7_roundtrip(self) -> None:
        key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        expected_ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
        rks = _aes128_expand_key(key)
        ct = _aes128_encrypt_block(pt, rks)
        self.assertEqual(ct, expected_ct)
        self.assertEqual(_aes128_decrypt_block(ct, rks), pt)

        for payload in (b"", b"a", b"0123456789abcdef", _TINY_PNG):
            enc = aes128_ecb_encrypt(payload, key)
            self.assertEqual(len(enc) % 16, 0)
            self.assertEqual(aes128_ecb_decrypt(enc, key), payload)

    def test_parse_ilink_aes_key_formats(self) -> None:
        raw_key = bytes.fromhex("0123456789abcdef0123456789abcdef")
        self.assertEqual(parse_ilink_aes_key(raw_key.hex()), raw_key)
        self.assertEqual(parse_ilink_aes_key(base64.b64encode(raw_key).decode("ascii")), raw_key)
        self.assertEqual(parse_ilink_aes_key(base64.b64encode(raw_key.hex().encode("ascii")).decode("ascii")), raw_key)
        self.assertIsNone(parse_ilink_aes_key(""))
        self.assertIsNone(parse_ilink_aes_key("invalid!!!"))

    def test_qr_encoder_and_svg_data_uri(self) -> None:
        url = "https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=557651536d54a7a1134a277740cff8fc&bot_type=3"
        mat = encode_qr_matrix(url)
        self.assertEqual(len(mat), 37)  # Version 5 QR
        self.assertTrue(all(len(row) == 37 for row in mat))
        # Dark module check (size - 8, 8)
        self.assertTrue(mat[37 - 8][8])

        uri = render_qr_svg_data_uri(url)
        self.assertTrue(uri.startswith("data:image/svg+xml;base64,"))
        svg = base64.b64decode(uri.split(",", 1)[1]).decode("utf-8")
        self.assertIn("<svg ", svg)
        self.assertIn('viewBox="0 0 45 45"', svg)  # 37 + 4*2 quiet zone
        self.assertIn("<path ", svg)

    def test_helpers_and_registry(self) -> None:
        self.assertEqual(normalize_weixin_api_base(""), "https://ilinkai.weixin.qq.com")
        self.assertEqual(normalize_weixin_api_base("ws://127.0.0.1:3001"), "https://ilinkai.weixin.qq.com")
        self.assertEqual(normalize_weixin_api_base("https://wx.example.com/"), "https://wx.example.com")

        # Defaults to direct domestic connection (does NOT inherit agy proxy unless opted in)
        cfg_default = PlatformConfig(id="wx1", type="weixin_ilink", extra={})
        self.assertEqual(resolve_weixin_proxy(cfg_default, "http://127.0.0.1:7890"), "")
        cfg_optin = PlatformConfig(id="wx1", type="weixin_ilink", extra={"use_agy_proxy": True})
        self.assertEqual(resolve_weixin_proxy(cfg_optin, "http://127.0.0.1:7890"), "http://127.0.0.1:7890")
        cfg_explicit = PlatformConfig(id="wx1", type="weixin_ilink", extra={"proxy": "http://127.0.0.1:10809"})
        self.assertEqual(resolve_weixin_proxy(cfg_explicit, "http://127.0.0.1:7890"), "http://127.0.0.1:10809")

        reg = AdapterRegistry()
        for alias in ("weixin_ilink", "weixin", "wechat", "weixin_oc", "clawbot"):
            aid = f"test_{alias}"
            built = reg.build_adapters([PlatformConfig(id=aid, type=alias, access_token="tok")])
            self.assertIn(aid, built)
            self.assertIsInstance(built[aid], WeixinILinkAdapter)


class WeixinILinkEndToEndTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.seen_headers: list[dict[str, str]] = []
        self.uploaded_blobs: list[bytes] = []
        self.cdn_store: dict[str, bytes] = {}
        self.qr_status_seq = ["wait", "scaned", "confirmed"]
        self.qr_poll_calls = 0
        self.getupdates_calls = 0
        self.expire_next_probe = False

        self.in_aes_key = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
        self.cdn_store["enc_in_img"] = aes128_ecb_encrypt(_TINY_PNG, self.in_aes_key)

        app = web.Application()
        app.router.add_get("/ilink/bot/get_bot_qrcode", self._h_get_qr)
        app.router.add_get("/ilink/bot/get_qrcode_status", self._h_qr_status)
        app.router.add_post("/ilink/bot/getupdates", self._h_getupdates)
        app.router.add_post("/ilink/bot/sendmessage", self._h_sendmessage)
        app.router.add_post("/ilink/bot/getuploadurl", self._h_getuploadurl)
        app.router.add_post("/c2c/upload", self._h_cdn_upload)
        app.router.add_get("/c2c/download", self._h_cdn_download)

        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        self.base_url = f"http://127.0.0.1:{port}"
        self.cdn_url = f"http://127.0.0.1:{port}/c2c"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def _h_get_qr(self, request: web.Request) -> web.Response:
        bt = request.query.get("bot_type", "")
        return web.json_response(
            {
                "ret": 0,
                "qrcode": "qr_token_abc123",
                "qrcode_img_content": f"https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=qr_token_abc123&bot_type={bt}",
            }
        )

    async def _h_qr_status(self, request: web.Request) -> web.Response:
        self.assertEqual(request.headers.get("iLink-App-ClientVersion"), "1")
        idx = min(self.qr_poll_calls, len(self.qr_status_seq) - 1)
        self.qr_poll_calls += 1
        st = self.qr_status_seq[idx]
        if st == "confirmed":
            return web.json_response(
                {
                    "status": "confirmed",
                    "bot_token": "wx_live_bot_token_999",
                    "baseurl": self.base_url,
                    "ilink_bot_id": "bot_999@im.bot",
                    "ilink_user_id": "owner_123@im.wechat",
                }
            )
        return web.json_response({"status": st})

    async def _h_getupdates(self, request: web.Request) -> web.Response:
        self.seen_headers.append(dict(request.headers))
        body = await request.json()
        if self.expire_next_probe:
            return web.json_response({"ret": -14, "errcode": -14, "errmsg": "session expired"})
        self.getupdates_calls += 1
        if self.getupdates_calls == 1 and body.get("get_updates_buf") == "":
            return web.json_response(
                {
                    "ret": 0,
                    "get_updates_buf": "cursor_2",
                    "msgs": [
                        {
                            "message_id": "m_bot_echo",
                            "message_type": 2,
                            "from_user_id": "user_wx_01@im.wechat",
                            "item_list": [{"type": 1, "text_item": {"text": "should be ignored"}}],
                        },
                        {
                            "message_id": "m_001",
                            "message_type": 1,
                            "from_user_id": "user_wx_01@im.wechat",
                            "context_token": "ctx_tok_001",
                            "create_time_ms": 1730000000000,
                            "item_list": [
                                {
                                    "type": 1,
                                    "text_item": {"text": "你好微信"},
                                    "ref_msg": {
                                        "title": "旧消息",
                                        "message_item": {"type": 1, "text_item": {"text": "被引用的正文"}},
                                    },
                                },
                                {
                                    "type": 3,
                                    "voice_item": {"text": "语音内容转写"},
                                },
                                {
                                    "type": 2,
                                    "image_item": {
                                        "media": {"encrypt_query_param": "enc_in_img"},
                                        "aeskey": self.in_aes_key.hex(),
                                    },
                                },
                            ],
                        },
                    ],
                }
            )
        await asyncio.sleep(0.05)
        return web.json_response({"ret": 0, "get_updates_buf": "cursor_2", "msgs": []})

    async def _h_sendmessage(self, request: web.Request) -> web.Response:
        self.seen_headers.append(dict(request.headers))
        body = await request.json()
        self.sent_messages.append(body)
        return web.json_response({"ret": 0})

    async def _h_getuploadurl(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.assertEqual(body.get("media_type"), 1)
        return web.json_response({"ret": 0, "upload_param": "up_param_xyz"})

    async def _h_cdn_upload(self, request: web.Request) -> web.Response:
        blob = await request.read()
        self.uploaded_blobs.append(blob)
        return web.Response(status=200, headers={"x-encrypted-param": "dl_param_987"})

    async def _h_cdn_download(self, request: web.Request) -> web.Response:
        param = request.query.get("encrypted_query_param", "")
        if param in self.cdn_store:
            return web.Response(status=200, body=self.cdn_store[param])
        return web.Response(status=404)

    async def test_qr_login_and_probe(self) -> None:
        qr = await fetch_weixin_qr_code(api_base=self.base_url)
        self.assertTrue(qr["ok"])
        self.assertEqual(qr["qrcode"], "qr_token_abc123")
        self.assertTrue(qr["qrcode_svg_data_uri"].startswith("data:image/svg+xml;base64,"))

        st1 = await poll_weixin_qr_status(qr["qrcode"], api_base=self.base_url)
        st2 = await poll_weixin_qr_status(qr["qrcode"], api_base=self.base_url)
        st3 = await poll_weixin_qr_status(qr["qrcode"], api_base=self.base_url)
        self.assertEqual(st1["status"], "wait")
        self.assertEqual(st2["status"], "scaned")
        self.assertEqual(st3["status"], "confirmed")
        self.assertEqual(st3["bot_token"], "wx_live_bot_token_999")
        self.assertEqual(st3["ilink_bot_id"], "bot_999@im.bot")

        ok_probe = await probe_weixin_ilink("wx_live_bot_token_999", api_base=self.base_url)
        self.assertTrue(ok_probe["ok"])

        self.expire_next_probe = True
        exp_probe = await probe_weixin_ilink("wx_live_bot_token_999", api_base=self.base_url)
        self.assertFalse(exp_probe["ok"])
        self.assertIn("-14", exp_probe["error"])

    async def test_long_poll_inbound_reply_image_roundtrip(self) -> None:
        cfg = PlatformConfig(
            id="wx_main",
            type="weixin_ilink",
            ws_url=self.base_url,
            access_token="wx_live_bot_token_999",
            self_id="bot_999@im.bot",
            extra={"cdn_base_url": self.cdn_url},
        )
        adapter = WeixinILinkAdapter(cfg)
        with tempfile.TemporaryDirectory() as td:
            adapter._media_cache_dir = Path(td)
            received: list[IncomingMessage] = []
            got_event = asyncio.Event()

            async def on_msg(msg: IncomingMessage) -> None:
                received.append(msg)
                got_event.set()

            task = asyncio.create_task(adapter.start(on_msg))
            try:
                await asyncio.wait_for(got_event.wait(), timeout=3.0)
            finally:
                await adapter.stop()
                await asyncio.wait_for(task, timeout=3.0)

            self.assertEqual(len(received), 1)
            msg = received[0]
            self.assertEqual(msg.platform, "weixin_ilink")
            self.assertEqual(msg.scene, "private")
            self.assertEqual(msg.channel_id, "user_wx_01@im.wechat")
            self.assertEqual(msg.reply_to_text, "被引用的正文")
            self.assertIn("你好微信", msg.text)
            self.assertIn("[语音转文字] 语音内容转写", msg.text)
            self.assertEqual(len(msg.attachments), 1)

            # Decrypt inbound CDN image
            resolved = await adapter.resolve_image_source(msg.attachments[0])
            self.assertIn("local_path", resolved)
            saved_bytes = Path(resolved["local_path"]).read_bytes()
            self.assertEqual(saved_bytes, _TINY_PNG)
            self.assertTrue(resolved["local_path"].endswith(".png"))

            # Send text reply & verify headers + context_token echo
            cid = await adapter.send_reply(msg, "收到啦！")
            self.assertIsNotNone(cid)
            self.assertEqual(len(self.sent_messages), 1)
            sent = self.sent_messages[0]["msg"]
            self.assertEqual(sent["to_user_id"], "user_wx_01@im.wechat")
            self.assertEqual(sent["context_token"], "ctx_tok_001")
            self.assertEqual(sent["message_type"], 2)
            self.assertEqual(sent["item_list"][0]["text_item"]["text"], "收到啦！")

            # Verify required iLink headers
            last_hdr = self.seen_headers[-1]
            self.assertEqual(last_hdr.get("AuthorizationType"), "ilink_bot_token")
            self.assertEqual(last_hdr.get("Authorization"), "Bearer wx_live_bot_token_999")
            self.assertTrue(last_hdr.get("X-WECHAT-UIN"))

            # Send encrypted image + caption
            img_cid = await adapter.send_image(msg, _TINY_PNG, caption="附上一张图")
            self.assertIsNotNone(img_cid)
            self.assertEqual(len(self.uploaded_blobs), 1)
            # 1 text reply + 1 image msg + 1 caption text msg
            self.assertEqual(len(self.sent_messages), 3)
            img_item = self.sent_messages[1]["msg"]["item_list"][0]["image_item"]
            self.assertEqual(img_item["media"]["encrypt_query_param"], "dl_param_987")
            # Verify the uploaded CDN blob decrypts back to _TINY_PNG using the sent aes_key
            sent_key = parse_ilink_aes_key(img_item["media"]["aes_key"])
            self.assertIsNotNone(sent_key)
            self.assertEqual(aes128_ecb_decrypt(self.uploaded_blobs[0], sent_key), _TINY_PNG)  # type: ignore[arg-type]
            self.assertEqual(self.sent_messages[2]["msg"]["item_list"][0]["text_item"]["text"], "附上一张图")
            await adapter.stop()

    async def test_webui_weixin_qr_and_test_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config_from_dict({"webui": {"password": ""}}, base_dir=Path(td))
            bot_app = BotApplication(cfg)
            srv = WebUIServer(bot_app, cfg.webui)
            runner = web.AppRunner(srv.web_app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            ui_base = f"http://127.0.0.1:{port}"
            try:
                import aiohttp

                async with aiohttp.ClientSession() as client:
                    async with client.post(
                        f"{ui_base}/api/platforms/weixin/qr-start",
                        json={"api_base": self.base_url},
                    ) as r1:
                        d1 = await r1.json()
                        self.assertEqual(r1.status, 200)
                        self.assertTrue(d1["ok"])
                        self.assertEqual(d1["qrcode"], "qr_token_abc123")

                    # Poll until confirmed
                    d2: dict[str, Any] = {}
                    for _ in range(3):
                        async with client.post(
                            f"{ui_base}/api/platforms/weixin/qr-status",
                            json={"api_base": self.base_url, "qrcode": d1["qrcode"]},
                        ) as r2:
                            d2 = await r2.json()
                    self.assertTrue(d2["ok"])
                    self.assertEqual(d2["status"], "confirmed")
                    self.assertEqual(d2["bot_token"], "wx_live_bot_token_999")

                    async with client.post(
                        f"{ui_base}/api/platforms/test",
                        json={
                            "id": "wx_main",
                            "type": "weixin_ilink",
                            "ws_url": self.base_url,
                            "access_token": d2["bot_token"],
                            "self_id": d2["ilink_bot_id"],
                        },
                    ) as r3:
                        d3 = await r3.json()
                        self.assertEqual(r3.status, 200)
                        self.assertTrue(d3["connected"])
                        self.assertEqual(d3["login_info"]["user_id"], "bot_999@im.bot")
            finally:
                await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
