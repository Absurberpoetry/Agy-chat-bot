from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_chat_bot.adapters.onebot_v11 import OneBotV11Adapter  # noqa: E402
from agy_chat_bot.app import BotApplication  # noqa: E402
from agy_chat_bot.config import BotConfig, PlatformConfig  # noqa: E402
from agy_chat_bot.models import IncomingMessage, JudgeDecision, RunnerResult  # noqa: E402

_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)
_GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"


class _FakeRunner:
    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    async def ask(self, session_key: str, prompt: str, **kwargs) -> RunnerResult:
        return RunnerResult(ok=True, response="ok")

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        return JudgeDecision(should_reply=False)

    async def reset_sessions(self, session_key: str | None = None) -> int:
        return 0

    async def update_config(self, *args, **kwargs) -> None:
        return None

    async def close(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        return {"model": "test-model", "warm_sessions": [], "circuit_open": False}


class ImageGifPluginTests(unittest.IsolatedAsyncioTestCase):
    async def test_draw_and_gif_separate_models_and_onebot_send_image(self) -> None:
        recorded_requests: list[tuple[str, dict[str, Any]]] = []

        async def handle_gen(request: web.Request) -> web.Response:
            body = await request.json()
            recorded_requests.append((request.path, body))
            if body.get("output_format") == "gif":
                if body.get("model") == "disabled-gif-model":
                    return web.json_response(
                        {"error": {"message": "GIF output is not enabled for this request.", "type": "invalid_request_error"}},
                        status=400,
                    )
                return web.json_response({
                    "created": 1700000000,
                    "output_format": "gif",
                    "data": [{"url": f"http://127.0.0.1:{request.url.port}/files/anim.gif"}],
                })
            return web.json_response({
                "created": 1700000000,
                "output_format": body.get("output_format", "png"),
                "data": [{"b64_json": base64.b64encode(_PNG_BYTES).decode("ascii")}],
            })

        async def handle_gif_file(request: web.Request) -> web.Response:
            return web.Response(body=_GIF_BYTES, content_type="image/gif")

        async def handle_models(request: web.Request) -> web.Response:
            return web.json_response({
                "object": "list",
                "data": [
                    {"id": "gpt-image-2.6-sunburst"},
                    {"id": "gpt-image-2.6-flare"},
                ],
            })

        mock_app = web.Application()
        mock_app.router.add_post("/v1/images/generations", handle_gen)
        mock_app.router.add_post("/custom-gif/generations", handle_gen)
        mock_app.router.add_get("/files/anim.gif", handle_gif_file)
        mock_app.router.add_get("/v1/models", handle_models)
        runner = web.AppRunner(mock_app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        base_url = f"http://127.0.0.1:{port}"

        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                plugins_dir = base / "plugins"
                plugins_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / "plugins" / "image_gif_gen.py", plugins_dir / "image_gif_gen.py")

                cfg = BotConfig(base_dir=base)
                cfg.agy.workspace_dir = str(base / "bot_workspace")
                cfg.agy.data_dir = str(base / "data")
                cfg.plugins.dir = str(plugins_dir)
                cfg.plugins.configs["image_gif_gen"] = {
                    "api_base": base_url,
                    "api_key": "sk-test-key",
                    "image_model": "gpt-image-2.6-sunburst",
                    "gif_model": "gpt-image-2.6-gif-pro",
                    "gif_path": "/custom-gif/generations",
                    "send_wait_notice": False,
                }
                cfg.platforms = [PlatformConfig(id="qq_1", type="onebot_v11", ws_url="ws://127.0.0.1:3001", self_id="999")]

                app = BotApplication(cfg, runner=_FakeRunner())  # type: ignore[arg-type]
                await app.ensure_plugins_loaded()

                sent_texts: list[str] = []
                sent_images: list[tuple[bytes, bool, bool]] = []

                class _CaptureAdapter:
                    adapter_id = "qq_1"
                    platform_name = "onebot_v11"
                    self_id = "999"
                    config = cfg.platforms[0]

                    def is_channel_allowed(self, scene, cid, uid):
                        return True

                    async def send_reply(self, target, text, *, quote_trigger=True, at_user=False):
                        sent_texts.append(text)
                        return "txt_1"

                    async def send_image(self, target, image, *, caption="", quote_trigger=True, at_user=False):
                        assert isinstance(image, bytes)
                        sent_images.append((image, quote_trigger, at_user))
                        return "img_1"

                app.registry._adapters["qq_1"] = _CaptureAdapter()  # type: ignore[assignment]
                app.channel_guard.activate_group("qq_1", "10001")

                def make_msg(mid: str, text: str) -> IncomingMessage:
                    return IncomingMessage(
                        adapter_id="qq_1",
                        platform="onebot_v11",
                        self_id="999",
                        scene="group",
                        channel_id="10001",
                        message_id=mid,
                        user_id="12345",
                        sender_name="阿星",
                        text=text,
                        timestamp=1700000000.0,
                    )

                # 1) /draw uses image_model + inline flags
                await app.handle_message(make_msg("m1", "/draw --size 1536x1024 --quality high 赛博朋克猫咪"))
                self.assertEqual(len(sent_images), 1)
                self.assertEqual(sent_images[0][0], _PNG_BYTES)
                self.assertEqual(recorded_requests[0][0], "/v1/images/generations")
                self.assertEqual(recorded_requests[0][1]["model"], "gpt-image-2.6-sunburst")
                self.assertEqual(recorded_requests[0][1]["size"], "1536x1024")
                self.assertEqual(recorded_requests[0][1]["quality"], "high")
                self.assertEqual(recorded_requests[0][1]["prompt"], "赛博朋克猫咪")

                # 2) /gif uses separate gif_model + separate gif_path + frame_count/duration
                await app.handle_message(make_msg("m2", "/gif --frames 12 --duration 154 会眨眼的小猫"))
                self.assertEqual(len(sent_images), 2)
                self.assertEqual(sent_images[1][0], _GIF_BYTES)
                self.assertEqual(recorded_requests[1][0], "/custom-gif/generations")
                self.assertEqual(recorded_requests[1][1]["model"], "gpt-image-2.6-gif-pro")
                self.assertEqual(recorded_requests[1][1]["output_format"], "gif")
                self.assertEqual(recorded_requests[1][1]["frame_count"], 12)
                self.assertEqual(recorded_requests[1][1]["frame_duration_ms"], 150)  # rounded to 10ms

                # 3) /gif upstream error returns friendly hint
                await app.handle_message(make_msg("m3", "/gif --model disabled-gif-model 旋转的星星"))
                self.assertTrue(any("gif_model" in t and "GIF output is not enabled" in t for t in sent_texts))

                # 4) /imgmodels lists models & separate configured image/gif models
                await app.handle_message(make_msg("m4", "/imgmodels"))
                self.assertTrue(any("gpt-image-2.6-sunburst" in t and "gpt-image-2.6-gif-pro" in t for t in sent_texts))

                # 5) Verify OneBotV11Adapter.send_image builds valid OneBot v11 base64:// image segment
                sent_packets: list[dict[str, Any]] = []
                ob = OneBotV11Adapter(cfg.platforms[0])

                async def fake_call_action(action: str, params: dict[str, Any], *, timeout=None):
                    sent_packets.append({"action": action, "params": params})
                    return {"status": "ok", "retcode": 0, "data": {"message_id": 777}}

                ob.call_action = fake_call_action  # type: ignore[method-assign]
                mid = await ob.send_image(make_msg("m5", "hi"), _GIF_BYTES, caption="动图来了", quote_trigger=True, at_user=True)
                self.assertEqual(mid, "777")
                self.assertEqual(sent_packets[0]["action"], "send_group_msg")
                segs = sent_packets[0]["params"]["message"]
                self.assertEqual([s["type"] for s in segs], ["reply", "at", "text", "text", "image"])
                self.assertTrue(segs[-1]["data"]["file"].startswith("base64://"))
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
