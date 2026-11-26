from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..config import PlatformConfig
from ..models import CachedMessage, IncomingMessage, MediaAttachment

OnMessageFn = Callable[[IncomingMessage], Awaitable[None]]
OnGroupBanFn = Callable[[str, str, bool, float, str], Awaitable[None]]
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
        self._on_group_ban: OnGroupBanFn | None = None

    def set_group_ban_handler(self, handler: OnGroupBanFn | None) -> None:
        self._on_group_ban = handler

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

    async def send_image(
        self,
        target: IncomingMessage,
        image: bytes | str | Path,
        *,
        caption: str = "",
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        """Send an image/GIF (bytes, base64://, URL, or local file path) to the target channel."""
        raise NotImplementedError(f"{self.platform_name} 适配器暂不支持发送图片")

    async def resolve_image_source(self, attachment: MediaAttachment) -> dict[str, str]:
        """Optional hook for platform adapters to resolve fresh image URL/local file via RPC."""
        return {}

    def is_channel_allowed(self, scene: str, channel_id: str, user_id: str) -> bool:
        uid = str(user_id or "").strip()
        cid = str(channel_id or "").strip()
        if uid and uid in self.config.user_blocklist:
            return False
        if scene == "group":
            return True
        if scene == "private":
            if (uid and uid in self.config.private_blocklist) or (cid and cid in self.config.private_blocklist):
                return False
            return True
        return False
