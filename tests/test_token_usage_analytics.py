from __future__ import annotations

import asyncio
import datetime as dt
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_bot.app import BotApplication  # noqa: E402
from agy_bot.config import AgyConfig, BotConfig, RateLimitConfig, WebUIConfig  # noqa: E402
from agy_bot.models import RunnerResult  # noqa: E402
from agy_bot.runner.agy_stream import AgyRunner, _extract_detailed_usage_stats  # noqa: E402
from agy_bot.usage_store import TokenUsageStore  # noqa: E402
from agy_bot.webui.cli_pty import parse_agy_quota_output  # noqa: E402
from agy_bot.webui.server import WebUIServer  # noqa: E402


_FAKE_TELEMETRY_AGY = textwrap.dedent(
    """
    import json
    import sys
    import time

    print(json.dumps({
        "event": "init",
        "conversation_id": "conv-telemetry-1",
        "init": {"model": "gemini-3.1-pro-high"},
    }), flush=True)

    turns = 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        turns += 1
        # Simulate thinking phase (~0.08s)
        print(json.dumps({
            "event": "step_update",
            "step_update": {"step_type": "thinking", "state": "RUNNING", "thought_delta": "let me think..."},
        }), flush=True)
        time.sleep(0.08)
        print(json.dumps({
            "event": "step_update",
            "step_update": {"step_type": "thinking", "state": "DONE"},
        }), flush=True)
        # Simulate first visible text delta and streaming finish (~0.08s)
        print(json.dumps({
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "state": "RUNNING", "text_delta": "你好"},
        }), flush=True)
        time.sleep(0.08)
        print(json.dumps({
            "event": "step_update",
            "step_update": {"step_type": "agent_response", "state": "DONE", "text_delta": "世界"},
        }), flush=True)
        print(json.dumps({
            "event": "result",
            "result": {
                "conversation_id": "conv-telemetry-1",
                "status": "SUCCESS",
                "response": "你好世界",
                "num_turns": turns,
                "model": "gemini-3.1-pro-high",
                "usage": {
                    "input_tokens": 2000,
                    "cache_read_tokens": 8000,
                    "output_tokens": 120,
                    "thinking_tokens": 45,
                },
            },
        }), flush=True)
    """
).strip()


class TokenUsageAnalyticsTests(unittest.IsolatedAsyncioTestCase):
    def test_extract_detailed_usage_both_anthropic_and_gemini_formats(self) -> None:
        # 1) agy / Anthropic separate input_tokens + cache_read_tokens
        d1 = _extract_detailed_usage_stats(
            {},
            {"usage": {"input_tokens": 28495, "cache_read_tokens": 77420, "output_tokens": 412}},
        )
        self.assertEqual(d1["input_tokens"], 28495)
        self.assertEqual(d1["cache_read_tokens"], 77420)
        self.assertEqual(d1["output_tokens"], 412)
        self.assertEqual(d1["total_tokens"], 28495 + 77420 + 412)
        self.assertAlmostEqual(d1["cache_hit_rate"], 73.1, places=1)

        # 2) Gemini usageMetadata where promptTokenCount includes cachedContentTokenCount
        d2 = _extract_detailed_usage_stats(
            {},
            {"usageMetadata": {"promptTokenCount": 10000, "cachedContentTokenCount": 7500, "candidatesTokenCount": 250}},
        )
        self.assertEqual(d2["input_tokens"], 2500)
        self.assertEqual(d2["cache_read_tokens"], 7500)
        self.assertEqual(d2["output_tokens"], 250)
        self.assertEqual(d2["total_tokens"], 10250)
        self.assertAlmostEqual(d2["cache_hit_rate"], 75.0, places=1)

    async def test_agy_runner_measures_ttft_thinking_speed_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_agy = tmp_path / "fake_telemetry_agy.py"
            fake_agy.write_text(_FAKE_TELEMETRY_AGY, encoding="utf-8")

            async def spawn_fake(*args, cwd=None, env=None):
                cmd = [sys.executable, str(fake_agy), *args[1:]]
                return await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    limit=10 * 1024 * 1024,
                )

            runner = AgyRunner(
                AgyConfig(mode="agy", executable="fake_agy", model="gemini-3.1-pro-high"),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=tmp_path / "ws",
                data_dir=tmp_path / "data",
                process_factory=spawn_fake,
            )
            try:
                res = await runner.ask("qq_1:group:100", "测一下首字和思考时间")
                self.assertTrue(res.ok)
                self.assertEqual(res.model, "gemini-3.1-pro-high")
                self.assertEqual(res.input_tokens, 2000)
                self.assertEqual(res.cache_read_tokens, 8000)
                self.assertEqual(res.output_tokens, 120)
                self.assertEqual(res.total_tokens, 10120)
                self.assertAlmostEqual(res.cache_hit_rate, 80.0, places=1)
                self.assertGreaterEqual(res.thinking_seconds, 0.05)
                self.assertGreaterEqual(res.ttft_seconds, 0.05)
                self.assertGreater(res.tokens_per_second, 0.0)
                self.assertGreaterEqual(res.duration_seconds, res.ttft_seconds)
            finally:
                await runner.shutdown()

    async def test_agy_runner_subtracts_cumulative_warm_usage_and_estimates_thinking_time(self) -> None:
        cumulative_script = textwrap.dedent(
            """
            import json, sys, time
            print(json.dumps({"event": "init", "conversation_id": "conv-cum-1", "init": {"model": "gemini-4-neon-medium"}}), flush=True)
            turns = 0
            cum_in = 0
            cum_cache = 0
            cum_out = 0
            cum_think = 0
            for raw in sys.stdin:
                if not raw.strip():
                    continue
                turns += 1
                time.sleep(0.08)
                # Simulate agy CLI streaming ONLY final text (no thinking step_update), while reporting cumulative usage & cumulative duration_seconds
                print(json.dumps({"event": "step_update", "step_update": {"step_type": "agent_response", "state": "DONE", "text_delta": "短回复"}}), flush=True)
                if turns == 1:
                    cum_in += 10000
                    cum_cache += 20000
                    cum_out += 500
                    cum_think += 450
                    cum_dur = 120.0
                else:
                    cum_in += 2000
                    cum_cache += 30000
                    cum_out += 400
                    cum_think += 360
                    cum_dur = 677.8
                print(json.dumps({
                    "event": "result",
                    "result": {
                        "conversation_id": "conv-cum-1",
                        "status": "SUCCESS",
                        "response": "短回复",
                        "num_turns": turns,
                        "duration_seconds": cum_dur,
                        "usage": {
                            "input_tokens": cum_in,
                            "cache_read_tokens": cum_cache,
                            "output_tokens": cum_out,
                            "thinking_tokens": cum_think,
                            "total_tokens": cum_in + cum_cache + cum_out,
                        },
                    },
                }), flush=True)
            """
        ).strip()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake = tmp_path / "fake_cum.py"
            fake.write_text(cumulative_script, encoding="utf-8")

            async def spawn_fake(*args, cwd=None, env=None):
                return await asyncio.create_subprocess_exec(
                    sys.executable, str(fake), *args[1:],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    limit=10 * 1024 * 1024,
                )

            runner = AgyRunner(
                AgyConfig(mode="agy", executable="fake_agy", model="gemini-4-neon-medium"),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=tmp_path / "ws",
                data_dir=tmp_path / "data",
                process_factory=spawn_fake,
            )
            try:
                r1 = await runner.ask("qq_1:private:1", "第一轮")
                r2 = await runner.ask("qq_1:private:1", "第二轮")
                self.assertTrue(r1.ok and r2.ok)
                # Turn 1 exact values
                self.assertEqual(r1.input_tokens, 10000)
                self.assertEqual(r1.cache_read_tokens, 20000)
                self.assertEqual(r1.output_tokens, 500)
                self.assertEqual(r1.thinking_tokens, 450)
                self.assertEqual(r1.total_tokens, 30500)
                # Turn 2 MUST be exact single-turn delta (NOT 12000 / 50000 / 900 / 62900!)
                self.assertEqual(r2.input_tokens, 2000)
                self.assertEqual(r2.cache_read_tokens, 30000)
                self.assertEqual(r2.output_tokens, 400)
                self.assertEqual(r2.thinking_tokens, 360)
                self.assertEqual(r2.total_tokens, 32400)
                self.assertAlmostEqual(r2.cache_hit_rate, 93.75, places=1)
                # Single-turn wall clock duration (NOT cumulative 677.8s!)
                self.assertLess(r2.duration_seconds, 5.0)
                # Thinking duration estimated cleanly from thinking_tokens share (> 0)
                self.assertGreater(r2.thinking_seconds, 0.04)
                self.assertLessEqual(r2.thinking_seconds, r2.duration_seconds)
                # Realistic token speed (computed over active LLM time, not blown up by cumulative tokens)
                self.assertGreater(r2.tokens_per_second, 0.0)
                self.assertLess(r2.tokens_per_second, 10000.0)
            finally:
                await runner.shutdown()

    def test_usage_store_calendar_models_layers_and_session_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TokenUsageStore(Path(tmp) / "token_usage.db")
            ts_nov10 = dt.datetime(2026, 11, 10, 10, 0, 0).timestamp()
            ts_nov24_1 = dt.datetime(2026, 11, 24, 14, 0, 0).timestamp()
            ts_nov24_2 = dt.datetime(2026, 11, 24, 15, 30, 0).timestamp()
            ts_oct05 = dt.datetime(2026, 10, 5, 9, 0, 0).timestamp()

            store.record(
                session_key="qq_1:group:1001",
                scene="group",
                channel_id="1001",
                display_name="1001",
                model="gemini-3-flash",
                input_tokens=1000,
                cache_read_tokens=3000,
                output_tokens=200,
                ttft_seconds=0.8,
                thinking_seconds=0.3,
                duration_seconds=2.5,
                tokens_per_second=80.0,
                context_messages=10,
                context_tokens=4000,
                turn_index=1,
                timestamp=ts_oct05,
            )
            store.record(
                session_key="qq_1:group:1001",
                scene="group",
                channel_id="1001",
                display_name="1001",
                model="gemini-3.1-pro-high",
                input_tokens=2000,
                cache_read_tokens=8000,
                output_tokens=400,
                ttft_seconds=1.5,
                thinking_seconds=0.9,
                duration_seconds=5.0,
                tokens_per_second=95.0,
                context_messages=16,
                context_tokens=10000,
                turn_index=2,
                timestamp=ts_nov10,
            )
            store.record(
                session_key="qq_1:private:8888",
                scene="private",
                channel_id="8888",
                display_name="小明",
                model="claude-sonnet-4-6",
                input_tokens=3000,
                cache_read_tokens=9000,
                output_tokens=600,
                ttft_seconds=1.2,
                thinking_seconds=0.5,
                duration_seconds=4.0,
                tokens_per_second=120.0,
                context_messages=8,
                context_tokens=12000,
                turn_index=1,
                timestamp=ts_nov24_1,
            )
            store.record(
                session_key="qq_1:group:1001",
                scene="group",
                channel_id="1001",
                display_name="1001",
                model="gemini-3.1-pro-high",
                input_tokens=1500,
                cache_read_tokens=13500,
                output_tokens=500,
                ttft_seconds=1.1,
                thinking_seconds=0.7,
                duration_seconds=3.8,
                tokens_per_second=110.0,
                context_messages=22,
                context_tokens=15000,
                turn_index=3,
                timestamp=ts_nov24_2,
            )

            # Workbench summary prioritizes latest request (ts_nov24_2)
            wb = store.get_workbench_summary()
            self.assertEqual(wb["latest"]["session_key"], "qq_1:group:1001")
            self.assertEqual(wb["latest"]["total_tokens"], 15500)
            self.assertAlmostEqual(wb["latest"]["cache_hit_rate"], 90.0, places=1)
            self.assertEqual(wb["latest"]["turn_index"], 3)
            # Latest session total across all 3 turns of group 1001: 4200 + 10400 + 15500 = 30100
            self.assertEqual(wb["latest_session"]["total_tokens"], 30100)
            self.assertEqual(wb["latest_session"]["requests"], 3)
            # Overall total: 30100 + 12600 = 42700
            self.assertEqual(wb["overall"]["total_tokens"], 42700)
            self.assertEqual(wb["overall"]["requests"], 4)

            # Dashboard scope=all
            dash_all = store.get_dashboard(scope="all", calendar_month="2026-11")
            self.assertEqual(dash_all["summary"]["total_tokens"], 42700)
            self.assertEqual(len(dash_all["models"]), 3)
            self.assertEqual(dash_all["models"][0]["model"], "gemini-3.1-pro-high")
            self.assertEqual(len(dash_all["requests"]), 4)
            self.assertIn("2026-11-10", dash_all["calendar"]["days"])
            self.assertIn("2026-11-24", dash_all["calendar"]["days"])
            self.assertEqual(dash_all["calendar"]["days"]["2026-11-24"]["total_tokens"], 12600 + 15500)

            # Dashboard scope=month (2026-11 excludes Oct 5)
            dash_month = store.get_dashboard(scope="month", month="2026-11")
            self.assertEqual(dash_month["summary"]["requests"], 3)
            self.assertEqual(dash_month["summary"]["total_tokens"], 10400 + 12600 + 15500)
            self.assertEqual(len(dash_month["models"]), 2)

            # Dashboard scope=day (2026-11-24)
            dash_day = store.get_dashboard(scope="day", date="2026-11-24")
            self.assertEqual(dash_day["summary"]["requests"], 2)
            self.assertEqual(dash_day["summary"]["total_tokens"], 12600 + 15500)
            self.assertEqual(len(dash_day["requests"]), 2)

            # Filter by single private session
            dash_priv = store.get_dashboard(scope="all", calendar_month="2026-11", session_key="qq_1:private:8888")
            self.assertEqual(dash_priv["summary"]["requests"], 1)
            self.assertEqual(dash_priv["summary"]["total_tokens"], 12600)
            self.assertEqual(len(dash_priv["models"]), 1)
            self.assertEqual(dash_priv["models"][0]["model"], "claude-sonnet-4-6")

    async def test_webui_usage_api_and_playground_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cfg = BotConfig(
                base_dir=tmp_path,
                agy=AgyConfig(
                    mode="mock",
                    workspace_dir=str(tmp_path / "ws"),
                    data_dir=str(tmp_path / "data"),
                ),
                webui=WebUIConfig(enabled=True, password=""),
            )
            bot = BotApplication(cfg)
            server = WebUIServer(bot, cfg.webui)

            async with TestServer(server.web_app) as srv:
                async with TestClient(srv) as client:
                    # 1) Playground turn automatically records usage
                    r_chat = await client.post("/api/playground/chat", json={"message": "你好，测试 token 统计"})
                    self.assertEqual(r_chat.status, 200)
                    chat_j = await r_chat.json()
                    self.assertTrue(chat_j["ok"])
                    self.assertIsInstance(chat_j.get("usage"), dict)
                    self.assertGreater(chat_j["usage"]["total_tokens"], 0)

                    # 2) Summary endpoint returns latest turn + overall totals
                    r_sum = await client.get("/api/usage/summary")
                    self.assertEqual(r_sum.status, 200)
                    sum_j = await r_sum.json()
                    self.assertTrue(sum_j["ok"])
                    self.assertEqual(sum_j["data"]["latest"]["scene"], "playground")
                    self.assertEqual(sum_j["data"]["overall"]["requests"], 1)

                    # 3) Dashboard endpoint supports all / month / day scopes
                    today = dt.datetime.now().strftime("%Y-%m-%d")
                    r_dash = await client.get(f"/api/usage/dashboard?scope=day&date={today}")
                    self.assertEqual(r_dash.status, 200)
                    dash_j = await r_dash.json()
                    self.assertTrue(dash_j["ok"])
                    self.assertEqual(dash_j["data"]["summary"]["requests"], 1)
                    self.assertIn(today, dash_j["data"]["calendar"]["days"])
                    self.assertEqual(len(dash_j["data"]["models"]), 1)
                    self.assertEqual(len(dash_j["data"]["requests"]), 1)

            await bot.shutdown()

    def test_parse_agy_quota_output_extracts_account_limits_and_reset_seconds(self) -> None:
        sample_tui = textwrap.dedent(
            """
            └ Models & Quota

              Account: demo_user@gmail.com

            GEMINI MODELS
              Models within this group: Gemini 4 Neon, Gemini 4 Carbon

              Weekly Limit Remaining
                [█████████████████████████████████░░░░░░░░░░░░░░░░░] 65.70%
                Refreshes in 156h 33m

              Five Hour Limit Remaining
                [██████████████████████████████████░░░░░░░░░░░░░░░░] 68.96%
                Refreshes in 3h 33m

            CLAUDE AND GPT MODELS
              Models within this group: Claude Opus 5.5, Claude Sonnet 5.5, GPT-OSS 120B

              Weekly Limit Remaining
                [██████████████████████████████████████████████████] 100.00%
                Quota available

              Five Hour Limit Remaining
                [██████████████████████████████████████████████████] 100.00%
                Quota available
            """
        )
        parsed = parse_agy_quota_output(sample_tui)
        self.assertTrue(parsed["ok"])
        self.assertTrue(parsed["signed_in"])
        self.assertEqual(parsed["account"], "demo_user@gmail.com")
        self.assertEqual(len(parsed["groups"]), 2)
        g0 = parsed["groups"][0]
        self.assertEqual(g0["label"], "Gemini")
        self.assertAlmostEqual(g0["weekly"]["remaining_pct"], 65.70, places=2)
        self.assertEqual(g0["weekly"]["reset_seconds"], 156 * 3600 + 33 * 60)
        self.assertAlmostEqual(g0["five_hour"]["remaining_pct"], 68.96, places=2)
        self.assertEqual(g0["five_hour"]["reset_seconds"], 3 * 3600 + 33 * 60)
        g1 = parsed["groups"][1]
        self.assertEqual(g1["label"], "Claude & GPT")
        self.assertAlmostEqual(g1["weekly"]["remaining_pct"], 100.0, places=2)
        self.assertEqual(g1["weekly"]["reset_seconds"], 0)

        # Unsigned-in error detection
        unauth = parse_agy_quota_output("Error: Please sign in to view quota.")
        self.assertFalse(unauth["ok"])
        self.assertFalse(unauth["signed_in"])

    async def test_plugin_scope_model_and_persona_options_applied_in_ask_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            pdir = tmp_path / "plugins"
            pdir.mkdir(parents=True, exist_ok=True)
            (pdir / "demo_plg.py").write_text(
                textwrap.dedent(
                    """
                    from agy_bot.plugins.sdk import BotPlugin, command

                    class DemoPlugin(BotPlugin):
                        id = "demo_plg"
                        name = "Demo Plugin"

                        @command("ping")
                        async def cmd_ping(self, msg, args):
                            return await self.ctx.ask_llm(msg.session_key, f"处理: {args}")
                    """
                ).strip(),
                encoding="utf-8",
            )
            cfg = BotConfig(
                base_dir=tmp_path,
                agy=AgyConfig(
                    mode="mock",
                    workspace_dir=str(tmp_path / "ws"),
                    data_dir=str(tmp_path / "data"),
                    subagent_enabled=True,
                ),
                webui=WebUIConfig(enabled=True, password=""),
            )
            cfg.plugins.dir = str(pdir)
            bot = BotApplication(cfg)
            bot.persona_store.upsert("plg_persona", "你是专业翻译助手，只输出中文。", "插件专属人设")
            await bot.ensure_plugins_loaded()

            updated_rec = await bot.plugin_manager.update_plugin_scope(
                "demo_plg",
                {
                    "mode": "all",
                    "allow_private": True,
                    "use_subagent": True,
                    "model": "gemini-3-flash",
                    "persona_id": "plg_persona",
                },
            )
            updated_scope = updated_rec["scope"]
            self.assertEqual(updated_scope["model"], "gemini-3-flash")
            self.assertEqual(updated_scope["persona_id"], "plg_persona")

            rec = bot.plugin_manager._records["demo_plg"]
            assert rec.ctx is not None
            reply = await rec.ctx.ask_llm("qq_1:group:100", "翻译 hello")
            self.assertIn("后台子代理已完成任务", reply)
            self.assertEqual(len(bot.runner.subagent_calls), 1)
            _, sent_prompt = bot.runner.subagent_calls[0]
            self.assertIn("你是专业翻译助手，只输出中文。", sent_prompt)
            self.assertIn("翻译 hello", sent_prompt)
            # Usage store also recorded this plugin call with the plugin's overridden model
            wb = bot.usage_store.get_workbench_summary()
            self.assertEqual(wb["latest"]["model"], "gemini-3-flash")
            await bot.shutdown()


if __name__ == "__main__":
    unittest.main()
