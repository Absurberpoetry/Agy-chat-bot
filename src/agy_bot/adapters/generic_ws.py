from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from ..config import PlatformConfig
from ..models import IncomingMessage, MediaAttachment, SceneType
from .base import (
    MessageLookupFn,
    OnMessageFn,
    PlatformAdapter,
    WsConnectFn,
    default_ws_connect,
)
from .onebot_v11 import _connect_headers_kwarg, _normalize_timestamp

logger = logging.getLogger(__name__)


class GenericWSAdapter(PlatformAdapter):
    """Extensible forward-WebSocket adapter for custom platform bridges.

    Inbound JSON schema:
      {
        "type": "message",
        "scene": "group" | "private",
        "channel_id": "...",
        "message_id": "...",
        "user_id": "...",
        "sender_name": "...",
        "text": "...",
        "self_id": "...",
        "at_targets": ["..."],
        "reply_to_msg_id": "...",
        "reply_to_user_id": "...",
        "reply_to_text": "..."
      }
    """

    def __init__(
        self,
        config: PlatformConfig,
        *,
        message_lookup: MessageLookupFn | None = None,
        ws_connect: WsConnectFn | None = None,
    ) -> None:
        super().__init__(config, message_lookup=message_lookup, ws_connect=ws_connect)
        self._stop_event = asyncio.Event()
        self._ws: Any = None
        self._on_message: OnMessageFn | None = None

    async def start(self, on_message: OnMessageFn) -> None:
        self._on_message = on_message
        self._stop_event.clear()
        connect_fn = self._ws_connect or default_ws_connect()
        backoff = max(0.5, self.config.reconnect_initial_seconds)

        while not self._stop_event.is_set():
            headers: dict[str, str] = {}
            if self.config.access_token:
                headers["Authorization"] = f"Bearer {self.config.access_token}"
            connect_kwargs = _connect_headers_kwarg(connect_fn, headers, self.config.ws_url)

            try:
                logger.info("[%s] 正在连接自定义平台 WS: %s", self.adapter_id, self.config.ws_url)
                async with connect_fn(self.config.ws_url, **connect_kwargs) as ws:
                    self._ws = ws
                    backoff = max(0.5, self.config.reconnect_initial_seconds)
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        try:
                            payload = json.loads(raw)
                        except json.JSONDecodeError:
                            logger.debug("[%s] 忽略非 JSON 帧: %s", self.adapter_id, str(raw)[:200])
                            continue
                        try:
                            msg = self.normalize_event(payload)
                            if msg is not None and self._on_message is not None:
                                await self._on_message(msg)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            logger.exception("[%s] 处理自定义平台消息帧失败，已跳过该帧", self.adapter_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("[%s] 自定义平台 WS 断开: %s，%.1fs 后重连", self.adapter_id, exc, backoff)
            finally:
                self._ws = None

            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(self.config.reconnect_max_seconds, backoff * 2)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    def normalize_event(self, payload: Any) -> IncomingMessage | None:
        if not isinstance(payload, dict) or payload.get("type") != "message":
            return None
        scene: SceneType = "private" if payload.get("scene") == "private" else "group"
        self_id = str(payload.get("self_id") or self.config.self_id or "").strip()
        user_id = str(payload.get("user_id") or "").strip()
        channel_id = str(payload.get("channel_id") or "").strip()
        if not user_id or not channel_id:
            return None

        reply_to_msg_id = (
            str(payload["reply_to_msg_id"]) if payload.get("reply_to_msg_id") is not None else None
        )
        reply_to_user_id = (
            str(payload["reply_to_user_id"]) if payload.get("reply_to_user_id") is not None else None
        )
        reply_to_text = (
            str(payload["reply_to_text"]) if payload.get("reply_to_text") is not None else None
        )
        reply_attachments: list[MediaAttachment] = []
        if reply_to_msg_id and not reply_to_user_id and self._message_lookup is not None:
            cached = self._message_lookup(self.adapter_id, reply_to_msg_id)
            if cached is not None:
                reply_to_user_id = cached.user_id
                reply_to_text = cached.text
                reply_attachments = [
                    MediaAttachment(
                        kind=a.kind,
                        url=a.url,
                        file_id=a.file_id,
                        file_name=a.file_name,
                        source_path=a.source_path,
                        local_path=a.local_path,
                        summary=a.summary,
                        source="reply",
                    )
                    for a in cached.attachments
                ]

        msg_attachments: list[MediaAttachment] = []
        raw_images = payload.get("images")
        if isinstance(raw_images, list):
            for img_item in raw_images:
                if isinstance(img_item, str) and img_item.strip():
                    msg_attachments.append(MediaAttachment(kind="image", url=img_item.strip(), source="message"))
                elif isinstance(img_item, dict):
                    msg_attachments.append(
                        MediaAttachment(
                            kind="image",
                            url=str(img_item.get("url") or "").strip(),
                            file_id=str(img_item.get("file") or img_item.get("file_id") or "").strip(),
                            source_path=str(img_item.get("path") or "").strip(),
                            summary=str(img_item.get("summary") or "").strip(),
                            source="message",
                        )
                    )

        raw_at = payload.get("at_targets")
        at_targets = [str(x).strip() for x in raw_at if str(x).strip()] if isinstance(raw_at, list) else []

        return IncomingMessage(
            adapter_id=self.adapter_id,
            platform=self.platform_name,
            self_id=self_id,
            scene=scene,
            channel_id=channel_id,
            message_id=str(payload.get("message_id") or uuid.uuid4().hex),
            user_id=user_id,
            sender_name=str(payload.get("sender_name") or user_id),
            text=str(payload.get("text") or "").strip(),
            timestamp=_normalize_timestamp(payload.get("timestamp")),
            at_targets=at_targets,
            reply_to_msg_id=reply_to_msg_id,
            reply_to_user_id=reply_to_user_id,
            reply_to_text=reply_to_text,
            is_self=bool(self_id and user_id == self_id),
            raw_event=payload,
            attachments=[*reply_attachments, *msg_attachments],
        )

    async def send_reply(
        self,
        target: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        if self._ws is None:
            raise ConnectionError(f"[{self.adapter_id}] WebSocket 未连接")
        msg_id = uuid.uuid4().hex
        packet = {
            "type": "send_message",
            "message_id": msg_id,
            "scene": target.scene,
            "channel_id": target.channel_id,
            "text": text,
            "reply_to_msg_id": target.message_id if quote_trigger else None,
            "at_user_id": target.user_id if at_user else None,
        }
        await self._ws.send(json.dumps(packet, ensure_ascii=False))
        return msg_id

    async def send_image(
        self,
        target: IncomingMessage,
        image: bytes | str | Path,
        *,
        caption: str = "",
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        if self._ws is None:
            raise ConnectionError(f"[{self.adapter_id}] WebSocket 未连接")
        msg_id = uuid.uuid4().hex
        image_b64 = ""
        image_url = ""
        if isinstance(image, bytes):
            image_b64 = base64.b64encode(image).decode("ascii")
        elif isinstance(image, Path):
            image_b64 = base64.b64encode(image.read_bytes()).decode("ascii")
        else:
            s = str(image).strip()
            if s.lower().startswith("base64://"):
                image_b64 = s[len("base64://") :]
            elif s.lower().startswith(("http://", "https://")):
                image_url = s
            else:
                p = Path(s)
                if p.is_file():
                    image_b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                else:
                    image_url = s
        packet = {
            "type": "send_image",
            "message_id": msg_id,
            "scene": target.scene,
            "channel_id": target.channel_id,
            "caption": caption,
            "image_base64": image_b64,
            "image_url": image_url,
            "reply_to_msg_id": target.message_id if quote_trigger else None,
            "at_user_id": target.user_id if at_user else None,
        }
        await self._ws.send(json.dumps(packet, ensure_ascii=False))
        return msg_id
