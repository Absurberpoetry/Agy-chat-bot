from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agy_bot.adapters.base import PlatformAdapter  # noqa: E402
from agy_bot.app import BotApplication, format_context_prompt, format_delta_prompt  # noqa: E402
from agy_bot.collector import GroupContextStore  # noqa: E402
from agy_bot.config import (  # noqa: E402
    AgyConfig,
    BotConfig,
    CollectorConfig,
    PlatformConfig,
    RateLimitConfig,
    config_to_dict,
    load_config_from_dict,
)
from agy_bot.models import IncomingMessage, MediaAttachment  # noqa: E402
from agy_bot.runner.agy_stream import AgyRunner  # noqa: E402


class _RecordingAdapter(PlatformAdapter):
    platform_name = "onebot_v11"

    def __init__(self, cfg: PlatformConfig) -> None:
        super().__init__(cfg)
        self.sent: list[str] = []

    async def start(self, on_message) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_reply(self, trigger_msg, text: str, *, quote_trigger: bool = True, at_user: bool = False):
        self.sent.append(text)
        return f"bot-msg-{len(self.sent)}"


class _FakeStreamProc:
    """In-memory fake `agy --input-format stream-json --output-format stream-json` process."""

    def __init__(self, proc_id: int, cmd: list[str], prompts_log: list[tuple[int, str]]) -> None:
        self.proc_id = proc_id
        self.cmd = list(cmd)
        self.prompts_log = prompts_log
        self.returncode: int | None = None
        self.pid = 90000 + proc_id
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self._buf = bytearray()
        self._closed = False
        self._turn = 0
        self.stdin = self

    def is_closing(self) -> bool:
        return self._closed

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        while b"\n" in self._buf:
            idx = self._buf.index(b"\n")
            line = bytes(self._buf[:idx]).decode("utf-8", errors="replace").strip()
            del self._buf[: idx + 1]
            if not line:
                continue
            obj = json.loads(line)
            content = str((obj.get("message") or {}).get("content") or "")
            self._turn += 1
            self.prompts_log.append((self.proc_id, content))
            if self._turn == 1:
                init_ev = {"event": "init", "conversation_id": f"conv-p{self.proc_id}"}
                self.stdout.feed_data((json.dumps(init_ev, ensure_ascii=False) + "\n").encode("utf-8"))
            cached = 1200 if self._turn > 1 else 0
            res_ev = {
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "conversation_id": f"conv-p{self.proc_id}",
                    "num_turns": self._turn,
                    "response": f"收到第{self._turn}轮(proc={self.proc_id})",
                    "usage": {"input_tokens": 1500, "cache_read_tokens": cached},
                },
            }
            self.stdout.feed_data((json.dumps(res_ev, ensure_ascii=False) + "\n").encode("utf-8"))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self._closed = True
        self.returncode = 0
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def terminate(self) -> None:
        self.close()

    def kill(self) -> None:
        self.close()

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _make_msg(
    seq_idx: int,
    *,
    text: str,
    is_self: bool = False,
    channel_id: str = "88888",
    user_id: str = "10001",
    sender_name: str = "测试群友",
    at_targets: list[str] | None = None,
) -> IncomingMessage:
    return IncomingMessage(
        adapter_id="qq_1",
        platform="onebot_v11",
        self_id="999000111",
        scene="group",
        channel_id=channel_id,
        message_id=f"m_{seq_idx}",
        user_id="999000111" if is_self else user_id,
        sender_name="安挽行" if is_self else sender_name,
        text=text,
        timestamp=1700000000.0 + seq_idx * 5.0,
        at_targets=at_targets or [],
        is_self=is_self,
    )


class SqliteContextAndCompactionTests(unittest.IsolatedAsyncioTestCase):
    def test_config_roundtrip_compaction_fields(self) -> None:
        raw = {
            "collector": {
                "db_context_enabled": True,
                "compact_max_messages": 45,
                "compact_drop_by_count": 15,
                "compact_max_tokens": 9000,
                "compact_max_bytes": 32768,
                "compact_drop_by_tokens": 12,
            }
        }
        cfg = load_config_from_dict(raw, base_dir=ROOT)
        self.assertTrue(cfg.collector.db_context_enabled)
        self.assertEqual(cfg.collector.compact_max_messages, 45)
        self.assertEqual(cfg.collector.compact_drop_by_count, 15)
        self.assertEqual(cfg.collector.compact_max_tokens, 9000)
        self.assertEqual(cfg.collector.compact_max_bytes, 32768)
        self.assertEqual(cfg.collector.compact_drop_by_tokens, 12)

        dumped = config_to_dict(cfg)
        self.assertEqual(dumped["collector"]["compact_max_messages"], 45)
        self.assertEqual(dumped["collector"]["compact_drop_by_count"], 15)
        self.assertEqual(dumped["collector"]["compact_max_tokens"], 9000)
        self.assertEqual(dumped["collector"]["compact_max_bytes"], 32768)
        self.assertEqual(dumped["collector"]["compact_drop_by_tokens"], 12)

    def test_named_model_presets_independent_compaction_and_cli_slug_resolution(self) -> None:
        raw = {
            "agy": {
                "executable": "fake-agy",
                "model": "mp_38_long",
                "fallback_models": ["mp_h2_fast"],
                "model_profiles": {
                    "mp_38_long": {
                        "name": "3.8长记忆",
                        "model": "gemini-3.8-high",
                        "compact_max_messages": 80,
                        "compact_drop_by_count": 20,
                        "compact_max_tokens": 24000,
                        "compact_drop_by_tokens": 15,
                    },
                    "mp_h2_fast": {
                        "name": "氢模型轻量",
                        "model": "gemini-4-hydrogen",
                        "compact_max_messages": 30,
                        "compact_drop_by_count": 10,
                        "compact_max_tokens": 6000,
                        "compact_max_bytes": 16384,
                        "compact_drop_by_tokens": 8,
                    },
                },
            }
        }
        cfg = load_config_from_dict(raw, base_dir=ROOT)
        cli_a, prof_a = cfg.agy.resolve_model_target("mp_38_long")
        cli_b, prof_b = cfg.agy.resolve_model_target("氢模型轻量")
        self.assertEqual(cli_a, "gemini-3.8-high")
        self.assertIsNotNone(prof_a)
        assert prof_a is not None
        self.assertEqual(prof_a.name, "3.8长记忆")
        self.assertEqual(prof_a.compact_max_messages, 80)
        self.assertEqual(prof_a.compact_drop_by_count, 20)

        self.assertEqual(cli_b, "gemini-4-hydrogen")
        self.assertIsNotNone(prof_b)
        assert prof_b is not None
        self.assertEqual(prof_b.compact_max_messages, 30)
        self.assertEqual(prof_b.compact_drop_by_count, 10)
        self.assertEqual(prof_b.compact_max_bytes, 16384)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            runner = AgyRunner(
                cfg.agy,
                cfg.rate_limit,
                workspace_dir=base / "ws",
                data_dir=base / "data",
            )
            cmd_a = runner._build_cli_args("mp_38_long")
            cmd_b = runner._build_cli_args("mp_h2_fast")
            self.assertEqual(cmd_a[cmd_a.index("--model") + 1], "gemini-3.8-high")
            self.assertEqual(cmd_b[cmd_b.index("--model") + 1], "gemini-4-hydrogen")

        dumped = config_to_dict(cfg)
        self.assertEqual(dumped["agy"]["model_profiles"]["mp_38_long"]["name"], "3.8长记忆")
        self.assertEqual(dumped["agy"]["model_profiles"]["mp_h2_fast"]["model"], "gemini-4-hydrogen")
        self.assertEqual(dumped["agy"]["model_profiles"]["mp_h2_fast"]["compact_drop_by_count"], 10)

    def test_sqlite_persistence_and_rule1_count_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat_history.db"
            store = GroupContextStore(history_limit=100, db_path=db_path)
            sk = "qq_1:group:88888"

            # Insert 5 user messages + 1 assistant message = 6 rows
            for i in range(1, 6):
                store.add_message(_make_msg(i, text=f"用户消息第{i}条"))
            store.add_message(_make_msg(6, text="这是我作为安挽行的第一条回复", is_self=True))

            # Before threshold (max_messages=8): no compaction
            w0 = store.get_active_window_and_compact(
                sk,
                max_messages=8,
                drop_by_count=3,
                max_tokens=50000,
                max_bytes=0,
                drop_by_tokens=3,
            )
            self.assertFalse(w0.compacted)
            self.assertEqual(len(w0.messages), 6)
            self.assertEqual(w0.archived_count, 0)

            # Hit Rule 1 (count >= 6 -> drop 3 oldest messages, keep 3 newest)
            w1 = store.get_active_window_and_compact(
                sk,
                max_messages=6,
                drop_by_count=3,
                max_tokens=50000,
                max_bytes=0,
                drop_by_tokens=2,
            )
            self.assertTrue(w1.compacted)
            self.assertEqual(w1.dropped_count, 3)
            self.assertEqual(len(w1.messages), 3)
            self.assertEqual(w1.archived_count, 3)
            self.assertEqual(w1.messages[0].db_seq, 4)
            self.assertEqual(w1.messages[-1].text, "这是我作为安挽行的第一条回复")
            self.assertTrue(w1.messages[-1].is_self)

            # Reopen SQLite in a brand new GroupContextStore -> cursor and history survive restart
            store2 = GroupContextStore(history_limit=100, db_path=db_path)
            w2 = store2.get_active_window_and_compact(
                sk,
                max_messages=10,
                drop_by_count=3,
                max_tokens=50000,
                max_bytes=0,
                drop_by_tokens=2,
            )
            self.assertFalse(w2.compacted)
            self.assertEqual(w2.active_min_seq, 4)
            self.assertEqual(len(w2.messages), 3)
            self.assertEqual(w2.archived_count, 3)
            # Old archived message is still resolvable via lookup_message (for QQ quote reply!)
            old_cached = store2.lookup_message("qq_1", "m_1")
            self.assertIsNotNone(old_cached)
            assert old_cached is not None
            self.assertEqual(old_cached.text, "用户消息第1条")

    def test_rule2_token_and_bytes_compaction_either_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat_history.db"
            store = GroupContextStore(history_limit=100, db_path=db_path)
            sk = "qq_1:group:88888"

            # 5 chunky messages (~80 tokens / ~300 bytes each)
            for i in range(1, 6):
                store.add_message(_make_msg(i, text=f"长消息#{i}：" + ("测试内容背景故事" * 12)))

            # Count threshold high (100), token threshold low (200) -> Rule 2 triggers!
            w_tok = store.get_active_window_and_compact(
                sk,
                max_messages=100,
                drop_by_count=10,
                max_tokens=200,
                max_bytes=0,
                drop_by_tokens=2,
            )
            self.assertTrue(w_tok.compacted)
            self.assertGreaterEqual(w_tok.dropped_count, 2)
            self.assertLess(w_tok.total_tokens, 200)
            self.assertTrue(any("Token" in r for r in w_tok.compact_reasons))

            # Add 4 more messages and test bytes threshold trigger (max_tokens=0, max_bytes=600)
            for i in range(6, 10):
                store.add_message(_make_msg(i, text=f"字节测试#{i}：" + ("ABCDEFGH" * 25)))
            w_bytes = store.get_active_window_and_compact(
                sk,
                max_messages=100,
                drop_by_count=10,
                max_tokens=0,
                max_bytes=600,
                drop_by_tokens=2,
            )
            self.assertTrue(w_bytes.compacted)
            self.assertLess(w_bytes.total_bytes, 600)
            self.assertTrue(any("字节" in r for r in w_bytes.compact_reasons))

    async def test_epoch_incremental_warm_cache_and_compaction_reset(self) -> None:
        """Verify:
        - Turn 1 seeds warm `agy` process from SQLite active window.
        - Turn 2 within same compaction epoch reuses the SAME warm process & sends ONLY incremental delta_prompt!
        - Interleaved ambient messages are preserved while bot's own reply is NOT duplicated.
        - Turn 3 triggers compaction -> advances `active_min_seq` -> recycles warm process & seeds cleanly from new window!
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "bot_workspace"
            data = base / "data"
            ws.mkdir()
            data.mkdir()

            spawned_cmds: list[list[str]] = []
            prompts_log: list[tuple[int, str]] = []

            async def _factory(*args: str, **kwargs: Any) -> _FakeStreamProc:
                proc_id = len(spawned_cmds) + 1
                spawned_cmds.append(list(args))
                return _FakeStreamProc(proc_id, list(args), prompts_log)

            cfg = BotConfig(base_dir=base)
            cfg.bot_name = "安挽行"
            cfg.agy.workspace_dir = str(ws)
            cfg.agy.data_dir = str(data)
            cfg.agy.executable = "fake-agy"
            cfg.agy.model = "gemini-4-neon-medium"
            cfg.collector.quiet_wait_seconds = 0.01
            cfg.collector.max_wait_seconds = 0.05
            cfg.collector.db_context_enabled = True
            # Trigger compaction at >= 6 active messages, dropping 3 oldest
            cfg.collector.compact_max_messages = 6
            cfg.collector.compact_drop_by_count = 3
            cfg.collector.compact_max_tokens = 100000
            cfg.collector.compact_max_bytes = 0
            cfg.collector.compact_drop_by_tokens = 3
            cfg.rate_limit.user_cooldown_seconds = 0
            cfg.rate_limit.channel_min_interval_seconds = 0
            cfg.rate_limit.max_replies_per_channel_per_minute = 100

            p_cfg = PlatformConfig(id="qq_1", enabled=True, ws_url="ws://127.0.0.1:3001", self_id="999000111")
            cfg.platforms = [p_cfg]

            runner = AgyRunner(
                cfg.agy,
                cfg.rate_limit,
                workspace_dir=ws,
                data_dir=data,
                process_factory=_factory,
            )
            app = BotApplication(cfg, runner=runner)
            adapter = _RecordingAdapter(p_cfg)
            app.registry.adapters["qq_1"] = adapter

            # Verify chat_history builtin MCP was auto-mounted in .agents/mcp_config.json
            mcp_cfg = json.loads((ws / ".agents" / "mcp_config.json").read_text(encoding="utf-8"))
            self.assertIn("chat_history", mcp_cfg["mcpServers"])

            # Turn 1: ambient #1 + @bot trigger #2 -> seeds warm proc #1
            await app.handle_message(_make_msg(1, text="早安大家，今天约定去吃火锅"))
            await app.handle_message(_make_msg(2, text="帮我记一下火锅暗号是9527", at_targets=["999000111"]))
            await asyncio.sleep(0.08)

            self.assertEqual(len(spawned_cmds), 1)
            self.assertEqual(len(prompts_log), 1)
            self.assertEqual(prompts_log[0][0], 1)
            self.assertIn("会话活跃上下文记录", prompts_log[0][1])
            self.assertIn("火锅暗号是9527", prompts_log[0][1])
            self.assertNotIn("--conversation", spawned_cmds[0])

            # Turn 2: ambient #4 + @bot trigger #5 (total 5 rows < 6, NO compaction)
            # Should reuse warm proc #1 with incremental delta prompt!
            await app.handle_message(_make_msg(4, text="对了我们下午三点出发"))
            await app.handle_message(_make_msg(5, text="你记住了吗？", at_targets=["999000111"]))
            await asyncio.sleep(0.08)

            self.assertEqual(len(spawned_cmds), 1, "同一压缩周期内应复用同一个暖进程以命中前缀缓存")
            self.assertEqual(len(prompts_log), 2)
            self.assertEqual(prompts_log[1][0], 1)
            self.assertIn("新增会话消息（接续上文", prompts_log[1][1])
            self.assertIn("下午三点出发", prompts_log[1][1])
            self.assertIn("你记住了吗？", prompts_log[1][1])
            # Earlier messages & bot's own previous reply MUST NOT be duplicated in delta_prompt
            self.assertNotIn("火锅暗号是9527", prompts_log[1][1])
            self.assertNotIn("收到第1轮", prompts_log[1][1])

            # Turn 3: trigger #7 pushes total active rows to 7 >= 6 -> COMPACTION FIRES!
            # Drops 3 oldest messages -> advances window_min_seq -> spawns clean proc #2 seeded from compacted window!
            await app.handle_message(_make_msg(7, text="现在还记得暗号吗？", at_targets=["999000111"]))
            await asyncio.sleep(0.08)

            self.assertEqual(len(spawned_cmds), 2, "触发上下文压缩后应开启新周期暖进程以丢弃旧前缀")
            self.assertEqual(len(prompts_log), 3)
            self.assertEqual(prompts_log[2][0], 2)
            self.assertIn("会话活跃上下文记录", prompts_log[2][1])
            self.assertIn("历史归档提示", prompts_log[2][1])
            self.assertIn("chat_history", prompts_log[2][1])
            self.assertNotIn("火锅暗号是9527", prompts_log[2][1], "被压缩归档的旧消息不应再出现在当前窗口中")

            await app.shutdown()

    async def test_builtin_chat_history_mcp_server_retrieves_compacted_user_and_bot_messages(self) -> None:
        """Requirement 4: verify real stdio JSON-RPC queries against compacted SQLite messages (both user & bot replies)."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "chat_history.db"
            store = GroupContextStore(history_limit=50, db_path=db_path)
            sk = "qq_1:group:88888"
            other_sk = "qq_1:group:99999"

            store.add_message(_make_msg(1, text="我们的秘密暗号是星辰大海"))
            store.add_message(_make_msg(2, text="好哒，安挽行已经记住秘密暗号星辰大海啦！", is_self=True))
            store.add_message(_make_msg(3, text="顺便聊聊今天天气真不错"))
            store.add_message(_make_msg(4, text="是的呢，阳光很好", is_self=True))
            store.add_message(_make_msg(5, text="这是另外一个群的隐私消息", channel_id="99999"))

            # Compact group 88888 so #1 and #2 are archived below active_min_seq
            win = store.get_active_window_and_compact(sk, max_messages=4, drop_by_count=2, max_tokens=0)
            self.assertTrue(win.compacted)
            self.assertEqual(win.archived_count, 2)

            # Spawn the actual stdio MCP server subprocess
            server_script = ROOT / "src" / "agy_bot" / "mcp_history_server.py"
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                str(server_script),
                "--db",
                str(db_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert proc.stdin is not None and proc.stdout is not None
            try:
                async def _rpc(req: dict[str, Any]) -> dict[str, Any]:
                    proc.stdin.write((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5.0)
                    return json.loads(raw.decode("utf-8"))

                # 1. initialize
                r_init = await _rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
                self.assertEqual(r_init["result"]["serverInfo"]["name"], "agy-bot-chat-history")

                # 2. tools/list
                r_list = await _rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                tool_names = [t["name"] for t in r_list["result"]["tools"]]
                self.assertIn("get_older_messages", tool_names)
                self.assertIn("search_chat_history", tool_names)
                self.assertIn("get_context_around", tool_names)

                # 3. get_older_messages before active_min_seq -> recovers compacted user & assistant messages!
                r_old = await _rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "get_older_messages",
                            "arguments": {"session_key": sk, "before_seq": win.active_min_seq, "limit": 10},
                        },
                    }
                )
                old_text = r_old["result"]["content"][0]["text"]
                self.assertIn("秘密暗号是星辰大海", old_text)
                self.assertIn("安挽行已经记住秘密暗号星辰大海啦", old_text)
                self.assertNotIn("另外一个群的隐私消息", old_text)

                # 4. search_chat_history filtering role="assistant" -> finds model's own archived reply!
                r_search = await _rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "search_chat_history",
                            "arguments": {"session_key": sk, "keyword": "星辰大海", "role": "assistant"},
                        },
                    }
                )
                search_text = r_search["result"]["content"][0]["text"]
                self.assertIn("安挽行已经记住秘密暗号星辰大海啦", search_text)
                self.assertNotIn("我们的秘密暗号是星辰大海", search_text)

                # 5. Cross-group isolation: searching other_sk returns only other_sk's rows
                r_iso = await _rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "search_chat_history",
                            "arguments": {"session_key": other_sk, "keyword": "星辰大海"},
                        },
                    }
                )
                iso_text = r_iso["result"]["content"][0]["text"]
                self.assertIn("共命中 0 条", iso_text)
                self.assertNotIn("星辰大海", iso_text)

                # 6. get_context_around(#2) -> returns surrounding rows
                r_ctx = await _rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "get_context_around",
                            "arguments": {"session_key": sk, "seq_id": 2, "before": 1, "after": 1},
                        },
                    }
                )
                ctx_text = r_ctx["result"]["content"][0]["text"]
                self.assertIn("秘密暗号是星辰大海", ctx_text)
                self.assertIn("今天天气真不错", ctx_text)
            finally:
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=5.0)

    async def test_same_epoch_model_switch_resumes_conversation_and_fallback_on_incompatible(self) -> None:
        """Switching model/effort within the same compaction epoch should pass `--conversation <old_id>`
        with incremental delta_prompt, and cleanly fall back to a fresh full-window session if resuming fails."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ws = base / "ws"
            data_dir = base / "data"
            ws.mkdir()
            data_dir.mkdir()

            spawned_cmds: list[list[str]] = []
            prompts_log: list[tuple[int, str]] = []
            fail_next_resumed = {"flag": False}

            class _SwitchableFakeProc(_FakeStreamProc):
                def __init__(self, proc_idx: int, cmd: list[str], fail_first: bool) -> None:
                    super().__init__(proc_idx, cmd, prompts_log)
                    self._fail_first = fail_first

                def write(self, data: bytes) -> None:
                    if self._fail_first:
                        self._fail_first = False
                        self._buf.extend(data)
                        while b"\n" in self._buf:
                            raw_line, _, rest = self._buf.partition(b"\n")
                            self._buf = bytearray(rest)
                            line = raw_line.decode("utf-8", errors="replace").strip()
                            if line:
                                obj = json.loads(line)
                                self.prompts_log.append((self.proc_id, str((obj.get("message") or {}).get("content") or "")))
                        self.stdout.feed_data(
                            (
                                json.dumps(
                                    {
                                        "event": "result",
                                        "result": {
                                            "status": "FAILED",
                                            "error": "conversation not compatible with requested model",
                                        },
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            ).encode("utf-8")
                        )
                        return
                    super().write(data)

            async def _factory(*cmd: str, **kwargs: Any) -> _SwitchableFakeProc:
                cmd_list = list(cmd)
                spawned_cmds.append(cmd_list)
                should_fail = bool(fail_next_resumed["flag"] and "--conversation" in cmd_list)
                if should_fail:
                    fail_next_resumed["flag"] = False
                return _SwitchableFakeProc(len(spawned_cmds), cmd_list, should_fail)

            p_cfg = PlatformConfig(id="qq_1", type="onebot_v11", enabled=True, ws_url="ws://127.0.0.1:3001", self_id="999000111")
            cfg = BotConfig(
                bot_name="小助手",
                platforms=[p_cfg],
                collector=CollectorConfig(
                    wait_triggers=[],
                    quiet_wait_seconds=0.01,
                    max_wait_seconds=0.05,
                    db_context_enabled=True,
                    compact_max_messages=20,
                    compact_drop_by_count=5,
                    compact_max_tokens=0,
                    compact_max_bytes=0,
                ),
                agy=AgyConfig(
                    mode="agy",
                    executable="fake-agy",
                    model="gemini-4-neon-medium",
                    workspace_dir=str(ws),
                    data_dir=str(data_dir),
                    max_warm_processes=2,
                    turn_timeout_seconds=5.0,
                ),
                rate_limit=RateLimitConfig(
                    max_concurrent_requests=1,
                    user_cooldown_seconds=0.0,
                    channel_min_interval_seconds=0.0,
                    max_replies_per_channel_per_minute=100,
                ),
                base_dir=base,
            )
            runner = AgyRunner(
                cfg.agy,
                cfg.rate_limit,
                workspace_dir=ws,
                data_dir=data_dir,
                process_factory=_factory,
            )
            app = BotApplication(cfg, runner=runner)
            app.registry.adapters["qq_1"] = _RecordingAdapter(p_cfg)

            # Turn 1 on gemini-4-neon-medium -> creates fresh conv-p1
            await app.handle_message(_make_msg(1, text="记住暗号 7788", at_targets=["999000111"]))
            await asyncio.sleep(0.08)
            self.assertEqual(len(spawned_cmds), 1)
            self.assertNotIn("--conversation", spawned_cmds[0])
            self.assertIn("gemini-4-neon-medium", spawned_cmds[0])

            # Hot-switch effort to gemini-4-neon-low via update_config (simulates WebUI Save & Reload)
            new_agy = AgyConfig(
                mode="agy",
                executable="fake-agy",
                model="gemini-4-neon-low",
                workspace_dir=str(ws),
                data_dir=str(data_dir),
                max_warm_processes=2,
                turn_timeout_seconds=5.0,
            )
            await runner.update_config(new_agy, cfg.rate_limit)

            # Turn 2 on gemini-4-neon-low -> MUST pass `--conversation conv-p1` and send ONLY delta_prompt!
            await app.handle_message(_make_msg(3, text="刚刚暗号是什么？", at_targets=["999000111"]))
            await asyncio.sleep(0.08)
            self.assertEqual(len(spawned_cmds), 2)
            self.assertIn("--conversation", spawned_cmds[1])
            conv_idx = spawned_cmds[1].index("--conversation")
            self.assertEqual(spawned_cmds[1][conv_idx + 1], "conv-p1")
            self.assertIn("gemini-4-neon-low", spawned_cmds[1])
            self.assertIn("新增会话消息（接续上文", prompts_log[1][1])
            self.assertNotIn("记住暗号 7788", prompts_log[1][1])

            # Turn 3: switch to another model where cloud rejects `--conversation`, verify clean fallback to full prompt!
            fail_next_resumed["flag"] = True
            cross_agy = AgyConfig(
                mode="agy",
                executable="fake-agy",
                model="claude-sonnet-5-5-medium",
                workspace_dir=str(ws),
                data_dir=str(data_dir),
                max_warm_processes=2,
                turn_timeout_seconds=5.0,
            )
            await runner.update_config(cross_agy, cfg.rate_limit)

            await app.handle_message(_make_msg(5, text="换个模型再确认一下暗号", at_targets=["999000111"]))
            await asyncio.sleep(0.1)
            # Spawn #3 tried `--conversation conv-p2` and failed; Spawn #4 retried cleanly WITHOUT `--conversation` and FULL prompt!
            self.assertEqual(len(spawned_cmds), 4)
            self.assertIn("--conversation", spawned_cmds[2])
            self.assertNotIn("--conversation", spawned_cmds[3])
            self.assertIn("会话活跃上下文记录", prompts_log[-1][1])
            self.assertIn("记住暗号 7788", prompts_log[-1][1])

            await app.shutdown()


if __name__ == "__main__":
    unittest.main()
