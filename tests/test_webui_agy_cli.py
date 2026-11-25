from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp import WSMsgType
from aiohttp.test_utils import TestClient, TestServer

from agy_bot.app import BotApplication
from agy_bot.config import BotConfig
from agy_bot.models import IncomingMessage, RunnerResult
from agy_bot.webui.cli_pty import extract_auth_urls, strip_ansi
from agy_bot.webui.server import WebUIServer


class _DummyRunner:
    def __init__(self) -> None:
        self.reset_calls = 0

    async def ask(self, session_key: str, prompt: str, **kwargs: Any) -> RunnerResult:
        return RunnerResult(ok=True, response="ok")

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> Any:
        raise NotImplementedError

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    def get_status(self) -> dict[str, Any]:
        return {"mode": "mock"}

    async def reset_sessions(self, session_key: str | None = None) -> int:
        self.reset_calls += 1
        return 1

    async def shutdown(self) -> None:
        return None


class WebUIAgyCliTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_auth_urls_handles_ansi_and_terminal_wraps(self) -> None:
        raw = (
            "\x1b[32mOpen this link in your browser:\x1b[0m\r\n"
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc123\r\n"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A8085&response_type=code&scope=openid\r\n"
            "Then paste the code below."
        )
        urls = extract_auth_urls(raw)
        self.assertEqual(len(urls), 1)
        self.assertEqual(
            urls[0],
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc123"
            "&redirect_uri=http%3A%2F%2Flocalhost%3A8085&response_type=code&scope=openid",
        )
        self.assertEqual(strip_ansi("\x1b[31mhi\x1b[0m"), "hi")

    def test_extract_auth_urls_prefers_osc8_and_joins_padded_conpty_wraps(self) -> None:
        full_url = (
            "https://accounts.google.com/o/oauth2/auth?access_type=offline"
            "&client_id=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
            "&code_challenge=3EAvrnpgTg55lm19BQEHKwrMNdzVrzSPeh6_wF4fKdE&code_challenge_method=S256"
            "&prompt=consent&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
            "&response_type=code&scope=openid&state=X7NZTL2wB2sW0Y062H6gEA"
        )
        # 1. OSC-8 hyperlink + padded wrapped visual lines (exact Windows ConPTY agy output shape)
        raw_osc8 = (
            f"\x1b]8;id=jkabf3e4510789a465;{full_url}\x07\x1b]8;;\x07"
            " \x1b[4m\x1b[34mhttps://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.goog\x1b[24m\x1b[m \r\n"
            " \x1b[4m\x1b[34mleusercontent.com&code_challenge=3EAvrnpgTg55lm19BQEHKwrMNdzVrzSPeh6_wF4fKdE&code_challenge_method=S256&prompt=consent&redirect_\x1b[24m\x1b[m \r\n"
            " \x1b[4m\x1b[34muri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&scope=openid&state=X7NZTL2wB2sW0Y062H6gEA\x1b[24m\x1b[m\x1b[K\r\n"
            " ────────────────────────── \r\n"
        )
        self.assertEqual(extract_auth_urls(raw_osc8), [full_url])

        # 2. Padded wrapped lines without OSC-8 still reconstruct the exact full URL
        raw_padded_only = (
            " \x1b[4m\x1b[34mhttps://accounts.google.com/o/oauth2/auth?access_type=offline&client_id=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.goog\x1b[24m\x1b[m \r\n"
            " \x1b[4m\x1b[34mleusercontent.com&code_challenge=3EAvrnpgTg55lm19BQEHKwrMNdzVrzSPeh6_wF4fKdE&code_challenge_method=S256&prompt=consent&redirect_\x1b[24m\x1b[m \r\n"
            " \x1b[4m\x1b[34muri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&scope=openid&state=X7NZTL2wB2sW0Y062H6gEA\x1b[24m\x1b[m\x1b[K\r\n"
            " ────────────────────────── \r\n"
        )
        self.assertEqual(extract_auth_urls(raw_padded_only), [full_url])

        # 3. Truncated first-line prefix must NOT be emitted prematurely
        raw_truncated = (
            " https://accounts.google.com/o/oauth2/auth?access_type=offline"
            "&client_id=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.goog \r\n"
        )
        self.assertEqual(extract_auth_urls(raw_truncated), [])

    async def test_cli_settings_quick_cmd_and_interactive_ws_pty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            fake_settings = base / "fake_home" / ".gemini" / "antigravity-cli" / "settings.json"
            fake_cli = base / "fake_agy.py"
            fake_cli.write_text(
                "import sys\n"
                "if '--version' in sys.argv:\n"
                "    print('9.9.9-test')\n"
                "    sys.exit(0)\n"
                "print('AGY_READY https://accounts.google.com/o/oauth2/auth?client_id=test_cli_12345&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback&response_type=code&state=abc', flush=True)\n"
                "line = sys.stdin.readline()\n"
                "print('ECHO:' + line.strip(), flush=True)\n",
                encoding="utf-8",
            )

            cfg = BotConfig(base_dir=base, config_path=base / "config.json")
            cfg.agy.workspace_dir = str(base / "ws")
            cfg.agy.data_dir = str(base / "data")
            cfg.webui.username = "admin"
            cfg.webui.password = "secret"
            runner = _DummyRunner()
            app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
            srv = WebUIServer(app, cfg.webui)

            with (
                patch("agy_bot.webui.cli_pty.get_global_agy_settings_path", return_value=fake_settings),
                patch.object(srv._cli_manager, "_resolve_cmd_prefix", return_value=[sys.executable, "-u", str(fake_cli)]),
            ):
                async with TestClient(TestServer(srv.web_app)) as client:
                    # Unauthenticated access blocked
                    r_unauth = await client.get("/api/cli/info")
                    self.assertEqual(r_unauth.status, 401)

                    # Login
                    r_login = await client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
                    self.assertEqual(r_login.status, 200)

                    # GET /api/cli/info
                    r_info = await client.get("/api/cli/info?refresh=1")
                    self.assertEqual(r_info.status, 200)
                    info_data = (await r_info.json())["data"]
                    self.assertTrue(info_data["installed"])
                    self.assertEqual(info_data["version"], "9.9.9-test")

                    # PUT /api/cli/settings & GET /api/cli/settings
                    r_put = await client.put(
                        "/api/cli/settings",
                        json={
                            "data": {
                                "agentMode": "auto",
                                "toolPermission": "proceed-in-sandbox",
                                "trustedWorkspaces": [str(base / "ws")],
                            },
                            "reset_sessions": True,
                        },
                    )
                    self.assertEqual(r_put.status, 200)
                    self.assertGreaterEqual(runner.reset_calls, 1)
                    self.assertTrue(fake_settings.is_file())

                    r_get = await client.get("/api/cli/settings")
                    self.assertEqual(r_get.status, 200)
                    got = await r_get.json()
                    self.assertEqual(got["data"]["agentMode"], "auto")
                    self.assertEqual(got["data"]["toolPermission"], "proceed-in-sandbox")

                    # Quick command: unknown action rejected with 400; valid action succeeds
                    r_bad_q = await client.post("/api/cli/quick-cmd", json={"action": "rm_rf"})
                    self.assertEqual(r_bad_q.status, 400)
                    r_ok_q = await client.post("/api/cli/quick-cmd", json={"action": "version"})
                    self.assertEqual(r_ok_q.status, 200)
                    self.assertIn("9.9.9-test", (await r_ok_q.json())["output"])

                    # Interactive WebSocket PTY end-to-end
                    got_status = False
                    got_auth_url = ""
                    got_output = ""
                    got_exit = False
                    async with client.ws_connect("/api/cli/ws?cols=100&rows=26&remote_login=1&use_proxy=0") as ws:
                        sent_input = False
                        deadline = asyncio.get_running_loop().time() + 10.0
                        while asyncio.get_running_loop().time() < deadline:
                            msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                            if msg.type != WSMsgType.TEXT:
                                break
                            pkt = json.loads(msg.data)
                            ptype = pkt.get("type")
                            if ptype == "status" and pkt.get("running"):
                                got_status = True
                            elif ptype == "auth_url":
                                got_auth_url = pkt.get("url", "")
                            elif ptype == "output":
                                got_output += pkt.get("data", "")
                                if "AGY_READY" in strip_ansi(got_output) and not sent_input:
                                    sent_input = True
                                    await ws.send_json({"type": "resize", "cols": 110, "rows": 30})
                                    await ws.send_json({"type": "input", "data": "hello_cli_ws\r\n"})
                            elif ptype == "exit":
                                got_exit = True
                                break

                    self.assertTrue(got_status)
                    self.assertIn("accounts.google.com", got_auth_url)
                    self.assertIn("ECHO:hello_cli_ws", strip_ansi(got_output))
                    self.assertTrue(got_exit)

            await srv.stop()
            await app.shutdown()


if __name__ == "__main__":
    unittest.main()
