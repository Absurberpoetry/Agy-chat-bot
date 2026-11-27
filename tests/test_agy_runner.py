from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_chat_bot.config import AgyConfig, ModelProfileConfig, RateLimitConfig  # noqa: E402
from agy_chat_bot.models import IncomingMessage  # noqa: E402
from agy_chat_bot.runner.agy_stream import AgyRunner  # noqa: E402

_FAKE_AGY_SCRIPT = textwrap.dedent(
    """
    import json
    import pathlib
    import sys
    import time

    args = sys.argv[1:]
    if "--json-schema" in args:
        schema_path = pathlib.Path(args[args.index("--json-schema") + 1])
        schema_obj = json.loads(schema_path.read_text(encoding="utf-8"))
        print(json.dumps({
            "event": "init",
            "conversation_id": "judge-conv-1",
            "init": {"cwd": ".", "tools": [], "permission_mode": "default", "json_schema": schema_obj},
        }), flush=True)
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            msg = json.loads(raw)
            content = msg["message"]["content"]
            payload = {"should_reply": "大家怎么看" in content, "reason": "test_ok"}
            print(json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": "judge-conv-1",
                    "status": "SUCCESS",
                    "response": json.dumps(payload),
                    "structured_output": payload,
                    "duration_seconds": 0.1,
                    "num_turns": 1,
                },
            }), flush=True)
        sys.exit(0)

    conv_id = "conv-new-1"
    if "--conversation" in args:
        idx = args.index("--conversation")
        requested = args[idx + 1]
        if requested == "stale-conv":
            # Consume first line and fail with stale error
            line = sys.stdin.readline()
            print(json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": requested,
                    "status": "ERROR",
                    "response": "",
                    "error": "conversation not found",
                    "num_turns": 0,
                },
            }), flush=True)
            sys.exit(1)
        conv_id = requested

    print(json.dumps({
        "event": "init",
        "conversation_id": conv_id,
        "init": {"cwd": ".", "tools": ["read_file"], "permission_mode": "default"},
    }), flush=True)

    turns = 0
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        msg = json.loads(raw)
        content = msg["message"]["content"]
        if "SIMULATE_429" in content:
            print(json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "ERROR",
                    "response": "",
                    "error": "429 RESOURCE_EXHAUSTED quota",
                    "num_turns": turns,
                },
            }), flush=True)
            continue
        if "SIMULATE_TOOL_EMPTY" in content:
            turns += 1
            print(json.dumps({
                "event": "step_update",
                "step_update": {"conversation_id": conv_id, "step_index": 1, "state": "ERROR", "step_type": "tool", "tool_name": "write_file", "error": "permission denied"},
            }), flush=True)
            print(json.dumps({
                "event": "step_update",
                "step_update": {"conversation_id": conv_id, "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command"},
            }), flush=True)
            print(json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "SUCCESS",
                    "response": "",
                    "usage": {"input_tokens": 200, "output_tokens": 500},
                    "num_turns": turns,
                },
            }), flush=True)
            continue
        if "SIMULATE_STREAM_FALLBACK" in content:
            turns += 1
            print(json.dumps({
                "event": "assistant",
                "delta": "画好啦 ![图片](logo.png)",
            }), flush=True)
            print(json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "SUCCESS",
                    "response": "",
                    "num_turns": turns,
                },
            }), flush=True)
            continue
        if "系统提醒：你刚才调用了工具但没有输出任何最终文字回复" in content:
            turns += 1
            print(json.dumps({
                "event": "result",
                "result": {
                    "conversation_id": conv_id,
                    "status": "SUCCESS",
                    "response": "已补发前端 logo ![图片](logo_minimal.png)",
                    "usage": {"input_tokens": 50, "output_tokens": 40},
                    "num_turns": turns,
                },
            }), flush=True)
            continue
        if "SIMULATE_SLOW" in content:
            time.sleep(0.15)
        turns += 1
        print(json.dumps({
            "event": "step_update",
            "step_update": {"conversation_id": conv_id, "step_index": turns, "state": "DONE", "step_type": "agent_response", "text_delta": "ok"},
        }), flush=True)
        print(json.dumps({
            "event": "result",
            "result": {
                "conversation_id": conv_id,
                "status": "SUCCESS",
                "response": f"turn={turns}:{content[:30]}",
                "num_turns": turns,
            },
        }), flush=True)
    """
).strip()


class AgyRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_json_warm_reuse_concurrency_and_circuit_breaker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_agy = tmp_path / "fake_agy.py"
            fake_agy.write_text(_FAKE_AGY_SCRIPT, encoding="utf-8")

            ws_dir = tmp_path / "workspace"
            data_dir = tmp_path / "data"

            async def spawn_fake(*args, cwd=None, env=None):
                # Strip fake binary token and invoke via sys.executable fake_agy.py
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
                AgyConfig(
                    mode="agy",
                    executable="fake_agy",
                    rate_limit_cooldown_seconds=60.0,
                    fatal_error_pause_seconds=300.0,
                ),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=ws_dir,
                data_dir=data_dir,
                process_factory=spawn_fake,
            )

            # 1) Multi-turn on warm process
            r1 = await runner.ask("qq_1:group:100", "第一轮问题")
            r2 = await runner.ask("qq_1:group:100", "第二轮问题")
            self.assertTrue(r1.ok)
            self.assertTrue(r2.ok)
            self.assertTrue(r1.response.startswith("turn=1:"))
            self.assertTrue(r2.response.startswith("turn=2:"))
            self.assertEqual(r1.conversation_id, "conv-new-1")

            # Sessions file persisted
            saved = json.loads((data_dir / "sessions.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["qq_1:group:100"], "conv-new-1")

            # 2) Concurrent asks serialize under semaphore(1)
            results = await asyncio.gather(
                runner.ask("qq_1:group:100", "SIMULATE_SLOW A"),
                runner.ask("qq_1:group:200", "SIMULATE_SLOW B"),
            )
            self.assertTrue(all(r.ok for r in results))

            # 3) Context LLM judge via stream-json stdin + --json-schema
            decision = await runner.judge_context(
                "qq_1:group:100",
                [
                    IncomingMessage(
                        adapter_id="qq_1",
                        platform="onebot_v11",
                        self_id="999",
                        scene="group",
                        channel_id="100",
                        message_id="1",
                        user_id="u1",
                        sender_name="小明",
                        text="大家怎么看？",
                        timestamp=1000.0,
                    )
                ],
            )
            self.assertTrue(decision.should_reply)

            # 4) Rate limit 429 trips circuit breaker
            r_err = await runner.ask("qq_1:group:100", "SIMULATE_429")
            self.assertFalse(r_err.ok)
            self.assertTrue(r_err.is_rate_limited)
            is_open, _ = runner.is_circuit_open()
            self.assertTrue(is_open)

            r_blocked = await runner.ask("qq_1:group:100", "被熔断拦截")
            self.assertFalse(r_blocked.ok)
            self.assertIn("熔断冷却中", r_blocked.error)

            await runner.shutdown()

    async def test_stale_conversation_resume_retries_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_agy = tmp_path / "fake_agy.py"
            fake_agy.write_text(_FAKE_AGY_SCRIPT, encoding="utf-8")

            ws_dir = tmp_path / "workspace"
            data_dir = tmp_path / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "sessions.json").write_text(
                json.dumps({"qq_1:group:555": "stale-conv"}),
                encoding="utf-8",
            )

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
                AgyConfig(mode="agy", executable="fake_agy", resume_on_restart=True),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=ws_dir,
                data_dir=data_dir,
                process_factory=spawn_fake,
            )
            res = await runner.ask("qq_1:group:555", "恢复失败后自动新建")
            self.assertTrue(res.ok)
            self.assertEqual(res.conversation_id, "conv-new-1")
            await runner.shutdown()

    async def test_missing_executable_handled_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            runner = AgyRunner(
                AgyConfig(mode="agy", executable="nonexistent_agy_binary_12345"),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=tmp_path / "workspace",
                data_dir=tmp_path / "data",
            )
            res = await runner.ask("qq_1:group:1", "hello")
            self.assertFalse(res.ok)
            self.assertTrue(bool(res.error))
            decision = await runner.judge_context(
                "qq_1:group:1",
                [
                    IncomingMessage(
                        adapter_id="qq_1",
                        platform="onebot_v11",
                        self_id="999",
                        scene="group",
                        channel_id="1",
                        message_id="1",
                        user_id="u1",
                        sender_name="U",
                        text="hi",
                        timestamp=1.0,
                    )
                ],
            )
            self.assertFalse(decision.should_reply)
            await runner.shutdown()

    async def test_settings_file_and_proxy_env_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_agy = tmp_path / "fake_agy.py"
            fake_agy.write_text(_FAKE_AGY_SCRIPT, encoding="utf-8")
            settings_path = tmp_path / "settings.bot.json"
            settings_path.write_text('{"permissions": {"deny": ["command(*)"]}}', encoding="utf-8")

            captured: list[tuple[tuple[str, ...], dict[str, str]]] = []

            async def spawn_fake(*args, cwd=None, env=None):
                captured.append((tuple(args), dict(env or {})))
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
                AgyConfig(mode="agy", executable="fake_agy", proxy="http://127.0.0.1:7890"),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=tmp_path / "workspace",
                data_dir=tmp_path / "data",
                settings_file=settings_path,
                process_factory=spawn_fake,
            )
            res = await runner.ask("qq_1:group:1", "hello")
            self.assertTrue(res.ok)
            self.assertEqual(len(captured), 1)
            args, env = captured[0]
            self.assertIn("--settings", args)
            self.assertEqual(args[args.index("--settings") + 1], str(settings_path))
            self.assertEqual(env.get("HTTP_PROXY"), "http://127.0.0.1:7890")
            self.assertEqual(env.get("HTTPS_PROXY"), "http://127.0.0.1:7890")
            self.assertEqual(env.get("ALL_PROXY"), "http://127.0.0.1:7890")
            await runner.shutdown()

    async def test_file_tools_permission_adds_allow_and_trusted_workspaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ws_dir = tmp_path / "bot_workspace"
            data_dir = tmp_path / "data"
            sub_ws = data_dir / "subagent_workspace"
            settings_path = tmp_path / "settings.bot.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "trustedWorkspaces": [r"C:\old_moved_repo\bot_workspace"],
                        "permissions": {"deny": ["command(*)", "write_file(*)"], "allow": ["read_file(.media_cache/*)"]},
                    }
                ),
                encoding="utf-8",
            )
            profile = ModelProfileConfig(
                model="gemini-4-neon-low",
                enable_web_search=True,
                enable_url_read=True,
                enable_vision=True,
                enable_file_tools=True,
                enable_command=True,
            )
            runner = AgyRunner(
                AgyConfig(
                    mode="agy",
                    executable="fake_agy",
                    allow_command_execution=True,
                    model="gemini-4-neon-low",
                    model_profiles={"gemini-4-neon-low": profile},
                ),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=ws_dir,
                data_dir=data_dir,
                settings_file=settings_path,
            )
            resolved = runner._resolve_model_settings_file("gemini-4-neon-low", profile)
            self.assertIsNotNone(resolved)
            cfg = json.loads(resolved.read_text(encoding="utf-8"))
            self.assertIn("write_file(*)", cfg["permissions"]["allow"])
            self.assertIn("edit_file(*)", cfg["permissions"]["allow"])
            self.assertNotIn("write_file(*)", cfg["permissions"]["deny"])
            self.assertIn("write_file(GEMINI.md)", cfg["permissions"]["deny"])
            self.assertIn("write_file(.agents/)", cfg["permissions"]["deny"])
            trusted = [str(Path(p).resolve()) for p in cfg["trustedWorkspaces"]]
            self.assertIn(str(ws_dir.resolve()), trusted)
            self.assertIn(str(sub_ws.resolve()), trusted)

            # Subagent profile also gets file tools when allow_command_execution is enabled
            sub_prof = runner._build_subagent_profile("gemini-4-neon-medium")
            self.assertTrue(sub_prof.enable_file_tools)
            await runner.shutdown()

    async def test_empty_response_after_tool_calls_auto_recovers_and_stream_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fake_agy = tmp_path / "fake_agy.py"
            fake_agy.write_text(_FAKE_AGY_SCRIPT, encoding="utf-8")

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
                AgyConfig(mode="agy", executable="fake_agy"),
                RateLimitConfig(max_concurrent_requests=1, max_queue_size=4),
                workspace_dir=tmp_path / "workspace",
                data_dir=tmp_path / "data",
                process_factory=spawn_fake,
            )
            r1 = await runner.ask("qq_1:group:777", "SIMULATE_TOOL_EMPTY 绘制前端logo")
            self.assertTrue(r1.ok)
            self.assertIn("![图片](logo_minimal.png)", r1.response)
            self.assertEqual(r1.tool_call_count, 2)
            self.assertTrue(any("write_file" in e for e in r1.tool_errors))
            self.assertGreaterEqual(r1.output_tokens, 540)

            r2 = await runner.ask("qq_1:group:777", "SIMULATE_STREAM_FALLBACK")
            self.assertTrue(r2.ok)
            self.assertIn("![图片](logo.png)", r2.response)
            await runner.shutdown()


if __name__ == "__main__":
    unittest.main()
