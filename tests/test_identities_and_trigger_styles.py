from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agy_bot.adapters.base import OnMessageFn, PlatformAdapter
from agy_bot.app import BotApplication
from agy_bot.config import ModelProfileConfig, PlatformConfig, config_to_dict, load_config_from_dict
from agy_bot.models import IncomingMessage, JudgeDecision, RunnerResult
from agy_bot.runner import AgyRunner, BaseAgentRunner
from agy_bot.webui.server import WebUIServer


class _StyleCaptureAdapter(PlatformAdapter):
    def __init__(self, cfg: PlatformConfig) -> None:
        super().__init__(cfg)
        self.self_id = cfg.self_id or "99999"
        self.sent: list[dict[str, object]] = []

    async def start(self, on_message: OnMessageFn) -> None:
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
        self.sent.append(
            {
                "scene": target.scene,
                "channel_id": target.channel_id,
                "user_id": target.user_id,
                "text": text,
                "quote": quote_trigger,
                "at": at_user,
            }
        )
        return f"m_{len(self.sent)}"


class _CaptureRunner(BaseAgentRunner):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ask(
        self,
        session_key: str,
        prompt: str,
        *,
        model_override: str | None = None,
        profile_override: ModelProfileConfig | None = None,
    ) -> RunnerResult:
        self.calls.append(
            {
                "session_key": session_key,
                "prompt": prompt,
                "model_override": model_override,
                "profile_override": profile_override,
            }
        )
        return RunnerResult(ok=True, response="收到回复")

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        return JudgeDecision(should_reply=False, reason="test")

    async def shutdown(self) -> None:
        return None

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0


class IdentitiesAndTriggerStylesTests(unittest.IsolatedAsyncioTestCase):
    def test_config_roundtrip_platform_blocklists_identities_and_plugin_scopes(self) -> None:
        raw = {
            "bot_name": "安挽行",
            "platforms": [
                {
                    "id": "qq_1",
                    "ws_url": "ws://127.0.0.1:3001",
                    "group_allowlist": ["1001"],
                    "user_blocklist": ["bad_u1"],
                    "private_chat_enabled": True,
                    "private_blocklist": ["priv_bad_u2"],
                }
            ],
            "triggers": {
                "mention": {"enabled": True, "quote_reply": False, "at_user": True},
                "keyword": {"enabled": True, "keywords": ["测试"], "quote_reply": True, "at_user": False},
                "script": {"enabled": True, "quote_reply": False, "at_user": False},
            },
            "identities": {
                "enabled": True,
                "strict_anti_spoof": True,
                "default_policy": {
                    "label": "普通群友",
                    "allow_plugins": False,
                    "allow_web_search": False,
                    "allow_file_tools": False,
                },
                "profiles": [
                    {
                        "id": "owner_1",
                        "name": "主人",
                        "adapter_id": "qq_1",
                        "target_type": "user",
                        "target_ids": ["1000010001"],
                        "is_admin": True,
                        "label": "主人",
                        "reply_style": "称呼主人",
                        "allowed_tasks": "开放全部合法能力",
                        "allow_plugins": True,
                        "bypass_rate_limit": True,
                        "allow_web_search": True,
                        "allow_file_tools": True,
                    }
                ],
            },
            "plugins": {
                "enabled": True,
                "dir": "./plugins",
                "scopes": {
                    "echo_pl": {"mode": "groups", "adapter_id": "qq_1", "group_ids": ["1001"], "allow_private": False}
                },
            },
        }
        cfg = load_config_from_dict(raw, base_dir=Path("."))
        dumped = config_to_dict(cfg)
        self.assertEqual(dumped["platforms"][0]["user_blocklist"], ["bad_u1"])
        self.assertEqual(dumped["platforms"][0]["private_blocklist"], ["priv_bad_u2"])
        self.assertNotIn("group_blocklist", dumped["platforms"][0])
        self.assertNotIn("private_allowlist", dumped["platforms"][0])
        self.assertIs(dumped["triggers"]["mention"]["quote_reply"], False)
        self.assertIs(dumped["triggers"]["mention"]["at_user"], True)
        self.assertEqual(dumped["identities"]["profiles"][0]["adapter_id"], "qq_1")
        self.assertTrue(dumped["identities"]["profiles"][0]["is_admin"])
        self.assertEqual(dumped["plugins"]["scopes"]["echo_pl"]["group_ids"], ["1001"])

    def test_platform_user_blocklist_and_private_blocklist_enforcement(self) -> None:
        cfg = PlatformConfig(
            id="client_a",
            ws_url="ws://127.0.0.1:3001",
            user_blocklist=["u_global_ban"],
            private_blocklist=["u_priv_ban"],
        )
        ad = _StyleCaptureAdapter(cfg)
        # Global user blocklist blocks both group and private
        self.assertFalse(ad.is_channel_allowed("group", "g1", "u_global_ban"))
        self.assertFalse(ad.is_channel_allowed("private", "u_global_ban", "u_global_ban"))
        # Private blocklist blocks private only, allows group
        self.assertFalse(ad.is_channel_allowed("private", "u_priv_ban", "u_priv_ban"))
        self.assertTrue(ad.is_channel_allowed("group", "g1", "u_priv_ban"))
        # Normal user allowed at adapter layer (anti-disturb activation guard handles group wake-up)
        self.assertTrue(ad.is_channel_allowed("private", "u_ok", "u_ok"))
        self.assertTrue(ad.is_channel_allowed("group", "g1", "u_ok"))
        self.assertTrue(ad.is_channel_allowed("group", "g2", "u_ok"))

    async def test_per_trigger_quote_at_multi_client_identity_and_plugin_scope_import(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            plugins_dir = base / "plugins"
            plugins_dir.mkdir()
            (plugins_dir / "echo_pl.py").write_text(
                "from agy_bot.plugins import BotPlugin, PluginReply, command\n"
                "class EchoPl(BotPlugin):\n"
                "    id = 'echo_pl'\n"
                "    @command('echo')\n"
                "    async def cmd_echo(self, ctx, msg, arg):\n"
                "        return PluginReply(text='ECHO:' + arg)\n",
                encoding="utf-8",
            )
            cfg = load_config_from_dict(
                {
                    "bot_name": "安挽行",
                    "admins": [],
                    "platforms": [
                        {"id": "qq_1", "ws_url": "ws://127.0.0.1:3001", "group_allowlist": ["1001", "1002"]},
                        {"id": "tg_1", "type": "generic_ws", "ws_url": "ws://127.0.0.1:3002", "group_allowlist": ["1001"]},
                    ],
                    "plugins": {
                        "enabled": True,
                        "dir": "./plugins",
                        "scopes": {
                            "echo_pl": {"mode": "groups", "adapter_id": "qq_1", "group_ids": ["1001"], "allow_private": False}
                        },
                    },
                    "reply": {"quote_trigger_message": True, "at_trigger_user": False},
                    "triggers": {
                        "mention": {"enabled": True, "quote_reply": False, "at_user": True},
                        "keyword": {"enabled": True, "keywords": ["召唤"], "quote_reply": True, "at_user": True},
                        "quote": {"enabled": False},
                        "dice": {"enabled": False},
                        "context_llm": {"enabled": False},
                    },
                    "identities": {
                        "enabled": True,
                        "strict_anti_spoof": True,
                        "default_policy": {
                            "label": "普通群友",
                            "reply_style": "简短礼貌",
                            "allowed_tasks": "仅限日常闲聊",
                            "allow_bot_commands": False,
                            "allow_plugins": False,
                            "bypass_rate_limit": False,
                            "allow_web_search": False,
                            "allow_url_read": False,
                            "allow_file_tools": False,
                            "allow_vision": False,
                            "allow_audio": False,
                        },
                        "profiles": [
                            {
                                "id": "owner_qq",
                                "name": "QQ主人",
                                "adapter_id": "qq_1",
                                "target_type": "user",
                                "target_ids": ["1000010001"],
                                "is_admin": True,
                                "label": "主人",
                                "reply_style": "称呼对方为主人，认真回答",
                                "allowed_tasks": "允许联网查资料和读写工作区",
                                "allow_plugins": True,
                                "bypass_rate_limit": True,
                                "allow_web_search": True,
                                "allow_url_read": True,
                                "allow_file_tools": True,
                                "allow_vision": True,
                                "allow_audio": True,
                            },
                            {
                                "id": "grp_core",
                                "name": "核心群",
                                "adapter_id": "qq_1",
                                "target_type": "group",
                                "target_ids": ["1001"],
                                "is_admin": False,
                                "label": "核心技术群",
                                "reply_style": "技术交流氛围",
                                "allow_plugins": False,
                            },
                        ],
                    },
                    "collector": {"quiet_wait_seconds": 0, "max_wait_seconds": 0},
                    "rate_limit": {"user_cooldown_seconds": 0, "channel_min_interval_seconds": 0},
                    "agy": {"mode": "mock", "workspace_dir": "./ws", "data_dir": "./data"},
                    "webui": {"enabled": False, "username": "star", "password": "pw"},
                },
                base_dir=base,
            )
            runner = _CaptureRunner()
            app = BotApplication(cfg, runner=runner)
            ad_qq = _StyleCaptureAdapter(cfg.platforms[0])
            ad_tg = _StyleCaptureAdapter(cfg.platforms[1])
            app.registry.adapters["qq_1"] = ad_qq
            app.registry.adapters["tg_1"] = ad_tg
            app.channel_guard.activate_group("qq_1", "1001")
            app.channel_guard.activate_group("qq_1", "1002")
            app.channel_guard.activate_group("tg_1", "1001")
            await app.ensure_plugins_loaded()

            # 1. Plugin scope: owner runs /echo in allowed group 1001 -> works; in 1002 -> scoped out!
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1", platform="onebot_v11", self_id="99999",
                    scene="group", channel_id="1001", message_id="m1",
                    user_id="1000010001", sender_name="星星", text="/echo ok", timestamp=time.time(),
                )
            )
            self.assertEqual(len(ad_qq.sent), 1)
            self.assertEqual(ad_qq.sent[-1]["text"], "ECHO:ok")

            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1", platform="onebot_v11", self_id="99999",
                    scene="group", channel_id="1002", message_id="m2",
                    user_id="1000010001", sender_name="星星", text="/echo blocked", timestamp=time.time(),
                )
            )
            self.assertEqual(len(ad_qq.sent), 1)

            # 2. Multi-client isolation: same ID 1000010001 on tg_1 does NOT match qq_1's owner profile!
            await app.handle_message(
                IncomingMessage(
                    adapter_id="tg_1", platform="generic_ws", self_id="99999",
                    scene="group", channel_id="1001", message_id="m3",
                    user_id="1000010001", sender_name="TG路人", text="召唤测试", timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            self.assertEqual(len(runner.calls), 1)
            self.assertIn("当前触发用户核验身份：普通群友", str(runner.calls[0]["prompt"]))
            self.assertNotIn("[身份:主人]", str(runner.calls[0]["prompt"]))

            # 3. On qq_1, Owner matches with is_admin=True & allowed tools + mention reply style override
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1", platform="onebot_v11", self_id="99999",
                    scene="group", channel_id="1001", message_id="m4",
                    user_id="1000010001", sender_name="星星", text="帮我查资料",
                    at_targets=["99999"], timestamp=time.time(),
                )
            )
            await asyncio.sleep(0.05)
            owner_call = runner.calls[-1]
            self.assertIn("[系统身份与权限认定]", str(owner_call["prompt"]))
            self.assertIn("主人", str(owner_call["prompt"]))
            self.assertIn("管理员", str(owner_call["prompt"]))
            self.assertIn("核心技术群", str(owner_call["prompt"]))
            owner_prof = owner_call["profile_override"]
            assert isinstance(owner_prof, ModelProfileConfig)
            self.assertTrue(owner_prof.enable_web_search)
            self.assertTrue(owner_prof.enable_file_tools)
            self.assertIs(ad_qq.sent[-1]["quote"], False)
            self.assertIs(ad_qq.sent[-1]["at"], True)

            # 4. WebUI API: /api/plugins/import & /api/plugins/{id}/scope
            server = WebUIServer(app, cfg.webui)
            async with TestClient(TestServer(server.web_app)) as client:
                lr = await client.post("/api/auth/login", json={"username": "star", "password": "pw"})
                self.assertEqual(lr.status, 200)

                imp = await client.post(
                    "/api/plugins/import",
                    json={
                        "filename": "hello_imp.py",
                        "code": (
                            "from agy_bot.plugins import BotPlugin, PluginReply, command\n"
                            "class HelloImp(BotPlugin):\n"
                            "    id = 'hello_imp'\n"
                            "    @command('hi_imp')\n"
                            "    async def cmd_hi(self, ctx, msg, arg):\n"
                            "        return PluginReply(text='HI_IMPORTED')\n"
                        ),
                    },
                )
                self.assertEqual(imp.status, 200)
                imp_json = await imp.json()
                self.assertTrue(imp_json["ok"])
                self.assertEqual(imp_json["data"]["id"], "hello_imp")

                sc = await client.put(
                    "/api/plugins/hello_imp/scope",
                    json={"mode": "groups", "adapter_id": "qq_1", "group_ids": ["1002"], "allow_private": False},
                )
                self.assertEqual(sc.status, 200)
                sc_json = await sc.json()
                self.assertEqual(sc_json["data"]["scope"]["group_ids"], ["1002"])

            await app.shutdown()

    def test_agy_runner_generates_isolated_settings_files_per_identity_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = load_config_from_dict(
                {
                    "agy": {
                        "mode": "agy",
                        "executable": sys.executable,
                        "model": "gemini-3.8-flash-medium",
                        "workspace_dir": "./ws",
                        "data_dir": "./data",
                    }
                },
                base_dir=base,
            )
            runner = AgyRunner(
                cfg.agy,
                cfg.rate_limit,
                workspace_dir=base / "ws",
                data_dir=base / "data",
            )
            owner_prof = ModelProfileConfig(
                effort="high", enable_web_search=True, enable_url_read=True, enable_vision=True, enable_file_tools=True
            )
            guest_prof = ModelProfileConfig(
                effort="low", enable_web_search=False, enable_url_read=False, enable_vision=False, enable_file_tools=False
            )
            args_owner = runner._build_cli_args("gemini-3.8-flash-medium", profile_override=owner_prof)
            args_guest = runner._build_cli_args("gemini-3.8-flash-medium", profile_override=guest_prof)

            s_owner = Path(args_owner[args_owner.index("--settings") + 1])
            s_guest = Path(args_guest[args_guest.index("--settings") + 1])
            self.assertNotEqual(s_owner, s_guest)

            d_owner = json.loads(s_owner.read_text(encoding="utf-8"))["permissions"]["deny"]
            d_guest = json.loads(s_guest.read_text(encoding="utf-8"))["permissions"]["deny"]
            self.assertNotIn("search_web", d_owner)
            self.assertNotIn("write_file(*)", d_owner)
            self.assertIn("command(*)", d_owner)

            self.assertIn("search_web", d_guest)
            self.assertIn("read_url(*)", d_guest)
            self.assertIn("read_file(*)", d_guest)
            self.assertIn("write_file(*)", d_guest)
            self.assertIn("command(*)", d_guest)


if __name__ == "__main__":
    unittest.main()
