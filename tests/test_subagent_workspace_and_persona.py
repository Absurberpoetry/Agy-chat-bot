from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agychat_bot.app import BotApplication
from agychat_bot.config import AgyConfig, RateLimitConfig, config_to_dict, load_config_from_dict
from agychat_bot.persona_store import SUBAGENT_FOLLOW_MAIN
from agychat_bot.runner.agy_stream import AgyRunner, MockAgentRunner
from agychat_bot.webui.server import WebUIServer
from aiohttp import web as aiohttp_web
from aiohttp.test_utils import TestClient, TestServer


_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _DummyStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        return b""


class _DummyStdin:
    def __init__(self) -> None:
        self._closed = False
        self.writes: list[bytes] = []

    def is_closing(self) -> bool:
        return self._closed

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True


class _FakeProc:
    def __init__(self, response_text: str) -> None:
        self.pid = 43210
        self.returncode: int | None = None
        self.stdin = _DummyStdin()
        self.stdout = _DummyStream(
            [
                (json.dumps({"event": "init", "conversation_id": "conv-1"}) + "\n").encode("utf-8"),
                (
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "status": "SUCCESS",
                                "response": response_text,
                                "num_turns": 1,
                            },
                        }
                    )
                    + "\n"
                ).encode("utf-8"),
            ]
        )
        self.stderr = _DummyStream([])

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        self.returncode = 0
        return 0


class SubagentWorkspaceAndPersonaTests(unittest.IsolatedAsyncioTestCase):
    def test_config_roundtrip_subagent_persona_id(self) -> None:
        cfg = load_config_from_dict(
            {"agy": {"subagent_persona_id": "draw_artist"}},
            base_dir=Path("."),
        )
        self.assertEqual(cfg.agy.subagent_persona_id, "draw_artist")
        dumped = config_to_dict(cfg)
        self.assertEqual(dumped["agy"]["subagent_persona_id"], "draw_artist")

    async def test_isolated_subagent_workspace_persona_modes_and_mcp_sync(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = load_config_from_dict(
                {
                    "bot_name": "安挽行",
                    "agy": {
                        "mode": "mock",
                        "workspace_dir": str(base / "bot_workspace"),
                        "data_dir": str(base / "data"),
                        "subagent_enabled": True,
                        "subagent_persona_id": "",
                    },
                    "collector": {"db_context_enabled": True},
                },
                base_dir=base,
            )
            app = BotApplication(cfg, runner=MockAgentRunner())
            try:
                app.persona_store.upsert("安挽行", "主聊人设：胆小怯生生的小猫性格，严禁 Markdown")
                app.persona_store.upsert("绘图专员", "子代理专属：白发蓝瞳原创二次元女性角色绘图规范")
                app.mcp_manager.upsert_server(
                    "wedraw",
                    {
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["-y", "mcp-remote", "https://example.com/mcp"],
                        "usage_prompt": "优先选用最新画图模型",
                        "async_subagent": True,
                        "enabled": True,
                    },
                )
                await app.apply_active_persona("安挽行")

                sub_ws = base / "data" / "subagent_workspace"
                # 1) Sibling directory (NOT inside bot_workspace)
                self.assertFalse(sub_ws.resolve().is_relative_to((base / "bot_workspace").resolve()))

                # 2) Subagent MCP config has wedraw, excludes builtin chat_history
                sub_mcp = json.loads((sub_ws / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
                self.assertIn("wedraw", sub_mcp["mcpServers"])
                self.assertNotIn("chat_history", sub_mcp["mcpServers"])

                # 3) Pure tool mode (""): NO main persona leak, HAS wedraw prompt, NO <agy_bg_task> dispatch protocol
                pure_md = (sub_ws / "GEMINI.md").read_text(encoding="utf-8")
                self.assertIn("纯净工具执行模式", pure_md)
                self.assertNotIn("胆小怯生生", pure_md)
                self.assertIn("优先选用最新画图模型", pure_md)
                self.assertNotIn("chat_history", pure_md)
                self.assertNotIn("后台并发子代理派发协议", pure_md)

                # 4) Dedicated Persona Workshop binding ("绘图专员")
                cfg2 = load_config_from_dict(config_to_dict(app.config), base_dir=base)
                cfg2.agy.subagent_persona_id = "绘图专员"
                await app.reload_config(cfg2)
                custom_md = (sub_ws / "GEMINI.md").read_text(encoding="utf-8")
                self.assertIn("子代理独立绑定人格：绘图专员", custom_md)
                self.assertIn("白发蓝瞳原创二次元女性角色绘图规范", custom_md)
                self.assertNotIn("胆小怯生生", custom_md)

                # 5) Follow main persona ("__follow_main__")
                cfg3 = load_config_from_dict(config_to_dict(app.config), base_dir=base)
                cfg3.agy.subagent_persona_id = SUBAGENT_FOLLOW_MAIN
                await app.reload_config(cfg3)
                follow_md = (sub_ws / "GEMINI.md").read_text(encoding="utf-8")
                self.assertIn("跟随主代理当前人格：安挽行", follow_md)
                self.assertIn("胆小怯生生", follow_md)

                # 6) Staging reference images writes into subagent_workspace/.media_cache/
                main_ref = base / "bot_workspace" / ".media_cache" / "ref_ok.png"
                main_ref.parent.mkdir(parents=True, exist_ok=True)
                main_ref.write_bytes(_TINY_PNG)
                staged = app._stage_subagent_reference_images([".media_cache/ref_ok.png"])
                self.assertEqual(staged, [".media_cache/ref_ok.png"])
                self.assertTrue((sub_ws / ".media_cache" / "ref_ok.png").is_file())

                # _load_reply_image_bytes can load relative image saved in subagent_workspace
                sub_out = sub_ws / ".media_cache" / "generated_1.png"
                sub_out.write_bytes(_TINY_PNG)
                loaded = await app._load_reply_image_bytes(".media_cache/generated_1.png")
                self.assertEqual(loaded, _TINY_PNG)
            finally:
                await app.shutdown()

    async def test_agy_runner_spawns_subagent_in_isolated_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            main_ws = base / "bot_workspace"
            data_dir = base / "data"
            sub_ws = data_dir / "subagent_workspace"
            seen_cwds: list[str] = []

            async def fake_factory(*args: Any, cwd: str = "", env: dict[str, str] | None = None) -> Any:
                seen_cwds.append(cwd)
                return _FakeProc("ok")

            runner = AgyRunner(
                AgyConfig(executable="fake-agy", subagent_max_concurrent=1, subagent_timeout_seconds=30.0),
                RateLimitConfig(max_concurrent_requests=1),
                workspace_dir=main_ws,
                data_dir=data_dir,
                subagent_workspace_dir=sub_ws,
                process_factory=fake_factory,
            )
            try:
                r1 = await runner.ask("qq:group:1", "hello main")
                self.assertTrue(r1.ok)
                r2 = await runner.run_subagent_task("t1", "draw illustration")
                self.assertTrue(r2.ok)
                self.assertEqual(len(seen_cwds), 2)
                self.assertEqual(Path(seen_cwds[0]).resolve(), main_ws.resolve())
                self.assertEqual(Path(seen_cwds[1]).resolve(), sub_ws.resolve())
            finally:
                await runner.shutdown()

    async def test_webui_platform_connection_test_endpoint(self) -> None:
        # Spin up a tiny mock OneBot v11 WS server on an ephemeral local port
        received_auth: list[str] = []

        async def mock_ob_ws(request: aiohttp_web.Request) -> aiohttp_web.WebSocketResponse:
            received_auth.append(request.headers.get("Authorization", ""))
            ws = aiohttp_web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                data = json.loads(msg.data)
                if data.get("action") == "get_login_info":
                    await ws.send_json(
                        {
                            "status": "ok",
                            "retcode": 0,
                            "echo": data.get("echo"),
                            "data": {"user_id": 999000111, "nickname": "TestBot"},
                        }
                    )
            return ws

        ob_app = aiohttp_web.Application()
        ob_app.router.add_get("/", mock_ob_ws)
        ob_runner = aiohttp_web.AppRunner(ob_app)
        await ob_runner.setup()
        ob_site = aiohttp_web.TCPSite(ob_runner, "127.0.0.1", 0)
        await ob_site.start()
        port = ob_site._server.sockets[0].getsockname()[1]

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = load_config_from_dict(
                {
                    "agy": {
                        "mode": "mock",
                        "workspace_dir": str(base / "bot_workspace"),
                        "data_dir": str(base / "data"),
                    },
                    "webui": {"enabled": True, "username": "star", "password": ""},
                },
                base_dir=base,
            )
            bot = BotApplication(cfg, runner=MockAgentRunner())
            webui = WebUIServer(bot, cfg.webui)
            try:
                async with TestClient(TestServer(webui.web_app)) as client:
                    # 1) Invalid scheme rejected gracefully
                    r_bad = await client.post(
                        "/api/platforms/test",
                        json={"id": "qq_1", "type": "onebot_v11", "ws_url": "http://127.0.0.1:3001"},
                    )
                    self.assertEqual(r_bad.status, 200)
                    d_bad = await r_bad.json()
                    self.assertFalse(d_bad["connected"])
                    self.assertIn("ws://", d_bad["error"])

                    # 2) Real mock OneBot v11 WS handshake + get_login_info succeeds
                    r_ok = await client.post(
                        "/api/platforms/test",
                        json={
                            "id": "qq_1",
                            "type": "onebot_v11",
                            "ws_url": f"ws://127.0.0.1:{port}",
                            "access_token": "tok123",
                        },
                    )
                    self.assertEqual(r_ok.status, 200)
                    d_ok = await r_ok.json()
                    self.assertTrue(d_ok["connected"])
                    self.assertEqual(d_ok["login_info"]["user_id"], "999000111")
                    self.assertEqual(d_ok["login_info"]["nickname"], "TestBot")
                    self.assertEqual(received_auth, ["Bearer tok123"])
            finally:
                await bot.shutdown()
                await ob_runner.cleanup()

    async def test_webui_chat_data_list_preview_and_permanent_delete(self) -> None:
        from agychat_bot.models import IncomingMessage

        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = load_config_from_dict(
                {
                    "agy": {
                        "mode": "mock",
                        "workspace_dir": str(base / "bot_workspace"),
                        "data_dir": str(base / "data"),
                    },
                    "collector": {"db_context_enabled": True},
                    "webui": {"enabled": True, "username": "star", "password": ""},
                },
                base_dir=base,
            )
            bot = BotApplication(cfg, runner=MockAgentRunner())
            webui = WebUIServer(bot, cfg.webui)
            try:
                # Seed 1 group session (3 msgs) and 1 private session (2 msgs)
                for i in range(1, 4):
                    bot.context_store.add_message(
                        IncomingMessage(
                            adapter_id="qq_1",
                            platform="onebot_v11",
                            self_id="999",
                            scene="group",
                            channel_id="10001",
                            message_id=f"gm_{i}",
                            user_id="u1",
                            sender_name="Alice",
                            text=f"群消息 {i}",
                            timestamp=1000.0 + i,
                        )
                    )
                for i in range(1, 3):
                    bot.context_store.add_message(
                        IncomingMessage(
                            adapter_id="qq_1",
                            platform="onebot_v11",
                            self_id="999",
                            scene="private",
                            channel_id="20002",
                            message_id=f"pm_{i}",
                            user_id="20002",
                            sender_name="Bob",
                            text=f"私聊消息 {i}",
                            timestamp=2000.0 + i,
                        )
                    )

                async with TestClient(TestServer(webui.web_app)) as client:
                    # 1) List sessions
                    r_list = await client.get("/api/chat-data/sessions")
                    self.assertEqual(r_list.status, 200)
                    d_list = await r_list.json()
                    self.assertEqual(d_list["stats"]["total_sessions"], 2)
                    self.assertEqual(d_list["stats"]["total_messages"], 5)

                    # 2) Preview group messages
                    r_prev = await client.get("/api/chat-data/messages?session_key=qq_1:group:10001&limit=10")
                    self.assertEqual(r_prev.status, 200)
                    d_prev = await r_prev.json()
                    self.assertEqual(len(d_prev["data"]), 3)
                    self.assertEqual(d_prev["data"][-1]["text"], "群消息 3")

                    # 3) Permanently delete ONLY the group session (both context & SQLite DB!)
                    r_del = await client.delete("/api/chat-data/sessions?session_key=qq_1:group:10001")
                    self.assertEqual(r_del.status, 200)
                    d_del = await r_del.json()
                    self.assertEqual(d_del["deleted_messages"], 3)

                    # Group session completely gone from DB & memory; private session untouched
                    self.assertEqual(bot.context_store.get_recent("qq_1:group:10001", limit=10), [])
                    r_list2 = await client.get("/api/chat-data/sessions")
                    d_list2 = await r_list2.json()
                    self.assertEqual(d_list2["stats"]["total_sessions"], 1)
                    self.assertEqual(d_list2["data"][0]["session_key"], "qq_1:private:20002")

                    # 4) Delete all remaining
                    r_del_all = await client.delete("/api/chat-data/sessions?all=1")
                    self.assertEqual(r_del_all.status, 200)
                    r_list3 = await client.get("/api/chat-data/sessions")
                    d_list3 = await r_list3.json()
                    self.assertEqual(d_list3["stats"]["total_sessions"], 0)
                    self.assertEqual(d_list3["stats"]["total_messages"], 0)
            finally:
                await bot.shutdown()


if __name__ == "__main__":
    unittest.main()
