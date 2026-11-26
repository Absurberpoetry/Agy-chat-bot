from __future__ import annotations

import asyncio
import io
import json
import os
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

from agy_chat_bot.adapters.generic_ws import GenericWSAdapter
from agy_chat_bot.adapters.onebot_v11 import OneBotV11Adapter
from agy_chat_bot.app import BotApplication, _format_ts, _is_safe_public_http_url
from agy_chat_bot.channel_guard import ChannelActivationStore
from agy_chat_bot.config import (
    BotConfig,
    ContentSafetyConfig,
    PlatformConfig,
    ReplyConfig,
    ScriptRuleConfig,
    ScriptTriggerConfig,
    SegmentedReplyConfig,
    TriggersConfig,
    KeywordTriggerConfig,
    load_config_from_dict,
)
from agy_chat_bot.extensions import SkillsManager, parse_skill_md, render_skill_md
from agy_chat_bot.media import (
    looks_like_image_bytes,
    materialize_image,
    prune_media_cache,
    sanitize_jpeg_segments,
)
from agy_chat_bot.models import IncomingMessage, MediaAttachment, RunnerResult
from agy_chat_bot.plugins.manager import _normalize_reply
from agy_chat_bot.proxy_guard import parse_controller_from_cmdline
from agy_chat_bot.reply_formatter import apply_content_safety, split_and_clean_reply, truncate_reply
from agy_chat_bot.triggers.engine import TriggerEngine
from agy_chat_bot.triggers.scheduler import _parse_cron_field, cron_matches
from agy_chat_bot.triggers.script_runner import ScriptTriggerEvaluator
from agy_chat_bot.webui.server import WebUIServer

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _DummyRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_ask: Exception | None = None

    async def ask(
        self,
        session_key: str,
        prompt: str,
        *,
        model_override: str | None = None,
        profile_override: Any = None,
        allow_fallback: bool = True,
        defer_geo_fallback: bool = False,
    ) -> RunnerResult:
        self.calls.append(
            {
                "session_key": session_key,
                "prompt": prompt,
                "model_override": model_override,
                "profile_override": profile_override,
                "allow_fallback": allow_fallback,
                "defer_geo_fallback": defer_geo_fallback,
            }
        )
        if self.raise_on_ask is not None:
            raise self.raise_on_ask
        return RunnerResult(ok=True, response="ok")

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> Any:
        raise NotImplementedError

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    def get_status(self) -> dict[str, Any]:
        return {"mode": "mock"}

    async def reset_sessions(self, session_key: str | None = None) -> int:
        return 0

    async def shutdown(self) -> None:
        return None

    async def update_config(self, *args: Any, **kwargs: Any) -> None:
        return None


class _DummyAdapter:
    platform_name = "onebot_v11"

    def __init__(self, cfg: PlatformConfig) -> None:
        self.config = cfg
        self.self_id = cfg.self_id
        self.sent: list[str] = []

    def is_channel_allowed(self, scene: str, channel_id: str, user_id: str) -> bool:
        return True

    async def send_reply(self, target: IncomingMessage, text: str, **kwargs: Any) -> str:
        self.sent.append(text)
        return f"m-{len(self.sent)}"

    async def stop(self) -> None:
        return None


class SecurityAndStabilityAuditTests(unittest.IsolatedAsyncioTestCase):
    async def test_script_runner_blocks_python_expr_sandbox_escapes_and_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base.parent / f"outside_{os.getpid()}.py"
            ev = ScriptTriggerEvaluator(ScriptTriggerConfig(enabled=True), base_dir=base)
            msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="999",
                scene="group",
                channel_id="100",
                message_id="m1",
                user_id="u1",
                sender_name="Alice",
                text="hello 123 world",
                timestamp=time.time(),
            )

            # Safe AST expression still works
            ok_rule = ScriptRuleConfig(
                id="safe",
                enabled=True,
                mode="python_expr",
                expr='bool(re.search(r"\\d+", text)) and "hello" in text',
            )
            ok_res = await ev.evaluate_rule(ok_rule, msg)
            self.assertTrue(ok_res.triggered)

            # Sandbox escape attempts MUST be rejected with ValueError
            for bad_expr in (
                '().__class__.__bases__[0].__subclasses__()',
                're.sys.modules["os"]',
                '__import__("os").system("echo pwn")',
                '(lambda: 1)()',
                '[x for x in range(10)]',
            ):
                bad_rule = ScriptRuleConfig(id="bad", enabled=True, mode="python_expr", expr=bad_expr)
                with self.assertRaises((ValueError, NameError), msg=f"Should block unsafe expr: {bad_expr}"):
                    await ev.evaluate_rule(bad_rule, msg)

            # python_file outside base_dir must be rejected with ValueError
            trav_rule = ScriptRuleConfig(
                id="trav",
                enabled=True,
                mode="python_file",
                script_path=f"../{outside.name}",
            )
            with self.assertRaises(ValueError):
                await ev.evaluate_rule(trav_rule, msg)

    def test_skill_zip_security_atomic_staging_and_frontmatter_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            mgr = SkillsManager(ws)

            # Frontmatter newline injection + backslash/quote roundtrip
            md = render_skill_md("demo\ninjected: true", 'say "hi" \\ ok', "body text")
            self.assertNotIn("\ninjected:", md)
            name, desc, body = parse_skill_md(md)
            self.assertEqual(name, "demo injected: true")
            self.assertEqual(desc, 'say "hi" \\ ok')
            self.assertEqual(body.strip(), "body text")
            # Second roundtrip must NOT double backslashes
            md2 = render_skill_md(name, desc, body)
            _, desc2, _ = parse_skill_md(md2)
            self.assertEqual(desc2, desc)

            # Create an existing valid skill
            mgr.upsert_skill("keep-me", name="Keep", description="orig", body="original body")
            orig_file = ws / ".agents" / "skills" / "keep-me" / "SKILL.md"
            self.assertTrue(orig_file.is_file())

            # Zip with >200 files (zip bomb / file count cap) with overwrite=True must NOT delete existing skill
            zbuf = io.BytesIO()
            with zipfile.ZipFile(zbuf, "w") as zf:
                zf.writestr("keep-me/SKILL.md", "---\nname: keep-me\n---\nnew")
                for i in range(210):
                    zf.writestr(f"keep-me/f{i}.txt", "x")
            with self.assertRaises(ValueError):
                mgr.import_skill("keep-me.zip", zbuf.getvalue(), skill_id="keep-me", overwrite=True)
            self.assertIn("original body", orig_file.read_text(encoding="utf-8"))

            # Zip with Windows drive-letter & absolute paths must skip dangerous entries
            zbuf2 = io.BytesIO()
            with zipfile.ZipFile(zbuf2, "w") as zf:
                zf.writestr("safe-zip/SKILL.md", "---\nname: safe-zip\n---\nhello")
                zf.writestr("C:/evil.txt", "nope")
                zf.writestr("/etc/evil.txt", "nope")
            rec = mgr.import_skill("safe-zip.zip", zbuf2.getvalue())
            self.assertEqual(rec["id"], "safe-zip")
            self.assertFalse((ws / ".agents" / "skills" / "safe-zip" / "evil.txt").exists())

    def test_reply_formatter_edge_cases(self) -> None:
        # Multi-group regex must NOT crash with TypeError
        cfg = ReplyConfig(
            segmented_reply=SegmentedReplyConfig(
                enabled=True,
                split_mode="regex",
                regex=r"([^。！？]+)(。|！|？|$)",
            )
        )
        segs = split_and_clean_reply("你好！我是测试。", cfg)
        self.assertEqual(segs, ["你好！", "我是测试。"])

        # URL protection in truncate_reply & words split
        url_text = "看图 ![img](https://example.com/very/long/path/image_1234567890.png) 后面还有一些文字"
        truncated = truncate_reply(url_text, 25)
        self.assertNotIn("https://example.com/ve...", truncated)
        self.assertIn("https://example.com/very/long/path/image_1234567890.png", truncated)

        # content_cleanup_rule stripping all content must return [] instead of leaking raw
        cfg_clean = ReplyConfig(
            segmented_reply=SegmentedReplyConfig(
                enabled=True,
                split_mode="regex",
                regex=r".+",
                content_cleanup_rule=r"SECRET_ONLY",
            )
        )
        self.assertEqual(split_and_clean_reply("SECRET_ONLY", cfg_clean), [])

        # Longest keyword masked first
        ok, masked, hit = apply_content_safety(
            "请不要发敏感词测试内容",
            ContentSafetyConfig(enabled=True, action="mask", blocked_keywords=["敏感", "敏感词测试"]),
        )
        self.assertTrue(ok)
        self.assertEqual(hit, "敏感")
        self.assertNotIn("测试", masked)

    def test_cron_dow_and_dos_guard_and_config_defensive_parsing(self) -> None:
        # Huge range must fail fast instead of hanging
        with self.assertRaises(ValueError):
            _parse_cron_field("0-999999999", 0, 59)

        # dow 5-7 includes Fri(5), Sat(6), Sun(0)
        self.assertEqual(_parse_cron_field("5-7", 0, 6, is_dow=True), {5, 6, 0})

        # dow 7-7 means Sunday(0) only, NOT all 7 days
        self.assertEqual(_parse_cron_field("7-7", 0, 6, is_dow=True), {0})
        import datetime as dt
        self.assertTrue(cron_matches("0 8 * * 7-7", dt.datetime(2026, 11, 22, 8, 0)))   # Sunday
        self.assertFalse(cron_matches("0 8 * * 7-7", dt.datetime(2026, 11, 23, 8, 0)))  # Monday

        # Null config sections & string-as-list must not crash or split chars
        cfg = load_config_from_dict(
            {
                "admins": "123456",
                "agy": None,
                "triggers": {"context_llm": {"judge_context_window": 0}},
                "collector": {"context_message_limit": -5, "history_buffer_size": 0},
            },
            base_dir=Path("."),
        )
        self.assertEqual(cfg.admins, ["123456"])
        self.assertGreaterEqual(cfg.triggers.context_llm.judge_context_window, 1)
        self.assertGreaterEqual(cfg.collector.context_message_limit, 1)
        self.assertGreaterEqual(cfg.collector.history_buffer_size, 1)

        # Invalid keyword regex must not crash TriggerEngine while valid regex still matches
        eng = TriggerEngine(TriggersConfig(keyword=KeywordTriggerConfig(enabled=True, regex_patterns=["[invalid(", r"ok\d+"])))
        hit = eng.evaluate_fast(
            IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="999",
                scene="group",
                channel_id="100",
                message_id="m_re",
                user_id="u1",
                sender_name="A",
                text="ok123",
                timestamp=time.time(),
            )
        )
        self.assertTrue(hit.triggered)

    async def test_ask_runner_never_swallows_errors_and_media_ssrf_and_staging_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(base / "ws")
            cfg.agy.data_dir = str(base / "data")
            runner = _DummyRunner()
            runner.raise_on_ask = RuntimeError("boom")
            app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]

            # _ask_runner must propagate RuntimeError and NOT invoke runner.ask a second time without kwargs
            with self.assertRaises(RuntimeError):
                await app._ask_runner("s1", "hello", allow_fallback=False, defer_geo_fallback=True)
            self.assertEqual(len(runner.calls), 1)
            self.assertFalse(runner.calls[0]["allow_fallback"])
            self.assertTrue(runner.calls[0]["defer_geo_fallback"])

            # Non-image secret file must NOT be staged into .media_cache/
            secret_file = base / "config.json"
            secret_file.write_text('{"password": "topsecret"}', encoding="utf-8")
            staged_secret = app._stage_subagent_reference_images([str(secret_file)])
            self.assertEqual(staged_secret, [])

            # Genuine image file IS staged cleanly
            real_img = base / "ref.jpg"
            real_img.write_bytes(_TINY_PNG)
            staged_img = app._stage_subagent_reference_images([str(real_img)])
            self.assertEqual(len(staged_img), 1)
            self.assertTrue(staged_img[0].startswith(".media_cache/subref_") and staged_img[0].endswith(".png"))

            # SSRF guard blocks loopback & metadata URLs
            for ssrf_url in (
                "http://127.0.0.1:8618/api/config",
                "http://localhost:8618/api/status",
                "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/secret.png",
                "http://[::1]:8618/",
            ):
                self.assertFalse(await _is_safe_public_http_url(ssrf_url), f"SSRF not blocked: {ssrf_url}")
                self.assertIsNone(await app._load_reply_image_bytes(ssrf_url))

            # Non-image local file blocked in _load_reply_image_bytes
            self.assertIsNone(await app._load_reply_image_bytes(str(secret_file)))

            await app.shutdown()

    async def test_media_pruning_jpeg_app14_and_html_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / ".media_cache"
            cache_dir.mkdir()
            for idx in range(6):
                p = cache_dir / f"img_{idx}.png"
                p.write_bytes(_TINY_PNG + b"x" * 100)
                os.utime(p, (1000 + idx, 1000 + idx))
            removed = prune_media_cache(cache_dir, max_files=3, max_bytes=10 * 1024 * 1024)
            self.assertEqual(removed, 3)
            remaining = sorted(p.name for p in cache_dir.iterdir())
            self.assertEqual(remaining, ["img_3.png", "img_4.png", "img_5.png"])

            # JPEG APP14 (0xEE) preserved while APP1 (0xE1 EXIF) stripped
            app1_payload = b"Exif\x00\x00test"
            app1 = b"\xff\xe1" + struct.pack(">H", len(app1_payload) + 2) + app1_payload
            app14_payload = b"Adobe\x00\x64\x00\x00\x00\x00\x02"
            app14 = b"\xff\xee" + struct.pack(">H", len(app14_payload) + 2) + app14_payload
            fake_jpg = b"\xff\xd8" + app1 + app14 + b"\xff\xda\x00\x02\xaa\xbb\xff\xd9"
            cleaned = sanitize_jpeg_segments(fake_jpg)
            self.assertNotIn(b"Exif\x00\x00", cleaned)
            self.assertIn(b"Adobe\x00", cleaned)

            # Non-image local file ignored in materialize_image -> falls back cleanly
            bad_local = Path(tmp) / "not_an_img.jpg"
            bad_local.write_text("<html>403 Forbidden</html>", encoding="utf-8")
            self.assertFalse(looks_like_image_bytes(bad_local.read_bytes()))
            att = MediaAttachment(kind="image", source_path=str(bad_local))
            res = await materialize_image(att, workspace_dir=Path(tmp))
            self.assertIsNone(res)

    async def test_webui_utf8_login_rate_limit_session_invalidation_and_400_handling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = BotConfig(base_dir=base, config_path=base / "config.json")
            cfg.agy.workspace_dir = str(base / "ws")
            cfg.agy.data_dir = str(base / "data")
            cfg.webui.username = "admin"
            cfg.webui.password = "pass123"
            app = BotApplication(cfg, runner=_DummyRunner())  # type: ignore[arg-type]
            srv = WebUIServer(app, cfg.webui)

            async with TestClient(TestServer(srv.web_app)) as client:
                # Non-ASCII login must return 401, NEVER 500
                r_cn = await client.post("/api/auth/login", json={"username": "中文用户", "password": "密码"})
                self.assertEqual(r_cn.status, 401)

                # Non-dict JSON body must return 400, NEVER 500
                r_arr = await client.post("/api/auth/login", json=["not", "a", "dict"])
                self.assertEqual(r_arr.status, 400)

                # Valid login
                r_ok = await client.post("/api/auth/login", json={"username": "admin", "password": "pass123"})
                self.assertEqual(r_ok.status, 200)

                # Non-dict JSON on authenticated endpoints -> 400
                r_pg = await client.post("/api/playground/chat", json=["bad"])
                self.assertEqual(r_pg.status, 400)
                r_sc = await client.post("/api/triggers/script/test", json={"rule": {"timeout_seconds": "abc"}})
                self.assertEqual(r_sc.status, 400)

                # Security settings path outside allowed roots -> 400
                app.config.agy.settings_file = str(base.parent / "outside.json")
                r_sec = await client.get("/api/security-settings")
                self.assertEqual(r_sec.status, 400)

                # Password change invalidates existing session cookie
                new_webui = load_config_from_dict(
                    {"webui": {"username": "admin", "password": "new_password"}},
                    base_dir=base,
                ).webui
                srv.update_config(new_webui)
                r_after = await client.get("/api/status")
                self.assertEqual(r_after.status, 401)

                # Login brute-force rate limit triggers 429 after 10 failures
                for _ in range(10):
                    r_bad = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
                    self.assertEqual(r_bad.status, 401)
                r_limited = await client.post("/api/auth/login", json={"username": "admin", "password": "new_password"})
                self.assertEqual(r_limited.status, 429)

            await app.shutdown()

    async def test_adapters_timestamps_dedup_mute_proxy_and_plugins(self) -> None:
        # Negative / millisecond timestamps must not crash on Windows
        self.assertRegex(_format_ts(-50), r"^\d{2}:\d{2}:\d{2}$")
        self.assertRegex(_format_ts(1700000000000), r"^\d{2}:\d{2}:\d{2}$")

        # OneBot null user_id / group_id rejected instead of turning into "None"
        ob = OneBotV11Adapter(PlatformConfig(id="qq_1", type="onebot_v11", self_id="999"))
        self.assertIsNone(await ob.normalize_event({"message_type": "group", "user_id": None, "group_id": 100}))
        self.assertIsNone(await ob.normalize_event({"message_type": "group", "user_id": 100, "group_id": None}))

        # Deeply nested inline forward terminates cleanly at depth limit
        deep: dict[str, Any] = {"type": "node", "data": {"user_id": "1", "nickname": "A", "content": "leaf"}}
        for _ in range(12):
            deep = {
                "type": "node",
                "data": {
                    "user_id": "1",
                    "nickname": "A",
                    "content": [{"type": "forward", "data": {"content": [deep]}}],
                },
            }
        rendered = await ob._expand_forward_nodes(
            [deep],
            visited_forwards=set(),
            attachments=[],
            attachment_source="forward",
        )
        self.assertIn("已截断", rendered)

        # OneBot send_reply raises RuntimeError on status=="failed"
        async def _fake_call_action(action: str, params: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
            return {"status": "failed", "retcode": 1200, "wording": "发送失败"}

        ob.call_action = _fake_call_action  # type: ignore[method-assign]
        sample_target = IncomingMessage(
            adapter_id="qq_1",
            platform="onebot_v11",
            self_id="999",
            scene="group",
            channel_id="100",
            message_id="m1",
            user_id="u1",
            sender_name="U",
            text="hi",
            timestamp=time.time(),
        )
        with self.assertRaises(RuntimeError):
            await ob.send_reply(sample_target, "test")

        # GenericWS handles null IDs, non-list at_targets, millisecond timestamps
        gws = GenericWSAdapter(PlatformConfig(id="g1", type="generic_ws", self_id="bot"))
        self.assertIsNone(gws.normalize_event({"type": "message", "user_id": None, "channel_id": "c1"}))
        gmsg = gws.normalize_event(
            {
                "type": "message",
                "user_id": "u1",
                "channel_id": "c1",
                "at_targets": "not-a-list",
                "timestamp": 1700000000123,
                "text": "hello",
            }
        )
        self.assertIsNotNone(gmsg)
        assert gmsg is not None
        self.assertEqual(gmsg.at_targets, [])
        self.assertLess(gmsg.timestamp, 1e10)

        # Duplicate message_id deduplication in BotApplication.handle_message
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(base / "ws")
            cfg.agy.data_dir = str(base / "data")
            cfg.collector.quiet_wait_seconds = 0.0
            cfg.collector.max_wait_seconds = 0.0
            plat = PlatformConfig(id="qq_1", type="onebot_v11", self_id="999")
            cfg.platforms = [plat]
            runner = _DummyRunner()
            app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
            adapter = _DummyAdapter(plat)
            app.registry.adapters["qq_1"] = adapter

            dup_msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="999",
                scene="group",
                channel_id="200",
                message_id="dup-1",
                user_id="u1",
                sender_name="Alice",
                text="hello",
                at_targets=["999"],
                timestamp=time.time(),
            )
            await app.handle_message(dup_msg)
            await app.handle_message(dup_msg)
            await asyncio.sleep(0.05)
            self.assertEqual(len(runner.calls), 1)

            # Whole-group mute (duration_seconds=0) sets mute cooldown until lift_ban
            guard = ChannelActivationStore(base / "data")
            guard.activate_group("qq_1", "300")
            guard.on_group_muted("qq_1", "300", duration_seconds=0.0)
            self.assertTrue(guard.is_muted("qq_1:group:300"))
            self.assertFalse(guard.is_activated("qq_1:group:300"))
            guard.on_group_unmuted("qq_1", "300")
            self.assertFalse(guard.is_muted("qq_1:group:300"))
            self.assertFalse(guard.is_activated("qq_1:group:300"))

            # Unknown plugin update does not pollute config
            with self.assertRaises(ValueError):
                await app.plugin_manager.update_plugin_config("nonexistent", {"k": "v"})
            self.assertNotIn("nonexistent", app.config.plugins.configs)

            await app.shutdown()

        # Plugin empty string reply must NOT swallow message
        self.assertIsNone(_normalize_reply("", default_stop=True))
        self.assertIsNone(_normalize_reply("   ", default_stop=True))

        # Proxy guard parses -ext-ctl=0.0.0.0:9097 and rewrites 0.0.0.0 -> 127.0.0.1
        ctl = parse_controller_from_cmdline("mihomo.exe -ext-ctl=0.0.0.0:9097 -secret=mysec")
        self.assertIsNotNone(ctl)
        assert ctl is not None
        self.assertEqual(ctl.controller_url, "http://127.0.0.1:9097")
        self.assertEqual(ctl.secret, "mysec")


if __name__ == "__main__":
    unittest.main()
