from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import time
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AgyConfig, RateLimitConfig
from ..models import IncomingMessage, JudgeDecision, RunnerResult

logger = logging.getLogger(__name__)

ProcessFactoryFn = Callable[..., Awaitable[asyncio.subprocess.Process]]

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "should_reply": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["should_reply", "reason"],
}

_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "quota",
    "resource_exhausted",
    "too many requests",
)
_FATAL_AUTH_TOS_MARKERS = (
    "403",
    "permission_denied",
    "terms of service",
    "disabled in this account",
    "authentication required",
    "not authenticated",
    "suspended",
)


def classify_error_text(text: str) -> tuple[bool, bool]:
    lowered = text.lower()
    is_rate_limit = any(marker in lowered for marker in _RATE_LIMIT_MARKERS)
    is_fatal = any(marker in lowered for marker in _FATAL_AUTH_TOS_MARKERS)
    return is_rate_limit, is_fatal


def resolve_agy_command(configured: str = "auto") -> list[str]:
    """Resolve official agy executable command cross-platform (Windows & Linux)."""
    target = configured.strip()
    if target and target.lower() != "auto":
        path_obj = Path(target)
        if os.name == "nt" and path_obj.suffix.lower() in (".cmd", ".bat"):
            return ["cmd.exe", "/c", str(path_obj)]
        return [target]

    candidates = ["agy"]
    if os.name == "nt":
        candidates = ["agy.exe", "agy", "agy.cmd"]

    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            if os.name == "nt" and Path(found).suffix.lower() in (".cmd", ".bat"):
                sibling_exe = Path(found).with_suffix(".exe")
                if sibling_exe.exists():
                    return [str(sibling_exe)]
                return ["cmd.exe", "/c", found]
            return [found]

    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            exe_path = Path(local_app) / "agy" / "bin" / "agy.exe"
            if exe_path.exists():
                return [str(exe_path)]

    raise FileNotFoundError(
        "未找到官方 Antigravity CLI (`agy`)。请先安装官方 CLI 并执行 `agy` 完成登录，"
        "或在 config.json 的 agy.executable 中填写 agy 可执行文件绝对路径。"
    )


def _format_judge_prompt(messages: list[IncomingMessage]) -> str:
    lines = [
        "请判断下面这段 QQ 群聊的最近消息中，作为群聊智能助手现在是否适合主动插话回复一次。",
        "只有当大家正在讨论需要帮忙回答的问题、明显停顿在等你接话、或有自然有趣的互动点时才返回 true；",
        "日常水群闲聊、话题与你无关、或他人对话正在进行中请返回 false，避免刷屏。",
        "",
        "最近群聊上下文：",
    ]
    for m in messages:
        role = "[我/机器人]" if m.is_self else f"{m.sender_name}({m.user_id})"
        quote_part = f" [引用: {m.reply_to_text[:60]}]" if m.reply_to_text else ""
        lines.append(f"- {role}{quote_part}: {m.text}")
    return "\n".join(lines)


class BaseAgentRunner(ABC):
    @abstractmethod
    async def ask(self, session_key: str, prompt: str) -> RunnerResult:
        """Send one prompt turn for a channel session and return its result."""

    @abstractmethod
    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        """Ask the model whether to chime in based on recent ambient messages."""

    @abstractmethod
    def is_circuit_open(self) -> tuple[bool, float]:
        """Return (is_open, remaining_seconds)."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Close underlying processes cleanly."""


@dataclass
class _SessionProcess:
    session_key: str
    proc: asyncio.subprocess.Process
    conversation_id: str = ""
    num_turns: int = 0
    stderr_lines: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    stderr_task: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class AgyRunner(BaseAgentRunner):
    """Manages official `agy` CLI `stream-json` subprocesses with concurrency & circuit breaking."""

    def __init__(
        self,
        agy_config: AgyConfig,
        rate_limit_config: RateLimitConfig,
        *,
        workspace_dir: Path,
        data_dir: Path,
        process_factory: ProcessFactoryFn | None = None,
    ) -> None:
        self._cfg = agy_config
        self._rate_cfg = rate_limit_config
        self._workspace_dir = workspace_dir
        self._data_dir = data_dir
        self._process_factory = process_factory

        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_file = self._data_dir / "sessions.json"
        self._judge_schema_file = self._data_dir / "judge_schema.json"
        self._judge_schema_file.write_text(
            json.dumps(_JUDGE_SCHEMA, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._saved_conversations: dict[str, str] = self._load_sessions()
        self._warm_pool: OrderedDict[str, _SessionProcess] = OrderedDict()
        self._pool_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, rate_limit_config.max_concurrent_requests))
        self._queued_or_running = 0
        self._circuit_until_mono = 0.0
        self._circuit_reason = ""
        self._cmd_prefix: list[str] | None = None

    def _get_cmd_prefix(self) -> list[str]:
        if self._cmd_prefix is None:
            self._cmd_prefix = resolve_agy_command(self._cfg.executable)
        return self._cmd_prefix

    def _load_sessions(self) -> dict[str, str]:
        if not self._cfg.resume_on_restart or not self._sessions_file.exists():
            return {}
        try:
            data = json.loads(self._sessions_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if v}
        except Exception as exc:
            logger.warning("读取会话记录 %s 失败: %s", self._sessions_file, exc)
        return {}

    def _save_sessions(self) -> None:
        if not self._cfg.resume_on_restart:
            return
        tmp = self._sessions_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._saved_conversations, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._sessions_file)

    def is_circuit_open(self) -> tuple[bool, float]:
        rem = self._circuit_until_mono - time.monotonic()
        if rem > 0:
            return True, rem
        return False, 0.0

    def _trip_circuit(self, *, is_fatal: bool, reason: str) -> None:
        duration = (
            self._cfg.fatal_error_pause_seconds if is_fatal else self._cfg.rate_limit_cooldown_seconds
        )
        self._circuit_until_mono = max(self._circuit_until_mono, time.monotonic() + max(5.0, duration))
        self._circuit_reason = reason
        logger.error(
            "Antigravity 熔断器触发 (%s): %s；暂停 %.0f 秒以免继续消耗限额或触发风控",
            "严重鉴权/条款限制" if is_fatal else "限流/配额",
            reason,
            duration,
        )

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    async def _spawn_process(self, args: list[str]) -> asyncio.subprocess.Process:
        if self._process_factory is not None:
            return await self._process_factory(
                *args,
                cwd=str(self._workspace_dir),
                env=self._build_env(),
            )
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._workspace_dir),
            env=self._build_env(),
            limit=10 * 1024 * 1024,  # 10 MiB to prevent asyncio 64KiB LimitOverrunError on large NDJSON
        )

    async def _drain_stderr(self, sp: _SessionProcess) -> None:
        if sp.proc.stderr is None:
            return
        try:
            while True:
                line = await sp.proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    sp.stderr_lines.append(text)
                    logger.debug("[agy stderr][%s] %s", sp.session_key, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _close_session_proc(self, sp: _SessionProcess) -> None:
        if sp.proc.stdin is not None and not sp.proc.stdin.is_closing():
            with contextlib.suppress(Exception):
                sp.proc.stdin.close()
        try:
            await asyncio.wait_for(sp.proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                sp.proc.terminate()
            try:
                await asyncio.wait_for(sp.proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    sp.proc.kill()
        if sp.stderr_task is not None:
            sp.stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sp.stderr_task

    async def _get_or_create_session(self, session_key: str, *, allow_resume: bool) -> _SessionProcess:
        async with self._pool_lock:
            existing = self._warm_pool.get(session_key)
            if existing is not None:
                if existing.proc.returncode is None and existing.num_turns < self._cfg.max_turns_per_session:
                    self._warm_pool.move_to_end(session_key)
                    return existing
                self._warm_pool.pop(session_key, None)
                await self._close_session_proc(existing)
                if existing.num_turns >= self._cfg.max_turns_per_session:
                    self._saved_conversations.pop(session_key, None)
                    self._save_sessions()

            while len(self._warm_pool) >= self._cfg.max_warm_processes:
                _, evicted = self._warm_pool.popitem(last=False)
                await self._close_session_proc(evicted)

            cmd = [
                *self._get_cmd_prefix(),
                "--input-format",
                "stream-json",
                "--output-format",
                "stream-json",
            ]
            if self._cfg.model:
                cmd.extend(["--model", self._cfg.model])
            if self._cfg.effort:
                cmd.extend(["--effort", self._cfg.effort])
            if self._cfg.agent:
                cmd.extend(["--agent", self._cfg.agent])
            if self._cfg.sandbox:
                cmd.append("--sandbox")

            resume_id = self._saved_conversations.get(session_key, "") if allow_resume else ""
            if resume_id:
                cmd.extend(["--conversation", resume_id])

            proc = await self._spawn_process(cmd)
            sp = _SessionProcess(session_key=session_key, proc=proc, conversation_id=resume_id)
            sp.stderr_task = asyncio.create_task(self._drain_stderr(sp), name=f"agy-stderr-{session_key}")
            self._warm_pool[session_key] = sp
            return sp

    async def _discard_session(self, session_key: str, *, clear_saved: bool) -> None:
        async with self._pool_lock:
            sp = self._warm_pool.pop(session_key, None)
            if clear_saved and session_key in self._saved_conversations:
                self._saved_conversations.pop(session_key, None)
                self._save_sessions()
        if sp is not None:
            await self._close_session_proc(sp)

    async def _run_turn(self, sp: _SessionProcess, prompt: str) -> RunnerResult:
        async with sp.lock:
            if sp.proc.stdin is None or sp.proc.stdout is None:
                return RunnerResult(ok=False, error="agy 子进程管道不可用")
            if sp.proc.returncode is not None:
                stderr_tail = " | ".join(sp.stderr_lines)
                return RunnerResult(ok=False, error=f"agy 进程已提前退出({sp.proc.returncode}): {stderr_tail}")

            payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False)
            sp.proc.stdin.write((payload + "\n").encode("utf-8"))
            await sp.proc.stdin.drain()

            deadline = time.monotonic() + self._cfg.turn_timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"等待 agy 响应超时 ({self._cfg.turn_timeout_seconds}s)")

                raw_line = await asyncio.wait_for(sp.proc.stdout.readline(), timeout=remaining)
                if not raw_line:
                    stderr_tail = " | ".join(sp.stderr_lines)
                    is_rl, is_fatal = classify_error_text(stderr_tail)
                    return RunnerResult(
                        ok=False,
                        error=f"agy 流意外结束: {stderr_tail or 'EOF'}",
                        is_rate_limited=is_rl,
                        is_fatal_auth_or_tos=is_fatal,
                    )

                line_str = raw_line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug("[agy stdout non-json] %s", line_str)
                    continue

                ev_type = event.get("event")
                if ev_type == "init":
                    cid = str(event.get("conversation_id") or "")
                    if cid:
                        sp.conversation_id = cid
                        self._saved_conversations[sp.session_key] = cid
                        self._save_sessions()
                elif ev_type == "step_update":
                    step = event.get("step_update") or {}
                    if step.get("step_type") == "tool" and step.get("state") == "DONE":
                        logger.info("[%s] agy 调用工具: %s", sp.session_key, step.get("tool_name"))
                elif ev_type == "result":
                    res = event.get("result") or {}
                    status = str(res.get("status") or "")
                    cid = str(res.get("conversation_id") or sp.conversation_id or "")
                    raw_num_turns = res.get("num_turns")
                    if status == "SUCCESS":
                        if cid:
                            sp.conversation_id = cid
                            self._saved_conversations[sp.session_key] = cid
                            self._save_sessions()
                        num_turns = (
                            int(raw_num_turns)
                            if raw_num_turns is not None
                            else (sp.num_turns + 1)
                        )
                        sp.num_turns = num_turns
                        return RunnerResult(
                            ok=True,
                            response=str(res.get("response") or "").strip(),
                            conversation_id=cid,
                            status=status,
                            num_turns=num_turns,
                        )
                    num_turns = int(raw_num_turns) if raw_num_turns is not None else sp.num_turns
                    sp.num_turns = num_turns
                    err_text = str(res.get("error") or status or "unknown error")
                    is_rl, is_fatal = classify_error_text(err_text)
                    return RunnerResult(
                        ok=False,
                        conversation_id=cid,
                        error=err_text,
                        status=status,
                        num_turns=num_turns,
                        is_rate_limited=is_rl,
                        is_fatal_auth_or_tos=is_fatal,
                    )

    async def ask(self, session_key: str, prompt: str) -> RunnerResult:
        open_circuit, rem = self.is_circuit_open()
        if open_circuit:
            return RunnerResult(ok=False, error=f"熔断冷却中 (剩余 {rem:.0f}s): {self._circuit_reason}")

        if self._queued_or_running >= self._rate_cfg.max_queue_size:
            return RunnerResult(ok=False, error="本地并发队列已满，跳过本次请求以防积压")

        self._queued_or_running += 1
        try:
            async with self._semaphore:
                open_circuit, rem = self.is_circuit_open()
                if open_circuit:
                    return RunnerResult(ok=False, error=f"熔断冷却中 (剩余 {rem:.0f}s): {self._circuit_reason}")

                resumed_tried = False
                for attempt in range(2):
                    allow_resume = attempt == 0
                    try:
                        sp = await self._get_or_create_session(session_key, allow_resume=allow_resume)
                    except FileNotFoundError as exc:
                        return RunnerResult(ok=False, error=str(exc))
                    if attempt == 0 and sp.conversation_id and sp.num_turns == 0:
                        resumed_tried = True
                    try:
                        result = await self._run_turn(sp, prompt)
                    except TimeoutError as exc:
                        await self._discard_session(session_key, clear_saved=False)
                        return RunnerResult(ok=False, error=str(exc))

                    if result.ok:
                        return result

                    if result.is_fatal_auth_or_tos or result.is_rate_limited:
                        self._trip_circuit(
                            is_fatal=result.is_fatal_auth_or_tos,
                            reason=result.error,
                        )
                        await self._discard_session(session_key, clear_saved=False)
                        return result

                    # If resuming a stale conversation failed before any turn succeeded, retry once cleanly.
                    if attempt == 0 and resumed_tried and sp.num_turns == 0:
                        logger.warning("[%s] 恢复历史会话失败，尝试新建干净会话重试一次: %s", session_key, result.error)
                        await self._discard_session(session_key, clear_saved=True)
                        continue

                    await self._discard_session(session_key, clear_saved=False)
                    return result

                return RunnerResult(ok=False, error="agy 调用失败")
        finally:
            self._queued_or_running -= 1

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        open_circuit, _ = self.is_circuit_open()
        if open_circuit or not messages:
            return JudgeDecision(should_reply=False, reason="circuit_open_or_empty")

        if self._queued_or_running >= self._rate_cfg.max_queue_size:
            return JudgeDecision(should_reply=False, reason="queue_full")

        self._queued_or_running += 1
        try:
            async with self._semaphore:
                open_circuit, _ = self.is_circuit_open()
                if open_circuit:
                    return JudgeDecision(should_reply=False, reason="circuit_open")

                prompt = _format_judge_prompt(messages)
                try:
                    cmd_prefix = self._get_cmd_prefix()
                    cmd = [
                        *cmd_prefix,
                        "--input-format",
                        "stream-json",
                        "--output-format",
                        "stream-json",
                        "--json-schema",
                        str(self._judge_schema_file),
                    ]
                    judge_model = self._cfg.judge_model or self._cfg.model
                    if judge_model:
                        cmd.extend(["--model", judge_model])
                    if self._cfg.sandbox:
                        cmd.append("--sandbox")

                    stdin_payload = (
                        json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
                    ).encode("utf-8")

                    proc = await self._spawn_process(cmd)
                except FileNotFoundError as exc:
                    return JudgeDecision(should_reply=False, reason=f"agy_not_found:{exc}")
                try:
                    stdout_b, stderr_b = await asyncio.wait_for(
                        proc.communicate(input=stdin_payload),
                        timeout=self._cfg.judge_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
                    return JudgeDecision(should_reply=False, reason="judge_timeout")

                stdout_s = stdout_b.decode("utf-8", errors="replace").strip()
                stderr_s = stderr_b.decode("utf-8", errors="replace").strip()

                envelope: dict[str, Any] = {}
                for raw_line in stdout_s.splitlines():
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        parsed = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        if parsed.get("event") == "result" and isinstance(parsed.get("result"), dict):
                            envelope = parsed["result"]
                        elif "status" in parsed and "event" not in parsed:
                            envelope = parsed

                if envelope.get("status") != "SUCCESS":
                    err_text = str(envelope.get("error") or stderr_s or f"exit={proc.returncode}")
                    is_rl, is_fatal = classify_error_text(err_text)
                    if is_rl or is_fatal:
                        self._trip_circuit(is_fatal=is_fatal, reason=err_text)
                    return JudgeDecision(should_reply=False, reason=f"judge_error:{err_text[:120]}")

                structured = envelope.get("structured_output")
                if not isinstance(structured, dict):
                    try:
                        structured = json.loads(str(envelope.get("response") or "{}"))
                    except json.JSONDecodeError:
                        structured = {}
                return JudgeDecision(
                    should_reply=bool(structured.get("should_reply", False)),
                    reason=str(structured.get("reason") or ""),
                )
        finally:
            self._queued_or_running -= 1

    async def shutdown(self) -> None:
        async with self._pool_lock:
            items = list(self._warm_pool.values())
            self._warm_pool.clear()
        for sp in items:
            await self._close_session_proc(sp)


class MockAgentRunner(BaseAgentRunner):
    """Offline dry-run runner for testing NapCat WS, triggers, and debouncing safely."""

    def __init__(self, *, reply_prefix: str = "[Dry-Run] ") -> None:
        self._prefix = reply_prefix
        self.calls: list[tuple[str, str]] = []
        self.judge_calls: list[tuple[str, int]] = []
        self.judge_result: bool = False

    async def ask(self, session_key: str, prompt: str) -> RunnerResult:
        self.calls.append((session_key, prompt))
        last_line = [ln for ln in prompt.splitlines() if ln.strip()][-1]
        return RunnerResult(
            ok=True,
            response=f"{self._prefix}已收到汇总上下文，最后一条为: {last_line[:120]}",
            conversation_id=f"mock-{session_key}",
            status="SUCCESS",
            num_turns=len(self.calls),
        )

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        self.judge_calls.append((session_key, len(messages)))
        return JudgeDecision(should_reply=self.judge_result, reason="mock_judge")

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0

    async def shutdown(self) -> None:
        return None


def build_runner(
    config: AgyConfig,
    rate_limit: RateLimitConfig,
    *,
    workspace_dir: Path,
    data_dir: Path,
) -> BaseAgentRunner:
    if config.mode.lower() == "mock":
        return MockAgentRunner()
    return AgyRunner(
        config,
        rate_limit,
        workspace_dir=workspace_dir,
        data_dir=data_dir,
    )
