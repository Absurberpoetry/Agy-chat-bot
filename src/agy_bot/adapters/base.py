from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import PlatformConfig
from ..models import CachedMessage, IncomingMessage

OnMessageFn = Callable[[IncomingMessage], Awaitable[None]]
MessageLookupFn = Callable[[str, str], CachedMessage | None]
WsConnectFn = Callable[..., Any]


def default_ws_connect() -> WsConnectFn:
    try:
        import websockets  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - runtime guidance when missing dependency
        raise RuntimeError(
            "缺少依赖 'websockets'，请先执行: pip install -r requirements.txt"
        ) from exc

    # Prefer modern websockets.asyncio.client.connect when available
    try:
        from websockets.asyncio.client import connect  # type: ignore[import-not-found]

        return connect
    except Exception:
        return websockets.connect


class PlatformAdapter(ABC):
    """Base interface for forward-WebSocket platform adapters."""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        message_lookup: MessageLookupFn | None = None,
        ws_connect: WsConnectFn | None = None,
    ) -> None:
        self.config = config
        self.adapter_id = config.id
        self.platform_name = config.type
        self._message_lookup = message_lookup
        self._ws_connect = ws_connect

    @abstractmethod
    async def start(self, on_message: OnMessageFn) -> None:
        """Connect to the platform WebSocket and stream events until stop() is called."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully close connections and background tasks."""

    @abstractmethod
    async def send_reply(
        self,
        target: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        """Reply to a triggering message on this platform, returning sent message_id if known."""

    def is_channel_allowed(self, scene: str, channel_id: str, user_id: str) -> bool:
        if scene == "group":
            if channel_id in self.config.group_blocklist:
                return False
            if self.config.group_allowlist and channel_id not in self.config.group_allowlist:
                return False
            return True
        if scene == "private":
            if not self.config.private_chat_enabled:
                return False
            if self.config.private_allowlist and user_id not in self.config.private_allowlist:
                return False
            return True
        return False
