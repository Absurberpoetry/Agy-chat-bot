from __future__ import annotations

import asyncio
import json
import logging
import random
import sys
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agychat_bot.app import BotApplication  # noqa: E402
from agychat_bot.config import (  # noqa: E402
    AgyConfig,
    BotConfig,
    PlatformConfig,
    RateLimitConfig,
    ReplyConfig,
    SegmentedReplyConfig,
    WebUIConfig,
    config_to_dict,
    load_config,
    save_config,
)
from agychat_bot.models import IncomingMessage  # noqa: E402
from agychat_bot.persona_store import PersonaStore  # noqa: E402
from agychat_bot.reply_formatter import compute_segment_delay, split_and_clean_reply  # noqa: E402
from agychat_bot.runner.agy_stream import AgyRunner, MockAgentRunner  # noqa: E402
from agychat_bot.webui.log_buffer import LogRingBufferHandler  # noqa: E402
from agychat_bot.webui.server import WebUIServer  # noqa: E402


class _FakeAdapter:
    def __init__(self) -> None:
        self.adapter_id = "qq_1"
        self.platform_name = "onebot_v11"
        self.config = PlatformConfig(id="qq_1", ws_url="ws://127.0.0.1:3001")
        self.self_id = "999000111"
        self._ws = object()
        self.sent: list[tuple[str, bool, bool]] = []

    def is_channel_allowed(self, scene: str, channel_id: str, user_id: str) -> bool:
        return True

    async def start(self, on_message) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_reply(self, target, text: str, *, quote_trigger: bool = True, at_user: bool = False) -> str:
        self.sent.append((text, quote_trigger, at_user))
        return f"sent-{len(self.sent)}"


class AstrBotFeaturesAndWebUITests(unittest.IsolatedAsyncioTestCase):
    def test_segmented_reply_regex_and_words_and_delay(self) -> None:
        cfg = ReplyConfig(
            reply_prefix="[Bot] ",
            max_reply_chars=500,
            segmented_reply=SegmentedReplyConfig(
                enabled=True,
                split_mode="regex",
                regex=r"[\s\S]+?(?:\|\|\||$)",
                content_cleanup_rule=r"\|\|\||^\s+|\s+$",
                interval_method="random",
                interval="1.5,3.5",
            ),
        )
        segs = split_and_clean_reply("第一句||| 第二句 ||| 第三句", cfg)
        self.assertEqual(segs, ["[Bot] 第一句", "第二句", "第三句"])

        rng = random.Random(42)
        d = compute_segment_delay(cfg.segmented_reply, rng=rng)
        self.assertGreaterEqual(d, 1.5)
        self.assertLessEqual(d, 3.5)

        words_cfg = ReplyConfig(
            segmented_reply=SegmentedReplyConfig(
                enabled=True,
                split_mode="words",
                split_words=["。", "！"],
                content_cleanup_rule=r"^\s+|\s+$",
                interval_method="fixed",
                interval="2.0",
            )
        )
        segs2 = split_and_clean_reply("你好呀！今天天气不错。晚上见", words_cfg)
        self.assertEqual(segs2, ["你好呀！", "今天天气不错。", "晚上见"])
        self.assertEqual(compute_segment_delay(words_cfg.segmented_reply), 2.0)

        custom_delim_cfg = ReplyConfig(
            segmented_reply=SegmentedReplyConfig(
                enabled=True,
                split_mode="words",
                split_words=["|||", "---"],
                content_cleanup_rule="",
            )
        )
        segs3 = split_and_clean_reply("第一句|||第二句---第三句", custom_delim_cfg)
        self.assertEqual(segs3, ["第一句", "第二句", "第三句"])

    def test_persona_store_syncs_gemini_md_with_security_redlines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            store = PersonaStore(data_dir=p / "data", workspace_dir=p / "ws")
            store.set_global_dialogs([{"user": "在吗", "assistant": "在的，有事直接说"}])
            store.upsert(
                "安挽行",
                "你是安挽行，说话温柔自然。",
                description="群聊人设",
                preset_dialogs=[{"user": "你好呀", "assistant": "叫我挽行就行～"}],
            )
            # Reload from disk to verify persistence of both persona & global dialogs
            reloaded = PersonaStore(data_dir=p / "data", workspace_dir=p / "ws")
            self.assertEqual(len(reloaded.list_personas()), 1)
            self.assertEqual(len(reloaded.get_global_dialogs()), 1)
            self.assertEqual(reloaded.get("安挽行").preset_dialogs[0]["assistant"], "叫我挽行就行～")

            md_path = reloaded.sync_to_workspace("安挽行", bot_name="安挽行", segmented_hint=True)
            text = md_path.read_text(encoding="utf-8")
            self.assertIn("安挽行", text)
            self.assertIn("全局预设对话与回复示例", text)
            self.assertIn("在的，有事直接说", text)
            self.assertIn("人格专属预设对话与回复示例", text)
            self.assertIn("叫我挽行就行～", text)
            self.assertNotIn("|||", text)
            self.assertIn("安全与防注入红线", text)
            self.assertIn("一律拒绝", text)

    async def test_fallback_model_on_primary_failure(self) -> None:
        script = """
import json, sys
args = sys.argv[1:]
model = args[args.index('--model') + 1] if '--model' in args else ''
print(json.dumps({"event": "init", "conversation_id": f"cid-{model}"}), flush=True)
for raw in sys.stdin:
    if not raw.strip():
        continue
    if model == 'primary-503':
        print(json.dumps({"event": "result", "result": {"status": "ERROR", "error": "503 No capacity"}}), flush=True)
        sys.exit(1)
    print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": f"ok-from-{model}", "num_turns": 1}}), flush=True)
""".strip()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_agy = tmp_path / "fake_agy.py"
            fake_agy.write_text(script, encoding="utf-8")

            async def spawn_fake(*args, cwd=None, env=None):
                return await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(fake_agy),
                    *args[1:],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                )

            runner = AgyRunner(
                AgyConfig(
                    mode="agy",
                    executable="fake_agy",
                    model="primary-503",
                    fallback_models=["backup-flash"],
                ),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=tmp_path / "ws",
                data_dir=tmp_path / "data",
                process_factory=spawn_fake,
            )
            res = await runner.ask("qq_1:group:1", "hi")
            self.assertTrue(res.ok)
            self.assertEqual(res.response, "ok-from-backup-flash")
            await runner.shutdown()

    async def test_admin_commands_and_webui_api_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg_path = tmp_path / "config.json"
            cfg = BotConfig(
                bot_name="安挽行",
                admins=["1000010001"],
                platforms=[PlatformConfig(id="qq_1", enabled=True, ws_url="ws://127.0.0.1:3001")],
                agy=AgyConfig(mode="mock", workspace_dir=str(tmp_path / "ws"), data_dir=str(tmp_path / "data")),
                webui=WebUIConfig(enabled=True, host="127.0.0.1", port=0, username="star", password="secret"),
                base_dir=tmp_path,
                config_path=cfg_path,
            )
            save_config(cfg, cfg_path)
            reloaded_cfg = load_config(cfg_path)
            self.assertEqual(reloaded_cfg.admins, ["1000010001"])
            self.assertEqual(reloaded_cfg.webui.username, "star")

            log_handler = LogRingBufferHandler(max_entries=200)
            log_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
            test_logger = logging.getLogger("test_webui_logger")
            test_logger.addHandler(log_handler)
            test_logger.setLevel(logging.INFO)
            test_logger.info("hello-webui-log")

            app = BotApplication(reloaded_cfg, runner=MockAgentRunner(), log_handler=log_handler)
            fake_adapter = _FakeAdapter()
            app.registry._adapters["qq_1"] = fake_adapter
            app.channel_guard.activate_group("qq_1", "1106615059")

            # Admin command /status works for admin, ignored for non-admin
            non_admin_msg = IncomingMessage(
                adapter_id="qq_1", platform="onebot_v11", self_id="999000111",
                scene="group", channel_id="1106615059", message_id="m1",
                user_id="999999", sender_name="路人", text="/status", timestamp=1.0,
            )
            await app.handle_message(non_admin_msg)
            self.assertEqual(len(fake_adapter.sent), 0)

            admin_msg = IncomingMessage(
                adapter_id="qq_1", platform="onebot_v11", self_id="999000111",
                scene="group", channel_id="1106615059", message_id="m2",
                user_id="1000010001", sender_name="管理员", text="/status", timestamp=2.0,
            )
            await app.handle_message(admin_msg)
            self.assertEqual(len(fake_adapter.sent), 1)
            self.assertIn("状态正常", fake_adapter.sent[0][0])

            # WebUI API test via aiohttp TestClient
            webui = WebUIServer(app, reloaded_cfg.webui, log_handler=log_handler)
            async with TestClient(TestServer(webui.web_app)) as client:
                # 1. Protected endpoint requires login
                r_unauth = await client.get("/api/status")
                self.assertEqual(r_unauth.status, 401)

                # 2. Login
                r_login = await client.post("/api/auth/login", json={"username": "star", "password": "secret"})
                self.assertEqual(r_login.status, 200)

                # 3. Status + Logs
                r_st = await client.get("/api/status")
                self.assertEqual(r_st.status, 200)
                st_data = (await r_st.json())["data"]
                self.assertEqual(st_data["bot_name"], "安挽行")

                r_logs = await client.get("/api/logs?search=hello-webui-log")
                self.assertEqual(r_logs.status, 200)
                logs_data = (await r_logs.json())["data"]
                self.assertTrue(any("hello-webui-log" in e["message"] for e in logs_data))

                # 4. Persona upsert + preset dialogs + global dialogs + activate
                r_gd = await client.put(
                    "/api/personas/global-dialogs",
                    json={"dialogs": [{"user": "群规是啥", "assistant": "友善交流不刷屏"}]},
                )
                self.assertEqual(r_gd.status, 200)
                r_p = await client.post(
                    "/api/personas",
                    json={
                        "persona_id": "测试人设",
                        "system_prompt": "你是测试人设",
                        "preset_dialogs": [{"user": "早安", "assistant": "早呀今天也要加油"}],
                    },
                )
                self.assertEqual(r_p.status, 200)
                r_act = await client.post("/api/personas/activate", json={"persona_id": "测试人设"})
                self.assertEqual(r_act.status, 200)
                self.assertEqual(app.config.active_persona_id, "测试人设")
                md_text = (tmp_path / "ws" / "GEMINI.md").read_text(encoding="utf-8")
                self.assertIn("测试人设", md_text)
                self.assertIn("友善交流不刷屏", md_text)
                self.assertIn("早呀今天也要加油", md_text)
                r_list = await client.get("/api/personas")
                list_json = await r_list.json()
                self.assertEqual(len(list_json["global_dialogs"]), 1)
                self.assertEqual(len(list_json["data"][0]["preset_dialogs"]), 1)

                # 5. Config PUT hot-reload
                raw_cfg = config_to_dict(app.config)
                raw_cfg["reply"]["segmented_reply"]["enabled"] = True
                r_put = await client.put("/api/config", json=raw_cfg)
                self.assertEqual(r_put.status, 200)
                self.assertTrue(app.config.reply.segmented_reply.enabled)

                # 6. Playground chat
                r_chat = await client.post("/api/playground/chat", json={"message": "测试消息"})
                self.assertEqual(r_chat.status, 200)
                chat_data = await r_chat.json()
                self.assertTrue(chat_data["ok"])
                self.assertTrue(len(chat_data["segments"]) >= 1)

            test_logger.removeHandler(log_handler)
            await app.shutdown()


if __name__ == "__main__":
    unittest.main()
