from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as dt
import importlib.util
import inspect
import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

from ..config import ScriptRuleConfig, ScriptTriggerConfig
from ..models import IncomingMessage, TriggerResult

logger = logging.getLogger(__name__)


class _SafeRegexNamespace:
    """Safe regex helper exposed to python_expr without leaking module internals (re.sys, etc.)."""

    @staticmethod
    def search(pattern: str, string: str, flags: int = 0) -> bool:
        return bool(re.search(pattern, string, flags))

    @staticmethod
    def match(pattern: str, string: str, flags: int = 0) -> bool:
        return bool(re.match(pattern, string, flags))

    @staticmethod
    def fullmatch(pattern: str, string: str, flags: int = 0) -> bool:
        return bool(re.fullmatch(pattern, string, flags))

    @staticmethod
    def findall(pattern: str, string: str, flags: int = 0) -> list[Any]:
        return re.findall(pattern, string, flags)

    IGNORECASE = re.IGNORECASE
    I = re.IGNORECASE


_SAFE_RE = _SafeRegexNamespace()

_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "lower": str.lower,
    "max": max,
    "min": min,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
}

_ALLOWED_AST_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.BinOp,
    ast.UnaryOp,
    ast.IfExp,
    ast.Compare,
    ast.Call,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.Subscript,
    ast.Slice,
    ast.Attribute,
    ast.ListComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.Store,
    ast.And,
    ast.Or,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
    ast.Is,
    ast.IsNot,
)

_SAFE_METHODS = {
    "lower",
    "upper",
    "strip",
    "lstrip",
    "rstrip",
    "startswith",
    "endswith",
    "split",
    "count",
    "find",
    "replace",
    "isdigit",
    "isalpha",
    "isalnum",
    "get",
    "keys",
    "values",
    "items",
    "search",
    "match",
    "fullmatch",
    "findall",
}


def _safe_eval_expr(expr: str, ctx: dict[str, Any]) -> Any:
    """Validate AST and evaluate a restricted Python trigger expression without sandbox escapes."""
    if len(expr) > 2000:
        raise ValueError("表达式过长（上限 2000 字符）")
    tree = ast.parse(expr, mode="eval")
    node_count = 0
    for node in ast.walk(tree):
        node_count += 1
        if node_count > 300:
            raise ValueError("表达式复杂度过高")
        if not isinstance(node, _ALLOWED_AST_NODES):
            raise ValueError(f"表达式包含不允许的语法: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise ValueError(f"表达式禁止访问内部标识符: {node.id}")
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr not in _SAFE_METHODS:
                raise ValueError(f"表达式禁止访问属性/方法: .{node.attr}")
    env = {**ctx, "re": _SAFE_RE}
    code = compile(tree, "<script_trigger_expr>", "eval")
    return eval(code, {"__builtins__": _SAFE_BUILTINS}, env)  # noqa: S307


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    pid = getattr(proc, "pid", None)
    if os.name == "nt" and pid:
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/T",
                "/F",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(killer.wait(), timeout=3.0)
    elif os.name != "nt" and pid:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=3.0)


class ScriptTriggerEvaluator:
    """Evaluates script-based message triggers (python_expr, python_file, shell_cmd)."""

    def __init__(self, config: ScriptTriggerConfig, *, base_dir: Path) -> None:
        self._config = config
        self._base_dir = base_dir
        self._last_rule_fire: dict[tuple[str, str], float] = {}
        self._module_cache: dict[str, tuple[float, Any]] = {}

    def update_config(self, config: ScriptTriggerConfig, *, base_dir: Path | None = None) -> None:
        self._config = config
        if base_dir is not None:
            self._base_dir = base_dir

    def _build_ctx(
        self,
        msg: IncomingMessage,
        *,
        is_admin: bool,
        recent_count: int,
    ) -> dict[str, Any]:
        now_dt = dt.datetime.fromtimestamp(msg.timestamp) if msg.timestamp else dt.datetime.now()
        return {
            "text": msg.text,
            "user_id": msg.user_id,
            "sender_name": msg.sender_name,
            "channel_id": msg.channel_id,
            "scene": msg.scene,
            "adapter_id": msg.adapter_id,
            "platform": msg.platform,
            "session_key": msg.session_key,
            "message_id": msg.message_id,
            "timestamp": msg.timestamp,
            "is_admin": is_admin,
            "length": len(msg.text),
            "at_bot": msg.at_bot_count > 0,
            "explicit_at_bot": msg.explicit_at_bot_count > 0,
            "has_at_all": msg.has_at_all,
            "is_quoted_bot": msg.is_quoted_bot,
            "hour": now_dt.hour,
            "minute": now_dt.minute,
            "weekday": now_dt.weekday(),
            "recent_count": recent_count,
        }

    def _resolve_script_path(self, raw_path: str) -> Path:
        base_resolved = self._base_dir.resolve()
        p = Path(raw_path)
        resolved = p.resolve() if p.is_absolute() else (base_resolved / p).resolve()
        if not resolved.is_relative_to(base_resolved):
            raise ValueError(f"脚本路径越出项目目录范围: {raw_path}")
        if resolved.suffix.lower() != ".py":
            raise ValueError("仅允许加载 .py Python 脚本文件")
        return resolved

    def _load_python_module(self, script_path: Path) -> Any:
        key = str(script_path)
        mtime = script_path.stat().st_mtime
        cached = self._module_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        spec = importlib.util.spec_from_file_location(f"agy_script_trigger_{abs(hash(key))}", script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载脚本文件: {script_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self._module_cache[key] = (mtime, mod)
        return mod

    def _normalize_outcome(
        self,
        rule: ScriptRuleConfig,
        raw_out: Any,
    ) -> TriggerResult:
        if not raw_out:
            return TriggerResult(triggered=False, reason=f"script_skip:{rule.id}")

        if isinstance(raw_out, dict):
            if not raw_out.get("trigger", False):
                return TriggerResult(triggered=False, reason=str(raw_out.get("reason") or f"script_skip:{rule.id}"))
            direct_reply = str(raw_out.get("reply", "") or "").strip()
            prompt_override = str(raw_out.get("prompt", "") or "").strip() or rule.prompt_override.strip()
            reason = str(raw_out.get("reason", "") or f"script:{rule.name or rule.id}")
            if not direct_reply and rule.action_mode == "text" and rule.reply_text.strip():
                direct_reply = rule.reply_text.strip()
            return TriggerResult(
                triggered=True,
                trigger_type="script",
                matched_triggers=["script"],
                reason=reason,
                should_wait_trailing=not bool(direct_reply),
                prompt_override=prompt_override,
                direct_reply=direct_reply,
            )

        # Truthy bool/string/value
        direct_reply = rule.reply_text.strip() if rule.action_mode == "text" else ""
        if isinstance(raw_out, str) and raw_out.strip() and rule.action_mode == "text":
            direct_reply = raw_out.strip()
        return TriggerResult(
            triggered=True,
            trigger_type="script",
            matched_triggers=["script"],
            reason=f"script:{rule.name or rule.id}",
            should_wait_trailing=not bool(direct_reply),
            prompt_override=rule.prompt_override.strip(),
            direct_reply=direct_reply,
        )

    async def evaluate_rule(
        self,
        rule: ScriptRuleConfig,
        msg: IncomingMessage,
        *,
        is_admin: bool = False,
        recent_count: int = 0,
    ) -> TriggerResult:
        ctx = self._build_ctx(msg, is_admin=is_admin, recent_count=recent_count)

        if rule.mode == "python_expr":
            expr = rule.expr.strip()
            if not expr:
                return TriggerResult(triggered=False, reason="empty_expr")
            out = await asyncio.wait_for(
                asyncio.to_thread(_safe_eval_expr, expr, ctx),
                timeout=max(1.0, rule.timeout_seconds),
            )
            return self._normalize_outcome(rule, out)

        if rule.mode == "python_file":
            if not rule.script_path.strip():
                return TriggerResult(triggered=False, reason="empty_script_path")
            path = self._resolve_script_path(rule.script_path.strip())
            if not path.exists():
                raise FileNotFoundError(f"脚本文件不存在: {path}")
            mod = self._load_python_module(path)
            fn = getattr(mod, "check", None) or getattr(mod, "should_trigger", None)
            if not callable(fn):
                raise RuntimeError(f"脚本 {path.name} 必须定义 check(ctx) 或 should_trigger(ctx) 函数")
            if inspect.iscoroutinefunction(fn):
                ret = await asyncio.wait_for(fn(ctx), timeout=max(1.0, rule.timeout_seconds))
            else:
                ret = await asyncio.wait_for(
                    asyncio.to_thread(fn, ctx),
                    timeout=max(1.0, rule.timeout_seconds),
                )
                if inspect.isawaitable(ret):
                    ret = await asyncio.wait_for(ret, timeout=max(1.0, rule.timeout_seconds))
            return self._normalize_outcome(rule, ret)

        if rule.mode == "shell_cmd":
            cmd = rule.command.strip()
            if not cmd:
                return TriggerResult(triggered=False, reason="empty_command")
            env = os.environ.copy()
            env["PYTHONIOENCODING"] = "utf-8"
            env["PYTHONUTF8"] = "1"
            popen_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            else:
                popen_kwargs["start_new_session"] = True
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._base_dir),
                env=env,
                **popen_kwargs,
            )
            payload = json.dumps(ctx, ensure_ascii=True).encode("utf-8")
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(payload),
                    timeout=max(1.0, rule.timeout_seconds),
                )
            except asyncio.TimeoutError:
                await _kill_process_tree(proc)
                raise TimeoutError(f"脚本命令超时 (>{rule.timeout_seconds}s): {cmd}")
            except asyncio.CancelledError:
                await _kill_process_tree(proc)
                raise

            if proc.returncode != 0:
                err_txt = stderr_b.decode("utf-8", errors="replace").strip()
                if err_txt:
                    logger.debug("脚本触发 [%s] 返回码 %s: %s", rule.id, proc.returncode, err_txt[:200])
                return TriggerResult(triggered=False, reason=f"exit_{proc.returncode}")

            out_text = stdout_b.decode("utf-8", errors="replace").strip()
            if not out_text:
                return self._normalize_outcome(rule, True)
            try:
                parsed = json.loads(out_text)
                return self._normalize_outcome(rule, parsed)
            except Exception:
                # Non-JSON plain text stdout: treat as trigger=True (and direct reply if action_mode=="text")
                return self._normalize_outcome(rule, out_text)

        return TriggerResult(triggered=False, reason=f"unknown_mode:{rule.mode}")

    async def evaluate(
        self,
        msg: IncomingMessage,
        *,
        is_admin: bool = False,
        recent_count: int = 0,
        now_mono: float | None = None,
    ) -> TriggerResult:
        if not self._config.enabled or not self._config.rules or msg.is_self:
            return TriggerResult(triggered=False, reason="script_disabled")

        now = time.monotonic() if now_mono is None else now_mono
        for rule in self._config.rules:
            if not rule.enabled:
                continue
            cd_key = (rule.id, msg.session_key)
            last = self._last_rule_fire.get(cd_key)
            if rule.cooldown_seconds > 0 and last is not None and (now - last) < rule.cooldown_seconds:
                continue
            try:
                res = await self.evaluate_rule(
                    rule,
                    msg,
                    is_admin=is_admin,
                    recent_count=recent_count,
                )
            except Exception as exc:
                logger.warning("脚本触发规则 [%s/%s] 执行异常: %s", rule.id, rule.name, exc)
                continue
            if res.triggered:
                self._last_rule_fire[cd_key] = now
                return res

        return TriggerResult(triggered=False, reason="script_no_match")
