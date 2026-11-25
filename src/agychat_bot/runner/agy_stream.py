from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import AgyConfig, ModelProfileConfig, RateLimitConfig, strip_model_effort_suffix
from ..models import IncomingMessage, JudgeDecision, RunnerResult
from ..proxy_guard import is_auth_required_error, is_geo_blocked_error, is_proxy_or_geo_error

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


def _flatten_step_text(val: Any) -> str:
    """Extract readable text from an agy step_update error/output/result field."""
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        for k in ("error", "error_message", "message", "reason", "detail", "text", "output", "stderr", "content", "result"):
            sub = _flatten_step_text(val.get(k))
            if sub:
                return sub
        return ""
    if isinstance(val, list):
        parts = [_flatten_step_text(x) for x in val[:6]]
        return " | ".join(p for p in parts if p)
    return str(val).strip()


def _extract_tool_error_detail(step: dict[str, Any], *, include_output: bool = False) -> str:
    """Extract the most informative error reason from a failed tool/MCP step_update."""
    keys: tuple[str, ...] = ("error", "error_message", "tool_error")
    if include_output:
        keys = (
            *keys,
            "reason",
            "message",
            "detail",
            "tool_output",
            "tool_result",
            "output",
            "result",
            "response",
            "content",
        )
    for key in keys:
        txt = _flatten_step_text(step.get(key))
        if txt:
            return re.sub(r"\s+", " ", txt).strip()
    return ""


def _extract_tool_log_detail(step: dict[str, Any]) -> tuple[bool, str, str]:
    """Return (is_mcp, tool_name, detail_suffix) from an agy stream-json tool step_update."""
    tool_name = str(step.get("tool_name") or step.get("name") or "unknown").strip()
    low_name = tool_name.lower()
    is_mcp = (
        low_name in ("call_mcp_tool", "list_resources", "read_resource")
        or low_name.startswith("mcp_")
        or bool(step.get("mcp_server") or step.get("server_name"))
    )

    raw_args: Any = (
        step.get("tool_input")
        or step.get("tool_args")
        or step.get("arguments")
        or step.get("args")
        or step.get("input")
        or step.get("params")
    )
    if raw_args is None and isinstance(step.get("tool_call"), dict):
        tc = step["tool_call"]
        if not tool_name or tool_name == "unknown":
            tool_name = str(tc.get("name") or tc.get("tool_name") or tool_name).strip()
        raw_args = tc.get("arguments") or tc.get("args") or tc.get("input")
    if isinstance(raw_args, str):
        try:
            raw_args = json.loads(raw_args)
        except Exception:
            raw_args = {}
    args = raw_args if isinstance(raw_args, dict) else {}

    details: list[str] = []
    srv = str(step.get("mcp_server") or step.get("server_name") or args.get("ServerName") or args.get("server_name") or "").strip()
    mcp_tool = str(args.get("ToolName") or args.get("tool_name") or "").strip()
    if not srv and low_name.startswith("mcp_") and low_name.count("_") >= 2:
        # e.g. mcp_image_generate -> server=image, tool=generate
        parts = tool_name.split("_", 2)
        if len(parts) == 3 and parts[1] and parts[2]:
            srv = parts[1]
            if not mcp_tool:
                mcp_tool = parts[2]
    if srv:
        is_mcp = True
        details.append(f"server={srv}")
    if mcp_tool:
        details.append(f"tool={mcp_tool}")

    nested_args = args.get("Arguments") or args.get("arguments")
    if isinstance(nested_args, str):
        try:
            nested_args = json.loads(nested_args)
        except Exception:
            nested_args = {}
    effective_args = nested_args if isinstance(nested_args, dict) else args

    for k in ("CommandLine", "command", "cmd", "model", "Model", "Path", "path", "file_path", "directory", "dir", "url", "query", "prompt"):
        v = effective_args.get(k)
        if v:
            val_s = re.sub(r"\s+", " ", str(v)).strip()
            if len(val_s) > 60:
                val_s = val_s[:57] + "..."
            details.append(f"{k.lower()}={val_s}")
            if len(details) >= 4:
                break

    if not details:
        summary = str(step.get("summary") or step.get("title") or step.get("description") or "").strip()
        if summary:
            details.append(re.sub(r"\s+", " ", summary)[:80])

    return is_mcp, tool_name, (", ".join(details))


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


def _estimate_tokens_fallback(text: str) -> int:
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x30FF
            or 0xFF00 <= cp <= 0xFFEF
            or 0xAC00 <= cp <= 0xD7AF
        ):
            cjk += 1
        else:
            other += 1
    return max(1, int(round(cjk * 0.8 + other / 3.8)))


def _extract_detailed_usage_stats(event: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    """Extract normalized token & timing usage fields from stream-json result/event payloads."""
    candidates: list[dict[str, Any]] = []
    for obj in (res, event):
        if not isinstance(obj, dict):
            continue
        for key in ("usage", "usage_metadata", "usageMetadata", "token_usage", "stats", "metrics"):
            val = obj.get(key)
            if isinstance(val, dict):
                candidates.append(val)
    if isinstance(res, dict):
        candidates.append(res)
    if isinstance(event, dict):
        candidates.append(event)

    def _pick_int_with_key(keys: tuple[str, ...]) -> tuple[int, str]:
        for d in candidates:
            for k in keys:
                v = d.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    return int(v), k
        return 0, ""

    def _pick_float_seconds(sec_keys: tuple[str, ...], ms_keys: tuple[str, ...]) -> float:
        for d in candidates:
            for k in sec_keys:
                v = d.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    return float(v)
            for k in ms_keys:
                v = d.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                    return float(v) / 1000.0
        return 0.0

    def _pick_str(keys: tuple[str, ...]) -> str:
        for d in candidates:
            for k in keys:
                v = d.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return ""

    raw_in, in_key = _pick_int_with_key(
        ("input_tokens", "inputTokens", "uncached_input_tokens", "prompt_token_count", "promptTokenCount", "promptTokens", "total_input_tokens")
    )
    cache_tok, _ = _pick_int_with_key(
        (
            "cache_read_tokens",
            "cached_content_token_count",
            "cachedContentTokenCount",
            "cache_read_input_tokens",
            "cachedTokens",
            "cached_tokens",
        )
    )
    cache_create_tok, _ = _pick_int_with_key(
        ("cache_creation_tokens", "cache_creation_input_tokens", "cacheWriteTokens", "cache_write_tokens")
    )
    out_tok, _ = _pick_int_with_key(
        ("output_tokens", "candidates_token_count", "candidatesTokenCount", "outputTokens", "completion_tokens", "completionTokens")
    )
    think_tok, _ = _pick_int_with_key(
        ("thinking_tokens", "thoughts_token_count", "thoughtsTokenCount", "reasoning_tokens", "reasoningTokens")
    )
    explicit_total, _ = _pick_int_with_key(("total_tokens", "total_token_count", "totalTokenCount", "totalTokens"))

    # Gemini promptTokenCount includes cachedContentTokenCount, whereas Anthropic/agy input_tokens is uncached input.
    if in_key in ("prompt_token_count", "promptTokenCount", "total_input_tokens") and cache_tok > 0 and raw_in >= cache_tok:
        fresh_in = raw_in - cache_tok
        total_in = raw_in
    else:
        fresh_in = raw_in
        total_in = raw_in + cache_tok

    cache_hit_rate = round((cache_tok / total_in) * 100.0, 2) if (total_in > 0 and cache_tok > 0) else 0.0
    computed_total = total_in + cache_create_tok + out_tok
    total_tok = max(computed_total, explicit_total) if (computed_total > 0 or explicit_total > 0) else 0

    return {
        "input_tokens": fresh_in,
        "cache_read_tokens": cache_tok,
        "cache_creation_tokens": cache_create_tok,
        "output_tokens": out_tok,
        "thinking_tokens": think_tok,
        "total_tokens": total_tok,
        "cache_hit_rate": cache_hit_rate,
        "ttft_seconds": _pick_float_seconds(("ttft_seconds", "time_to_first_token_seconds", "ttft"), ("ttft_ms", "time_to_first_token_ms")),
        "thinking_seconds": _pick_float_seconds(
            ("thinking_seconds", "thought_seconds", "thinking_duration_seconds", "reasoning_seconds"),
            ("thinking_ms", "thought_ms", "thinking_duration_ms", "reasoning_duration_ms"),
        ),
        "duration_seconds": _pick_float_seconds(("duration_seconds", "elapsed_seconds", "latency_seconds"), ("duration_ms", "latency_ms")),
        "model": _pick_str(("model", "model_id", "modelId", "model_name", "modelName")),
    }


def _extract_usage_stats(event: dict[str, Any], res: dict[str, Any]) -> tuple[int, int, int]:
    """Extract (input_tokens, cache_read_tokens, output_tokens) from stream-json result if available."""
    d = _extract_detailed_usage_stats(event, res)
    return int(d["input_tokens"]), int(d["cache_read_tokens"]), int(d["output_tokens"])


class BaseAgentRunner(ABC):
    @abstractmethod
    async def ask(self, session_key: str, prompt: str) -> RunnerResult:
        """Send one prompt turn for a channel session and return its result."""

    def peek_warm_cursor(self, session_key: str) -> tuple[int, int, set[int]] | None:
        """Return (window_min_seq, last_sent_seq, self_reply_seqs) if an active warm session exists."""
        return None

    def notify_self_reply_seq(self, session_key: str, db_seq: int) -> None:
        """Record bot reply seq_id on the active warm session so next delta turn avoids duplicating it."""
        return None

    async def run_subagent_task(
        self,
        task_id: str,
        prompt: str,
        *,
        model_override: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RunnerResult:
        """Execute an isolated background subagent turn without blocking main chat sessions."""
        return await self.ask(f"subagent:{task_id}", prompt)

    @abstractmethod
    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        """Ask the model whether to chime in based on recent ambient messages."""

    @abstractmethod
    def is_circuit_open(self) -> tuple[bool, float]:
        """Return (is_open, remaining_seconds)."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Close underlying processes cleanly."""

    async def reset_sessions(self, session_key: str | None = None, *, clear_saved: bool = True) -> int:
        return 0

    def clear_circuit_breaker(self) -> None:
        return None

    def get_status(self) -> dict[str, Any]:
        is_open, rem = self.is_circuit_open()
        return {
            "mode": "unknown",
            "circuit_open": is_open,
            "circuit_remaining_seconds": round(rem, 1),
            "warm_sessions": [],
            "saved_sessions_count": 0,
            "queued_or_running": 0,
        }

    async def list_available_models(self) -> list[dict[str, str]]:
        return []

    async def list_available_agents(self) -> list[dict[str, str]]:
        return []

    async def update_config(
        self,
        agy_config: AgyConfig,
        rate_limit_config: RateLimitConfig,
        *,
        settings_file: Path | None = None,
    ) -> None:
        return None


@dataclass
class _EpochCursor:
    """Tracks the latest cloud conversation_id & DB cursor within an active compaction epoch.

    Preserved across same-epoch model / effort / config switches so the restarted `agy`
    process can pass `--conversation <id>` with the new `--model` / `--effort` and send
    only incremental delta messages instead of re-sending the entire history.
    """

    conversation_id: str
    window_min_seq: int
    last_sent_seq: int
    self_reply_seqs: set[int] = field(default_factory=set)


@dataclass
class _SessionProcess:
    session_key: str
    proc: asyncio.subprocess.Process
    conversation_id: str = ""
    num_turns: int = 0
    config_sig: str = ""
    model_name: str = ""
    window_min_seq: int = -1
    last_sent_seq: int = 0
    self_reply_seqs: set[int] = field(default_factory=set)
    resumed_from_same_epoch: bool = False
    cum_input_tokens: int = 0
    cum_output_tokens: int = 0
    cum_cache_read_tokens: int = 0
    cum_cache_creation_tokens: int = 0
    cum_thinking_tokens: int = 0
    cum_total_tokens: int = 0
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
        settings_file: Path | None = None,
        subagent_workspace_dir: Path | None = None,
        process_factory: ProcessFactoryFn | None = None,
    ) -> None:
        self._cfg = agy_config
        self._rate_cfg = rate_limit_config
        self._workspace_dir = workspace_dir
        self._data_dir = data_dir
        self._settings_file = settings_file
        self._subagent_workspace_dir = subagent_workspace_dir or (data_dir / "subagent_workspace")
        self._process_factory = process_factory

        self._workspace_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._subagent_workspace_dir.mkdir(parents=True, exist_ok=True)
        self._sessions_file = self._data_dir / "sessions.json"
        self._judge_schema_file = self._data_dir / "judge_schema.json"
        self._judge_schema_file.write_text(
            json.dumps(_JUDGE_SCHEMA, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._saved_conversations: dict[str, str] = self._load_sessions()
        self._warm_pool: OrderedDict[str, _SessionProcess] = OrderedDict()
        self._epoch_cursors: dict[str, _EpochCursor] = {}
        self._pool_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max(1, rate_limit_config.max_concurrent_requests))
        self._subagent_semaphore = asyncio.Semaphore(max(1, agy_config.subagent_max_concurrent))
        self._subagent_procs: dict[int, _SessionProcess] = {}
        self._subagent_running = 0
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
        tmp = self._sessions_file.with_name(f".{self._sessions_file.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(self._saved_conversations, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self._sessions_file)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

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
        proxy = self._cfg.proxy.strip()
        if proxy:
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["ALL_PROXY"] = proxy
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
            env["all_proxy"] = proxy
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
            env.setdefault("no_proxy", "localhost,127.0.0.1,::1")
        return env

    async def _spawn_process(self, args: list[str], *, cwd: Path | None = None) -> asyncio.subprocess.Process:
        run_cwd = str(cwd or self._workspace_dir)
        if self._process_factory is not None:
            return await self._process_factory(
                *args,
                cwd=run_cwd,
                env=self._build_env(),
            )
        return await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=run_cwd,
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
                    if sp.session_key.startswith("subagent:"):
                        low_t = text.lower()
                        if any(k in low_t for k in ("error", "warn", "fail", "timeout", "mcp", "exception")):
                            logger.warning("[子代理 stderr][%s] %s", sp.session_key, text[:300])
                        else:
                            logger.debug("[子代理 stderr][%s] %s", sp.session_key, text[:300])
                    else:
                        logger.debug("[agy stderr][%s] %s", sp.session_key, text)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _force_kill_process(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        pid = getattr(proc, "pid", None)
        if os.name == "nt" and isinstance(pid, int) and pid > 0 and self._process_factory is None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    subprocess.run,
                    ["taskkill", "/T", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(proc.wait(), timeout=2.0)

    async def _close_session_proc(self, sp: _SessionProcess) -> None:
        if sp.proc.stdin is not None and not sp.proc.stdin.is_closing():
            with contextlib.suppress(Exception):
                sp.proc.stdin.close()
        try:
            await asyncio.wait_for(sp.proc.wait(), timeout=3.0)
        except BaseException:
            with contextlib.suppress(ProcessLookupError, OSError):
                sp.proc.terminate()
            try:
                await asyncio.wait_for(sp.proc.wait(), timeout=2.0)
            except BaseException:
                await self._force_kill_process(sp.proc)
        if sp.stderr_task is not None:
            sp.stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await sp.stderr_task

    def _resolve_model_settings_file(
        self,
        effective_model: str,
        profile: ModelProfileConfig | None,
    ) -> Path | None:
        if profile is None and not self._cfg.allow_command_execution:
            return self._settings_file

        eff_profile = profile or ModelProfileConfig(enable_command=True)

        base_data: dict[str, Any] = {}
        if self._settings_file is not None and self._settings_file.exists():
            try:
                loaded = json.loads(self._settings_file.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    base_data = loaded
            except Exception as exc:
                logger.warning("读取基础安全权限文件失败: %s", exc)

        perms = base_data.get("permissions")
        if not isinstance(perms, dict):
            perms = {}
            base_data["permissions"] = perms
        deny_list = [str(x) for x in (perms.get("deny") or []) if str(x).strip()]
        deny_set = set(deny_list)

        # Always keep mandatory workspace integrity redlines
        hard_denies = {
            "execute_url(*)",
            "write_file(GEMINI.md)",
            "write_file(AGENTS.md)",
            "write_file(.agents/)",
        }
        deny_set.update(hard_denies)

        allow_list = [str(x) for x in (perms.get("allow") or []) if str(x).strip()]
        allow_set = set(allow_list)

        cmd_allowed = bool(self._cfg.allow_command_execution or eff_profile.enable_command)
        if cmd_allowed:
            deny_set.discard("command(*)")
            allow_set.add("command(*)")
            deny_set.discard("read_file(*)")
            allow_set.add("read_file(*)")
        else:
            deny_set.add("command(*)")
            allow_set.discard("command(*)")

        if profile is not None:
            # Tool toggles mapped to agy --settings permissions.deny
            if profile.enable_web_search:
                deny_set.difference_update({"search_web", "web_search"})
            else:
                deny_set.update({"search_web", "web_search"})

            if profile.enable_url_read:
                deny_set.difference_update({"read_url(*)", "read_url_content"})
            else:
                deny_set.update({"read_url(*)", "read_url_content"})

            if profile.enable_vision:
                deny_set.discard("read_file(*)")
                allow_set.add("read_file(.media_cache/*)")
            else:
                deny_set.add("read_file(*)")
                allow_set.discard("read_file(.media_cache/*)")

            if profile.enable_file_tools:
                deny_set.difference_update({"write_file(*)", "edit_file(*)"})
            else:
                deny_set.update({"write_file(*)", "edit_file(*)"})

        perms["deny"] = sorted(deny_set)
        perms["allow"] = sorted(allow_set)

        out_dir = self._data_dir / "model_settings"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_slug = re.sub(r"[^a-zA-Z0-9_.\-]", "_", effective_model or "default")
        if profile is None:
            perm_tag = f"base_c{int(cmd_allowed)}"
        else:
            perm_tag = (
                f"w{int(profile.enable_web_search)}"
                f"u{int(profile.enable_url_read)}"
                f"v{int(profile.enable_vision)}"
                f"f{int(profile.enable_file_tools)}"
                f"c{int(cmd_allowed)}"
            )
        out_path = out_dir / f"{safe_slug}_{perm_tag}.json"
        tmp = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(base_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, out_path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return out_path

    def _build_cli_args(
        self,
        effective_model: str,
        *,
        resume_id: str = "",
        json_schema: Path | None = None,
        profile_override: ModelProfileConfig | None = None,
    ) -> list[str]:
        cli_model, resolved_profile = self._cfg.resolve_model_target(effective_model)
        profile = profile_override if profile_override is not None else resolved_profile
        if profile is not None and profile.model.strip():
            cli_model = profile.model.strip()
        cmd = [
            *self._get_cmd_prefix(),
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ]
        if json_schema is not None:
            cmd.extend(["--json-schema", str(json_schema)])
        if cli_model:
            cmd.extend(["--model", cli_model])

        eff = (profile.effort if (profile and profile.effort) else self._cfg.effort).strip()
        if eff:
            cmd.extend(["--effort", eff])
        if self._cfg.agent:
            cmd.extend(["--agent", self._cfg.agent])
        if profile and profile.execution_mode:
            cmd.extend(["--mode", profile.execution_mode])
        use_sandbox = (
            profile.sandbox
            if (profile is not None and profile.sandbox is not None)
            else self._cfg.sandbox
        )
        if use_sandbox:
            cmd.append("--sandbox")
        if profile and profile.disable_slash_commands:
            cmd.append("--disable-slash-commands")

        settings_path = self._resolve_model_settings_file(cli_model or effective_model, profile)
        if settings_path is not None:
            cmd.extend(["--settings", str(settings_path)])
        if resume_id:
            cmd.extend(["--conversation", resume_id])
        return cmd

    def _session_config_sig(
        self,
        effective_model: str,
        profile: ModelProfileConfig | None,
    ) -> str:
        cmd_flag = int(bool(self._cfg.allow_command_execution or (profile is not None and profile.enable_command)))
        if profile is None:
            return f"{effective_model}|default|c{cmd_flag}"
        return (
            f"{effective_model}|{profile.model}|{profile.name}|{profile.effort}|{profile.execution_mode}|{profile.sandbox}|"
            f"{int(profile.disable_slash_commands)}|{int(profile.enable_web_search)}|"
            f"{int(profile.enable_url_read)}|{int(profile.enable_vision)}|"
            f"{int(profile.enable_audio)}|{int(profile.enable_file_tools)}|c{cmd_flag}"
        )

    def peek_warm_cursor(self, session_key: str) -> tuple[int, int, set[int]] | None:
        sp = self._warm_pool.get(session_key)
        if sp is not None and sp.proc.returncode is None:
            if (sp.num_turns > 0 or sp.resumed_from_same_epoch) and sp.last_sent_seq > 0 and sp.window_min_seq >= 0:
                return (sp.window_min_seq, sp.last_sent_seq, set(sp.self_reply_seqs))
        cur = self._epoch_cursors.get(session_key)
        if cur is not None and cur.conversation_id and cur.last_sent_seq > 0 and cur.window_min_seq >= 0:
            return (cur.window_min_seq, cur.last_sent_seq, set(cur.self_reply_seqs))
        return None

    def notify_self_reply_seq(self, session_key: str, db_seq: int) -> None:
        if db_seq <= 0:
            return
        sp = self._warm_pool.get(session_key)
        if sp is not None and sp.proc.returncode is None and sp.last_sent_seq > 0:
            if db_seq == sp.last_sent_seq + 1:
                sp.last_sent_seq = db_seq
            else:
                sp.self_reply_seqs.add(db_seq)
        cur = self._epoch_cursors.get(session_key)
        if cur is not None and cur.last_sent_seq > 0:
            if db_seq == cur.last_sent_seq + 1:
                cur.last_sent_seq = db_seq
            else:
                cur.self_reply_seqs.add(db_seq)

    def _snapshot_epoch_cursor(self, sp: _SessionProcess) -> None:
        if sp.conversation_id and sp.window_min_seq >= 0 and sp.last_sent_seq > 0:
            self._epoch_cursors[sp.session_key] = _EpochCursor(
                conversation_id=sp.conversation_id,
                window_min_seq=sp.window_min_seq,
                last_sent_seq=sp.last_sent_seq,
                self_reply_seqs=set(sp.self_reply_seqs),
            )

    async def _get_or_create_session(
        self,
        session_key: str,
        *,
        allow_resume: bool,
        model_override: str | None = None,
        profile_override: ModelProfileConfig | None = None,
        window_min_seq: int | None = None,
    ) -> _SessionProcess:
        effective_model = self._cfg.model if not model_override else model_override
        profile = profile_override if profile_override is not None else self._cfg.resolve_model_profile(effective_model)
        sig = self._session_config_sig(effective_model, profile)
        base_max_turns = (
            profile.max_turns
            if (profile is not None and profile.max_turns > 0)
            else self._cfg.max_turns_per_session
        )
        max_turns = max(base_max_turns, 500) if window_min_seq is not None else base_max_turns
        to_close: list[_SessionProcess] = []
        inherited_cursor: _EpochCursor | None = None
        async with self._pool_lock:
            existing = self._warm_pool.get(session_key)
            if existing is not None:
                eff_max_turns = (
                    max(base_max_turns, 500)
                    if (window_min_seq is not None or existing.window_min_seq >= 0)
                    else base_max_turns
                )
                epoch_matches = (
                    (existing.window_min_seq == window_min_seq and (existing.num_turns > 0 or existing.resumed_from_same_epoch))
                    if window_min_seq is not None
                    else True
                )
                if (
                    existing.proc.returncode is None
                    and existing.num_turns < eff_max_turns
                    and existing.config_sig == sig
                    and epoch_matches
                ):
                    self._warm_pool.move_to_end(session_key)
                    return existing

                # Same compaction epoch & only config/model/effort changed -> preserve cloud conversation!
                can_inherit_same_epoch = bool(
                    allow_resume
                    and window_min_seq is not None
                    and epoch_matches
                    and existing.num_turns < eff_max_turns
                    and existing.conversation_id
                    and existing.last_sent_seq > 0
                )
                if can_inherit_same_epoch:
                    self._snapshot_epoch_cursor(existing)
                    inherited_cursor = self._epoch_cursors.get(session_key)
                else:
                    self._epoch_cursors.pop(session_key, None)

                self._warm_pool.pop(session_key, None)
                to_close.append(existing)
                if (window_min_seq is not None and not can_inherit_same_epoch) or existing.num_turns >= eff_max_turns:
                    self._saved_conversations.pop(session_key, None)
                    self._save_sessions()
            elif allow_resume and window_min_seq is not None:
                cur = self._epoch_cursors.get(session_key)
                if cur is not None:
                    if cur.window_min_seq == window_min_seq and cur.conversation_id and cur.last_sent_seq > 0:
                        inherited_cursor = cur
                    else:
                        self._epoch_cursors.pop(session_key, None)
            elif not allow_resume:
                self._epoch_cursors.pop(session_key, None)

            while len(self._warm_pool) >= self._cfg.max_warm_processes:
                idle_key = next((k for k, v in self._warm_pool.items() if not v.lock.locked()), None)
                if idle_key is None:
                    break
                evicted = self._warm_pool.pop(idle_key)
                self._snapshot_epoch_cursor(evicted)
                to_close.append(evicted)

            # When SQLite active-window context mode is active (`window_min_seq is not None`),
            # reuse `--conversation <id>` ONLY within the SAME compaction epoch (e.g. model/effort
            # hot-switch or LRU eviction); once compaction advances `window_min_seq`, start fresh.
            resumed_from_same_epoch = False
            if window_min_seq is not None:
                if inherited_cursor is not None:
                    resume_id = inherited_cursor.conversation_id
                    resumed_from_same_epoch = True
                    logger.info(
                        "[%s] 同压缩周期复用云端会话 (conversation=%s, model=%s) 以保留上下文与前缀缓存",
                        session_key,
                        resume_id[:12],
                        effective_model or "default",
                    )
                else:
                    resume_id = ""
            else:
                resume_id = self._saved_conversations.get(session_key, "") if allow_resume else ""
                if resume_id:
                    logger.info("[%s] 已加载持久化历史会话 (conversation=%s)", session_key, resume_id[:12])
            cmd = self._build_cli_args(
                effective_model,
                resume_id=resume_id,
                profile_override=profile,
            )

            cli_model_name, _ = self._cfg.resolve_model_target(effective_model)
            resolved_model_label = (profile.model.strip() if (profile is not None and profile.model.strip()) else (cli_model_name or effective_model)).strip()
            proc = await self._spawn_process(cmd)
            sp = _SessionProcess(
                session_key=session_key,
                proc=proc,
                conversation_id=resume_id,
                config_sig=sig,
                model_name=resolved_model_label or effective_model or "default",
                window_min_seq=window_min_seq if window_min_seq is not None else -1,
                last_sent_seq=inherited_cursor.last_sent_seq if resumed_from_same_epoch and inherited_cursor is not None else 0,
                self_reply_seqs=set(inherited_cursor.self_reply_seqs) if resumed_from_same_epoch and inherited_cursor is not None else set(),
                resumed_from_same_epoch=resumed_from_same_epoch,
            )
            sp.stderr_task = asyncio.create_task(self._drain_stderr(sp), name=f"agy-stderr-{session_key}")
            self._warm_pool[session_key] = sp

        for old_sp in to_close:
            await self._close_session_proc(old_sp)
        return sp

    async def _discard_session(
        self,
        session_key: str,
        *,
        clear_saved: bool,
        expected_sp: _SessionProcess | None = None,
    ) -> None:
        sp: _SessionProcess | None = None
        async with self._pool_lock:
            cur = self._warm_pool.get(session_key)
            if cur is not None and (expected_sp is None or cur is expected_sp):
                sp = self._warm_pool.pop(session_key, None)
            elif expected_sp is not None:
                sp = expected_sp
            if clear_saved:
                self._epoch_cursors.pop(session_key, None)
                if session_key in self._saved_conversations:
                    self._saved_conversations.pop(session_key, None)
                    self._save_sessions()
        if sp is not None:
            await self._close_session_proc(sp)

    async def _run_turn(
        self,
        sp: _SessionProcess,
        prompt: str,
        *,
        timeout_override: float | None = None,
    ) -> RunnerResult:
        async with sp.lock:
            if sp.proc.stdin is None or sp.proc.stdout is None:
                return RunnerResult(ok=False, error="agy 子进程管道不可用")
            if sp.proc.returncode is not None:
                stderr_tail = " | ".join(sp.stderr_lines)
                return RunnerResult(ok=False, error=f"agy 进程已提前退出({sp.proc.returncode}): {stderr_tail}")

            payload = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False)
            sp.proc.stdin.write((payload + "\n").encode("utf-8"))
            await sp.proc.stdin.drain()

            eff_timeout = float(timeout_override) if (timeout_override is not None and timeout_override > 0) else self._cfg.turn_timeout_seconds
            turn_t0 = time.monotonic()
            deadline = turn_t0 + eff_timeout
            tool_started_at: dict[str, float] = {}
            tool_logged_done: set[str] = set()
            tool_intervals: list[tuple[float, float]] = []
            is_subagent = sp.session_key.startswith("subagent:")
            role_tag = "[子代理]" if is_subagent else "agy"

            think_start_t: float | None = None
            think_accum = 0.0
            first_output_t: float | None = None
            last_output_t: float | None = None
            step_usage_fallback: dict[str, Any] = {}

            def _close_thinking_span(now_mono: float) -> None:
                nonlocal think_start_t, think_accum
                if think_start_t is not None:
                    think_accum += max(0.0, now_mono - think_start_t)
                    think_start_t = None

            def _merged_tool_seconds(end_mono: float) -> float:
                spans: list[tuple[float, float]] = list(tool_intervals)
                for sid, t0 in tool_started_at.items():
                    if sid not in tool_logged_done and end_mono > t0:
                        spans.append((t0, end_mono))
                if not spans:
                    return 0.0
                spans.sort()
                merged_start, merged_end = spans[0]
                total = 0.0
                for s, e in spans[1:]:
                    if s <= merged_end:
                        merged_end = max(merged_end, e)
                    else:
                        total += max(0.0, merged_end - merged_start)
                        merged_start, merged_end = s, e
                total += max(0.0, merged_end - merged_start)
                return total

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"等待 agy 响应超时 ({eff_timeout}s)")

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
                if not isinstance(event, dict):
                    continue

                now_mono = time.monotonic()
                ev_type = event.get("event")
                if ev_type == "init":
                    cid = str(event.get("conversation_id") or "")
                    init_obj = event.get("init") if isinstance(event.get("init"), dict) else {}
                    init_model = str(init_obj.get("model") or event.get("model") or "").strip()
                    if init_model and (not sp.model_name or sp.model_name == "default"):
                        sp.model_name = init_model
                    if cid:
                        sp.conversation_id = cid
                        if not is_subagent:
                            self._saved_conversations[sp.session_key] = cid
                            self._save_sessions()
                elif ev_type == "step_update":
                    raw_step = event.get("step_update")
                    step = raw_step if isinstance(raw_step, dict) else {}
                    st_type = str(step.get("step_type") or "").strip().lower()
                    raw_state = str(step.get("state") or step.get("status") or "").strip().upper()
                    state = re.sub(r"^(?:STEP_STATE_|TOOL_STATE_|STATUS_)", "", raw_state)

                    # Capture any step-level usage metadata as fallback
                    su_usage = _extract_detailed_usage_stats(event, step)
                    if su_usage["total_tokens"] > 0 or su_usage["input_tokens"] > 0 or su_usage["output_tokens"] > 0:
                        step_usage_fallback = su_usage

                    is_tool_step = (
                        st_type in ("tool", "tool_call", "tool_use", "mcp", "mcp_tool", "command")
                        or bool(step.get("tool_name") or step.get("mcp_server") or step.get("server_name"))
                    )
                    if is_tool_step:
                        _close_thinking_span(now_mono)
                        step_idx = step.get("step_index")
                        if step.get("step_id") is not None:
                            step_id = str(step.get("step_id"))
                        elif step.get("id") is not None:
                            step_id = str(step.get("id"))
                        elif step_idx is not None:
                            step_id = f"idx:{step_idx}"
                        else:
                            step_id = f"tool:{step.get('tool_name') or step.get('name') or 'unknown'}"

                        is_mcp, tool_name, detail = _extract_tool_log_detail(step)
                        sub_tag = "[子代理]" if is_subagent else ""
                        kind_tag = "[MCP]" if is_mcp else "[工具]"
                        prefix = f"[{sp.session_key}] {sub_tag}{kind_tag} "
                        detail_str = f" ({detail})" if detail else ""

                        is_start_state = state in ("RUNNING", "IN_PROGRESS", "PENDING", "STARTED", "EXECUTING", "WAITING", "GENERATING", "")
                        is_err_state = state in ("ERROR", "FAILED", "DENIED", "REJECTED", "CANCELED", "CANCELLED", "BLOCKED", "WAITING_FOR_APPROVAL", "PERMISSION_DENIED")
                        is_done_state = state in ("DONE", "COMPLETED", "COMPLETE", "SUCCESS", "FINISHED")

                        if is_start_state and step_id not in tool_started_at and step_id not in tool_logged_done:
                            tool_started_at[step_id] = now_mono
                            logger.info("%s开始调用工具: %s%s", prefix, tool_name, detail_str)
                        elif (is_done_state or is_err_state) and step_id not in tool_logged_done:
                            tool_logged_done.add(step_id)
                            t_start = tool_started_at.get(step_id)
                            if t_start is not None and now_mono > t_start:
                                tool_intervals.append((t_start, now_mono))
                            dur_str = f", 耗时 {now_mono - t_start:.1f}s" if t_start is not None else ""
                            err_msg = _extract_tool_error_detail(step, include_output=is_err_state)
                            if is_err_state or err_msg:
                                logger.warning(
                                    "%s工具调用失败: %s%s%s -> %s",
                                    prefix,
                                    tool_name,
                                    detail_str,
                                    dur_str,
                                    err_msg[:360] or state,
                                )
                            else:
                                logger.info(
                                    "%s%s调用工具完成: %s%s%s",
                                    prefix,
                                    "" if (is_mcp or is_subagent) else f"{role_tag} ",
                                    tool_name,
                                    detail_str,
                                    dur_str,
                                )
                    else:
                        has_think_payload = bool(
                            step.get("thinking")
                            or step.get("thought")
                            or step.get("thinking_delta")
                            or step.get("thought_delta")
                            or step.get("reasoning")
                            or step.get("reasoning_delta")
                        )
                        is_think_type = any(k in st_type for k in ("think", "thought", "reason", "planner", "plan"))
                        has_text_payload = bool(
                            step.get("text_delta")
                            or step.get("content_delta")
                            or step.get("response_delta")
                            or step.get("delta")
                            or (
                                st_type in ("agent_response", "response", "assistant", "text", "output", "message", "answer")
                                and (step.get("text") or step.get("content") or step.get("response"))
                            )
                        )
                        if (is_think_type or has_think_payload) and not has_text_payload:
                            if think_start_t is None:
                                think_start_t = now_mono
                            if state in ("DONE", "COMPLETED", "COMPLETE", "SUCCESS", "FINISHED"):
                                _close_thinking_span(now_mono)
                        elif has_text_payload:
                            _close_thinking_span(now_mono)
                            if first_output_t is None:
                                first_output_t = now_mono
                            last_output_t = now_mono
                elif ev_type in ("thought", "thinking", "reasoning"):
                    if think_start_t is None:
                        think_start_t = now_mono
                elif ev_type in ("assistant", "delta", "text", "content_block_delta"):
                    _close_thinking_span(now_mono)
                    if first_output_t is None:
                        first_output_t = now_mono
                    last_output_t = now_mono
                elif ev_type == "result":
                    _close_thinking_span(now_mono)
                    raw_res = event.get("result")
                    res = raw_res if isinstance(raw_res, dict) else {}
                    status = str(res.get("status") or "")
                    cid = str(res.get("conversation_id") or sp.conversation_id or "")
                    raw_num_turns = res.get("num_turns")
                    if status == "SUCCESS":
                        if cid:
                            sp.conversation_id = cid
                            if not is_subagent:
                                self._saved_conversations[sp.session_key] = cid
                                self._save_sessions()
                        num_turns = (
                            int(raw_num_turns)
                            if raw_num_turns is not None
                            else (sp.num_turns + 1)
                        )
                        sp.num_turns = num_turns
                        resp_text = str(res.get("response") or "").strip()

                        u = _extract_detailed_usage_stats(event, res)
                        if u["total_tokens"] <= 0 and step_usage_fallback:
                            for k in (
                                "input_tokens",
                                "cache_read_tokens",
                                "cache_creation_tokens",
                                "output_tokens",
                                "thinking_tokens",
                                "total_tokens",
                                "cache_hit_rate",
                            ):
                                if not u[k] and step_usage_fallback.get(k):
                                    u[k] = step_usage_fallback[k]

                        raw_in = int(u["input_tokens"])
                        raw_cache = int(u["cache_read_tokens"])
                        raw_cache_create = int(u["cache_creation_tokens"])
                        raw_out = int(u["output_tokens"])
                        raw_think = int(u["thinking_tokens"])
                        raw_total = max(int(u["total_tokens"]), raw_in + raw_cache + raw_cache_create + raw_out)

                        # agy CLI reports cumulative conversation totals on warm multi-turn subprocesses.
                        # Convert cumulative counters into exact single-turn deltas whenever counters advance monotonically.
                        is_cumulative = (
                            sp.cum_total_tokens > 0
                            and raw_total > sp.cum_total_tokens
                            and raw_in >= sp.cum_input_tokens
                            and raw_out >= sp.cum_output_tokens
                            and raw_cache >= sp.cum_cache_read_tokens
                            and raw_cache_create >= sp.cum_cache_creation_tokens
                            and raw_think >= sp.cum_thinking_tokens
                        )
                        if is_cumulative:
                            in_tok = raw_in - sp.cum_input_tokens
                            cache_tok = raw_cache - sp.cum_cache_read_tokens
                            cache_create_tok = raw_cache_create - sp.cum_cache_creation_tokens
                            out_tok = raw_out - sp.cum_output_tokens
                            think_tok = raw_think - sp.cum_thinking_tokens
                            total_tok = max(raw_total - sp.cum_total_tokens, in_tok + cache_tok + cache_create_tok + out_tok)
                        else:
                            in_tok = raw_in
                            cache_tok = raw_cache
                            cache_create_tok = raw_cache_create
                            out_tok = raw_out
                            think_tok = raw_think
                            total_tok = raw_total

                        if raw_total > 0:
                            sp.cum_input_tokens = raw_in
                            sp.cum_cache_read_tokens = raw_cache
                            sp.cum_cache_creation_tokens = raw_cache_create
                            sp.cum_output_tokens = raw_out
                            sp.cum_thinking_tokens = raw_think
                            sp.cum_total_tokens = raw_total

                        if in_tok <= 0 and cache_tok <= 0 and prompt:
                            in_tok = _estimate_tokens_fallback(prompt)
                        if out_tok <= 0 and resp_text:
                            out_tok = _estimate_tokens_fallback(resp_text)

                        total_in = in_tok + cache_tok
                        cache_hit_rate = round((cache_tok / total_in) * 100.0, 2) if (total_in > 0 and cache_tok > 0) else 0.0
                        total_tok = max(total_tok, total_in + cache_create_tok + out_tok)

                        # Always use this turn's actual wall clock duration (agy result.duration_seconds is cumulative conversation age).
                        wall_dur = max(0.001, now_mono - turn_t0)
                        duration_sec = round(wall_dur, 3)
                        tool_sec = _merged_tool_seconds(now_mono)
                        llm_active_sec = max(0.05, wall_dur - tool_sec)

                        if u["ttft_seconds"] > 0:
                            ttft_sec = round(float(u["ttft_seconds"]), 3)
                        elif first_output_t is not None:
                            ttft_sec = round(max(0.0, first_output_t - turn_t0), 3)
                        else:
                            ttft_sec = round(wall_dur, 3) if resp_text else 0.0

                        if u["thinking_seconds"] > 0:
                            thinking_sec = round(float(u["thinking_seconds"]), 3)
                        elif think_accum > 0:
                            thinking_sec = round(think_accum, 3)
                        elif think_tok > 0:
                            # When CLI bundles thinking tokens into output_tokens without streaming thought deltas,
                            # estimate thinking time from its share of active LLM generation time (bounded by TTFT on direct turns).
                            think_ratio = min(1.0, think_tok / max(1, out_tok, think_tok))
                            est_think = llm_active_sec * think_ratio
                            if tool_sec <= 0.01 and first_output_t is not None:
                                est_think = min(est_think, ttft_sec)
                            thinking_sec = round(max(0.0, est_think), 3)
                        else:
                            thinking_sec = 0.0

                        if think_tok > 0:
                            # output_tokens includes thinking_tokens, so speed is total generated tokens / active LLM time.
                            tps = round(out_tok / llm_active_sec, 1) if out_tok > 0 else 0.0
                        elif first_output_t is not None and (now_mono - first_output_t) >= 0.2:
                            gen_window = max(0.2, (last_output_t or now_mono) - first_output_t)
                            if gen_window < 0.2:
                                gen_window = max(0.2, now_mono - first_output_t)
                            tps = round(out_tok / gen_window, 1) if out_tok > 0 else 0.0
                        else:
                            tps = round(out_tok / llm_active_sec, 1) if out_tok > 0 else 0.0

                        used_model = (u["model"] or sp.model_name or self._cfg.model or "default").strip()

                        if not is_subagent:
                            tool_cnt = len(tool_logged_done | set(tool_started_at.keys()))
                            tool_note = f", 工具调用 {tool_cnt} 次" if tool_cnt else ""
                            if cache_tok > 0 and in_tok > 0:
                                usage_note = f", 输入 {in_tok} tok (缓存命中 {cache_tok} tok / {cache_hit_rate:.1f}%), 输出 {out_tok} tok"
                            elif cache_tok > 0:
                                usage_note = f", 缓存命中 {cache_tok} tok, 输出 {out_tok} tok"
                            elif in_tok > 0:
                                usage_note = f", 输入 {in_tok} tok, 输出 {out_tok} tok"
                            else:
                                usage_note = ""
                            logger.info(
                                "[%s] Antigravity 推理完成 (耗时 %.1fs, 首字 %.2fs, 返回 %d 字%s%s)",
                                sp.session_key,
                                wall_dur,
                                ttft_sec,
                                len(resp_text),
                                tool_note,
                                usage_note,
                            )
                        return RunnerResult(
                            ok=True,
                            response=resp_text,
                            conversation_id=cid,
                            status=status,
                            num_turns=num_turns,
                            model=used_model,
                            input_tokens=in_tok,
                            output_tokens=out_tok,
                            cache_read_tokens=cache_tok,
                            cache_creation_tokens=cache_create_tok,
                            thinking_tokens=think_tok,
                            total_tokens=total_tok,
                            cache_hit_rate=cache_hit_rate,
                            ttft_seconds=ttft_sec,
                            thinking_seconds=thinking_sec,
                            duration_seconds=duration_sec,
                            tokens_per_second=tps,
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
                        model=sp.model_name or self._cfg.model or "default",
                        duration_seconds=round(max(0.001, now_mono - turn_t0), 3),
                    )

    async def _ask_with_model(
        self,
        session_key: str,
        prompt: str,
        *,
        model_override: str | None = None,
        profile_override: ModelProfileConfig | None = None,
        delta_prompt: str | None = None,
        delta_base_seq: int = 0,
        window_min_seq: int | None = None,
        latest_seq: int = 0,
    ) -> RunnerResult:
        resumed_tried = False
        for attempt in range(2):
            allow_resume = attempt == 0
            try:
                sp = await self._get_or_create_session(
                    session_key,
                    allow_resume=allow_resume,
                    model_override=model_override,
                    profile_override=profile_override,
                    window_min_seq=window_min_seq,
                )
            except FileNotFoundError as exc:
                return RunnerResult(ok=False, error=str(exc))
            if attempt == 0 and sp.conversation_id and sp.num_turns == 0:
                resumed_tried = True
            use_delta = bool(
                delta_prompt
                and window_min_seq is not None
                and (sp.num_turns > 0 or sp.resumed_from_same_epoch)
                and sp.window_min_seq == window_min_seq
                and sp.last_sent_seq > 0
                and sp.last_sent_seq == delta_base_seq
            )
            effective_prompt = (delta_prompt or prompt) if use_delta else prompt
            try:
                result = await self._run_turn(sp, effective_prompt)
            except TimeoutError as exc:
                await self._discard_session(session_key, clear_saved=False, expected_sp=sp)
                msg = str(exc) or f"等待 agy 响应超时 ({self._cfg.turn_timeout_seconds}s)"
                return RunnerResult(ok=False, error=msg)
            except asyncio.CancelledError:
                await self._discard_session(session_key, clear_saved=False, expected_sp=sp)
                raise
            except (OSError, ValueError) as exc:
                await self._discard_session(session_key, clear_saved=False, expected_sp=sp)
                return RunnerResult(ok=False, error=f"agy 通信异常: {exc}")

            if result.ok:
                sp.resumed_from_same_epoch = False
                if window_min_seq is not None:
                    sp.window_min_seq = window_min_seq
                    if latest_seq > 0:
                        sp.last_sent_seq = max(sp.last_sent_seq, latest_seq)
                        if sp.self_reply_seqs:
                            sp.self_reply_seqs = {s for s in sp.self_reply_seqs if s > sp.last_sent_seq}
                    self._snapshot_epoch_cursor(sp)
                return result

            if result.is_fatal_auth_or_tos or result.is_rate_limited:
                self._trip_circuit(
                    is_fatal=result.is_fatal_auth_or_tos,
                    reason=result.error,
                )
                await self._discard_session(session_key, clear_saved=False, expected_sp=sp)
                return result

            # If resuming a stale conversation failed before any turn succeeded (and NOT due to
            # auth/network/proxy/geo failure), retry once cleanly with a fresh conversation.
            if (
                attempt == 0
                and resumed_tried
                and sp.num_turns == 0
                and not is_auth_required_error(result.error)
                and not is_proxy_or_geo_error(result.error)
            ):
                logger.warning("[%s] 恢复历史会话失败，尝试新建干净会话重试一次: %s", session_key, result.error)
                await self._discard_session(session_key, clear_saved=True, expected_sp=sp)
                continue

            await self._discard_session(session_key, clear_saved=False, expected_sp=sp)
            return result

        return RunnerResult(ok=False, error="agy 调用失败")

    async def ask(
        self,
        session_key: str,
        prompt: str,
        *,
        model_override: str | None = None,
        profile_override: ModelProfileConfig | None = None,
        allow_fallback: bool = True,
        defer_geo_fallback: bool = False,
        delta_prompt: str | None = None,
        delta_base_seq: int = 0,
        window_min_seq: int | None = None,
        latest_seq: int = 0,
    ) -> RunnerResult:
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

                primary_model = model_override or self._cfg.model
                result = await self._ask_with_model(
                    session_key,
                    prompt,
                    model_override=model_override,
                    profile_override=profile_override,
                    delta_prompt=delta_prompt,
                    delta_base_seq=delta_base_seq,
                    window_min_seq=window_min_seq,
                    latest_seq=latest_seq,
                )
                if (
                    result.ok
                    or result.is_fatal_auth_or_tos
                    or result.is_rate_limited
                    or is_auth_required_error(result.error)
                    or not allow_fallback
                    or (defer_geo_fallback and is_geo_blocked_error(result.error))
                ):
                    return result

                # Fallback models (AstrBot-style automatic failover on 503/capacity/timeout)
                fallbacks = [m.strip() for m in self._cfg.fallback_models if m.strip() and m.strip() != primary_model]
                for fb_model in fallbacks:
                    logger.warning(
                        "[%s] 主模型 (%s) 失败 (%s)，自动回退至备用模型: %s",
                        session_key,
                        primary_model or "default",
                        result.error,
                        fb_model,
                    )
                    fb_result = await self._ask_with_model(
                        session_key,
                        prompt,
                        model_override=fb_model,
                        profile_override=profile_override,
                        delta_prompt=delta_prompt,
                        delta_base_seq=delta_base_seq,
                        window_min_seq=window_min_seq,
                        latest_seq=latest_seq,
                    )
                    if fb_result.ok or fb_result.is_fatal_auth_or_tos or fb_result.is_rate_limited:
                        return fb_result
                    result = fb_result

                return result
        finally:
            self._queued_or_running -= 1

    def _build_subagent_profile(self, effective_model: str) -> ModelProfileConfig:
        cli_model, base = self._cfg.resolve_model_target(effective_model)
        if base is None:
            return ModelProfileConfig(
                model=cli_model,
                enable_vision=True,
                enable_web_search=True,
                enable_url_read=False,
                enable_file_tools=False,
                enable_command=self._cfg.allow_command_execution,
            )
        return ModelProfileConfig(
            name=base.name,
            model=cli_model,
            effort=base.effort,
            max_context_messages=base.max_context_messages,
            max_turns=base.max_turns,
            compact_max_messages=base.compact_max_messages,
            compact_drop_by_count=base.compact_drop_by_count,
            compact_max_tokens=base.compact_max_tokens,
            compact_max_bytes=base.compact_max_bytes,
            compact_drop_by_tokens=base.compact_drop_by_tokens,
            execution_mode=base.execution_mode,
            sandbox=base.sandbox,
            disable_slash_commands=base.disable_slash_commands,
            enable_web_search=base.enable_web_search,
            enable_url_read=base.enable_url_read,
            enable_vision=True,  # Always allow read_file(.media_cache/*) & lazy MCP schemas
            enable_audio=base.enable_audio,
            enable_file_tools=base.enable_file_tools,
            enable_command=bool(self._cfg.allow_command_execution or base.enable_command),
        )

    async def _run_single_subagent(
        self,
        task_id: str,
        prompt: str,
        *,
        effective_model: str,
        timeout_sec: float,
    ) -> RunnerResult:
        profile = self._build_subagent_profile(effective_model)
        try:
            self._subagent_workspace_dir.mkdir(parents=True, exist_ok=True)
            cmd = self._build_cli_args(
                effective_model,
                resume_id="",
                profile_override=profile,
            )
            proc = await self._spawn_process(cmd, cwd=self._subagent_workspace_dir)
        except FileNotFoundError as exc:
            return RunnerResult(ok=False, error=str(exc))

        sp = _SessionProcess(
            session_key=f"subagent:{task_id}",
            proc=proc,
            conversation_id="",
            config_sig="subagent",
            model_name=(profile.model.strip() or effective_model or self._cfg.model or "default"),
        )
        sp.stderr_task = asyncio.create_task(self._drain_stderr(sp), name=f"agy-subagent-stderr-{task_id}")
        self._subagent_procs[id(sp)] = sp
        t0 = time.monotonic()
        pid = getattr(proc, "pid", None)
        logger.info(
            "[subagent:%s] 子代理进程已启动 (pid=%s, model=%s, timeout=%.0fs)",
            task_id,
            pid,
            effective_model or "default",
            timeout_sec,
        )
        try:
            result = await self._run_turn(sp, prompt, timeout_override=timeout_sec)
            elapsed = time.monotonic() - t0
            if result.ok and not (result.response or "").strip():
                logger.warning(
                    "[subagent:%s] 子代理进程返回空内容 (pid=%s, model=%s, 耗时 %.1fs, 返回 0 字)，标记为异常以便重试",
                    task_id,
                    pid,
                    effective_model or "default",
                    elapsed,
                )
                return RunnerResult(
                    ok=False,
                    response="",
                    conversation_id=result.conversation_id,
                    error="子代理返回空响应 (0字)",
                    status="EMPTY_RESPONSE",
                    num_turns=result.num_turns,
                )
            if result.ok:
                preview = re.sub(r"\s+", " ", result.response or "").strip()
                if len(preview) > 360:
                    preview = preview[:357] + "..."
                low_resp = (result.response or "").lower()
                if "prohibited use policy" in low_resp or "contains sensitive words" in low_resp:
                    logger.warning(
                        "[subagent:%s] 上游模型输入审核拦截 (pid=%s, model=%s, 耗时 %.1fs): %s",
                        task_id,
                        pid,
                        effective_model or "default",
                        elapsed,
                        preview,
                    )
                else:
                    logger.info(
                        "[subagent:%s] 子代理进程执行成功 (pid=%s, model=%s, 耗时 %.1fs, 返回 %d 字)%s",
                        task_id,
                        pid,
                        effective_model or "default",
                        elapsed,
                        len(result.response or ""),
                        f" 摘要: {preview}" if preview else "",
                    )
            else:
                logger.warning(
                    "[subagent:%s] 子代理进程执行失败 (pid=%s, model=%s, 耗时 %.1fs): %s",
                    task_id,
                    pid,
                    effective_model or "default",
                    elapsed,
                    result.error,
                )
            if not result.ok and (result.is_fatal_auth_or_tos or result.is_rate_limited):
                self._trip_circuit(is_fatal=result.is_fatal_auth_or_tos, reason=result.error)
            return result
        except TimeoutError as exc:
            elapsed = time.monotonic() - t0
            err_msg = str(exc) or f"后台子代理超时 ({timeout_sec}s)"
            logger.warning(
                "[subagent:%s] 子代理进程超时中止 (pid=%s, model=%s, 耗时 %.1fs): %s",
                task_id,
                pid,
                effective_model or "default",
                elapsed,
                err_msg,
            )
            return RunnerResult(ok=False, error=err_msg)
        finally:
            self._subagent_procs.pop(id(sp), None)
            await self._close_session_proc(sp)
            logger.info("[subagent:%s] 子代理进程已回收 (pid=%s)", task_id, pid)

    async def run_subagent_task(
        self,
        task_id: str,
        prompt: str,
        *,
        model_override: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RunnerResult:
        """Run an isolated background subagent process concurrently with main chat."""
        open_circuit, rem = self.is_circuit_open()
        if open_circuit:
            return RunnerResult(ok=False, error=f"熔断冷却中 (剩余 {rem:.0f}s): {self._circuit_reason}")

        eff_timeout = float(timeout_seconds) if (timeout_seconds and timeout_seconds > 0) else self._cfg.subagent_timeout_seconds
        primary_model = (model_override or self._cfg.subagent_model or self._cfg.model or "").strip()

        self._subagent_running += 1
        try:
            async with self._subagent_semaphore:
                open_circuit, rem = self.is_circuit_open()
                if open_circuit:
                    return RunnerResult(ok=False, error=f"熔断冷却中 (剩余 {rem:.0f}s): {self._circuit_reason}")

                result = await self._run_single_subagent(
                    task_id,
                    prompt,
                    effective_model=primary_model,
                    timeout_sec=eff_timeout,
                )
                if result.status == "EMPTY_RESPONSE":
                    logger.warning(
                        "[subagent:%s] 主模型 (%s) 返回空响应，自动使用同模型重试一次...",
                        task_id,
                        primary_model or "default",
                    )
                    result = await self._run_single_subagent(
                        task_id,
                        prompt,
                        effective_model=primary_model,
                        timeout_sec=eff_timeout,
                    )
                if result.ok or result.is_fatal_auth_or_tos or result.is_rate_limited:
                    return result

                fallbacks = [m.strip() for m in self._cfg.fallback_models if m.strip() and m.strip() != primary_model]
                for fb_model in fallbacks:
                    logger.warning(
                        "[subagent:%s] 子代理模型 (%s) 失败 (%s)，回退至备用模型: %s",
                        task_id,
                        primary_model or "default",
                        result.error,
                        fb_model,
                    )
                    fb_res = await self._run_single_subagent(
                        task_id,
                        prompt,
                        effective_model=fb_model,
                        timeout_sec=eff_timeout,
                    )
                    if fb_res.ok or fb_res.is_fatal_auth_or_tos or fb_res.is_rate_limited:
                        return fb_res
                    result = fb_res
                return result
        finally:
            self._subagent_running -= 1

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
                    judge_model = self._cfg.judge_model or self._cfg.model
                    cmd = self._build_cli_args(
                        judge_model,
                        json_schema=self._judge_schema_file,
                    )

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
                    await self._force_kill_process(proc)
                    return JudgeDecision(should_reply=False, reason="judge_timeout")
                except BaseException:
                    await self._force_kill_process(proc)
                    raise

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
        sub_items = list(self._subagent_procs.values())
        self._subagent_procs.clear()
        for sp in items + sub_items:
            await self._close_session_proc(sp)

    async def reset_sessions(self, session_key: str | None = None, *, clear_saved: bool = True) -> int:
        if session_key:
            sub_prefix = f"{session_key}:u:"
            to_close: list[_SessionProcess] = []
            matched = 0
            async with self._pool_lock:
                warm_keys = [k for k in list(self._warm_pool.keys()) if k == session_key or k.startswith(sub_prefix)]
                for k in warm_keys:
                    sp = self._warm_pool.pop(k, None)
                    if sp is not None:
                        to_close.append(sp)
                        matched += 1
                if clear_saved:
                    cursor_keys = [
                        k for k in list(self._epoch_cursors.keys())
                        if k == session_key or k.startswith(sub_prefix)
                    ]
                    for k in cursor_keys:
                        self._epoch_cursors.pop(k, None)
                    saved_keys = [
                        k for k in list(self._saved_conversations.keys())
                        if k == session_key or k.startswith(sub_prefix)
                    ]
                    for k in saved_keys:
                        self._saved_conversations.pop(k, None)
                    if saved_keys:
                        self._save_sessions()
                        matched = max(matched, len(saved_keys))
            for sp in to_close:
                await self._close_session_proc(sp)
            return matched

        async with self._pool_lock:
            items = list(self._warm_pool.values())
            self._warm_pool.clear()
            if clear_saved:
                self._epoch_cursors.clear()
                count = max(len(items), len(self._saved_conversations))
                self._saved_conversations.clear()
                self._save_sessions()
            else:
                for sp in items:
                    self._snapshot_epoch_cursor(sp)
                count = len(items)
        for sp in items:
            await self._close_session_proc(sp)
        return count

    def clear_circuit_breaker(self) -> None:
        self._circuit_until_mono = 0.0
        self._circuit_reason = ""

    def get_status(self) -> dict[str, Any]:
        is_open, rem = self.is_circuit_open()
        return {
            "mode": self._cfg.mode,
            "model": self._cfg.model,
            "fallback_models": list(self._cfg.fallback_models),
            "judge_model": self._cfg.judge_model,
            "circuit_open": is_open,
            "circuit_remaining_seconds": round(rem, 1),
            "circuit_reason": self._circuit_reason if is_open else "",
            "warm_sessions": [
                {
                    "session_key": k,
                    "conversation_id": sp.conversation_id,
                    "num_turns": sp.num_turns,
                    "window_min_seq": sp.window_min_seq,
                    "last_sent_seq": sp.last_sent_seq,
                }
                for k, sp in self._warm_pool.items()
            ],
            "saved_sessions_count": len(self._saved_conversations),
            "queued_or_running": self._queued_or_running,
            "subagent_running": self._subagent_running,
        }

    async def list_available_models(self) -> list[dict[str, str]]:
        proc: asyncio.subprocess.Process | None = None
        try:
            cmd = [*self._get_cmd_prefix(), "models"]
            proc = await self._spawn_process(cmd)
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=25.0)
        except asyncio.CancelledError:
            if proc is not None:
                await self._force_kill_process(proc)
            raise
        except Exception as exc:
            if proc is not None:
                await self._force_kill_process(proc)
            logger.warning("获取 agy models 失败: %s", exc)
            return []

        raw = (stdout_b.decode("utf-8", errors="replace") + "\n" + stderr_b.decode("utf-8", errors="replace"))
        models: list[dict[str, str]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("fetching") or line.lower().startswith("error:"):
                continue
            slug = ""
            label = ""
            if "\t" in line:
                slug, label = line.split("\t", 1)
            elif " " in line and not line.startswith("Usage"):
                parts = line.split(None, 1)
                if "-" in parts[0]:
                    slug, label = parts[0], parts[1]
            slug = slug.strip()
            if slug:
                family, effort = strip_model_effort_suffix(slug)
                models.append(
                    {
                        "id": slug,
                        "label": label.strip() or slug,
                        "family": family,
                        "effort": effort,
                    }
                )
        return models

    async def list_available_agents(self) -> list[dict[str, str]]:
        proc: asyncio.subprocess.Process | None = None
        try:
            cmd = [*self._get_cmd_prefix(), "agents"]
            proc = await self._spawn_process(cmd)
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        except asyncio.CancelledError:
            if proc is not None:
                await self._force_kill_process(proc)
            raise
        except Exception as exc:
            if proc is not None:
                await self._force_kill_process(proc)
            logger.warning("获取 agy agents 失败: %s", exc)
            return []

        raw = stdout_b.decode("utf-8", errors="replace") + "\n" + stderr_b.decode("utf-8", errors="replace")
        agents: list[dict[str, str]] = []
        seen: set[str] = set()
        for line in raw.splitlines():
            line = line.strip()
            if (
                not line
                or line.lower().startswith(("fetching", "error:", "usage", "no agents", "available"))
                or line.startswith("-")
            ):
                continue
            if "\t" in line:
                aid, desc = line.split("\t", 1)
            elif " " in line:
                aid, desc = line.split(None, 1)
            else:
                aid, desc = line, ""
            aid = aid.strip()
            if aid and aid not in seen:
                seen.add(aid)
                agents.append({"id": aid, "description": desc.strip()})
        return agents

    async def update_config(
        self,
        agy_config: AgyConfig,
        rate_limit_config: RateLimitConfig,
        *,
        settings_file: Path | None = None,
    ) -> None:
        need_recycle = (
            agy_config.model != self._cfg.model
            or agy_config.executable != self._cfg.executable
            or agy_config.proxy != self._cfg.proxy
            or agy_config.effort != self._cfg.effort
            or agy_config.agent != self._cfg.agent
            or agy_config.sandbox != self._cfg.sandbox
            or agy_config.allow_command_execution != self._cfg.allow_command_execution
            or agy_config.model_profiles != self._cfg.model_profiles
            or settings_file != self._settings_file
        )
        old_sub_conc = self._cfg.subagent_max_concurrent
        self._cfg = agy_config
        self._rate_cfg = rate_limit_config
        self._settings_file = settings_file
        self._cmd_prefix = None
        if agy_config.subagent_max_concurrent != old_sub_conc:
            self._subagent_semaphore = asyncio.Semaphore(max(1, agy_config.subagent_max_concurrent))
        if need_recycle:
            async with self._pool_lock:
                items = list(self._warm_pool.values())
                for sp in items:
                    self._snapshot_epoch_cursor(sp)
                self._warm_pool.clear()
            for sp in items:
                await self._close_session_proc(sp)


class MockAgentRunner(BaseAgentRunner):
    """Offline dry-run runner for testing NapCat WS, triggers, and debouncing safely."""

    def __init__(self, *, reply_prefix: str = "[Dry-Run] ") -> None:
        self._prefix = reply_prefix
        self.calls: list[tuple[str, str]] = []
        self.subagent_calls: list[tuple[str, str]] = []
        self.judge_calls: list[tuple[str, int]] = []
        self.judge_result: bool = False

    async def ask(self, session_key: str, prompt: str, **kwargs: Any) -> RunnerResult:
        eff_prompt = str(kwargs.get("delta_prompt") or prompt)
        self.calls.append((session_key, eff_prompt))
        last_line = [ln for ln in eff_prompt.splitlines() if ln.strip()][-1]
        resp = f"{self._prefix}已收到汇总上下文，最后一条为: {last_line[:120]}"
        in_tok = _estimate_tokens_fallback(eff_prompt)
        out_tok = _estimate_tokens_fallback(resp)
        return RunnerResult(
            ok=True,
            response=resp,
            conversation_id=f"mock-{session_key}",
            status="SUCCESS",
            num_turns=len(self.calls),
            model=str(kwargs.get("model_override") or "mock-model"),
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=in_tok + out_tok,
            ttft_seconds=0.05,
            thinking_seconds=0.0,
            duration_seconds=0.1,
            tokens_per_second=round(out_tok / 0.1, 1),
        )

    async def run_subagent_task(
        self,
        task_id: str,
        prompt: str,
        *,
        model_override: str | None = None,
        timeout_seconds: float | None = None,
    ) -> RunnerResult:
        self.subagent_calls.append((task_id, prompt))
        resp = f"{self._prefix}后台子代理已完成任务 {task_id}"
        in_tok = _estimate_tokens_fallback(prompt)
        out_tok = _estimate_tokens_fallback(resp)
        return RunnerResult(
            ok=True,
            response=resp,
            conversation_id=f"mock-subagent-{task_id}",
            status="SUCCESS",
            num_turns=1,
            model=str(model_override or "mock-subagent"),
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=in_tok + out_tok,
            ttft_seconds=0.05,
            thinking_seconds=0.0,
            duration_seconds=0.1,
            tokens_per_second=round(out_tok / 0.1, 1),
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
    settings_file: Path | None = None,
    subagent_workspace_dir: Path | None = None,
) -> BaseAgentRunner:
    if config.mode.lower() == "mock":
        return MockAgentRunner()
    return AgyRunner(
        config,
        rate_limit,
        workspace_dir=workspace_dir,
        data_dir=data_dir,
        settings_file=settings_file,
        subagent_workspace_dir=subagent_workspace_dir,
    )
