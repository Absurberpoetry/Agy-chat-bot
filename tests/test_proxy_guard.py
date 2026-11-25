from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agychat_bot.proxy_guard import (  # noqa: E402
    ProbeOutcome,
    ProxyControllerInfo,
    extract_local_proxy_port,
    is_allowed_proxy_node,
    is_auth_required_error,
    is_geo_blocked_error,
    is_proxy_or_geo_error,
    parse_controller_from_cmdline,
    select_best_working_node,
)


class ProxyGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_cmdline_and_error_classification(self) -> None:
        cmd = (
            '"\\\\?\\C:\\Users\\star\\牛逼\\core\\NiuBiCore.exe" -d C:\\Users\\star\\AppData\\Roaming\\牛逼 '
            '-f C:\\Users\\star\\AppData\\Roaming\\牛逼\\config.runtime.yaml '
            '-ext-ctl 127.0.0.1:62399 -secret nexgen-7468-1794202769961373100'
        )
        info = parse_controller_from_cmdline(cmd, proxy_port=7890, pid=10796, name="NiuBiCore.exe")
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info.controller_url, "http://127.0.0.1:62399")
        self.assertEqual(info.secret, "nexgen-7468-1794202769961373100")
        self.assertEqual(extract_local_proxy_port("http://127.0.0.1:7890"), 7890)
        self.assertIsNone(extract_local_proxy_port("http://10.0.0.2:7890"))

        self.assertTrue(is_proxy_or_geo_error("FAILED_PRECONDITION (code 400): User location is not supported for the API use."))
        self.assertTrue(is_geo_blocked_error("FAILED_PRECONDITION (code 400): User location is not supported for the API use."))
        self.assertTrue(is_proxy_or_geo_error("request timed out after 90s"))
        self.assertFalse(is_geo_blocked_error("request timed out after 90s"))
        self.assertTrue(is_proxy_or_geo_error("503 Service Unavailable"))
        self.assertFalse(is_proxy_or_geo_error("Account suspended for Terms of Service violation"))
        self.assertTrue(is_auth_required_error("authentication failed or timed out"))
        self.assertFalse(is_proxy_or_geo_error("authentication failed or timed out"))
        self.assertFalse(is_proxy_or_geo_error("You are not logged into Antigravity."))

    def test_allowed_proxy_node_whitelist_only_tw_ai_jp_ai_and_phoenix(self) -> None:
        allowed_cases = [
            "🇨🇳台湾02|Google play|AI|电信优化",
            "🇨🇳台湾04|Google play|AI|电信优化",
            "🇨🇳台湾1-流媒体解锁|AI-hy2",
            "🇨🇳台湾2-流媒体解锁|AI-hy2",
            "🇯🇵日本01|hy2|流媒体解锁|AI",
            "🇯🇵日本04|流媒体解锁|AI",
            "🇺🇸凤凰城04|流媒体解锁|AI|电信优化",
        ]
        blocked_cases = [
            "🇭🇰AWS香港01",
            "🇭🇰香港HKT04",
            "🇸🇬新加坡01|hy2|流媒体解锁|AI",
            "🇯🇵AWS日本01|流媒体解锁",
            "🇯🇵AWS日本02|hy2|流媒体解锁",
            "🇰🇷韩国04|家宽",
            "🇺🇸亚特兰大02|流媒体解锁|AI|电信优化",
            "🇩🇪德国02|电信优化",
            "🇬🇧英国02|流媒体解锁|AI|移动优化",
            "⬇️下载专用|0.1x",
        ]
        for n in allowed_cases:
            self.assertTrue(is_allowed_proxy_node(n), f"Should allow: {n}")
        for n in blocked_cases:
            self.assertFalse(is_allowed_proxy_node(n), f"Should reject: {n}")

    async def test_select_lowest_latency_working_node_skips_timeout_and_geoblock(self) -> None:
        state: dict[str, Any] = {
            "current": "🇯🇵日本04|流媒体解锁|AI",
            "closed_conns": [],
            "switches": [],
        }

        delays = {
            "🇭🇰香港HKT04": 35,                           # fastest ping, but NOT in whitelist!
            "🇯🇵AWS日本01|流媒体解锁": 45,                  # JP without AI -> NOT in whitelist!
            "🇺🇸亚特兰大02|流媒体解锁|AI|电信优化": 120,      # US Atlanta -> NOT in whitelist!
            "🇯🇵日本01|hy2|流媒体解锁|AI": 60,              # allowed & fastest, but geo-blocked during probe!
            "🇨🇳台湾2-流媒体解锁|AI-hy2": 72,              # allowed, clean & lowest latency -> MUST BE SELECTED!
            "🇺🇸凤凰城04|流媒体解锁|AI|电信优化": 190,        # allowed & clean, slower
            "🇨🇳台湾04|Google play|AI|电信优化": None,      # timeout
            "🇯🇵日本04|流媒体解锁|AI": 55,                  # current broken node
        }

        async def handle_proxies(request: web.Request) -> web.Response:
            self.assertEqual(request.headers.get("Authorization"), "Bearer test-secret")
            return web.json_response({
                "proxies": {
                    "GLOBAL": {"type": "Selector", "now": "DIRECT", "all": ["DIRECT"]},
                    "自动选择": {"type": "URLTest", "now": "🇨🇳台湾2-流媒体解锁|AI-hy2", "all": ["🇨🇳台湾2-流媒体解锁|AI-hy2"]},
                    "🐮牛逼": {
                        "type": "Selector",
                        "now": state["current"],
                        "all": ["自动选择", "DIRECT", *delays.keys()],
                    },
                    **{k: {"type": "AnyTLS"} for k in delays},
                }
            })

        async def handle_delay(request: web.Request) -> web.Response:
            name = request.match_info["name"]
            d = delays.get(name)
            if d is None:
                return web.json_response({"message": "timeout"}, status=504)
            return web.json_response({"delay": d})

        async def handle_switch(request: web.Request) -> web.Response:
            body = await request.json()
            state["current"] = body["name"]
            state["switches"].append(body["name"])
            return web.Response(status=204)

        async def handle_get_conns(request: web.Request) -> web.Response:
            return web.json_response({
                "connections": [
                    {"id": "c-google", "metadata": {"host": "cloudcode-pa.googleapis.com"}},
                    {"id": "c-other", "metadata": {"host": "qq.com"}},
                ]
            })

        async def handle_del_conn(request: web.Request) -> web.Response:
            state["closed_conns"].append(request.match_info["cid"])
            return web.Response(status=204)

        app = web.Application()
        app.router.add_get("/proxies", handle_proxies)
        app.router.add_get("/proxies/{name}/delay", handle_delay)
        app.router.add_put("/proxies/{group}", handle_switch)
        app.router.add_get("/connections", handle_get_conns)
        app.router.add_delete("/connections/{cid}", handle_del_conn)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

        try:
            ctl = ProxyControllerInfo(proxy_port=7890, controller_url=f"http://127.0.0.1:{port}", secret="test-secret")

            async def fake_probe() -> ProbeOutcome:
                cur = state["current"]
                if cur in ("🇯🇵日本01|hy2|流媒体解锁|AI", "🇯🇵日本04|流媒体解锁|AI"):
                    return ProbeOutcome(
                        ok=False,
                        elapsed_ms=300,
                        error="FAILED_PRECONDITION (code 400): User location is not supported for the API use.",
                        geo_blocked=True,
                    )
                return ProbeOutcome(ok=True, elapsed_ms=850, response="ok", geo_blocked=False)

            res = await select_best_working_node(ctl, probe_fn=fake_probe, settle_seconds=0.0)
            self.assertTrue(res.ok)
            self.assertEqual(res.group, "🐮牛逼")
            self.assertEqual(res.filtered_nodes, 5)
            self.assertEqual(res.selected_node, "🇨🇳台湾2-流媒体解锁|AI-hy2")
            self.assertEqual(res.selected_delay_ms, 72)
            # Non-whitelist nodes (HK/JP-non-AI/Atlanta) were NEVER touched!
            self.assertEqual(state["switches"], ["🇯🇵日本01|hy2|流媒体解锁|AI", "🇨🇳台湾2-流媒体解锁|AI-hy2"])
            self.assertEqual(res.geo_blocked_nodes, ["🇯🇵日本01|hy2|流媒体解锁|AI"])
            # Verified stale google connection closed, non-google connection left intact
            self.assertIn("c-google", state["closed_conns"])
            self.assertNotIn("c-other", state["closed_conns"])

            # Pass 2: Known geo-blocked fast node (🇯🇵日本01, 60ms) is deprioritized BELOW clean Phoenix (190ms)!
            state["switches"].clear()
            state["current"] = "🇨🇳台湾2-流媒体解锁|AI-hy2"
            res2 = await select_best_working_node(
                ctl,
                probe_fn=fake_probe,
                geo_blocked_nodes={"🇯🇵日本01|hy2|流媒体解锁|AI", "🇯🇵日本04|流媒体解锁|AI"},
                settle_seconds=0.0,
            )
            self.assertTrue(res2.ok)
            self.assertEqual(res2.selected_node, "🇺🇸凤凰城04|流媒体解锁|AI|电信优化")
            # Tried clean Phoenix BEFORE geo-blocked JP01/JP04 despite higher ping!
            self.assertEqual(state["switches"], ["🇺🇸凤凰城04|流媒体解锁|AI|电信优化"])
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
