from __future__ import annotations

import asyncio
from collections.abc import Callable

from ..config import PlatformConfig
from .base import MessageLookupFn, OnMessageFn, PlatformAdapter, WsConnectFn
from .generic_ws import GenericWSAdapter
from .onebot_v11 import OneBotV11Adapter

AdapterFactory = Callable[..., PlatformAdapter]


class AdapterRegistry:
    """Builds and runs multiple forward-WebSocket platform adapters concurrently."""

    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {
            "onebot_v11": OneBotV11Adapter,
            "napcat": OneBotV11Adapter,
            "generic_ws": GenericWSAdapter,
        }
        self._adapters: dict[str, PlatformAdapter] = {}

    def register_type(self, type_name: str, factory: AdapterFactory) -> None:
        self._factories[type_name] = factory

    def build_adapters(
        self,
        platform_configs: list[PlatformConfig],
        *,
        message_lookup: MessageLookupFn | None = None,
        ws_connect: WsConnectFn | None = None,
    ) -> dict[str, PlatformAdapter]:
        self._adapters.clear()
        for cfg in platform_configs:
            if not cfg.enabled:
                continue
            factory = self._factories.get(cfg.type)
            if factory is None:
                raise ValueError(f"未知平台适配器类型: {cfg.type} (platform id={cfg.id})")
            if cfg.id in self._adapters:
                raise ValueError(f"重复的平台实例 ID: {cfg.id}")
            self._adapters[cfg.id] = factory(
                cfg,
                message_lookup=message_lookup,
                ws_connect=ws_connect,
            )
        return dict(self._adapters)

    def get(self, adapter_id: str) -> PlatformAdapter | None:
        return self._adapters.get(adapter_id)

    @property
    def adapters(self) -> dict[str, PlatformAdapter]:
        return self._adapters

    async def run_all(self, on_message: OnMessageFn) -> None:
        if not self._adapters:
            raise RuntimeError("没有任何已启用的平台连接，请检查 config.json 的 platforms 配置")
        await asyncio.gather(*(adapter.start(on_message) for adapter in self._adapters.values()))

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(adapter.stop() for adapter in self._adapters.values()),
            return_exceptions=True,
        )
