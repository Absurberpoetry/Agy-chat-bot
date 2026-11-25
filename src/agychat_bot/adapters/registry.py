from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable
from typing import Any

from ..config import PlatformConfig
from .base import MessageLookupFn, OnGroupBanFn, OnMessageFn, PlatformAdapter, WsConnectFn
from .generic_ws import GenericWSAdapter
from .onebot_v11 import OneBotV11Adapter
from .telegram import TelegramAdapter

AdapterFactory = Callable[..., PlatformAdapter]


class AdapterRegistry:
    """Builds and runs multiple platform adapters concurrently (OneBot v11, Telegram, Generic WS)."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {
            "onebot_v11": OneBotV11Adapter,
            "napcat": OneBotV11Adapter,
            "telegram": TelegramAdapter,
            "tg": TelegramAdapter,
            "generic_ws": GenericWSAdapter,
        }
        self._adapters: dict[str, PlatformAdapter] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._on_message: OnMessageFn | None = None
        self._wakeup: asyncio.Event | None = None
        self._stopped: bool = False
        self._message_lookup: MessageLookupFn | None = None
        self._ws_connect: WsConnectFn | None = None
        self._on_group_ban: OnGroupBanFn | None = None
        self._default_proxy: str = ""

    def register_type(self, type_name: str, factory: AdapterFactory) -> None:
        self._factories[type_name] = factory

    def _create_adapter(
        self,
        cfg: PlatformConfig,
        *,
        message_lookup: MessageLookupFn | None,
        ws_connect: WsConnectFn | None,
        on_group_ban: OnGroupBanFn | None,
        default_proxy: str,
    ) -> PlatformAdapter:
        factory = self._factories.get(cfg.type)
        if factory is None:
            raise ValueError(f"未知平台适配器类型: {cfg.type} (platform id={cfg.id})")
        kwargs: dict[str, object] = {
            "message_lookup": message_lookup,
            "ws_connect": ws_connect,
        }
        try:
            if "default_proxy" in inspect.signature(factory).parameters:
                kwargs["default_proxy"] = default_proxy
        except (TypeError, ValueError):
            pass
        adapter = factory(cfg, **kwargs)
        if on_group_ban is not None:
            adapter.set_group_ban_handler(on_group_ban)
        return adapter

    def build_adapters(
        self,
        platform_configs: list[PlatformConfig],
        *,
        message_lookup: MessageLookupFn | None = None,
        ws_connect: WsConnectFn | None = None,
        on_group_ban: OnGroupBanFn | None = None,
        default_proxy: str = "",
    ) -> dict[str, PlatformAdapter]:
        self._message_lookup = message_lookup
        self._ws_connect = ws_connect
        self._on_group_ban = on_group_ban
        self._default_proxy = (default_proxy or "").strip()
        self._adapters.clear()
        for cfg in platform_configs:
            if not cfg.enabled:
                continue
            if cfg.id in self._adapters:
                raise ValueError(f"重复的平台实例 ID: {cfg.id}")
            self._adapters[cfg.id] = self._create_adapter(
                cfg,
                message_lookup=message_lookup,
                ws_connect=ws_connect,
                on_group_ban=on_group_ban,
                default_proxy=self._default_proxy,
            )
        return dict(self._adapters)

    async def sync_adapters(
        self,
        platform_configs: list[PlatformConfig],
        *,
        default_proxy: str | None = None,
    ) -> bool:
        """Hot-add, hot-restart, or hot-remove platform adapters without disconnecting untouched clients."""
        if default_proxy is not None:
            self._default_proxy = default_proxy.strip()
        new_enabled = {p.id: p for p in platform_configs if p.enabled}
        changed = False

        def _needs_rebuild(old_cfg: PlatformConfig, new_cfg: PlatformConfig) -> bool:
            return (
                old_cfg.type != new_cfg.type
                or old_cfg.ws_url != new_cfg.ws_url
                or old_cfg.access_token != new_cfg.access_token
                or (old_cfg.extra or {}) != (new_cfg.extra or {})
            )

        # 1) Stop removed, disabled, or connection-changed adapters
        for aid in list(self._adapters.keys()):
            old_ad = self._adapters[aid]
            np = new_enabled.get(aid)
            if np is None or _needs_rebuild(old_ad.config, np):
                changed = True
                self._adapters.pop(aid, None)
                with contextlib.suppress(Exception):
                    await old_ad.stop()
                task = self._tasks.pop(aid, None)
                if task is not None and not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
            else:
                old_ad.config = np
                if hasattr(old_ad, "_default_proxy"):
                    old_ad._default_proxy = self._default_proxy

        # 2) Start newly added or rebuilt enabled adapters
        for aid, cfg in new_enabled.items():
            if aid in self._adapters:
                continue
            changed = True
            ad = self._create_adapter(
                cfg,
                message_lookup=self._message_lookup,
                ws_connect=self._ws_connect,
                on_group_ban=self._on_group_ban,
                default_proxy=self._default_proxy,
            )
            self._adapters[aid] = ad
            if self._on_message is not None:
                self._tasks[aid] = asyncio.create_task(ad.start(self._on_message), name=f"adapter-{aid}")

        if changed and self._wakeup is not None:
            self._wakeup.set()
        return changed

    def set_group_ban_handler(self, handler: OnGroupBanFn | None) -> None:
        self._on_group_ban = handler
        for adapter in self._adapters.values():
            adapter.set_group_ban_handler(handler)

    def get(self, adapter_id: str) -> PlatformAdapter | None:
        return self._adapters.get(adapter_id)

    @property
    def adapters(self) -> dict[str, PlatformAdapter]:
        return self._adapters

    async def run_all(self, on_message: OnMessageFn) -> None:
        self._stopped = False
        self._on_message = on_message
        self._wakeup = asyncio.Event()
        self._tasks = {
            aid: asyncio.create_task(adapter.start(on_message), name=f"adapter-{aid}")
            for aid, adapter in self._adapters.items()
        }
        try:
            while not self._stopped:
                self._wakeup.clear()
                waiters: list[asyncio.Task[Any]] = list(self._tasks.values())
                wake_task: asyncio.Task[Any] = asyncio.create_task(self._wakeup.wait(), name="adapter-registry-wakeup")
                waiters.append(wake_task)
                try:
                    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if not wake_task.done():
                        wake_task.cancel()
                        with contextlib.suppress(BaseException):
                            await wake_task
                for task in done:
                    if task is wake_task:
                        continue
                    for k, v in list(self._tasks.items()):
                        if v is task:
                            self._tasks.pop(k, None)
                            break
                    if not task.cancelled():
                        exc = task.exception()
                        if exc is not None:
                            raise exc
        finally:
            self._on_message = None
            self._wakeup = None
            for t in list(self._tasks.values()):
                if not t.done():
                    t.cancel()
            if self._tasks:
                with contextlib.suppress(BaseException):
                    await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            self._tasks.clear()

    async def stop_all(self) -> None:
        self._stopped = True
        if self._wakeup is not None:
            self._wakeup.set()
        await asyncio.gather(
            *(adapter.stop() for adapter in self._adapters.values()),
            return_exceptions=True,
        )
        for t in list(self._tasks.values()):
            if not t.done():
                t.cancel()

    def get_status(self) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        for aid, adapter in self._adapters.items():
            is_conn = bool(getattr(adapter, "connected", False)) or (getattr(adapter, "_ws", None) is not None)
            extra_info = adapter.config.extra if isinstance(adapter.config.extra, dict) else {}
            items.append(
                {
                    "id": aid,
                    "type": adapter.platform_name,
                    "ws_url": adapter.config.ws_url,
                    "connected": is_conn,
                    "self_id": getattr(adapter, "self_id", adapter.config.self_id),
                    "bot_username": getattr(adapter, "bot_username", ""),
                    "user_blocklist": list(adapter.config.user_blocklist),
                    "private_blocklist": list(adapter.config.private_blocklist),
                    "extra": dict(extra_info),
                }
            )
        return items
