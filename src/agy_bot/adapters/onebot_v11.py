from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import re
import time
import uuid
from typing import Any

from ..config import PlatformConfig
from ..models import IncomingMessage, SceneType, Segment
from .base import (
    MessageLookupFn,
    OnMessageFn,
    PlatformAdapter,
    WsConnectFn,
    default_ws_connect,
)

logger = logging.getLogger(__name__)

_CQ_PATTERN = re.compile(r"\[CQ:([a-zA-Z0-9_-]+)(?:,([^\]]*))?\]")


def _cq_unescape(val: str) -> str:
    return (
        val.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def parse_onebot_segments(raw_message: Any) -> list[Segment]:
    """Normalize either OneBot array segments or CQ-code string into Segment list."""
    if isinstance(raw_message, list):
        segments: list[Segment] = []
        for item in raw_message:
            if not isinstance(item, dict):
                continue
            seg_type = str(item.get("type", ""))
            seg_data = item.get("data")
            if not isinstance(seg_data, dict):
                seg_data = {}
            if seg_type:
                segments.append(Segment(type=seg_type, data=dict(seg_data)))
        return segments

    if isinstance(raw_message, str):
        segments = []
        pos = 0
        for match in _CQ_PATTERN.finditer(raw_message):
            if match.start() > pos:
                segments.append(
                    Segment(type="text", data={"text": _cq_unescape(raw_message[pos : match.start()])})
                )
            cq_type = match.group(1)
            params_str = match.group(2) or ""
            data: dict[str, Any] = {}
            if params_str:
                for pair in params_str.split(","):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        data[k.strip()] = _cq_unescape(v)
            segments.append(Segment(type=cq_type, data=data))
            pos = match.end()
        if pos < len(raw_message):
            segments.append(Segment(type="text", data={"text": _cq_unescape(raw_message[pos:])}))
        return segments

    return []


def _maybe_int(value: str) -> int | str:
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _connect_headers_kwarg(connect_fn: WsConnectFn, headers: dict[str, str]) -> dict[str, Any]:
    if not headers:
        return {}
    try:
        params = inspect.signature(connect_fn).parameters
        if "additional_headers" in params:
            return {"additional_headers": headers}
        if "extra_headers" in params:
            return {"extra_headers": headers}
    except (TypeError, ValueError):
        pass
    return {"additional_headers": headers}


class OneBotV11Adapter(PlatformAdapter):
    """Forward WebSocket adapter for NapCat / standard OneBot v11 implementations."""

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
        self._pending_rpc: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self._self_id: str = config.self_id
        self._on_message: OnMessageFn | None = None
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def self_id(self) -> str:
        return self._self_id

    async def start(self, on_message: OnMessageFn) -> None:
        self._on_message = on_message
        self._stop_event.clear()
        self._worker_task = asyncio.create_task(self._consume_events(), name=f"{self.adapter_id}-worker")

        connect_fn = self._ws_connect or default_ws_connect()
        backoff = max(0.5, self.config.reconnect_initial_seconds)

        try:
            while not self._stop_event.is_set():
                headers: dict[str, str] = {}
                if self.config.access_token:
                    headers["Authorization"] = f"Bearer {self.config.access_token}"
                connect_kwargs = _connect_headers_kwarg(connect_fn, headers)

                try:
                    logger.info("[%s] 正在连接 NapCat 正向 WS: %s", self.adapter_id, self.config.ws_url)
                    async with connect_fn(self.config.ws_url, **connect_kwargs) as ws:
                        self._ws = ws
                        backoff = max(0.5, self.config.reconnect_initial_seconds)
                        logger.info("[%s] NapCat 正向 WS 已连接", self.adapter_id)
                        asyncio.create_task(self._refresh_login_info(), name=f"{self.adapter_id}-login-info")

                        async for raw_frame in ws:
                            if self._stop_event.is_set():
                                break
                            self._handle_raw_frame(raw_frame)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    logger.warning("[%s] WebSocket 连接断开/异常: %s，%.1fs 后重连", self.adapter_id, exc, backoff)
                finally:
                    self._ws = None
                    self._fail_pending_rpcs("WebSocket disconnected")

                if self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(self.config.reconnect_max_seconds, backoff * 2)
        finally:
            if self._worker_task is not None:
                self._worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._worker_task
                self._worker_task = None

    async def stop(self) -> None:
        self._stop_event.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._fail_pending_rpcs("Adapter stopped")

    def _fail_pending_rpcs(self, reason: str) -> None:
        for echo, fut in list(self._pending_rpc.items()):
            if not fut.done():
                fut.set_exception(ConnectionError(reason))
            self._pending_rpc.pop(echo, None)

    def _handle_raw_frame(self, raw_frame: str | bytes) -> None:
        if isinstance(raw_frame, bytes):
            raw_frame = raw_frame.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw_frame)
        except json.JSONDecodeError:
            logger.debug("[%s] 忽略非 JSON 帧: %s", self.adapter_id, raw_frame[:200])
            return
        if not isinstance(payload, dict):
            return

        echo = payload.get("echo")
        if echo is not None and str(echo) in self._pending_rpc:
            fut = self._pending_rpc.pop(str(echo))
            if not fut.done():
                fut.set_result(payload)
            return

        post_type = payload.get("post_type")
        if post_type not in ("message", "message_sent"):
            return

        if payload.get("self_id") and not self._self_id:
            self._self_id = str(payload["self_id"])

        try:
            self._event_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("[%s] 事件队列已满，丢弃一条消息", self.adapter_id)

    async def _consume_events(self) -> None:
        while not self._stop_event.is_set():
            payload = await self._event_queue.get()
            try:
                msg = await self.normalize_event(payload)
                if msg is not None and self._on_message is not None:
                    await self._on_message(msg)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[%s] 处理消息事件失败", self.adapter_id)
            finally:
                self._event_queue.task_done()

    async def _refresh_login_info(self) -> None:
        if self._self_id:
            return
        try:
            resp = await self.call_action("get_login_info", {}, timeout=self.config.rpc_timeout_seconds)
            data = resp.get("data") or {}
            if isinstance(data, dict) and data.get("user_id"):
                self._self_id = str(data["user_id"])
                logger.info("[%s] 自动获取机器人 QQ: %s (%s)", self.adapter_id, self._self_id, data.get("nickname", ""))
        except Exception as exc:
            logger.debug("[%s] get_login_info 未能自动获取 self_id: %s", self.adapter_id, exc)

    async def call_action(
        self,
        action: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        if self._ws is None:
            raise ConnectionError(f"[{self.adapter_id}] WebSocket 未连接")
        echo = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_rpc[echo] = fut
        packet = {"action": action, "params": params, "echo": echo}
        try:
            await self._ws.send(json.dumps(packet, ensure_ascii=False))
            effective_timeout = timeout if timeout is not None else self.config.rpc_timeout_seconds
            return await asyncio.wait_for(fut, timeout=effective_timeout)
        finally:
            self._pending_rpc.pop(echo, None)

    async def _resolve_reply(self, reply_msg_id: str) -> tuple[str | None, str | None]:
        if self._message_lookup is not None:
            cached = self._message_lookup(self.adapter_id, reply_msg_id)
            if cached is not None:
                return cached.user_id, cached.text

        if self._ws is None:
            return None, None

        try:
            resp = await self.call_action(
                "get_msg",
                {"message_id": _maybe_int(reply_msg_id)},
                timeout=min(3.0, self.config.rpc_timeout_seconds),
            )
        except Exception as exc:
            logger.debug("[%s] get_msg(%s) 查询失败: %s", self.adapter_id, reply_msg_id, exc)
            return None, None

        if resp.get("status") != "ok" and resp.get("retcode") != 0:
            return None, None

        data = resp.get("data")
        if not isinstance(data, dict):
            return None, None

        sender = data.get("sender")
        reply_user_id: str | None = None
        if isinstance(sender, dict) and sender.get("user_id") is not None:
            reply_user_id = str(sender["user_id"])
        elif data.get("user_id") is not None:
            reply_user_id = str(data["user_id"])

        raw_quoted = data.get("message") if data.get("message") is not None else data.get("raw_message", "")
        quoted_segments = parse_onebot_segments(raw_quoted)
        quoted_text = self._render_segments_text(quoted_segments, quoted_bot=False)
        return reply_user_id, quoted_text

    def _render_segments_text(
        self,
        segments: list[Segment],
        *,
        quoted_bot: bool,
    ) -> str:
        """Render OneBot segments to clean human-readable text.

        When `quoted_bot` is True, QQ automatically injects one `@bot` segment right after `reply`.
        Skipping that one auto-injected `@bot` avoids noisy `[@123456]` prefixes in context/keywords.
        """
        parts: list[str] = []
        skipped_quote_auto_at = False
        self_id = self._self_id

        for seg in segments:
            if seg.type == "text":
                parts.append(str(seg.data.get("text", "")))
            elif seg.type == "at":
                qq = str(seg.data.get("qq", ""))
                if quoted_bot and not skipped_quote_auto_at and self_id and qq == self_id:
                    skipped_quote_auto_at = True
                    continue
                if qq == "all":
                    parts.append("[@全体成员]")
                elif self_id and qq == self_id:
                    parts.append("[@机器人]")
                else:
                    name = str(seg.data.get("name", "")).strip()
                    parts.append(f"[@{name or qq}]")
            elif seg.type == "reply":
                continue
            elif seg.type == "image":
                parts.append("[图片]")
            elif seg.type == "face":
                parts.append("[表情]")
            elif seg.type == "record":
                parts.append("[语音]")
            elif seg.type == "video":
                parts.append("[视频]")
            elif seg.type == "file":
                parts.append(f"[文件:{seg.data.get('name', '')}]")
            elif seg.type == "forward":
                parts.append("[合并转发消息]")
            elif seg.type in ("json", "xml"):
                parts.append("[卡片消息]")

        return re.sub(r"\s+", " ", "".join(parts)).strip()

    async def normalize_event(self, event: dict[str, Any]) -> IncomingMessage | None:
        msg_type = str(event.get("message_type", ""))
        if msg_type not in ("group", "private"):
            return None

        scene: SceneType = "group" if msg_type == "group" else "private"
        self_id = str(event.get("self_id") or self._self_id or "")
        if self_id and not self._self_id:
            self._self_id = self_id

        user_id = str(event.get("user_id", ""))
        channel_id = str(event.get("group_id", "")) if scene == "group" else user_id
        message_id = str(event.get("message_id", ""))
        if not channel_id or not user_id:
            return None

        is_self = event.get("post_type") == "message_sent" or (bool(self_id) and user_id == self_id)

        sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
        sender_name = (
            str(sender.get("card") or "").strip()
            or str(sender.get("nickname") or "").strip()
            or user_id
        )

        raw_payload = event.get("message")
        if raw_payload is None:
            raw_payload = event.get("raw_message", "")
        segments = parse_onebot_segments(raw_payload)

        at_targets: list[str] = []
        reply_to_msg_id: str | None = None
        for seg in segments:
            if seg.type == "at" and seg.data.get("qq") is not None:
                at_targets.append(str(seg.data["qq"]))
            elif seg.type == "reply" and reply_to_msg_id is None and seg.data.get("id") is not None:
                reply_to_msg_id = str(seg.data["id"])

        reply_to_user_id: str | None = None
        reply_to_text: str | None = None
        if reply_to_msg_id:
            reply_to_user_id, reply_to_text = await self._resolve_reply(reply_to_msg_id)

        is_quoted_bot = bool(reply_to_msg_id and reply_to_user_id and self_id and reply_to_user_id == self_id)
        text = self._render_segments_text(segments, quoted_bot=is_quoted_bot)

        ts_raw = event.get("time")
        timestamp = float(ts_raw) if isinstance(ts_raw, (int, float)) else time.time()

        return IncomingMessage(
            adapter_id=self.adapter_id,
            platform=self.platform_name,
            self_id=self_id,
            scene=scene,
            channel_id=channel_id,
            message_id=message_id,
            user_id=user_id,
            sender_name=sender_name,
            text=text,
            timestamp=timestamp,
            raw_segments=segments,
            at_targets=at_targets,
            reply_to_msg_id=reply_to_msg_id,
            reply_to_user_id=reply_to_user_id,
            reply_to_text=reply_to_text,
            is_self=is_self,
            raw_event=event,
        )

    async def send_reply(
        self,
        target: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        segments: list[dict[str, Any]] = []
        if quote_trigger and target.message_id:
            segments.append({"type": "reply", "data": {"id": str(target.message_id)}})
        if at_user and target.scene == "group" and target.user_id:
            segments.append({"type": "at", "data": {"qq": str(target.user_id)}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": text}})

        if target.scene == "group":
            resp = await self.call_action(
                "send_group_msg",
                {"group_id": _maybe_int(target.channel_id), "message": segments},
            )
        else:
            resp = await self.call_action(
                "send_private_msg",
                {"user_id": _maybe_int(target.channel_id), "message": segments},
            )

        data = resp.get("data")
        if isinstance(data, dict) and data.get("message_id") is not None:
            return str(data["message_id"])
        return None
