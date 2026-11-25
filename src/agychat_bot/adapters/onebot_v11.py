from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import inspect
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from ..config import PlatformConfig
from ..models import IncomingMessage, MediaAttachment, SceneType, Segment
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


def _normalize_timestamp(ts_raw: Any) -> float:
    """Safely normalize epoch seconds/milliseconds into a valid POSIX timestamp."""
    if isinstance(ts_raw, bool) or ts_raw is None:
        return time.time()
    try:
        val = float(ts_raw)
    except (TypeError, ValueError):
        return time.time()
    if val > 1e11:
        val /= 1000.0
    # Guard Windows datetime.fromtimestamp range (reject negative / pre-1970-01-02 / post-2100)
    if val < 86400.0 or val > 4102444800.0:
        return time.time()
    return val


_MAX_FORWARD_DEPTH = 6
_MAX_FORWARD_NODES = 200


def _is_loopback_ws_url(ws_url: str) -> bool:
    host = (urlparse(ws_url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _connect_headers_kwarg(
    connect_fn: WsConnectFn,
    headers: dict[str, str],
    ws_url: str = "",
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}
    params: Any = None
    try:
        params = inspect.signature(connect_fn).parameters
    except (TypeError, ValueError):
        params = None

    if headers:
        if params is not None and "additional_headers" in params:
            kwargs["additional_headers"] = headers
        elif params is not None and "extra_headers" in params:
            kwargs["extra_headers"] = headers
        else:
            kwargs["additional_headers"] = headers

    if ws_url and _is_loopback_ws_url(ws_url) and params is not None and "proxy" in params:
        kwargs["proxy"] = None

    return kwargs


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
        self._login_info_task: asyncio.Task[None] | None = None

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
                connect_kwargs = _connect_headers_kwarg(connect_fn, headers, self.config.ws_url)

                connected_at = 0.0
                received_frames = 0
                had_exc = False
                try:
                    logger.info("[%s] 正在连接 OneBot v11 正向 WS: %s", self.adapter_id, self.config.ws_url)
                    async with connect_fn(self.config.ws_url, **connect_kwargs) as ws:
                        self._ws = ws
                        connected_at = time.monotonic()
                        logger.info("[%s] OneBot v11 正向 WS 已连接", self.adapter_id)
                        if self._login_info_task is not None and not self._login_info_task.done():
                            self._login_info_task.cancel()
                        self._login_info_task = asyncio.create_task(
                            self._refresh_login_info(),
                            name=f"{self.adapter_id}-login-info",
                        )

                        async for raw_frame in ws:
                            if self._stop_event.is_set():
                                break
                            received_frames += 1
                            if received_frames == 1:
                                backoff = max(0.5, self.config.reconnect_initial_seconds)
                            self._handle_raw_frame(raw_frame)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    had_exc = True
                    if self._stop_event.is_set():
                        break
                    logger.warning("[%s] WebSocket 连接断开/异常: %s，%.1fs 后重连", self.adapter_id, exc, backoff)
                finally:
                    self._ws = None
                    if self._login_info_task is not None and not self._login_info_task.done():
                        self._login_info_task.cancel()
                        with contextlib.suppress(BaseException):
                            await self._login_info_task
                    self._fail_pending_rpcs("WebSocket disconnected")

                if self._stop_event.is_set():
                    break
                if connected_at > 0.0:
                    alive_sec = time.monotonic() - connected_at
                    if alive_sec >= 5.0 and received_frames > 0:
                        backoff = max(0.5, self.config.reconnect_initial_seconds)
                    elif not had_exc and received_frames == 0 and alive_sec < 3.0:
                        logger.warning(
                            "[%s] 连接刚建立即被客户端断开 (%.2fs)，常见原因是 Access Token 未填写或与客户端不一致；%.1fs 后重连（可在 WebUI「全局配置 → 平台」修改并热重载）",
                            self.adapter_id,
                            alive_sec,
                            backoff,
                        )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(self.config.reconnect_max_seconds, backoff * 2)
        finally:
            if self._login_info_task is not None:
                self._login_info_task.cancel()
                with contextlib.suppress(BaseException):
                    await self._login_info_task
                self._login_info_task = None
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
                fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
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
        if post_type not in ("message", "message_sent", "notice"):
            return

        if payload.get("self_id") and not self._self_id:
            self._self_id = str(payload["self_id"])

        try:
            self._event_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("[%s] 事件队列已满，丢弃一条消息", self.adapter_id)

    async def _handle_notice_event(self, payload: dict[str, Any]) -> None:
        notice_type = str(payload.get("notice_type") or "").strip().lower()
        if notice_type != "group_ban":
            return
        group_id = str(payload.get("group_id") or "").strip()
        if not group_id:
            return
        self_id = str(payload.get("self_id") or self._self_id or "").strip()
        target_uid = str(payload.get("user_id") or "").strip()
        sub_type = str(payload.get("sub_type") or "").strip().lower()
        try:
            duration = float(payload.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0

        # Trigger on direct bot mute (user_id == self_id) or whole-group mute (user_id == "0")
        affects_bot = (bool(self_id) and target_uid == self_id) or target_uid == "0"
        if not affects_bot or self._on_group_ban is None:
            return

        if sub_type == "ban":
            reason = f"notice:group_ban(user={target_uid},duration={int(duration)}s)"
            await self._on_group_ban(self.adapter_id, group_id, True, duration, reason)
        elif sub_type == "lift_ban":
            reason = f"notice:group_ban_lift(user={target_uid})"
            await self._on_group_ban(self.adapter_id, group_id, False, 0.0, reason)

    @staticmethod
    def _is_muted_send_error(resp_or_err: Any) -> str:
        if isinstance(resp_or_err, dict):
            status = str(resp_or_err.get("status") or "").lower()
            retcode = resp_or_err.get("retcode", 0)
            if status != "failed" and retcode in (0, None):
                return ""
            text = " ".join(
                str(resp_or_err.get(k) or "")
                for k in ("wording", "msg", "message", "error")
            ).strip()
        else:
            text = str(resp_or_err or "").strip()
        low = text.lower()
        if any(tok in low for tok in ("禁言", "muted", "shut up", "shutup", "group_ban", "banned")):
            return text or "group_muted"
        return ""

    async def _consume_events(self) -> None:
        while not self._stop_event.is_set():
            payload = await self._event_queue.get()
            try:
                if payload.get("post_type") == "notice":
                    await self._handle_notice_event(payload)
                else:
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

    async def resolve_image_source(self, attachment: MediaAttachment) -> dict[str, str]:
        """Query NapCat / OneBot v11 `get_image` RPC for refreshed URL, local path, or base64."""
        if self._ws is None or not attachment.file_id:
            return {}
        try:
            resp = await self.call_action(
                "get_image",
                {"file": attachment.file_id},
                timeout=max(10.0, self.config.rpc_timeout_seconds),
            )
        except Exception as exc:
            logger.debug("[%s] get_image(%s) 查询失败: %s", self.adapter_id, attachment.file_id, exc)
            return {}
        if resp.get("status") != "ok" and resp.get("retcode") != 0:
            return {}
        data = resp.get("data")
        if not isinstance(data, dict):
            return {}
        return {
            "file": str(data.get("file") or data.get("path") or "").strip(),
            "url": str(data.get("url") or "").strip(),
            "base64": str(data.get("base64") or "").strip(),
        }

    async def _fetch_forward_nodes(self, fwd_id: str) -> list[Any] | None:
        """Fetch merged-forward message nodes via NapCat / OneBot v11 `get_forward_msg`."""
        if self._ws is None or not fwd_id:
            return None
        rpc_timeout = max(30.0, self.config.rpc_timeout_seconds)
        param_variants = [{"id": fwd_id}, {"message_id": fwd_id}]
        for params in param_variants:
            try:
                resp = await self.call_action("get_forward_msg", params, timeout=rpc_timeout)
            except Exception as exc:
                logger.debug("[%s] get_forward_msg(%s) 查询失败: %s", self.adapter_id, params, exc)
                continue
            if resp.get("status") != "ok" and resp.get("retcode") != 0:
                continue
            data = resp.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("messages", "message", "nodes"):
                    val = data.get(key)
                    if isinstance(val, list):
                        return val
        return None

    async def _expand_forward_nodes(
        self,
        nodes: list[Any],
        *,
        visited_forwards: set[str],
        attachments: list[MediaAttachment],
        attachment_source: Literal["message", "reply", "forward"],
        depth: int = 0,
        node_budget: list[int] | None = None,
    ) -> str:
        """Recursively expand nodes in a merged-forward message with depth & node-count guards."""
        indent = "  " * depth
        if depth >= _MAX_FORWARD_DEPTH:
            return f"{indent}[合并转发嵌套超过 {_MAX_FORWARD_DEPTH} 层，已截断]"
        budget = node_budget if node_budget is not None else [_MAX_FORWARD_NODES]
        lines: list[str] = [f"{indent}[合并转发聊天记录 开始]"]

        for node in nodes:
            if budget[0] <= 0:
                lines.append(f"{indent}[合并转发节点超过 {_MAX_FORWARD_NODES} 条上限，后续已截断]")
                break
            if not isinstance(node, dict):
                continue
            budget[0] -= 1

            # Support both OneBot `{"type":"node","data":{...}}` and NapCat full message objects
            payload = node.get("data") if (node.get("type") == "node" and isinstance(node.get("data"), dict)) else node
            if not isinstance(payload, dict):
                continue

            sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
            uid = str(
                payload.get("user_id")
                or payload.get("uin")
                or sender.get("user_id")
                or ""
            ).strip()
            name = (
                str(sender.get("card") or "").strip()
                or str(sender.get("nickname") or "").strip()
                or str(payload.get("nickname") or "").strip()
                or str(payload.get("name") or "").strip()
                or uid
                or "群友"
            )
            ts_raw = payload.get("time")
            ts_prefix = ""
            if isinstance(ts_raw, (int, float)) and ts_raw > 0:
                try:
                    norm_ts = _normalize_timestamp(ts_raw)
                    ts_prefix = f"[{dt.datetime.fromtimestamp(norm_ts).strftime('%H:%M:%S')}] "
                except Exception:
                    ts_prefix = ""

            raw_content = payload.get("content")
            if raw_content is None:
                raw_content = payload.get("message")
            if raw_content is None:
                raw_content = payload.get("raw_message", "")

            node_segments = parse_onebot_segments(raw_content)
            rendered = await self._render_segments_async(
                node_segments,
                quoted_bot=False,
                visited_forwards=visited_forwards,
                attachments=attachments,
                attachment_source=attachment_source,
                depth=depth + 1,
                node_budget=budget,
            )
            speaker = f"{name}({uid})" if uid and uid != name else name
            if "\n" in rendered:
                body_lines = rendered.splitlines()
                first_line = body_lines[0]
                rest_lines = "\n".join(body_lines[1:])
                if first_line.strip().startswith("[合并转发聊天记录 开始]"):
                    lines.append(f"{indent}{ts_prefix}{speaker}:")
                    lines.append(rendered)
                else:
                    lines.append(f"{indent}{ts_prefix}{speaker}: {first_line}")
                    lines.append(rest_lines)
            else:
                lines.append(f"{indent}{ts_prefix}{speaker}: {rendered or '[空消息]'}")

        lines.append(f"{indent}[合并转发聊天记录 结束]")
        return "\n".join(lines)

    async def _resolve_reply(
        self,
        reply_msg_id: str,
    ) -> tuple[str | None, str | None, list[MediaAttachment]]:
        if self._message_lookup is not None:
            cached = self._message_lookup(self.adapter_id, reply_msg_id)
            if cached is not None:
                reply_atts = [
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
                return cached.user_id, cached.text, reply_atts

        if self._ws is None:
            return None, None, []

        try:
            resp = await self.call_action(
                "get_msg",
                {"message_id": _maybe_int(reply_msg_id)},
                timeout=max(5.0, self.config.rpc_timeout_seconds),
            )
        except Exception as exc:
            logger.debug("[%s] get_msg(%s) 查询失败: %s", self.adapter_id, reply_msg_id, exc)
            return None, None, []

        if resp.get("status") != "ok" and resp.get("retcode") != 0:
            return None, None, []

        data = resp.get("data")
        if not isinstance(data, dict):
            return None, None, []

        sender = data.get("sender")
        reply_user_id: str | None = None
        if isinstance(sender, dict) and sender.get("user_id") is not None:
            reply_user_id = str(sender["user_id"])
        elif data.get("user_id") is not None:
            reply_user_id = str(data["user_id"])

        raw_quoted = data.get("message") if data.get("message") is not None else data.get("raw_message", "")
        quoted_segments = parse_onebot_segments(raw_quoted)
        reply_atts: list[MediaAttachment] = []
        quoted_text = await self._render_segments_async(
            quoted_segments,
            quoted_bot=False,
            attachments=reply_atts,
            attachment_source="reply",
        )
        return reply_user_id, quoted_text, reply_atts

    @staticmethod
    def _normalize_rendered_text(parts: list[str]) -> str:
        raw = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
        if "\n" not in raw:
            return re.sub(r"[ \t]+", " ", raw).strip()
        cleaned_lines: list[str] = []
        for line in raw.split("\n"):
            # Preserve leading indentation used by nested merged-forward blocks
            leading_len = len(line) - len(line.lstrip(" "))
            body = re.sub(r"[ \t]+", " ", line[leading_len:]).rstrip()
            if body:
                cleaned_lines.append((" " * leading_len) + body)
        return "\n".join(cleaned_lines).strip()

    async def _render_segments_async(
        self,
        segments: list[Segment],
        *,
        quoted_bot: bool,
        visited_forwards: set[str] | None = None,
        attachments: list[MediaAttachment] | None = None,
        attachment_source: Literal["message", "reply", "forward"] = "message",
        depth: int = 0,
        node_budget: list[int] | None = None,
    ) -> str:
        """Render OneBot segments to readable text, recursively expanding forwards and collecting images.

        When `quoted_bot` is True, QQ automatically injects one `@bot` segment right after `reply`.
        Skipping that one auto-injected `@bot` avoids noisy `[@123456]` prefixes in context/keywords.
        """
        parts: list[str] = []
        skipped_quote_auto_at = False
        self_id = self._self_id
        visited = visited_forwards if visited_forwards is not None else set()

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
                url = str(seg.data.get("url") or "").strip()
                file_id = str(seg.data.get("file") or "").strip()
                source_path = str(seg.data.get("path") or "").strip()
                file_name = str(seg.data.get("filename") or file_id or "").strip()
                summary = str(seg.data.get("summary") or "").strip()
                if summary in ("[图片]", "图片"):
                    summary = ""
                if attachments is not None:
                    attachments.append(
                        MediaAttachment(
                            kind="image",
                            url=url,
                            file_id=file_id,
                            file_name=file_name,
                            source_path=source_path,
                            summary=summary,
                            source=attachment_source,
                        )
                    )
                parts.append("[图片]")
            elif seg.type == "mface":
                url = str(seg.data.get("url") or "").strip()
                summary = str(seg.data.get("summary") or "").strip()
                if url and attachments is not None:
                    attachments.append(
                        MediaAttachment(
                            kind="image",
                            url=url,
                            file_id=str(seg.data.get("emoji_id") or "").strip(),
                            file_name=f"mface_{seg.data.get('emoji_id', '')}.png",
                            summary=summary or "表情包",
                            source=attachment_source,
                        )
                    )
                    parts.append("[图片]")
                else:
                    parts.append(f"[表情:{summary}]" if summary else "[表情]")
            elif seg.type == "face":
                parts.append("[表情]")
            elif seg.type == "record":
                parts.append("[语音]")
            elif seg.type == "video":
                parts.append("[视频]")
            elif seg.type == "file":
                parts.append(f"[文件:{seg.data.get('name', '')}]")
            elif seg.type == "forward":
                inline_nodes = seg.data.get("content")
                fwd_id = str(
                    seg.data.get("id")
                    or seg.data.get("res_id")
                    or seg.data.get("resid")
                    or ""
                ).strip()
                if fwd_id and fwd_id in visited:
                    parts.append("[合并转发消息(循环引用已跳过)]")
                    continue

                nodes: list[Any] | None = None
                if isinstance(inline_nodes, list) and inline_nodes:
                    nodes = inline_nodes
                elif fwd_id:
                    nodes = await self._fetch_forward_nodes(fwd_id)

                if nodes:
                    next_visited = visited | ({fwd_id} if fwd_id else set())
                    fwd_source: Literal["message", "reply", "forward"] = (
                        "reply" if attachment_source == "reply" else "forward"
                    )
                    expanded = await self._expand_forward_nodes(
                        nodes,
                        visited_forwards=next_visited,
                        attachments=attachments if attachments is not None else [],
                        attachment_source=fwd_source,
                        depth=depth,
                        node_budget=node_budget,
                    )
                    parts.append(f"\n{expanded}\n")
                else:
                    parts.append("[合并转发消息]")
            elif seg.type in ("json", "xml"):
                parts.append("[卡片消息]")

        return self._normalize_rendered_text(parts)

    async def normalize_event(self, event: dict[str, Any]) -> IncomingMessage | None:
        msg_type = str(event.get("message_type", ""))
        if msg_type not in ("group", "private"):
            return None

        scene: SceneType = "group" if msg_type == "group" else "private"
        self_id = str(event.get("self_id") or self._self_id or "")
        if self_id and not self._self_id:
            self._self_id = self_id

        user_id = str(event.get("user_id") or "").strip()
        channel_id = str(event.get("group_id") or "").strip() if scene == "group" else user_id
        message_id = str(event.get("message_id") or "").strip()
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
        reply_attachments: list[MediaAttachment] = []
        if reply_to_msg_id:
            reply_to_user_id, reply_to_text, reply_attachments = await self._resolve_reply(reply_to_msg_id)

        is_quoted_bot = bool(reply_to_msg_id and reply_to_user_id and self_id and reply_to_user_id == self_id)
        msg_attachments: list[MediaAttachment] = []
        text = await self._render_segments_async(
            segments,
            quoted_bot=is_quoted_bot,
            attachments=msg_attachments,
            attachment_source="message",
        )

        ts_raw = event.get("time")
        timestamp = _normalize_timestamp(ts_raw)

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
        segments: list[dict[str, Any]] = []
        if quote_trigger and target.message_id:
            segments.append({"type": "reply", "data": {"id": str(target.message_id)}})
        if at_user and target.scene == "group" and target.user_id:
            segments.append({"type": "at", "data": {"qq": str(target.user_id)}})
            segments.append({"type": "text", "data": {"text": " "}})
        segments.append({"type": "text", "data": {"text": text}})

        if target.scene == "group":
            try:
                resp = await self.call_action(
                    "send_group_msg",
                    {"group_id": _maybe_int(target.channel_id), "message": segments},
                )
            except Exception as exc:
                mute_err = self._is_muted_send_error(exc)
                if mute_err and self._on_group_ban is not None:
                    await self._on_group_ban(self.adapter_id, target.channel_id, True, 0.0, f"send_error:{mute_err[:80]}")
                raise
            mute_err = self._is_muted_send_error(resp)
            if mute_err and self._on_group_ban is not None:
                await self._on_group_ban(self.adapter_id, target.channel_id, True, 0.0, f"send_failed:{mute_err[:80]}")
        else:
            resp = await self.call_action(
                "send_private_msg",
                {"user_id": _maybe_int(target.channel_id), "message": segments},
            )

        if isinstance(resp, dict) and (
            str(resp.get("status") or "").lower() == "failed"
            or resp.get("retcode") not in (0, None)
        ):
            detail = " ".join(
                str(resp.get(k) or "") for k in ("wording", "msg", "message", "error")
            ).strip()
            raise RuntimeError(
                f"OneBot 发送文本失败 (status={resp.get('status')}, retcode={resp.get('retcode')}): {detail or 'unknown'}"
            )

        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict) and data.get("message_id") is not None:
            return str(data["message_id"])
        return None

    async def send_image(
        self,
        target: IncomingMessage,
        image: bytes | str | Path,
        *,
        caption: str = "",
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        if isinstance(image, bytes):
            file_spec = "base64://" + base64.b64encode(image).decode("ascii")
        elif isinstance(image, Path):
            file_spec = "base64://" + base64.b64encode(image.read_bytes()).decode("ascii")
        else:
            raw_str = str(image).strip()
            if raw_str.lower().startswith(("http://", "https://", "base64://", "file://")):
                file_spec = raw_str
            else:
                p = Path(raw_str)
                if p.is_file():
                    file_spec = "base64://" + base64.b64encode(p.read_bytes()).decode("ascii")
                else:
                    file_spec = raw_str

        segments: list[dict[str, Any]] = []
        if quote_trigger and target.message_id:
            segments.append({"type": "reply", "data": {"id": str(target.message_id)}})
        if at_user and target.scene == "group" and target.user_id:
            segments.append({"type": "at", "data": {"qq": str(target.user_id)}})
            segments.append({"type": "text", "data": {"text": " "}})
        if caption.strip():
            segments.append({"type": "text", "data": {"text": caption.strip() + "\n"}})
        segments.append({"type": "image", "data": {"file": file_spec}})

        rpc_timeout = max(30.0, self.config.rpc_timeout_seconds)
        if target.scene == "group":
            try:
                resp = await self.call_action(
                    "send_group_msg",
                    {"group_id": _maybe_int(target.channel_id), "message": segments},
                    timeout=rpc_timeout,
                )
            except Exception as exc:
                mute_err = self._is_muted_send_error(exc)
                if mute_err and self._on_group_ban is not None:
                    await self._on_group_ban(self.adapter_id, target.channel_id, True, 0.0, f"send_image_error:{mute_err[:80]}")
                raise
            mute_err = self._is_muted_send_error(resp)
            if mute_err and self._on_group_ban is not None:
                await self._on_group_ban(self.adapter_id, target.channel_id, True, 0.0, f"send_image_failed:{mute_err[:80]}")
        else:
            resp = await self.call_action(
                "send_private_msg",
                {"user_id": _maybe_int(target.channel_id), "message": segments},
                timeout=rpc_timeout,
            )

        if isinstance(resp, dict) and (
            str(resp.get("status") or "").lower() == "failed"
            or resp.get("retcode") not in (0, None)
        ):
            detail = " ".join(
                str(resp.get(k) or "") for k in ("wording", "msg", "message", "error")
            ).strip()
            raise RuntimeError(
                f"OneBot 发送图片失败 (status={resp.get('status')}, retcode={resp.get('retcode')}): {detail or 'unknown'}"
            )

        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict) and data.get("message_id") is not None:
            return str(data["message_id"])
        return None
