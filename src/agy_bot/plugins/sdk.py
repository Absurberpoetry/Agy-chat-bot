from __future__ import annotations

import contextvars
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..app import BotApplication
    from ..config import BotConfig
    from ..models import ContextSnapshot, IncomingMessage

PLUGIN_SUBAGENT_CTX: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "agy_plugin_subagent_active",
    default=False,
)


@dataclass(slots=True)
class CommandMeta:
    name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    admin_only: bool = False


@dataclass(slots=True)
class HookMeta:
    kind: str  # "on_message" | "on_before_llm" | "on_after_llm"
    priority: int = 50


@dataclass(slots=True)
class PluginReply:
    """Explicit return value from a plugin hook or command."""

    text: str = ""
    stop: bool = True
    quote: bool = True
    at: bool = False


def command(
    name: str,
    *,
    aliases: list[str] | tuple[str, ...] = (),
    description: str = "",
    admin_only: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a chat command method on a BotPlugin subclass."""

    clean_name = name.strip().lstrip("/!#.").lower()
    clean_aliases = tuple(a.strip().lstrip("/!#.").lower() for a in aliases if a.strip())

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        cmds: list[CommandMeta] = getattr(fn, "_agy_commands", [])
        cmds.append(
            CommandMeta(
                name=clean_name,
                aliases=clean_aliases,
                description=description,
                admin_only=admin_only,
            )
        )
        setattr(fn, "_agy_commands", cmds)
        return fn

    return decorator


def on_message(*, priority: int = 50) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Hook called for every incoming non-self message after channel allowlist check."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        hooks: list[HookMeta] = getattr(fn, "_agy_hooks", [])
        hooks.append(HookMeta(kind="on_message", priority=priority))
        setattr(fn, "_agy_hooks", hooks)
        return fn

    return decorator


def on_before_llm(*, priority: int = 50) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Hook called right before sending prompt to Antigravity CLI; return modified prompt string."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        hooks: list[HookMeta] = getattr(fn, "_agy_hooks", [])
        hooks.append(HookMeta(kind="on_before_llm", priority=priority))
        setattr(fn, "_agy_hooks", hooks)
        return fn

    return decorator


def on_after_llm(*, priority: int = 50) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Hook called right after receiving model output; return modified reply text."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        hooks: list[HookMeta] = getattr(fn, "_agy_hooks", [])
        hooks.append(HookMeta(kind="on_after_llm", priority=priority))
        setattr(fn, "_agy_hooks", hooks)
        return fn

    return decorator


class PluginContext:
    """Safe runtime API surface exposed to external plugins."""

    def __init__(
        self,
        *,
        app: BotApplication,
        plugin_id: str,
        plugin_config: dict[str, Any],
        data_dir: Path,
    ) -> None:
        self._app = app
        self.plugin_id = plugin_id
        self._plugin_config = plugin_config
        self.data_dir = data_dir
        self.logger = logging.getLogger(f"agy_bot.plugin.{plugin_id}")

    @property
    def bot_config(self) -> BotConfig:
        return self._app.config

    @property
    def config(self) -> dict[str, Any]:
        return self._plugin_config

    def set_plugin_config(self, cfg: dict[str, Any]) -> None:
        self._plugin_config = cfg

    async def send_reply(
        self,
        msg: IncomingMessage,
        text: str,
        *,
        quote: bool = True,
        at: bool = False,
    ) -> str | None:
        adapter = self._app.registry.get(msg.adapter_id)
        if adapter is None:
            return None
        return await adapter.send_reply(msg, text, quote_trigger=quote, at_user=at)

    async def send_image(
        self,
        msg: IncomingMessage,
        image: bytes | str | Path,
        *,
        caption: str = "",
        quote: bool = True,
        at: bool = False,
    ) -> str | None:
        adapter = self._app.registry.get(msg.adapter_id)
        if adapter is None:
            return None
        return await adapter.send_image(msg, image, caption=caption, quote_trigger=quote, at_user=at)

    async def ask_llm(
        self,
        session_key: str,
        prompt: str,
        *,
        use_subagent: bool | None = None,
        model_override: str | None = None,
        persona_override: str | None = None,
    ) -> str:
        pm = getattr(self._app, "plugin_manager", None)
        llm_opts = pm.get_plugin_llm_options(self.plugin_id) if (pm is not None and hasattr(pm, "get_plugin_llm_options")) else {}

        eff_sub = use_subagent
        if eff_sub is None:
            eff_sub = bool(PLUGIN_SUBAGENT_CTX.get())
            if not eff_sub and pm is not None:
                eff_sub = pm.should_use_subagent(self.plugin_id)

        eff_model = (model_override or llm_opts.get("model") or "").strip() or None
        eff_persona = (persona_override if persona_override is not None else llm_opts.get("persona_id", "")).strip()

        eff_prompt = prompt
        if eff_persona == "__none__":
            eff_prompt = f"[插件纯净模式：请保持客观中立，不套用日常聊天人设，直接完成下方任务]\n{prompt}"
        elif eff_persona:
            target_pid = (self._app.config.active_persona_id or "").strip() if eff_persona == "__follow_main__" else eff_persona
            pstore = getattr(self._app, "persona_store", None)
            p_item = pstore.get(target_pid) if (pstore is not None and target_pid) else None
            if p_item is not None and (p_item.system_prompt or "").strip():
                eff_prompt = (
                    f"[插件载入人设: {p_item.persona_id}]\n"
                    f"{p_item.system_prompt.strip()}\n\n"
                    f"[插件任务]\n{prompt}"
                )

        t0 = time.monotonic()
        if eff_sub and self._app.config.agy.subagent_enabled:
            task_id = f"pl-{self.plugin_id}-{uuid.uuid4().hex[:6]}"
            self.logger.info(
                "[%s] [插件:%s] 通过后台子代理 #%s 调用 LLM (model=%s, persona=%s)",
                session_key,
                self.plugin_id,
                task_id,
                eff_model or "default",
                eff_persona or "default",
            )
            res = await self._app.runner.run_subagent_task(
                task_id,
                eff_prompt,
                model_override=eff_model,
                timeout_seconds=self._app.config.agy.subagent_timeout_seconds,
            )
        else:
            self.logger.info(
                "[%s] [插件:%s] 通过主对话通道调用 LLM (model=%s, persona=%s)",
                session_key,
                self.plugin_id,
                eff_model or "default",
                eff_persona or "default",
            )
            ask_fn = getattr(self._app, "_ask_runner", None)
            if callable(ask_fn):
                res = await ask_fn(session_key, eff_prompt, model_override=eff_model)
            else:
                res = await self._app.runner.ask(session_key, eff_prompt)
        elapsed = time.monotonic() - t0
        if not res.ok:
            self.logger.warning(
                "[%s] [插件:%s] 调用 LLM 失败 (耗时 %.2fs): %s",
                session_key,
                self.plugin_id,
                elapsed,
                res.error,
            )
            raise RuntimeError(res.error or "Antigravity 调用失败")
        rec_fn = getattr(self._app, "record_token_usage", None)
        if callable(rec_fn):
            rec_fn(
                session_key=session_key,
                result=res,
                trigger_type=f"plugin:{self.plugin_id}",
            )
        self.logger.info(
            "[%s] [插件:%s] 调用 LLM 完成 (耗时 %.2fs, 返回 %d 字)",
            session_key,
            self.plugin_id,
            elapsed,
            len(res.response or ""),
        )
        return res.response

    def get_recent_messages(self, session_key: str, limit: int = 20) -> list[IncomingMessage]:
        return self._app.context_store.get_recent(session_key, limit=limit)

    def get_active_persona_id(self) -> str:
        return self._app.config.active_persona_id

    def list_plugins(self) -> list[dict[str, Any]]:
        if self._app.plugin_manager is None:
            return []
        return self._app.plugin_manager.list_plugins()


class BotPlugin:
    """Base class for external Antigravity QQ Bot plugins."""

    id: str = ""
    name: str = ""
    version: str = "1.0.0"
    author: str = ""
    description: str = ""
    default_config: dict[str, Any] = {}
    config_schema: list[dict[str, Any]] = []

    def __init__(self, ctx: PluginContext) -> None:
        self.ctx = ctx

    @property
    def config(self) -> dict[str, Any]:
        return self.ctx.config

    async def on_load(self) -> None:
        """Called once after the plugin is instantiated and enabled."""

    async def on_unload(self) -> None:
        """Called before the plugin is disabled, reloaded, or bot shuts down."""

    async def on_config_update(self, new_config: dict[str, Any]) -> None:
        """Called when this plugin's config is updated live from WebUI."""


PluginHandler = Callable[..., Awaitable[Any] | Any]
