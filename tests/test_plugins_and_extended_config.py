from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_chat_bot.adapters.base import PlatformAdapter
from agy_chat_bot.adapters.registry import AdapterRegistry
from agy_chat_bot.app import BotApplication, format_context_prompt
from agy_chat_bot.config import (
    ContentSafetyConfig,
    ModelProfileConfig,
    SegmentedReplyConfig,
    config_to_dict,
    load_config_from_dict,
    save_config,
)
from agy_chat_bot.models import ContextSnapshot, IncomingMessage, JudgeDecision, RunnerResult
from agy_chat_bot.reply_formatter import apply_content_safety, compute_segment_delay
from agy_chat_bot.runner import AgyRunner, BaseAgentRunner
from agy_chat_bot.triggers import TriggerEngine
from agy_chat_bot.webui.log_buffer import LogRingBufferHandler
from agy_chat_bot.webui.server import WebUIServer


class FakeAdapter(PlatformAdapter):
    def __init__(self, cfg) -> None:
        super().__init__(cfg)
        self.sent: list[dict[str, Any]] = []

    async def start(self, on_message) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_reply(
        self,
        trigger_msg: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool,
        at_user: bool,
    ) -> str:
        self.sent.append(
            {
                "channel_id": trigger_msg.channel_id,
                "user_id": trigger_msg.user_id,
                "text": text,
                "quote": quote_trigger,
                "at": at_user,
            }
        )
        return f"m-{len(self.sent)}"


class SpyRunner(BaseAgentRunner):
    def __init__(self, *, reply_text: str = "raw model reply", fail: bool = False) -> None:
        self.reply_text = reply_text
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def ask(self, session_key: str, prompt: str) -> RunnerResult:
        self.calls.append((session_key, prompt))
        if self.fail:
            return RunnerResult(ok=False, response="", error="simulated error")
        return RunnerResult(ok=True, response=self.reply_text, conversation_id="conv-1", num_turns=1)

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        return JudgeDecision(should_reply=False, reason="test")

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    async def shutdown(self) -> None:
        return None


class PluginsAndExtendedConfigTests(unittest.IsolatedAsyncioTestCase):
    def test_extended_config_roundtrip_and_formatters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg_path = base / "config.json"
            raw = {
                "bot_name": "测试机",
                "admins": ["10001"],
                "platforms": [{"id": "qq_1", "type": "onebot_v11", "ws_url": "ws://127.0.0.1:3001"}],
                "triggers": {
                    "keyword": {"enabled": True, "keywords": ["唤醒"]},
                    "private_needs_wake": True,
                    "disable_builtin_commands": True,
                    "command_prefixes": ["/", "!"],
                    "admin_bypass_rate_limit": True,
                },
                "session": {"unique_session": True},
                "prompt": {
                    "datetime_system_prompt": False,
                    "group_name_display": False,
                    "timezone": "Asia/Shanghai",
                    "prompt_template": "你是 {{bot_name}}",
                    "custom_error_message": "稍后再试",
                },
                "content_safety": {
                    "enabled": True,
                    "check_input": True,
                    "check_output": True,
                    "action": "mask",
                    "blocked_keywords": ["违禁词"],
                    "block_reply": "已拦截",
                },
                "reply": {
                    "segmented_reply": {
                        "enabled": True,
                        "only_llm_result": False,
                        "interval_method": "log",
                        "log_base": 2.6,
                    }
                },
                "plugins": {
                    "enabled": True,
                    "dir": "./plugins",
                    "disabled": ["old_plugin"],
                    "configs": {"demo": {"k": "v"}},
                },
            }
            cfg = load_config_from_dict(raw, base_dir=base, config_path=cfg_path)
            save_config(cfg)
            reloaded = load_config_from_dict(
                json.loads(cfg_path.read_text(encoding="utf-8")),
                base_dir=base,
                config_path=cfg_path,
            )
            d = config_to_dict(reloaded)
            self.assertTrue(d["session"]["unique_session"])
            self.assertEqual(d["prompt"]["prompt_template"], "你是 {{bot_name}}")
            self.assertEqual(d["prompt"]["custom_error_message"], "稍后再试")
            self.assertTrue(d["content_safety"]["enabled"])
            self.assertEqual(d["content_safety"]["action"], "mask")
            self.assertEqual(d["triggers"]["command_prefixes"], ["/", "!"])
            self.assertTrue(d["triggers"]["private_needs_wake"])
            self.assertTrue(d["triggers"]["admin_bypass_rate_limit"])
            self.assertEqual(d["reply"]["segmented_reply"]["interval_method"], "log")
            self.assertEqual(d["plugins"]["configs"]["demo"]["k"], "v")

            # Log delay & content safety helpers
            delay = compute_segment_delay(
                SegmentedReplyConfig(enabled=True, interval_method="log", log_base=2.6),
                text="这是一条有一定长度的测试消息",
            )
            self.assertGreater(delay, 0.3)
            self.assertLessEqual(delay, 8.0)

            ok_mask, masked, hit = apply_content_safety(
                "这里包含违禁词内容",
                ContentSafetyConfig(enabled=True, action="mask", blocked_keywords=["违禁词"]),
            )
            self.assertTrue(ok_mask)
            self.assertEqual(masked, "这里包含***内容")
            self.assertEqual(hit, "违禁词")

            ok_blk, _, _ = apply_content_safety(
                "违禁词",
                ContentSafetyConfig(enabled=True, action="block", blocked_keywords=["违禁词"]),
            )
            self.assertFalse(ok_blk)

            # private_needs_wake gating
            engine = TriggerEngine(reloaded.triggers)
            pm_no = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="999",
                scene="private",
                channel_id="222",
                message_id="m1",
                user_id="222",
                sender_name="U",
                text="普通私聊",
                timestamp=1.0,
            )
            self.assertFalse(engine.evaluate_fast(pm_no).triggered)
            pm_yes = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="999",
                scene="private",
                channel_id="222",
                message_id="m2",
                user_id="222",
                sender_name="U",
                text="唤醒一下",
                timestamp=2.0,
            )
            self.assertTrue(engine.evaluate_fast(pm_yes).triggered)

    async def test_external_plugin_hooks_commands_unique_session_and_webui_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            plugins_dir = base / "plugins"
            plugins_dir.mkdir(parents=True, exist_ok=True)
            (plugins_dir / "ext_demo.py").write_text(
                """
from agy_chat_bot.plugins import BotPlugin, PluginReply, command, on_message, on_before_llm, on_after_llm

class DemoPlugin(BotPlugin):
    id = "ext_demo"
    name = "扩展演示"
    version = "1.2.3"
    author = "tester"
    description = "测试全部钩子与命令"
    default_config = {"tag": "[TAG]"}

    @command("hello", aliases=["hi"], description="问好")
    async def cmd_hello(self, ctx, msg, args):
        return f"{self.config.get('tag')} hi {args}".strip()

    @command("secret", admin_only=True)
    async def cmd_secret(self, ctx, msg, args):
        return "admin_ok"

    @on_message(priority=10)
    async def intercept(self, ctx, msg):
        if msg.text.strip() == "intercept_me":
            return PluginReply(text="intercepted_by_plugin", stop=True)
        return None

    @on_before_llm(priority=20)
    async def before(self, ctx, snapshot, prompt):
        return prompt + "\\n[PLUGIN_CTX]"

    @on_after_llm(priority=20)
    async def after(self, ctx, snapshot, reply_text):
        return reply_text + " [PLUGIN_POST]"
""".strip(),
                encoding="utf-8",
            )

            cfg = load_config_from_dict(
                {
                    "bot_name": "安挽行",
                    "admins": ["9999"],
                    "platforms": [{"id": "qq_1", "type": "onebot_v11", "ws_url": "ws://127.0.0.1:3001"}],
                    "triggers": {
                        "command_prefixes": ["/", "!"],
                        "mention": {"enabled": True},
                    },
                    "session": {"unique_session": True},
                    "prompt": {
                        "prompt_template": "自定义头部:{{bot_name}}",
                        "custom_error_message": "模型开小差了",
                    },
                    "content_safety": {
                        "enabled": True,
                        "check_input": True,
                        "check_output": True,
                        "action": "mask",
                        "blocked_keywords": ["badword"],
                    },
                    "collector": {"wait_triggers": []},
                    "rate_limit": {"channel_min_interval_seconds": 0, "user_cooldown_seconds": 0},
                    "webui": {"enabled": True, "password": ""},
                    "plugins": {"enabled": True, "dir": "./plugins"},
                },
                base_dir=base,
                config_path=base / "config.json",
            )
            save_config(cfg)

            registry = AdapterRegistry()
            fake_adapter = FakeAdapter(cfg.platforms[0])
            runner = SpyRunner(reply_text="hello badword")
            app = BotApplication(cfg, registry=registry, runner=runner, log_handler=LogRingBufferHandler())
            registry.adapters["qq_1"] = fake_adapter
            app.channel_guard.activate_group("qq_1", "500")
            await app.ensure_plugins_loaded()

            def mk_msg(text: str, user_id: str = "1001", explicit_at: int = 0) -> IncomingMessage:
                return IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="8888",
                    scene="group",
                    channel_id="500",
                    message_id=f"id-{user_id}-{text[:4]}",
                    user_id=user_id,
                    sender_name="Alice",
                    text=text,
                    timestamp=100.0,
                    at_targets=["8888"] * explicit_at,
                )

            # 1. Plugin command with custom prefix '!'
            await app.handle_message(mk_msg("!hello world"))
            self.assertEqual(fake_adapter.sent[-1]["text"], "[TAG] hi world")

            # 2. Admin-only plugin command permission check
            await app.handle_message(mk_msg("/secret", user_id="1001"))
            self.assertIn("权限不足", fake_adapter.sent[-1]["text"])
            await app.handle_message(mk_msg("/secret", user_id="9999"))
            self.assertEqual(fake_adapter.sent[-1]["text"], "admin_ok")

            # 3. on_message interception stops LLM
            await app.handle_message(mk_msg("intercept_me", explicit_at=1))
            self.assertEqual(fake_adapter.sent[-1]["text"], "intercepted_by_plugin")
            self.assertEqual(len(runner.calls), 0)

            # 4. Normal @trigger runs before_llm + unique_session + after_llm + content_safety mask
            await app.handle_message(mk_msg("问个问题", user_id="7777", explicit_at=1))
            await asyncio.sleep(0.05)
            self.assertEqual(len(runner.calls), 1)
            sess_key, sent_prompt = runner.calls[0]
            self.assertEqual(sess_key, "qq_1:group:500:u:7777")
            self.assertIn("自定义头部:安挽行", sent_prompt)
            self.assertIn("[PLUGIN_CTX]", sent_prompt)
            self.assertEqual(fake_adapter.sent[-1]["text"], "hello *** [PLUGIN_POST]")

            # 5. Runner failure triggers custom_error_message
            runner.fail = True
            await app.handle_message(mk_msg("再来一次", user_id="8888", explicit_at=1))
            await asyncio.sleep(0.05)
            self.assertEqual(fake_adapter.sent[-1]["text"], "模型开小差了")

            # 6. WebUI Plugin API CRUD flow
            server = WebUIServer(app, cfg.webui, log_handler=app._log_handler)
            async with TestClient(TestServer(server.web_app)) as client:
                r = await client.get("/api/plugins")
                self.assertEqual(r.status, 200)
                body = await r.json()
                self.assertEqual(len(body["data"]), 1)
                self.assertEqual(body["data"][0]["id"], "ext_demo")

                # Update plugin config live
                r = await client.put("/api/plugins/ext_demo/config", json={"tag": "[NEW]"})
                self.assertEqual(r.status, 200)
                await app.handle_message(mk_msg("/hello ok"))
                self.assertEqual(fake_adapter.sent[-1]["text"], "[NEW] hi ok")

                # Create new plugin from template
                r = await client.post("/api/plugins/create", json={"plugin_id": "my_new_plugin"})
                self.assertEqual(r.status, 200)
                self.assertTrue((plugins_dir / "my_new_plugin.py").exists())

                # Read & edit source + hot reload
                r = await client.get("/api/plugins/my_new_plugin/source")
                self.assertEqual(r.status, 200)
                src = (await r.json())["code"]
                self.assertIn("MyNewPluginPlugin", src)

                # Disable & delete
                r = await client.post("/api/plugins/my_new_plugin/toggle", json={"enabled": False})
                self.assertEqual(r.status, 200)
                self.assertFalse((await r.json())["data"]["loaded"])

                r = await client.delete("/api/plugins/my_new_plugin")
                self.assertEqual(r.status, 200)
                self.assertFalse((plugins_dir / "my_new_plugin.py").exists())

            await app.shutdown()

    def test_per_model_profile_maps_to_agy_cli_flags_and_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            base_sec = base / "settings.antigravity-cli.bot.json"
            base_sec.write_text(
                json.dumps(
                    {
                        "trustedWorkspaces": [str(base / "bot_workspace")],
                        "permissions": {"deny": ["command(*)", "read_url(*)"], "allow": []},
                    }
                ),
                encoding="utf-8",
            )
            cfg = load_config_from_dict(
                {
                    "agy": {
                        "executable": "agy",
                        "model": "gemini-3.8-flash-medium",
                        "settings_file": str(base_sec),
                        "model_profiles": {
                            "gemini-3.8-flash-medium": {
                                "effort": "high",
                                "max_context_messages": 2,
                                "max_turns": 7,
                                "execution_mode": "plan",
                                "sandbox": True,
                                "disable_slash_commands": True,
                                "enable_web_search": False,
                                "enable_url_read": True,
                                "enable_vision": False,
                                "enable_audio": False,
                                "enable_file_tools": True,
                            }
                        },
                    }
                },
                base_dir=base,
            )
            d = config_to_dict(cfg)
            self.assertEqual(d["agy"]["model_profiles"]["gemini-3.8-flash-medium"]["effort"], "high")
            self.assertEqual(d["agy"]["model_profiles"]["gemini-3.8-flash-medium"]["execution_mode"], "plan")

            runner = AgyRunner(
                cfg.agy,
                cfg.rate_limit,
                workspace_dir=base / "bot_workspace",
                data_dir=base / "data",
                settings_file=base_sec,
            )
            args = runner._build_cli_args("gemini-3.8-flash-medium")
            self.assertIn("--model", args)
            self.assertIn("gemini-3.8-flash-medium", args)
            self.assertIn("--effort", args)
            self.assertIn("high", args)
            self.assertIn("--mode", args)
            self.assertIn("plan", args)
            self.assertIn("--sandbox", args)
            self.assertIn("--disable-slash-commands", args)
            self.assertIn("--settings", args)

            settings_idx = args.index("--settings")
            merged_path = Path(args[settings_idx + 1])
            self.assertTrue(merged_path.exists())
            merged = json.loads(merged_path.read_text(encoding="utf-8"))
            deny = set(merged["permissions"]["deny"])
            self.assertIn("command(*)", deny)
            self.assertIn("write_file(GEMINI.md)", deny)
            self.assertIn("search_web", deny)
            self.assertIn("read_file(*)", deny)
            self.assertNotIn("read_url(*)", deny)
            self.assertNotIn("write_file(*)", deny)

            # Verify context window & modality handling in format_context_prompt
            msgs = [
                IncomingMessage("qq_1", "onebot_v11", "999", "group", "1", f"m{i}", "100", "U", f"msg-{i} [图片] [语音]", float(i))
                for i in range(5)
            ]
            snap = ContextSnapshot(
                session_key="qq_1:group:1",
                adapter_id="qq_1",
                platform="onebot_v11",
                scene="group",
                channel_id="1",
                primary_trigger="mention",
                matched_triggers=["mention"],
                trigger_reason="at",
                trigger_message=msgs[-1],
                tracked_user_ids=["100"],
                messages=msgs,
                extra_messages_collected=0,
                settled_at=100.0,
            )
            prof = cfg.agy.resolve_model_profile("gemini-3.8-flash-medium")
            prompt = format_context_prompt(snap, "安挽行", cfg.prompt, model_profile=prof)
            self.assertNotIn("msg-0", prompt)
            self.assertIn("msg-4", prompt)
            self.assertIn("[图片已忽略]", prompt)
            self.assertIn("[语音已忽略]", prompt)


if __name__ == "__main__":
    unittest.main()
