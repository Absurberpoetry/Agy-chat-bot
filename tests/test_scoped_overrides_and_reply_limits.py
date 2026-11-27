from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agy_chat_bot.capability_policy import (
    has_any_override_enabling,
    resolve_capability_enabled,
    resolve_effective_capabilities_map,
    resolve_group_compact_override,
    resolve_group_trigger_override,
)
from agy_chat_bot.config import (
    AgyConfig,
    BotConfig,
    CapabilityOverridesConfig,
    CollectorConfig,
    ModelProfileConfig,
    PluginsConfig,
    RateLimitConfig,
    ReplyConfig,
    SegmentedReplyConfig,
    TriggersConfig,
    config_to_dict,
    load_config_from_dict,
)
from agy_chat_bot.extensions import McpManager
from agy_chat_bot.models import IncomingMessage
from agy_chat_bot.persona_store import PersonaStore, render_reply_limit_guidance
from agy_chat_bot.plugins.manager import PluginManager
from agy_chat_bot.reply_formatter import split_and_clean_reply
from agy_chat_bot.runner.agy_stream import AgyRunner
from agy_chat_bot.triggers.engine import TriggerEngine


class ScopedOverridesAndReplyLimitsTests(unittest.IsolatedAsyncioTestCase):
    def test_capability_four_layer_precedence(self) -> None:
        overrides = CapabilityOverridesConfig(
            clients={"qq_main": {"plugins": {"p1": False}, "mcp": {"m1": True}, "skills": {}}},
            sessions={
                "qq_main:group:100": {"plugins": {"p1": True}, "mcp": {"m1": False, "m2": True}, "skills": {"s1": True}},
                "private:999": {"skills": {"s1": False}, "plugins": {}, "mcp": {}},
            },
        )
        model_prof = ModelProfileConfig(
            model="gemini-3.8-flash",
            plugin_overrides={"p1": True, "p2": True},
            mcp_overrides={"m1": False, "m2": False, "m3": True},
            skill_overrides={"s1": False, "s2": True},
        )

        # 1. Client beats Session, Model, Global
        enabled, layer = resolve_capability_enabled(
            "plugins",
            "p1",
            global_enabled=True,
            capability_overrides=overrides,
            model_profile=model_prof,
            adapter_id="qq_main",
            session_key="qq_main:group:100",
            scene="group",
            channel_id="100",
        )
        self.assertFalse(enabled)
        self.assertEqual(layer, "client")

        enabled, layer = resolve_capability_enabled(
            "mcp",
            "m1",
            global_enabled=False,
            capability_overrides=overrides,
            model_profile=model_prof,
            adapter_id="qq_main",
            session_key="qq_main:group:100",
            scene="group",
            channel_id="100",
        )
        self.assertTrue(enabled)
        self.assertEqual(layer, "client")

        # 2. Session beats Model & Global (when client does not override m2 / s1)
        enabled, layer = resolve_capability_enabled(
            "mcp",
            "m2",
            global_enabled=False,
            capability_overrides=overrides,
            model_profile=model_prof,
            adapter_id="qq_main",
            session_key="qq_main:group:100",
            scene="group",
            channel_id="100",
        )
        self.assertTrue(enabled)
        self.assertEqual(layer, "session")

        enabled, layer = resolve_capability_enabled(
            "skills",
            "s1",
            global_enabled=True,
            capability_overrides=overrides,
            model_profile=model_prof,
            adapter_id="qq_other",
            session_key="qq_other:private:999",
            scene="private",
            channel_id="999",
        )
        self.assertFalse(enabled)
        self.assertEqual(layer, "session")

        # 3. Model beats Global (when neither client nor session overrides)
        enabled, layer = resolve_capability_enabled(
            "mcp",
            "m3",
            global_enabled=False,
            capability_overrides=overrides,
            model_profile=model_prof,
            adapter_id="qq_other",
            session_key="qq_other:group:200",
            scene="group",
            channel_id="200",
        )
        self.assertTrue(enabled)
        self.assertEqual(layer, "model")

        # 4. Global fallback
        enabled, layer = resolve_capability_enabled(
            "skills",
            "s_global",
            global_enabled=True,
            capability_overrides=overrides,
            model_profile=model_prof,
            adapter_id="qq_other",
            session_key="qq_other:group:200",
            scene="group",
            channel_id="200",
        )
        self.assertTrue(enabled)
        self.assertEqual(layer, "global")

        # Effective map resolution
        eff_mcp = resolve_effective_capabilities_map(
            "mcp",
            {"m1": False, "m2": False, "m3": False, "m4": True},
            capability_overrides=overrides,
            model_profile=model_prof,
            adapter_id="qq_main",
            session_key="qq_main:group:100",
            scene="group",
            channel_id="100",
        )
        self.assertEqual(eff_mcp, {"m1": True, "m2": True, "m3": True, "m4": True})

        # has_any_override_enabling detects globally disabled items enabled by higher scopes
        self.assertTrue(
            has_any_override_enabling(
                "mcp",
                "m3",
                capability_overrides=overrides,
                model_profiles={"fast": model_prof},
            )
        )
        self.assertFalse(
            has_any_override_enabling(
                "mcp",
                "m_never_enabled",
                capability_overrides=overrides,
                model_profiles={"fast": model_prof},
            )
        )

    async def test_plugin_manager_loads_globally_disabled_when_higher_override_enables(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdir = Path(td) / "plugins"
            ddir = Path(td) / "data"
            pdir.mkdir(parents=True, exist_ok=True)
            ddir.mkdir(parents=True, exist_ok=True)
            (pdir / "hello_ext.py").write_text(
                'from agy_chat_bot.plugins import BotPlugin, command\n'
                'class HelloExtPlugin(BotPlugin):\n'
                '    plugin_id = "hello_ext"\n'
                '    name = "Hello Ext"\n'
                '    version = "1.0"\n'
                '    @command("hi", description="say hi")\n'
                '    async def cmd_hi(self, ctx):\n'
                '        return "hi"\n',
                encoding="utf-8",
            )
            cfg = BotConfig(
                plugins=PluginsConfig(enabled=True, dir=str(pdir), disabled=["hello_ext"]),
                capability_overrides=CapabilityOverridesConfig(
                    clients={"qq_vip": {"plugins": {"hello_ext": True}, "mcp": {}, "skills": {}}},
                    sessions={"group:777": {"plugins": {"hello_ext": True}, "mcp": {}, "skills": {}}},
                ),
            )
            fake_app = MagicMock()
            fake_app.config = cfg
            pm = PluginManager(fake_app, plugins_dir=pdir, data_dir=ddir)
            await pm.load_all()
            self.assertIn("hello_ext", pm._records)
            self.assertTrue(pm._records["hello_ext"].loaded)

            # Global default is disabled -> disallowed on ordinary group
            self.assertFalse(
                pm.is_plugin_allowed(
                    "hello_ext",
                    adapter_id="qq_normal",
                    scene="group",
                    channel_id="111",
                    session_key="qq_normal:group:111",
                )
            )
            # Allowed in group 777 (session override)
            self.assertTrue(
                pm.is_plugin_allowed(
                    "hello_ext",
                    adapter_id="qq_normal",
                    scene="group",
                    channel_id="777",
                    session_key="qq_normal:group:777",
                )
            )
            # Allowed on client qq_vip (client override)
            self.assertTrue(
                pm.is_plugin_allowed(
                    "hello_ext",
                    adapter_id="qq_vip",
                    scene="group",
                    channel_id="111",
                    session_key="qq_vip:group:111",
                )
            )

    def test_mcp_override_registration_and_runner_hard_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "bot_workspace"
            data = root / "data"
            ws.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)

            base_settings = root / "settings.json"
            base_settings.write_text(
                json.dumps(
                    {
                        "permissions": {
                            "allow": ["search_web", "mcp(draw/*)"],
                            "deny": ["command(*)"],
                        }
                    }
                ),
                encoding="utf-8",
            )

            mgr = McpManager(workspace_dir=ws, data_dir=data, settings_file=base_settings)
            mgr.upsert_server(
                "secret_tool",
                {
                    "transport": "stdio",
                    "enabled": False,
                    "command": "python",
                    "args": ["-V"],
                },
            )
            # Globally disabled -> not in mcp_config.json initially
            cfg_json = json.loads((ws / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
            self.assertNotIn("secret_tool", cfg_json.get("mcpServers", {}))

            # Register override-enabled id -> now present in mcp_config.json for higher-scope allow
            mgr.set_override_enabled_ids({"secret_tool"})
            cfg_json2 = json.loads((ws / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
            self.assertIn("secret_tool", cfg_json2.get("mcpServers", {}))

            # AgyRunner generates separate --settings JSON with exact allow/deny for MCP & Skills
            agy_cfg = AgyConfig(
                workspace_dir=str(ws),
                data_dir=str(data),
                settings_file=str(base_settings),
            )
            runner = AgyRunner(agy_cfg, RateLimitConfig(), workspace_dir=ws, data_dir=data)
            prof = ModelProfileConfig(model="gemini-3.8-flash", enable_file_tools=True)

            sf_allow = runner._resolve_model_settings_file(
                "gemini-3.8-flash",
                prof,
                mcp_states={"secret_tool": True, "draw": False},
                skill_states={"art_skill": True, "banned_skill": False},
            )
            sf_deny = runner._resolve_model_settings_file(
                "gemini-3.8-flash",
                prof,
                mcp_states={"secret_tool": False, "draw": True},
                skill_states={"art_skill": False, "banned_skill": True},
            )
            self.assertNotEqual(sf_allow, sf_deny)
            d_allow = json.loads(Path(sf_allow).read_text(encoding="utf-8"))["permissions"]
            self.assertIn("mcp(secret_tool/*)", d_allow["allow"])
            self.assertIn("mcp(draw/*)", d_allow["deny"])
            self.assertNotIn("mcp(draw/*)", d_allow["allow"])
            self.assertIn("read_file(.agents/skills/art_skill/*)", d_allow["allow"])
            self.assertIn("read_file(.agents/skills/banned_skill/*)", d_allow["deny"])

            sig1 = runner._session_config_sig(
                "gemini-3.8-flash",
                prof,
                mcp_states={"secret_tool": True},
                skill_states={"art_skill": True},
            )
            sig2 = runner._session_config_sig(
                "gemini-3.8-flash",
                prof,
                mcp_states={"secret_tool": False},
                skill_states={"art_skill": True},
            )
            self.assertNotEqual(sig1, sig2)

    def test_per_group_trigger_and_compact_overrides(self) -> None:
        tr_cfg = TriggersConfig()
        tr_cfg.mention.enabled = True
        tr_cfg.keyword.enabled = True
        tr_cfg.keyword.keywords = ["全局词"]
        tr_cfg.group_overrides = {
            "qq1:group:555": {
                "mention": {"enabled": False},
                "keyword": {"enabled": True, "keywords": ["群专属词"]},
            }
        }
        engine = TriggerEngine(tr_cfg)

        def _mk(channel_id: str, text: str, at_bot: bool = False) -> IncomingMessage:
            return IncomingMessage(
                adapter_id="qq1",
                platform="onebot_v11",
                self_id="999",
                scene="group",
                channel_id=channel_id,
                message_id=f"m_{channel_id}_{abs(hash(text))}",
                user_id="u1",
                sender_name="Alice",
                text=text,
                timestamp=1000.0,
                at_targets=["999"] if at_bot else [],
            )

        # Group 555 disables mention
        self.assertFalse(engine.evaluate_fast(_mk("555", "你好", at_bot=True)).triggered)

        # Group 555 uses its own keyword list
        self.assertFalse(engine.evaluate_fast(_mk("555", "聊一下全局词")).triggered)

        dec = engine.evaluate_fast(_mk("555", "测试群专属词"))
        self.assertTrue(dec.triggered)
        self.assertEqual(dec.trigger_type, "keyword")

        # Group 666 still inherits global rules
        dec2 = engine.evaluate_fast(_mk("666", "你好", at_bot=True))
        self.assertTrue(dec2.triggered)
        self.assertEqual(dec2.trigger_type, "mention")

        # Per-group compaction override lookup
        col_cfg = CollectorConfig(
            compact_max_messages=60,
            group_compact_overrides={
                "group:555": {"compact_max_messages": 25, "compact_max_tokens": 4000},
            },
        )
        ov = resolve_group_compact_override(col_cfg, session_key="qq1:group:555", channel_id="555")
        self.assertEqual(ov["compact_max_messages"], 25)
        self.assertEqual(ov["compact_max_tokens"], 4000)
        self.assertEqual(resolve_group_trigger_override(tr_cfg, session_key="qq1:group:555", channel_id="555")["mention"]["enabled"], False)

    def test_reply_limits_formatter_and_plain_gemini_md(self) -> None:
        # 1. Formatter enforces max_chars_per_message, max_messages_per_reply, max_reply_chars
        reply_cfg = ReplyConfig(
            max_chars_per_message=12,
            max_messages_per_reply=2,
            max_reply_chars=24,
            segmented_reply=SegmentedReplyConfig(enabled=True, split_mode="words", split_words=["|||"]),
        )
        segs = split_and_clean_reply("一二三四五六七八九十|||甲乙丙丁戊己庚辛壬癸|||第三段超额消息", reply_cfg)
        self.assertLessEqual(len(segs), 2)
        self.assertTrue(all(len(s) <= 15 for s in segs))  # <= max_chars_per_message + ellipsis
        self.assertLessEqual(sum(len(s) for s in segs), 30)

        # 0 means unlimited total chars
        unlimited_cfg = ReplyConfig(
            max_chars_per_message=0,
            max_messages_per_reply=0,
            max_reply_chars=0,
            segmented_reply=SegmentedReplyConfig(enabled=False),
        )
        long_text = "测试" * 600
        segs_unlim = split_and_clean_reply(long_text, unlimited_cfg)
        self.assertEqual(len(segs_unlim), 1)
        self.assertEqual(segs_unlim[0], long_text)

        # 2. GEMINI.md natural language guidance (NO synthetic tags / protocol markers)
        self.assertEqual(
            render_reply_limit_guidance(
                max_chars_per_message=0,
                max_messages_per_reply=0,
                max_total_reply_chars=0,
            ),
            "",
        )
        guidance = render_reply_limit_guidance(
            max_chars_per_message=80,
            max_messages_per_reply=3,
            max_total_reply_chars=200,
        )
        self.assertIn("80", guidance)
        self.assertIn("3", guidance)
        self.assertIn("200", guidance)
        self.assertNotIn("<", guidance)
        self.assertNotIn(">", guidance)
        self.assertNotIn("[", guidance)
        self.assertNotIn("]", guidance)

        with tempfile.TemporaryDirectory() as td:
            store = PersonaStore(Path(td) / "data", Path(td) / "ws")
            md = store.render_gemini_md(
                "",
                bot_name="Agy",
                max_chars_per_message=60,
                max_messages_per_reply=2,
                max_total_reply_chars=120,
            )
            self.assertIn("60", md)
            self.assertIn("2 条", md)
            self.assertIn("120", md)

    def test_config_dict_roundtrip_all_new_fields(self) -> None:
        raw = {
            "agy": {
                "model": "fast_one",
                "model_profiles": {
                    "fast_one": {
                        "name": "轻量模型",
                        "model": "gemini-3.8-flash-low",
                        "enable_command": True,
                        "mcp_overrides": {"draw": True},
                        "skill_overrides": {"s1": False},
                        "plugin_overrides": {"p1": True},
                    }
                },
            },
            "triggers": {
                "group_overrides": {
                    "qq_1:group:12345": {
                        "mention": {"enabled": False},
                        "dice": {"enabled": True, "probability": 0.15},
                    }
                }
            },
            "collector": {
                "group_compact_overrides": {
                    "qq_1:group:12345": {"compact_max_messages": 30, "compact_max_tokens": 5000}
                }
            },
            "reply": {
                "max_chars_per_message": 120,
                "max_messages_per_reply": 3,
                "max_reply_chars": 360,
            },
            "capability_overrides": {
                "clients": {"qq_1": {"plugins": {"p1": False}, "mcp": {"draw": True}, "skills": {}}},
                "sessions": {"qq_1:private:888": {"skills": {"s1": True}}},
            },
        }
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config_from_dict(raw, base_dir=Path(td))
            dumped = config_to_dict(cfg)
        self.assertEqual(dumped["reply"]["max_chars_per_message"], 120)
        self.assertEqual(dumped["reply"]["max_messages_per_reply"], 3)
        self.assertEqual(dumped["reply"]["max_reply_chars"], 360)
        self.assertEqual(dumped["triggers"]["group_overrides"]["qq_1:group:12345"]["dice"]["probability"], 0.15)
        self.assertEqual(dumped["collector"]["group_compact_overrides"]["qq_1:group:12345"]["compact_max_messages"], 30)
        self.assertTrue(dumped["agy"]["model_profiles"]["fast_one"]["enable_command"])
        self.assertEqual(dumped["agy"]["model_profiles"]["fast_one"]["mcp_overrides"], {"draw": True})
        self.assertEqual(dumped["capability_overrides"]["clients"]["qq_1"]["plugins"], {"p1": False})
        self.assertEqual(dumped["capability_overrides"]["sessions"]["qq_1:private:888"]["skills"], {"s1": True})


if __name__ == "__main__":
    unittest.main()
