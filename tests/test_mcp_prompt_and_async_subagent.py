from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agychat_bot.adapters.base import PlatformAdapter  # noqa: E402
from agychat_bot.app import BotApplication, extract_bg_tasks  # noqa: E402
from agychat_bot.config import (  # noqa: E402
    BotConfig,
    PlatformConfig,
    WebUIConfig,
    config_to_dict,
    load_config_from_dict,
)
from agychat_bot.models import IncomingMessage, JudgeDecision, MediaAttachment, RunnerResult  # noqa: E402
from agychat_bot.runner.agy_stream import AgyRunner  # noqa: E402
from agychat_bot.webui.server import WebUIServer  # noqa: E402

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01"
    b"\xf6\x178U\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _DummyAdapter(PlatformAdapter):
    platform_name = "onebot_v11"

    def __init__(self, cfg: PlatformConfig) -> None:
        super().__init__(cfg)
        self.sent_replies: list[dict[str, Any]] = []
        self.sent_images: list[dict[str, Any]] = []

    async def start(self, on_message) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_reply(self, trigger_msg, text: str, *, quote_trigger: bool = True, at_user: bool = False):
        self.sent_replies.append(
            {
                "msg_id": trigger_msg.message_id,
                "text": text,
                "quote": quote_trigger,
                "at": at_user,
            }
        )
        return f"reply-{len(self.sent_replies)}"

    async def send_image(
        self,
        trigger_msg,
        image_bytes: bytes,
        *,
        caption: str = "",
        quote_trigger: bool = True,
        at_user: bool = False,
    ):
        self.sent_images.append(
            {
                "msg_id": trigger_msg.message_id,
                "size": len(image_bytes),
                "bytes": image_bytes,
                "caption": caption,
                "quote": quote_trigger,
                "at": at_user,
            }
        )
        return f"img-{len(self.sent_images)}"


class _ConcurrentFakeRunner:
    """Simulates a slow background subagent while main chat responds immediately."""

    def __init__(self, out_image_rel: str) -> None:
        self.out_image_rel = out_image_rel
        self.main_prompts: list[str] = []
        self.subagent_prompts: list[str] = []
        self.subagent_started = asyncio.Event()
        self.allow_subagent_finish = asyncio.Event()
        self.handoff_done = asyncio.Event()
        self.reset_calls = 0

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    async def ask(self, session_key: str, prompt: str, **kwargs) -> RunnerResult:
        self.main_prompts.append(prompt)
        if "[后台子代理任务已完成" in prompt:
            self.handoff_done.set()
            return RunnerResult(
                ok=True,
                response="久等啦！赛博朋克版本刚出炉，霓虹氛围拉满了！",
                status="SUCCESS",
            )
        if "今晚吃啥" in prompt:
            return RunnerResult(ok=True, response="今晚吃火锅不错呀！", status="SUCCESS")
        if "赛博朋克" in prompt:
            return RunnerResult(
                ok=True,
                response=(
                    "好嘞，我把参考图和赛博朋克要求交给后台画师去画了，期间咱们可以继续聊~\n"
                    '<agy_bg_task>{"mcp": "draw-mcp", "task": "将参考图重绘为赛博朋克霓虹夜景风格"}</agy_bg_task>'
                ),
                status="SUCCESS",
            )
        return RunnerResult(ok=True, response="普通回复", status="SUCCESS")

    async def run_subagent_task(
        self,
        task_id: str,
        prompt: str,
        *,
        model_override: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RunnerResult:
        self.subagent_prompts.append(prompt)
        self.subagent_started.set()
        await self.allow_subagent_finish.wait()
        return RunnerResult(
            ok=True,
            response=f"已使用 flux-pro 完成重绘。\n![图片]({self.out_image_rel})",
            status="SUCCESS",
        )

    async def judge_context(self, session_key: str, messages) -> JudgeDecision:
        return JudgeDecision(should_reply=False)

    async def reset_sessions(self, session_key: str | None = None) -> int:
        self.reset_calls += 1
        return 1

    async def update_config(self, *args, **kwargs) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        return {"model": "test-model", "warm_sessions": [], "circuit_open": False}


class McpPromptAndAsyncSubagentTests(unittest.IsolatedAsyncioTestCase):
    async def test_config_roundtrip_subagent_fields(self) -> None:
        raw = {
            "agy": {
                "subagent_enabled": True,
                "subagent_model": "gemini-3.8-flash-high",
                "subagent_max_concurrent": 3,
                "subagent_timeout_seconds": 300.0,
                "subagent_handoff_to_main": False,
            }
        }
        cfg = load_config_from_dict(raw, base_dir=Path("."))
        self.assertTrue(cfg.agy.subagent_enabled)
        self.assertEqual(cfg.agy.subagent_model, "gemini-3.8-flash-high")
        self.assertEqual(cfg.agy.subagent_max_concurrent, 3)
        self.assertEqual(cfg.agy.subagent_timeout_seconds, 300.0)
        self.assertFalse(cfg.agy.subagent_handoff_to_main)
        d = config_to_dict(cfg)
        self.assertEqual(d["agy"]["subagent_model"], "gemini-3.8-flash-high")
        self.assertEqual(d["agy"]["subagent_max_concurrent"], 3)
        self.assertEqual(d["agy"]["subagent_timeout_seconds"], 300.0)
        self.assertFalse(d["agy"]["subagent_handoff_to_main"])

    async def test_extract_bg_tasks_strips_tags_and_parses_json(self) -> None:
        raw = (
            "先帮你排队画图啦~\n"
            '<agy_bg_task>{"mcp":"draw-mcp","task":"画一只宇航员猫","images":[".media_cache/a.png"]}</agy_bg_task>'
        )
        cleaned, tasks = extract_bg_tasks(raw)
        self.assertEqual(cleaned, "先帮你排队画图啦~")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["mcp"], "draw-mcp")
        self.assertEqual(tasks[0]["task"], "画一只宇航员猫")
        self.assertEqual(tasks[0]["images"], [".media_cache/a.png"])

    async def test_mcp_usage_prompt_syncs_gemini_md_and_probes_tools_list(self) -> None:
        # Mock Streamable HTTP MCP server supporting initialize + notifications/initialized + tools/list
        async def _mcp_endpoint(request: web.Request) -> web.Response:
            body = await request.json()
            method = body.get("method")
            if method == "initialize":
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 1),
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "serverInfo": {"name": "multi-model-draw", "version": "2.1.0"},
                        },
                    },
                    headers={"Mcp-Session-Id": "sess-123"},
                )
            if method == "notifications/initialized":
                return web.Response(status=202)
            if method == "tools/list":
                return web.json_response(
                    {
                        "jsonrpc": "2.0",
                        "id": body.get("id", 2),
                        "result": {
                            "tools": [
                                {"name": "list_models", "description": "列出所有可用绘图模型", "inputSchema": {"type": "object", "properties": {}}},
                                {
                                    "name": "generate_image",
                                    "description": "根据提示词和模型生成图片",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "prompt": {"type": "string"},
                                            "model": {"type": "string", "enum": ["flux-pro", "anime-v2"]},
                                        },
                                        "required": ["prompt"],
                                    },
                                },
                            ]
                        },
                    }
                )
            return web.json_response({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

        mock_mcp_app = web.Application()
        mock_mcp_app.router.add_post("/mcp", _mcp_endpoint)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(base / "bot_workspace")
            cfg.agy.data_dir = str(base / "data")
            cfg.webui = WebUIConfig(enabled=True, host="127.0.0.1", port=0, username="star", password="")
            runner = _ConcurrentFakeRunner("out.png")
            bot_app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
            srv = WebUIServer(bot_app, cfg.webui)

            async with TestServer(mock_mcp_app) as mcp_srv, TestClient(TestServer(srv.web_app)) as client:
                mcp_url = str(mcp_srv.make_url("/mcp"))

                # 1) Test connection + tools/list discovery
                r_test = await client.post(
                    "/api/mcp/test",
                    json={"id": "draw-mcp", "transport": "http", "url": mcp_url},
                )
                self.assertEqual(r_test.status, 200)
                d_test = await r_test.json()
                self.assertTrue(d_test["ok"])
                self.assertEqual(len(d_test["tools"]), 2)
                self.assertEqual(d_test["tools"][0]["name"], "list_models")
                self.assertIn("model(string) [flux-pro, anime-v2]", d_test["tools"][1]["params"])

                # 2) Save MCP with usage_prompt + async_subagent=True -> syncs GEMINI.md cleanly
                r_save = await client.post(
                    "/api/mcp",
                    json={
                        "id": "draw-mcp",
                        "transport": "http",
                        "url": mcp_url,
                        "enabled": True,
                        "description": "多模型画图服务",
                        "usage_prompt": "画写实用 flux-pro，画二次元用 anime-v2。",
                        "async_subagent": True,
                    },
                )
                self.assertEqual(r_save.status, 200)

                # Standard mcp_config.json stays clean without non-standard fields
                mcp_json = json.loads((base / "bot_workspace" / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
                self.assertEqual(mcp_json["mcpServers"]["draw-mcp"], {"url": mcp_url})

                # GEMINI.md has both MCP usage prompt and async subagent protocol
                gemini_md = (base / "bot_workspace" / "GEMINI.md").read_text(encoding="utf-8")
                self.assertIn("已启用 MCP 工具调用规范", gemini_md)
                self.assertIn("画写实用 flux-pro，画二次元用 anime-v2。", gemini_md)
                self.assertIn("后台并发子代理派发协议（不阻塞群聊）", gemini_md)
                self.assertIn("<agy_bg_task>", gemini_md)

                # 3) Disabling MCP removes its guide & async protocol from GEMINI.md
                r_off = await client.post("/api/mcp/draw-mcp/toggle", json={"enabled": False})
                self.assertEqual(r_off.status, 200)
                gemini_md_off = (base / "bot_workspace" / "GEMINI.md").read_text(encoding="utf-8")
                self.assertNotIn("画写实用 flux-pro", gemini_md_off)
                self.assertNotIn("后台并发子代理派发协议", gemini_md_off)

    async def test_concurrent_chat_while_subagent_draws_and_hands_off_to_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "bot_workspace"
            (ws / ".media_cache").mkdir(parents=True, exist_ok=True)
            (ws / ".media_cache" / "ref.png").write_bytes(_TINY_PNG)
            (ws / "generated_cyber.png").write_bytes(_TINY_PNG)

            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(ws)
            cfg.agy.data_dir = str(base / "data")
            cfg.agy.subagent_enabled = True
            cfg.agy.subagent_handoff_to_main = True
            cfg.collector.quiet_wait_seconds = 0.01
            cfg.collector.max_wait_seconds = 0.05
            cfg.rate_limit.user_cooldown_seconds = 0.0
            cfg.rate_limit.channel_min_interval_seconds = 0.0

            plat = PlatformConfig(id="qq_1", type="onebot_v11")
            cfg.platforms = [plat]

            runner = _ConcurrentFakeRunner("generated_cyber.png")
            app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
            adapter = _DummyAdapter(plat)
            app.registry.adapters["qq_1"] = adapter

            app.mcp_manager.upsert_server(
                "draw-mcp",
                {
                    "transport": "http",
                    "url": "http://127.0.0.1:9999/mcp",
                    "enabled": True,
                    "usage_prompt": "画图先选 flux-pro 模型",
                    "async_subagent": True,
                },
            )
            app.sync_workspace_instructions()

            # 1) User sends reference image + drawing request
            draw_msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="99999",
                scene="group",
                channel_id="10001",
                message_id="m-draw-1",
                user_id="12345",
                sender_name="小明",
                text="[图片] 帮我把这张图改成赛博朋克风格",
                timestamp=time.time(),
                at_targets=["99999"],
                attachments=[
                    MediaAttachment(
                        kind="image",
                        local_path=".media_cache/ref.png",
                    )
                ],
            )
            await app.handle_message(draw_msg)
            await asyncio.wait_for(runner.subagent_started.wait(), timeout=2.0)

            # Main agent immediately sent clean ack without leaking <agy_bg_task>
            self.assertEqual(len(adapter.sent_replies), 1)
            self.assertIn("后台画师", adapter.sent_replies[0]["text"])
            self.assertNotIn("<agy_bg_task>", adapter.sent_replies[0]["text"])

            # Subagent received reference image path + MCP usage prompt + non-recursive header
            self.assertEqual(len(runner.subagent_prompts), 1)
            self.assertIn(".media_cache/ref.png", runner.subagent_prompts[0])
            self.assertIn("画图先选 flux-pro 模型", runner.subagent_prompts[0])
            self.assertIn("[后台子代理执行模式", runner.subagent_prompts[0])

            # 2) WHILE subagent is still running, user continues chatting and gets immediate reply!
            chat_msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="99999",
                scene="group",
                channel_id="10001",
                message_id="m-chat-2",
                user_id="12345",
                sender_name="小明",
                text="顺便问下今晚吃啥好",
                timestamp=time.time(),
                at_targets=["99999"],
            )
            await app.handle_message(chat_msg)
            for _ in range(50):
                if len(adapter.sent_replies) >= 2:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(adapter.sent_replies), 2)
            self.assertEqual(adapter.sent_replies[1]["text"], "今晚吃火锅不错呀！")
            # Subagent still hasn't delivered image yet!
            self.assertEqual(len(adapter.sent_images), 0)

            # 3) Now subagent finishes -> hands off to main agent -> delivers image + in-persona caption!
            runner.allow_subagent_finish.set()
            await asyncio.wait_for(runner.handoff_done.wait(), timeout=2.0)
            for _ in range(50):
                if len(adapter.sent_images) >= 1:
                    break
                await asyncio.sleep(0.02)

            self.assertEqual(len(adapter.sent_images), 1)
            img_call = adapter.sent_images[0]
            self.assertEqual(img_call["msg_id"], "m-draw-1")
            self.assertEqual(img_call["bytes"], _TINY_PNG)
            self.assertIn("赛博朋克版本刚出炉", img_call["caption"])

            await app.shutdown()

    async def test_agy_runner_subagent_is_not_blocked_by_main_chat_semaphore(self) -> None:
        from agychat_bot.config import AgyConfig, RateLimitConfig

        sub_started = asyncio.Event()
        sub_release = asyncio.Event()

        class _FakeStdin:
            def __init__(self, on_write) -> None:
                self._on_write = on_write
                self._closed = False

            def is_closing(self) -> bool:
                return self._closed

            def write(self, data: bytes) -> None:
                self._on_write(data.decode("utf-8"))

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self._closed = True

        class _FakeStdout:
            def __init__(self, q: asyncio.Queue[bytes]) -> None:
                self._q = q

            async def readline(self) -> bytes:
                return await self._q.get()

        class _FakeStderr:
            async def readline(self) -> bytes:
                return b""

        class _FakeProc:
            def __init__(self, is_subagent: bool) -> None:
                self.returncode: int | None = None
                self._q: asyncio.Queue[bytes] = asyncio.Queue()
                self._is_sub = is_subagent
                self.stdin = _FakeStdin(self._handle_write)
                self.stdout = _FakeStdout(self._q)
                self.stderr = _FakeStderr()

            def _handle_write(self, raw: str) -> None:
                async def _respond() -> None:
                    if self._is_sub:
                        sub_started.set()
                        await sub_release.wait()
                        res = {"event": "result", "result": {"status": "SUCCESS", "response": "sub-done", "conversation_id": "sub-1", "num_turns": 1}}
                    else:
                        res = {"event": "result", "result": {"status": "SUCCESS", "response": "main-fast", "conversation_id": "main-1", "num_turns": 1}}
                    await self._q.put((json.dumps(res) + "\n").encode("utf-8"))

                asyncio.create_task(_respond())

            async def wait(self) -> int:
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

        spawn_count = 0

        async def _factory(*args, **kwargs):
            nonlocal spawn_count
            spawn_count += 1
            # First spawn is subagent, second is main chat
            return _FakeProc(is_subagent=(spawn_count == 1))

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runner = AgyRunner(
                AgyConfig(executable="fake-agy", subagent_max_concurrent=2, subagent_timeout_seconds=30.0),
                RateLimitConfig(max_concurrent_requests=1),
                workspace_dir=base / "ws",
                data_dir=base / "data",
                process_factory=_factory,
            )

            sub_task = asyncio.create_task(runner.run_subagent_task("t1", "draw something slow"))
            await asyncio.wait_for(sub_started.wait(), timeout=2.0)

            # While subagent is mid-flight, main chat completes immediately despite max_concurrent_requests=1!
            main_res = await asyncio.wait_for(runner.ask("qq:group:1", "hello main"), timeout=2.0)
            self.assertTrue(main_res.ok)
            self.assertEqual(main_res.response, "main-fast")
            self.assertFalse(sub_task.done())

            sub_release.set()
            sub_res = await asyncio.wait_for(sub_task, timeout=2.0)
            self.assertTrue(sub_res.ok)
            self.assertEqual(sub_res.response, "sub-done")
            await runner.shutdown()

    async def test_mcp_per_tool_subagent_policy_rendered_in_gemini_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(base / "bot_workspace")
            cfg.agy.data_dir = str(base / "data")
            cfg.webui = WebUIConfig(enabled=True, host="127.0.0.1", port=0, username="star", password="")
            runner = _ConcurrentFakeRunner("out.png")
            bot_app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
            srv = WebUIServer(bot_app, cfg.webui)

            async with TestClient(TestServer(srv.web_app)) as client:
                r = await client.post(
                    "/api/mcp",
                    json={
                        "id": "draw-mcp",
                        "transport": "http",
                        "url": "http://127.0.0.1:9999/mcp",
                        "enabled": True,
                        "async_subagent": False,
                        "discovered_tools": [
                            {"name": "list_models", "description": "查看模型", "params": []},
                            {"name": "generate_image", "description": "生图", "params": ["prompt*(string)"]},
                            {"name": "edit_image", "description": "改图", "params": ["prompt*(string)"]},
                        ],
                        "tool_subagent": {
                            "list_models": False,
                            "generate_image": True,
                            "edit_image": True,
                        },
                    },
                )
                self.assertEqual(r.status, 200)
                saved = (await r.json())["data"]
                self.assertTrue(saved["tool_subagent"]["generate_image"])
                self.assertFalse(saved["tool_subagent"]["list_models"])

                gemini_md = (base / "bot_workspace" / "GEMINI.md").read_text(encoding="utf-8")
                self.assertIn("使用后台子代理并发执行的工具", gemini_md)
                self.assertIn("`generate_image`", gemini_md)
                self.assertIn("`edit_image`", gemini_md)
                self.assertIn("主代理直接同步调用的工具（无需子代理）", gemini_md)
                self.assertIn("`list_models`", gemini_md)
                self.assertIn("后台并发子代理派发协议（不阻塞群聊）", gemini_md)

    async def test_plugin_and_command_subagent_concurrent_execution(self) -> None:
        plugin_code = '''from __future__ import annotations
from agychat_bot.plugins import BotPlugin, PluginContext, PluginReply, command

class MixedSpeedPlugin(BotPlugin):
    id = "mixed_speed"
    name = "Mixed Speed"

    @command("slowgen", description="耗时生图分析命令")
    async def cmd_slowgen(self, ctx: PluginContext, msg, args: str) -> PluginReply:
        res = await ctx.ask_llm(msg.session_key, f"slow-work:{args}")
        return PluginReply(text=res, quote=True)

    @command("fastinfo", description="快速查询命令")
    async def cmd_fastinfo(self, ctx: PluginContext, msg, args: str) -> PluginReply:
        return PluginReply(text="快速查询结果: OK", quote=True)
'''
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "bot_workspace"
            ws.mkdir(parents=True, exist_ok=True)
            (ws / "out_pl.png").write_bytes(_TINY_PNG)
            plugins_dir = base / "plugins"
            plugins_dir.mkdir(parents=True, exist_ok=True)
            (plugins_dir / "mixed_speed.py").write_text(plugin_code, encoding="utf-8")

            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(ws)
            cfg.agy.data_dir = str(base / "data")
            cfg.plugins.dir = str(plugins_dir)
            cfg.agy.subagent_enabled = True
            cfg.agy.subagent_handoff_to_main = True
            plat = PlatformConfig(id="qq_1", type="onebot_v11")
            cfg.platforms = [plat]

            runner = _ConcurrentFakeRunner("out_pl.png")
            app = BotApplication(cfg, runner=runner)  # type: ignore[arg-type]
            adapter = _DummyAdapter(plat)
            app.registry.adapters["qq_1"] = adapter
            app.channel_guard.activate_group("qq_1", "10001")
            await app.ensure_plugins_loaded()

            # Configure per-command subagent: slowgen=True, fastinfo=False
            await app.plugin_manager.update_plugin_scope(
                "mixed_speed",
                {
                    "mode": "all",
                    "allow_private": True,
                    "use_subagent": False,
                    "command_subagent": {"slowgen": True, "fastinfo": False},
                },
            )
            self.assertTrue(app.plugin_manager.should_use_subagent("mixed_speed", "slowgen"))
            self.assertFalse(app.plugin_manager.should_use_subagent("mixed_speed", "fastinfo"))

            slow_msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="99999",
                scene="group",
                channel_id="10001",
                message_id="m-pl-1",
                user_id="12345",
                sender_name="小明",
                text="/slowgen 画猫",
                timestamp=time.time(),
            )
            # handle_message returns IMMEDIATELY while /slowgen runs in background subagent
            await asyncio.wait_for(app.handle_message(slow_msg), timeout=1.0)
            await asyncio.wait_for(runner.subagent_started.wait(), timeout=2.0)
            self.assertEqual(len(runner.subagent_prompts), 1)
            self.assertIn("slow-work:画猫", runner.subagent_prompts[0])

            # Meanwhile /fastinfo runs synchronously right away without waiting!
            fast_msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="99999",
                scene="group",
                channel_id="10001",
                message_id="m-pl-2",
                user_id="12345",
                sender_name="小明",
                text="/fastinfo",
                timestamp=time.time(),
            )
            await asyncio.wait_for(app.handle_message(fast_msg), timeout=1.0)
            self.assertEqual(len(adapter.sent_replies), 1)
            self.assertEqual(adapter.sent_replies[0]["text"], "快速查询结果: OK")

            # Release slowgen -> extracts generated image & delivers via send_image
            runner.allow_subagent_finish.set()
            for _ in range(50):
                if len(adapter.sent_images) >= 1:
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len(adapter.sent_images), 1)
            self.assertEqual(adapter.sent_images[0]["msg_id"], "m-pl-1")
            self.assertEqual(adapter.sent_images[0]["bytes"], _TINY_PNG)
            await app.shutdown()

    async def test_subagent_mcp_and_plugin_structured_logging(self) -> None:
        from agychat_bot.config import AgyConfig, RateLimitConfig
        from agychat_bot.runner.agy_stream import _extract_tool_log_detail

        # 1) MCP tool log detail extraction
        is_mcp, tool_name, detail = _extract_tool_log_detail(
            {
                "step_type": "tool",
                "tool_name": "call_mcp_tool",
                "input": {
                    "server_name": "wedraw",
                    "tool_name": "generate_image",
                    "arguments": {"model": "flux-pro", "prompt": "cat"},
                },
            }
        )
        self.assertTrue(is_mcp)
        self.assertEqual(tool_name, "call_mcp_tool")
        self.assertIn("server=wedraw", detail)
        self.assertIn("tool=generate_image", detail)
        self.assertIn("model=flux-pro", detail)

        # 2) Subagent process lifecycle + MCP step logging
        class _FakeStdin:
            def __init__(self, on_write) -> None:
                self._on_write = on_write
                self._closed = False

            def is_closing(self) -> bool:
                return self._closed

            def write(self, data: bytes) -> None:
                self._on_write(data.decode("utf-8"))

            async def drain(self) -> None:
                return None

            def close(self) -> None:
                self._closed = True

        class _FakeStdout:
            def __init__(self, q: asyncio.Queue[bytes]) -> None:
                self._q = q

            async def readline(self) -> bytes:
                return await self._q.get()

        class _FakeStderr:
            def __init__(self) -> None:
                self._sent = False

            async def readline(self) -> bytes:
                if not self._sent:
                    self._sent = True
                    return b"[mcp] wedraw warning: retrying upload\n"
                return b""

        class _FakeSubProc:
            def __init__(self) -> None:
                self.pid = 43210
                self.returncode: int | None = None
                self._q: asyncio.Queue[bytes] = asyncio.Queue()
                self.stdin = _FakeStdin(self._handle_write)
                self.stdout = _FakeStdout(self._q)
                self.stderr = _FakeStderr()

            def _handle_write(self, raw: str) -> None:
                async def _respond() -> None:
                    events = [
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_id": "s1",
                                "step_type": "tool",
                                "tool_name": "call_mcp_tool",
                                "state": "RUNNING",
                                "input": {
                                    "server_name": "wedraw",
                                    "tool_name": "generate_image",
                                    "arguments": {"model": "flux-pro"},
                                },
                            },
                        },
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_id": "s1",
                                "step_type": "tool",
                                "tool_name": "call_mcp_tool",
                                "state": "DONE",
                                "input": {
                                    "server_name": "wedraw",
                                    "tool_name": "generate_image",
                                    "arguments": {"model": "flux-pro"},
                                },
                            },
                        },
                        {
                            "event": "result",
                            "result": {
                                "status": "SUCCESS",
                                "response": "done",
                                "conversation_id": "sub-log-1",
                                "num_turns": 1,
                            },
                        },
                    ]
                    for ev in events:
                        await self._q.put((json.dumps(ev) + "\n").encode("utf-8"))

                asyncio.create_task(_respond())

            async def wait(self) -> int:
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

        async def _factory(*args, **kwargs):
            return _FakeSubProc()

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runner = AgyRunner(
                AgyConfig(executable="fake-agy", subagent_max_concurrent=1, subagent_timeout_seconds=30.0),
                RateLimitConfig(max_concurrent_requests=1),
                workspace_dir=base / "ws",
                data_dir=base / "data",
                process_factory=_factory,
            )
            with self.assertLogs("agychat_bot.runner.agy_stream", level="INFO") as cm:
                res = await runner.run_subagent_task("log1", "draw a cat")
            self.assertTrue(res.ok)
            joined = "\n".join(cm.output)
            self.assertIn("子代理进程已启动", joined)
            self.assertIn("[子代理 stderr]", joined)
            self.assertIn("[子代理][MCP] 开始调用工具: call_mcp_tool", joined)
            self.assertIn("server=wedraw", joined)
            self.assertIn("[子代理][MCP] 调用工具完成: call_mcp_tool", joined)
            self.assertIn("子代理进程执行成功", joined)
            self.assertIn("子代理进程已回收", joined)
            await runner.shutdown()

    async def test_log_buffer_boot_id_tags_and_step_index_tool_logging(self) -> None:
        import logging as _logging
        from agychat_bot.config import AgyConfig, RateLimitConfig
        from agychat_bot.runner.agy_stream import AgyRunner
        from agychat_bot.webui.log_buffer import LogRingBufferHandler

        handler = LogRingBufferHandler(max_entries=100)
        handler.setFormatter(_logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        test_logger = _logging.getLogger("agychat_bot.test_live_logs")
        test_logger.setLevel(_logging.INFO)
        test_logger.addHandler(handler)
        try:
            test_logger.info("[qq_1:group:1] [工具] 开始调用工具: read_file (path=.media_cache/a.png)")
            test_logger.info("[subagent:abc] [子代理][MCP] 开始调用工具: mcp_wedraw_generate_image (server=wedraw, tool=generate_image)")
            test_logger.info("[qq_1:group:1] [插件:image_gif_gen] 触发命令 /draw")

            all_entries = handler.get_entries(limit=50)
            self.assertEqual(len(all_entries), 3)
            self.assertTrue(all(e["boot_id"] == handler.boot_id for e in all_entries))
            self.assertEqual(handler.latest_seq, 3)
            self.assertIn("tool", all_entries[0]["tags"])
            self.assertEqual(set(all_entries[1]["tags"]), {"subagent", "mcp", "tool"})
            self.assertIn("plugin", all_entries[2]["tags"])

            self.assertEqual(len(handler.get_entries(tag="mcp")), 1)
            self.assertEqual(len(handler.get_entries(tag="subagent")), 1)
            self.assertEqual(len(handler.get_entries(tag="plugin")), 1)
            self.assertEqual(len(handler.get_entries(tag="tool")), 2)
            self.assertEqual(len(handler.get_entries(since_seq=2)), 1)

            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                cfg = BotConfig(base_dir=base)
                cfg.agy.workspace_dir = str(base / "bot_workspace")
                cfg.agy.data_dir = str(base / "data")
                cfg.webui = WebUIConfig(enabled=True, host="127.0.0.1", port=0, username="star", password="")
                runner = _ConcurrentFakeRunner("out.png")
                bot_app = BotApplication(cfg, runner=runner, log_handler=handler)  # type: ignore[arg-type]
                srv = WebUIServer(bot_app, cfg.webui, log_handler=handler)

                async with TestClient(TestServer(srv.web_app)) as client:
                    r = await client.get("/api/logs?tag=mcp")
                    self.assertEqual(r.status, 200)
                    body = await r.json()
                    self.assertEqual(body["boot_id"], handler.boot_id)
                    self.assertEqual(body["latest_seq"], 3)
                    self.assertEqual(len(body["data"]), 1)

                    r_inc = await client.get("/api/logs?since_seq=2")
                    self.assertEqual(r_inc.status, 200)
                    self.assertEqual(len((await r_inc.json())["data"]), 1)
        finally:
            test_logger.removeHandler(handler)

        # Verify step_index-based tool logging (main agent built-in tool + mcp_<srv>_<tool>)
        class _FakeStdin2:
            def __init__(self, on_write) -> None:
                self._on_write = on_write

            def write(self, data: bytes) -> None:
                self._on_write(data.decode("utf-8"))

            async def drain(self) -> None:
                pass

            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                pass

        class _FakeStdout2:
            def __init__(self, q: asyncio.Queue[bytes]) -> None:
                self._q = q

            async def readline(self) -> bytes:
                return await self._q.get()

        class _FakeMainProc:
            def __init__(self) -> None:
                self.pid = 54321
                self.returncode: int | None = None
                self._q: asyncio.Queue[bytes] = asyncio.Queue()
                self.stdin = _FakeStdin2(self._handle_write)
                self.stdout = _FakeStdout2(self._q)
                self.stderr = None

            def _handle_write(self, raw: str) -> None:
                async def _respond() -> None:
                    events = [
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_index": 0,
                                "step_type": "tool",
                                "tool_name": "read_file",
                                "state": "STEP_STATE_RUNNING",
                                "tool_input": {"path": ".media_cache/ref.png"},
                            },
                        },
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_index": 0,
                                "step_type": "tool",
                                "tool_name": "read_file",
                                "state": "STEP_STATE_DONE",
                                "tool_input": {"path": ".media_cache/ref.png"},
                            },
                        },
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_index": 1,
                                "step_type": "tool",
                                "tool_name": "mcp_wedraw_generate_image",
                                "state": "RUNNING",
                                "args": {"model": "gemini-img"},
                            },
                        },
                        {
                            "event": "step_update",
                            "step_update": {
                                "step_index": 1,
                                "step_type": "tool",
                                "tool_name": "mcp_wedraw_generate_image",
                                "state": "DONE",
                                "args": {"model": "gemini-img"},
                            },
                        },
                        {
                            "event": "result",
                            "result": {
                                "status": "SUCCESS",
                                "response": "all good",
                                "conversation_id": "main-log-1",
                                "num_turns": 1,
                            },
                        },
                    ]
                    for ev in events:
                        await self._q.put((json.dumps(ev) + "\n").encode("utf-8"))

                asyncio.create_task(_respond())

            async def wait(self) -> int:
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

        async def _factory2(*args, **kwargs):
            return _FakeMainProc()

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runner = AgyRunner(
                AgyConfig(executable="fake-agy"),
                RateLimitConfig(max_concurrent_requests=1),
                workspace_dir=base / "ws",
                data_dir=base / "data",
                process_factory=_factory2,
            )
            with self.assertLogs("agychat_bot.runner.agy_stream", level="INFO") as cm:
                res = await runner.ask("qq_1:group:7", "check image")
            self.assertTrue(res.ok)
            joined = "\n".join(cm.output)
            self.assertIn("[工具] 开始调用工具: read_file (path=.media_cache/ref.png)", joined)
            self.assertIn("[工具] agy 调用工具完成: read_file", joined)
            self.assertIn("耗时 ", joined)
            self.assertIn("[MCP] 开始调用工具: mcp_wedraw_generate_image (server=wedraw, tool=generate_image, model=gemini-img)", joined)
            self.assertIn("Antigravity 推理完成", joined)
            self.assertIn("工具调用 2 次", joined)
            await runner.shutdown()

    async def test_allow_command_execution_updates_settings_and_gemini_md(self) -> None:
        from agychat_bot.config import AgyConfig, RateLimitConfig, config_to_dict, load_config_from_dict
        from agychat_bot.runner.agy_stream import AgyRunner

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "bot_workspace"
            data = base / "data"
            ws.mkdir(parents=True, exist_ok=True)
            data.mkdir(parents=True, exist_ok=True)
            settings_p = base / "settings.bot.json"
            settings_p.write_text(
                json.dumps({"permissions": {"deny": ["command(*)", "read_url(*)"], "allow": ["mcp(wedraw/*)"]}}),
                encoding="utf-8",
            )

            # Roundtrip config
            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(ws)
            cfg.agy.data_dir = str(data)
            cfg.agy.settings_file = str(settings_p)
            cfg.agy.allow_command_execution = True
            rt = load_config_from_dict(config_to_dict(cfg), base_dir=base)
            self.assertTrue(rt.agy.allow_command_execution)

            # Runner generates settings with command(*) in allow and NOT in deny (both main & subagent)
            runner = AgyRunner(
                rt.agy,
                RateLimitConfig(),
                workspace_dir=ws,
                data_dir=data,
                settings_file=settings_p,
            )
            main_settings = runner._resolve_model_settings_file("gemini-4-neon-medium", None)
            self.assertIsNotNone(main_settings)
            assert main_settings is not None
            ms_data = json.loads(main_settings.read_text(encoding="utf-8"))
            self.assertIn("command(*)", ms_data["permissions"]["allow"])
            self.assertIn("read_file(*)", ms_data["permissions"]["allow"])
            self.assertNotIn("command(*)", ms_data["permissions"]["deny"])
            self.assertIn("write_file(GEMINI.md)", ms_data["permissions"]["deny"])

            sub_prof = runner._build_subagent_profile("gemini-4-neon-medium")
            self.assertTrue(sub_prof.enable_command)
            sub_settings = runner._resolve_model_settings_file("gemini-4-neon-medium", sub_prof)
            self.assertIsNotNone(sub_settings)
            assert sub_settings is not None
            ss_data = json.loads(sub_settings.read_text(encoding="utf-8"))
            self.assertIn("command(*)", ss_data["permissions"]["allow"])
            self.assertIn("read_file(*)", ss_data["permissions"]["allow"])
            self.assertNotIn("command(*)", ss_data["permissions"]["deny"])

            # PersonaStore reflects allow_command_execution in GEMINI.md
            app = BotApplication(rt, runner=runner)
            gm_path = app.sync_workspace_instructions()
            gm_text = gm_path.read_text(encoding="utf-8")
            self.assertIn("命令行工具 (`run_command`) 已由管理员显式开启", gm_text)
            await app.shutdown()

    async def test_subagent_empty_response_retry_ref_staging_and_segmented_handoff(self) -> None:
        from agychat_bot.config import AgyConfig, RateLimitConfig, SegmentedReplyConfig
        from agychat_bot.runner.agy_stream import AgyRunner

        # 1) AgyRunner treats 0-char subagent response as EMPTY_RESPONSE and retries automatically
        attempts = 0

        class _FakeStdin3:
            def __init__(self, on_write) -> None:
                self._on_write = on_write

            def write(self, data: bytes) -> None:
                self._on_write(data.decode("utf-8"))

            async def drain(self) -> None:
                pass

            def is_closing(self) -> bool:
                return False

            def close(self) -> None:
                pass

        class _FakeStdout3:
            def __init__(self, q: asyncio.Queue[bytes]) -> None:
                self._q = q

            async def readline(self) -> bytes:
                return await self._q.get()

        class _FlakyEmptySubProc:
            def __init__(self, attempt_num: int) -> None:
                self.pid = 60000 + attempt_num
                self.returncode: int | None = None
                self._attempt = attempt_num
                self._q: asyncio.Queue[bytes] = asyncio.Queue()
                self.stdin = _FakeStdin3(self._handle_write)
                self.stdout = _FakeStdout3(self._q)
                self.stderr = None

            def _handle_write(self, raw: str) -> None:
                async def _respond() -> None:
                    ev = {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "response": "" if self._attempt == 1 else "![图片](done.png) 重试成功",
                            "conversation_id": f"c-{self._attempt}",
                            "num_turns": 1,
                        },
                    }
                    await self._q.put((json.dumps(ev) + "\n").encode("utf-8"))

                asyncio.create_task(_respond())

            async def wait(self) -> int:
                self.returncode = 0
                return 0

            def terminate(self) -> None:
                self.returncode = 0

            def kill(self) -> None:
                self.returncode = -9

        async def _flaky_factory(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return _FlakyEmptySubProc(attempts)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "bot_workspace"
            ws.mkdir(parents=True, exist_ok=True)
            ext_dir = base / "external_refs"
            ext_dir.mkdir(parents=True, exist_ok=True)
            # PNG bytes misnamed with .jpg extension (exact real-world WeDraw rejection case!)
            ext_img = ext_dir / "hero_png_named_jpg.jpg"
            ext_img.write_bytes(_TINY_PNG)

            runner = AgyRunner(
                AgyConfig(executable="fake-agy", model="gemini-4-neon-medium"),
                RateLimitConfig(),
                workspace_dir=ws,
                data_dir=base / "data",
                process_factory=_flaky_factory,
            )
            res = await runner.run_subagent_task("retry1", "draw")
            self.assertTrue(res.ok)
            self.assertEqual(attempts, 2)
            self.assertIn("重试成功", res.response)
            await runner.shutdown()

            # 2) External reference images are staged into .media_cache AND handoff splits ||| segments cleanly
            class _SegmentedHandoffRunner:
                def __init__(self) -> None:
                    self.last_sub_prompt = ""

                def is_circuit_open(self) -> tuple[bool, float]:
                    return False, 0.0

                def get_status(self) -> dict[str, Any]:
                    return {}

                async def reset_sessions(self, session_key: str | None = None) -> int:
                    return 0

                async def shutdown(self) -> None:
                    pass

                async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> Any:
                    raise NotImplementedError

                async def ask(self, session_key: str, prompt: str, **kwargs: Any) -> RunnerResult:
                    if "[后台子代理任务已完成" in prompt or "[后台子代理任务执行失败" in prompt:
                        return RunnerResult(ok=True, response="第一句|||第二句|||第三句")
                    return RunnerResult(
                        ok=True,
                        response=(
                            "收到马上画~"
                            f'<agy_bg_task>{{"mcp":"wedraw","task":"重画","images":["{ext_img.as_posix()}"]}}</agy_bg_task>'
                        ),
                    )

                async def run_subagent_task(self, task_id: str, prompt: str, **kwargs: Any) -> RunnerResult:
                    self.last_sub_prompt = prompt
                    return RunnerResult(ok=False, error="模拟失败")

            cfg = BotConfig(base_dir=base)
            cfg.agy.workspace_dir = str(ws)
            cfg.agy.data_dir = str(base / "data")
            cfg.collector.quiet_wait_seconds = 0.02
            cfg.collector.max_wait_seconds = 0.05
            cfg.reply.segmented_reply = SegmentedReplyConfig(
                enabled=True,
                interval_method="fixed",
                interval="0,0",
                split_mode="regex",
                regex=r"[\s\S]+?(?:\|\|\||$)",
                content_cleanup_rule=r"\|\|\||^\s+|\s+$",
            )
            plat = PlatformConfig(id="qq_1", type="onebot_v11", self_id="999")
            cfg.platforms = [plat]
            fake_r = _SegmentedHandoffRunner()
            app = BotApplication(cfg, runner=fake_r)  # type: ignore[arg-type]
            adapter = _DummyAdapter(plat)
            app.registry.adapters["qq_1"] = adapter
            app.channel_guard.activate_group("qq_1", "10001")

            msg = IncomingMessage(
                adapter_id="qq_1",
                platform="onebot_v11",
                self_id="999",
                scene="group",
                channel_id="10001",
                message_id="m1",
                user_id="u1",
                sender_name="Alice",
                text="重画一张",
                at_targets=["999"],
                timestamp=1000.0,
            )
            await app.handle_message(msg)
            await asyncio.sleep(0.2)
            for t in list(app._bg_tasks):
                await asyncio.wait_for(t, timeout=2.0)

            # External ref image (PNG bytes named .jpg) was staged into .media_cache with REAL .png extension!
            self.assertIn(".media_cache/subref_", fake_r.last_sub_prompt)
            self.assertIn(".png", fake_r.last_sub_prompt)
            self.assertNotIn("hero_png_named_jpg.jpg", fake_r.last_sub_prompt)
            # Also verify misnamed file ALREADY inside .media_cache gets re-staged to .png
            misnamed_inside = ws / ".media_cache" / "awx_fake.jpg"
            misnamed_inside.parent.mkdir(parents=True, exist_ok=True)
            misnamed_inside.write_bytes(_TINY_PNG)
            staged_inside = app._stage_subagent_reference_images([".media_cache/awx_fake.jpg"])
            self.assertEqual(len(staged_inside), 1)
            self.assertTrue(staged_inside[0].endswith(".png"), f"Expected .png, got {staged_inside[0]}")
            # Handoff reply split into 3 bubbles and contains NO raw |||
            sent_texts = [r["text"] for r in adapter.sent_replies]
            self.assertIn("第一句", sent_texts)
            self.assertIn("第二句", sent_texts)
            self.assertIn("第三句", sent_texts)
            self.assertTrue(all("|||" not in t for t in sent_texts))
            await app.shutdown()


if __name__ == "__main__":
    unittest.main()
