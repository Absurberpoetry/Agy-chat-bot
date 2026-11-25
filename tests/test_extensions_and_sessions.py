from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agychat_bot.app import BotApplication  # noqa: E402
from agychat_bot.config import BotConfig, WebUIConfig  # noqa: E402
from agychat_bot.models import IncomingMessage, JudgeDecision, RunnerResult  # noqa: E402
from agychat_bot.webui.server import WebUIServer  # noqa: E402


class _FakeRunner:
    def __init__(self) -> None:
        self.reset_calls = 0

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    async def ask(self, session_key: str, prompt: str, **kwargs) -> RunnerResult:
        return RunnerResult(ok=True, response="ok")

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        return JudgeDecision(should_reply=False)

    async def reset_sessions(self, session_key: str | None = None) -> int:
        self.reset_calls += 1
        return 1

    async def update_config(self, *args, **kwargs) -> None:
        return None

    async def close(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        return {"model": "test-model", "warm_sessions": [], "circuit_open": False}


class ExtensionsAndSessionsTests(unittest.IsolatedAsyncioTestCase):
    def _make_app(self, base: Path) -> tuple[BotApplication, _FakeRunner, WebUIConfig]:
        cfg = BotConfig(base_dir=base)
        cfg.agy.workspace_dir = str(base / "bot_workspace")
        cfg.agy.data_dir = str(base / "data")
        cfg.plugins.dir = str(base / "plugins")
        cfg.webui = WebUIConfig(enabled=True, host="127.0.0.1", port=0, username="star", password="secret")
        runner = _FakeRunner()
        app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
        return app, runner, cfg.webui

    async def test_persistent_7day_session_survives_server_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_app, _, wcfg = self._make_app(base)

            # Instance 1: login
            srv1 = WebUIServer(bot_app, wcfg)
            async with TestClient(TestServer(srv1.web_app)) as client1:
                r = await client1.post("/api/auth/login", json={"username": "star", "password": "secret"})
                self.assertEqual(r.status, 200)
                cookie = r.cookies.get("agychat_bot_session")
                self.assertIsNotNone(cookie)
                assert cookie is not None
                # At least 2 days (we set 7 days = 604800s)
                self.assertGreaterEqual(int(cookie["max-age"]), 2 * 86400)
                self.assertEqual(int(cookie["max-age"]), 7 * 86400)
                token_val = cookie.value

            sessions_file = base / "data" / "webui_sessions.json"
            self.assertTrue(sessions_file.is_file())
            saved = json.loads(sessions_file.read_text(encoding="utf-8"))
            self.assertIn(token_val, saved)
            self.assertGreater(saved[token_val], time.time() + 6 * 86400)

            # Instance 2 (simulating process restart): same cookie stays authenticated!
            srv2 = WebUIServer(bot_app, wcfg)
            async with TestClient(TestServer(srv2.web_app)) as client2:
                r_me = await client2.get("/api/auth/me", cookies={"agychat_bot_session": token_val})
                self.assertEqual(r_me.status, 200)
                d_me = await r_me.json()
                self.assertTrue(d_me["authenticated"])
                self.assertGreaterEqual(d_me["session_ttl_days"], 2.0)

                # Logout removes persisted token
                r_out = await client2.post("/api/auth/logout", cookies={"agychat_bot_session": token_val})
                self.assertEqual(r_out.status, 200)
                r_me2 = await client2.get("/api/auth/me", cookies={"agychat_bot_session": token_val})
                self.assertFalse((await r_me2.json())["authenticated"])

    async def test_plugins_ui_hides_source_and_open_location_works(self) -> None:
        html = (ROOT / "src" / "agychat_bot" / "webui" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("打开插件所在位置", html)
        self.assertNotIn("plSourceEditor", html)
        self.assertNotIn("plPaneSource", html)
        self.assertNotIn("plConfigEditor", html)
        self.assertNotIn("sk_body", html)
        self.assertIn("Skill &amp; MCP", html)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_app, _, wcfg = self._make_app(base)
            wcfg.password = ""
            await bot_app.plugin_manager.create_plugin("demo_open")
            srv = WebUIServer(bot_app, wcfg)

            with patch("agychat_bot.webui.server.open_in_file_manager", return_value=(True, "ok")) as mock_open:
                async with TestClient(TestServer(srv.web_app)) as client:
                    r1 = await client.post("/api/plugins/demo_open/open-location")
                    self.assertEqual(r1.status, 200)
                    d1 = await r1.json()
                    self.assertTrue(d1["ok"])
                    self.assertTrue(d1["path"].endswith("demo_open.py"))
                    mock_open.assert_called_once()

                    r2 = await client.post("/api/plugins/open-location")
                    self.assertEqual(r2.status, 200)
                    d2 = await r2.json()
                    self.assertTrue(d2["ok"])

    async def test_skills_crud_toggle_and_zip_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            bot_app, runner, wcfg = self._make_app(base)
            wcfg.password = ""
            srv = WebUIServer(bot_app, wcfg)

            async with TestClient(TestServer(srv.web_app)) as client:
                # Create workspace skill
                r_add = await client.post(
                    "/api/skills",
                    json={
                        "id": "group-summary",
                        "name": "group-summary",
                        "description": "当群友要求总结聊天记录时使用",
                        "body": "# 总结步骤\n1. 提取要点\n2. 简短输出",
                    },
                )
                self.assertEqual(r_add.status, 200)
                skill_file = base / "bot_workspace" / ".agents" / "skills" / "group-summary" / "SKILL.md"
                self.assertTrue(skill_file.is_file())
                self.assertIn("当群友要求总结聊天记录时使用", skill_file.read_text(encoding="utf-8"))
                self.assertGreaterEqual(runner.reset_calls, 1)

                # Disable skill -> moved to skills_disabled
                r_dis = await client.post("/api/skills/group-summary/toggle", json={"enabled": False})
                self.assertEqual(r_dis.status, 200)
                self.assertFalse(skill_file.exists())
                self.assertTrue((base / "bot_workspace" / ".agents" / "skills_disabled" / "group-summary" / "SKILL.md").is_file())

                # Re-enable -> restored to .agents/skills
                r_en = await client.post("/api/skills/group-summary/toggle", json={"enabled": True})
                self.assertEqual(r_en.status, 200)
                self.assertTrue(skill_file.is_file())

                # Import .zip skill bundle (with scripts/ and a ZipSlip traversal entry)
                zbuf = io.BytesIO()
                with zipfile.ZipFile(zbuf, "w") as zf:
                    zf.writestr("my-bundle/SKILL.md", "---\nname: my-bundle\ndescription: zip skill\n---\n\nHello zip\n")
                    zf.writestr("my-bundle/scripts/run.py", "print('hi')\n")
                    zf.writestr("../escape.txt", "should not escape")
                b64_zip = base64.b64encode(zbuf.getvalue()).decode("ascii")
                r_imp = await client.post(
                    "/api/skills/import",
                    json={"filename": "my-bundle.zip", "content_base64": b64_zip},
                )
                self.assertEqual(r_imp.status, 200)
                self.assertTrue((base / "bot_workspace" / ".agents" / "skills" / "my-bundle" / "SKILL.md").is_file())
                self.assertTrue((base / "bot_workspace" / ".agents" / "skills" / "my-bundle" / "scripts" / "run.py").is_file())
                self.assertFalse((base / "bot_workspace" / ".agents" / "skills" / "escape.txt").exists())

                # Delete workspace skill
                r_del = await client.delete("/api/skills/group-summary")
                self.assertEqual(r_del.status, 200)
                self.assertFalse(skill_file.exists())

    async def test_mcp_crud_sync_import_and_live_handshake_probe(self) -> None:
        # Mock Streamable HTTP MCP endpoint
        async def handle_mcp_http(request: web.Request) -> web.Response:
            req = await request.json()
            return web.json_response({
                "jsonrpc": "2.0",
                "id": req.get("id", 1),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "serverInfo": {"name": "mock-http-mcp", "version": "2.1.0"},
                    "capabilities": {},
                },
            })

        http_app = web.Application()
        http_app.router.add_post("/mcp", handle_mcp_http)
        http_runner = web.AppRunner(http_app)
        await http_runner.setup()
        site = web.TCPSite(http_runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                bot_app, runner, wcfg = self._make_app(base)
                wcfg.password = ""
                srv = WebUIServer(bot_app, wcfg)
                mcp_file = base / "bot_workspace" / ".agents" / "mcp_config.json"

                async with TestClient(TestServer(srv.web_app)) as client:
                    # Add HTTP MCP server
                    r_http = await client.post(
                        "/api/mcp",
                        json={
                            "id": "remote-docs",
                            "transport": "http",
                            "url": f"http://127.0.0.1:{port}/mcp",
                            "headers": {"X-Test": "1"},
                            "enabled": True,
                        },
                    )
                    self.assertEqual(r_http.status, 200)
                    synced = json.loads(mcp_file.read_text(encoding="utf-8"))
                    self.assertIn("remote-docs", synced["mcpServers"])
                    self.assertEqual(synced["mcpServers"]["remote-docs"]["url"], f"http://127.0.0.1:{port}/mcp")

                    # Test HTTP MCP handshake
                    r_test_http = await client.post("/api/mcp/remote-docs/test")
                    self.assertEqual(r_test_http.status, 200)
                    d_th = await r_test_http.json()
                    self.assertTrue(d_th["ok"])
                    self.assertEqual(d_th["server_info"]["name"], "mock-http-mcp")

                    # Test Stdio MCP handshake using a tiny python inline JSON-RPC responder
                    stdio_code = (
                        "import sys, json; "
                        "line = sys.stdin.readline(); "
                        "req = json.loads(line); "
                        "print(json.dumps({'jsonrpc':'2.0','id':req.get('id',1),'result':{'serverInfo':{'name':'mock-stdio','version':'1.0'}}}), flush=True)"
                    )
                    r_test_stdio = await client.post(
                        "/api/mcp/test",
                        json={
                            "id": "local-stdio",
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": ["-c", stdio_code],
                        },
                    )
                    self.assertEqual(r_test_stdio.status, 200)
                    d_ts = await r_test_stdio.json()
                    self.assertTrue(d_ts["ok"])
                    self.assertEqual(d_ts["server_info"]["name"], "mock-stdio")

                    # Batch JSON import
                    r_imp = await client.post(
                        "/api/mcp/import",
                        json={
                            "config": {
                                "mcpServers": {
                                    "sqlite-helper": {"command": "python", "args": ["-m", "sqlite_mcp"]},
                                }
                            }
                        },
                    )
                    self.assertEqual(r_imp.status, 200)
                    synced2 = json.loads(mcp_file.read_text(encoding="utf-8"))
                    self.assertIn("sqlite-helper", synced2["mcpServers"])

                    # Disable sqlite-helper -> removed from active mcp_config.json but kept in registry
                    r_dis = await client.post("/api/mcp/sqlite-helper/toggle", json={"enabled": False})
                    self.assertEqual(r_dis.status, 200)
                    synced3 = json.loads(mcp_file.read_text(encoding="utf-8"))
                    self.assertNotIn("sqlite-helper", synced3["mcpServers"])
                    self.assertIn("remote-docs", synced3["mcpServers"])

                    # List still shows disabled server
                    r_list = await client.get("/api/mcp")
                    ws_ids = [x["id"] for x in (await r_list.json())["data"] if x["scope"] == "workspace"]
                    self.assertIn("sqlite-helper", ws_ids)

                    # Delete removes completely
                    r_del = await client.delete("/api/mcp/sqlite-helper")
                    self.assertEqual(r_del.status, 200)
                    self.assertGreaterEqual(runner.reset_calls, 3)
        finally:
            await http_runner.cleanup()

    async def test_context_history_persistence_and_warm_recycle_preserves_sessions(self) -> None:
        from agychat_bot.collector import GroupContextStore
        from agychat_bot.config import AgyConfig, RateLimitConfig
        from agychat_bot.models import IncomingMessage, MediaAttachment
        from agychat_bot.runner.agy_stream import AgyRunner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ctx_file = base / "context_history.json"
            store1 = GroupContextStore(history_limit=50, persist_file=ctx_file)
            msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="10000",
                scene="private",
                channel_id="1000010001",
                message_id="m_99",
                user_id="1000010001",
                sender_name="荒谬成诗",
                text="帮我记住这句话",
                timestamp=1700000000.0,
                attachments=[MediaAttachment(kind="image", local_path=".media_cache/a.png")],
            )
            store1.add_message(msg)
            store1.flush()
            self.assertTrue(ctx_file.exists())

            # Reload in a brand new process/store instance
            store2 = GroupContextStore(history_limit=50, persist_file=ctx_file)
            recent = store2.get_recent("qq_1:private:1000010001", limit=10)
            self.assertEqual(len(recent), 1)
            self.assertEqual(recent[0].text, "帮我记住这句话")
            self.assertEqual(recent[0].attachments[0].local_path, ".media_cache/a.png")
            cached = store2.lookup_message("qq_1", "m_99")
            self.assertIsNotNone(cached)
            assert cached is not None
            self.assertEqual(cached.text, "帮我记住这句话")

            # Verify clear_saved=False preserves sessions.json while clear_saved=True wipes it
            data_dir = base / "data"
            ws_dir = base / "ws"
            data_dir.mkdir()
            (data_dir / "sessions.json").write_text(
                json.dumps({"qq_1:private:1000010001": "conv-abc-123"}),
                encoding="utf-8",
            )
            runner = AgyRunner(
                AgyConfig(resume_on_restart=True),
                RateLimitConfig(),
                workspace_dir=ws_dir,
                data_dir=data_dir,
            )
            await runner.reset_sessions(clear_saved=False)
            saved_after_warm = json.loads((data_dir / "sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_after_warm.get("qq_1:private:1000010001"), "conv-abc-123")

            await runner.reset_sessions(clear_saved=True)
            saved_after_full = json.loads((data_dir / "sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_after_full, {})


if __name__ == "__main__":
    unittest.main()
