from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

import aiohttp

from ..config import PlatformConfig
from ..models import IncomingMessage, MediaAttachment, Segment
from .base import MessageLookupFn, OnMessageFn, PlatformAdapter, WsConnectFn

logger = logging.getLogger(__name__)

_DEFAULT_TG_API_BASE = "https://api.telegram.org"


def normalize_telegram_api_base(raw_url: str) -> str:
    val = (raw_url or "").strip().rstrip("/")
    if not val or val.lower().startswith(("ws://", "wss://")):
        return _DEFAULT_TG_API_BASE
    if not val.lower().startswith(("http://", "https://")):
        return f"https://{val}"
    return val


def resolve_telegram_proxy(config: PlatformConfig, default_proxy: str = "") -> str:
    extra = config.extra if isinstance(config.extra, dict) else {}
    explicit = str(extra.get("proxy") or "").strip()
    if explicit:
        return explicit
    use_agy = extra.get("use_agy_proxy", True)
    if use_agy is False or str(use_agy).strip().lower() in ("0", "false", "no", "off"):
        return ""
    return (default_proxy or "").strip()


def _aiohttp_proxy_arg(proxy_url: str) -> str | None:
    p = (proxy_url or "").strip()
    if not p:
        return None
    if p.lower().startswith(("http://", "https://")):
        return p
    logger.warning("Telegram 适配器当前直接支持 HTTP(S) 代理（如 http://127.0.0.1:7890），已忽略非 HTTP 代理: %s", p)
    return None


async def probe_telegram_bot(
    token: str,
    *,
    api_base: str = "",
    proxy: str = "",
    timeout_seconds: float = 6.0,
) -> dict[str, Any]:
    """Probe Telegram Bot API via `getMe` to verify token, network/proxy reachability, and bot identity."""
    clean_tok = (token or "").strip()
    base = normalize_telegram_api_base(api_base)
    if not clean_tok:
        return {"ok": False, "error": "请先填写 Telegram Bot Token（从 @BotFather 获取）"}

    url = f"{base}/bot{clean_tok}/getMe"
    eff_proxy = _aiohttp_proxy_arg(proxy)
    timeout = aiohttp.ClientTimeout(total=max(2.0, float(timeout_seconds)))
    t0 = time.monotonic()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, proxy=eff_proxy) as resp:
                latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                data = await resp.json(content_type=None)
                if resp.status == 200 and isinstance(data, dict) and data.get("ok") and isinstance(data.get("result"), dict):
                    me = data["result"]
                    return {
                        "ok": True,
                        "latency_ms": latency_ms,
                        "api_base": base,
                        "proxy_used": eff_proxy or "",
                        "user_id": str(me.get("id") or ""),
                        "username": str(me.get("username") or ""),
                        "first_name": str(me.get("first_name") or ""),
                        "can_read_all_group_messages": bool(me.get("can_read_all_group_messages", False)),
                    }
                desc = data.get("description") if isinstance(data, dict) else f"HTTP {resp.status}"
                return {
                    "ok": False,
                    "latency_ms": latency_ms,
                    "api_base": base,
                    "proxy_used": eff_proxy or "",
                    "error": f"Telegram API 返回错误 (HTTP {resp.status}): {desc}",
                }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "api_base": base,
            "proxy_used": eff_proxy or "",
            "error": f"连接 Telegram API 超时 ({timeout_seconds:.0f}s)，请检查代理设置",
        }
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            "api_base": base,
            "proxy_used": eff_proxy or "",
            "error": f"连接 Telegram API 失败: {exc}",
        }


def _extract_tg_attachments(msg: dict[str, Any], *, source: str = "message") -> tuple[list[MediaAttachment], list[str]]:
    atts: list[MediaAttachment] = []
    tags: list[str] = []

    photos = msg.get("photo")
    if isinstance(photos, list) and photos:
        best = max(
            (p for p in photos if isinstance(p, dict) and p.get("file_id")),
            key=lambda p: int(p.get("file_size") or (int(p.get("width") or 0) * int(p.get("height") or 0))),
            default=None,
        )
        if best is not None:
            fid = str(best["file_id"])
            atts.append(
                MediaAttachment(
                    kind="image",
                    file_id=fid,
                    file_name=f"tg_{fid[:14]}.jpg",
                    source=source,  # type: ignore[arg-type]
                )
            )
            tags.append("[图片]")

    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        mime = str(doc.get("mime_type") or "").lower()
        fname = str(doc.get("file_name") or f"tg_{str(doc['file_id'])[:14]}")
        if mime.startswith("image/"):
            atts.append(
                MediaAttachment(
                    kind="image",
                    file_id=str(doc["file_id"]),
                    file_name=fname,
                    source=source,  # type: ignore[arg-type]
                )
            )
            tags.append("[图片]")
        elif mime.startswith("audio/"):
            atts.append(
                MediaAttachment(
                    kind="audio",
                    file_id=str(doc["file_id"]),
                    file_name=fname,
                    source=source,  # type: ignore[arg-type]
                )
            )
            tags.append("[语音]")

    sticker = msg.get("sticker")
    if isinstance(sticker, dict) and not sticker.get("is_animated") and not sticker.get("is_video"):
        fid = str(sticker.get("file_id") or "")
        if fid:
            emoji = str(sticker.get("emoji") or "")
            atts.append(
                MediaAttachment(
                    kind="image",
                    file_id=fid,
                    file_name=f"tg_sticker_{fid[:12]}.webp",
                    summary=f"贴纸{emoji}" if emoji else "贴纸",
                    source=source,  # type: ignore[arg-type]
                )
            )
            tags.append("[图片]")

    if isinstance(msg.get("voice"), dict) or isinstance(msg.get("audio"), dict):
        v = msg.get("voice") or msg.get("audio")
        if isinstance(v, dict) and v.get("file_id"):
            atts.append(
                MediaAttachment(
                    kind="audio",
                    file_id=str(v["file_id"]),
                    file_name=f"tg_voice_{str(v['file_id'])[:12]}.ogg",
                    source=source,  # type: ignore[arg-type]
                )
            )
            tags.append("[语音]")

    return atts, tags


def _sender_display_name(user_obj: dict[str, Any]) -> str:
    first = str(user_obj.get("first_name") or "").strip()
    last = str(user_obj.get("last_name") or "").strip()
    full = " ".join(p for p in (first, last) if p).strip()
    if full:
        return full
    uname = str(user_obj.get("username") or "").strip()
    if uname:
        return f"@{uname}"
    return str(user_obj.get("id") or "Telegram用户")


class TelegramAdapter(PlatformAdapter):
    """Official Telegram Bot API long-polling adapter (zero webhook/public-IP requirement)."""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        message_lookup: MessageLookupFn | None = None,
        ws_connect: WsConnectFn | None = None,
        default_proxy: str = "",
        session_factory: Any | None = None,
    ) -> None:
        super().__init__(config, message_lookup=message_lookup, ws_connect=ws_connect)
        self.self_id: str = config.self_id
        self.bot_username: str = ""
        self.connected: bool = False
        self._default_proxy = (default_proxy or "").strip()
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None
        self._stopped = False
        self._offset: int = 0
        self._seen_updates: set[int] = set()

    @property
    def api_base(self) -> str:
        return normalize_telegram_api_base(self.config.ws_url)

    @property
    def bot_token(self) -> str:
        return (self.config.access_token or "").strip()

    @property
    def effective_proxy(self) -> str | None:
        return _aiohttp_proxy_arg(resolve_telegram_proxy(self.config, self._default_proxy))

    def _endpoint(self, method: str) -> str:
        return f"{self.api_base}/bot{self.bot_token}/{method}"

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                self._session = aiohttp.ClientSession()
        return self._session

    async def _api_call(
        self,
        method: str,
        *,
        json_body: dict[str, Any] | None = None,
        form_data: aiohttp.FormData | None = None,
        timeout_seconds: float | None = None,
    ) -> Any:
        if not self.bot_token:
            raise RuntimeError(f"[{self.adapter_id}] 未配置 Telegram Bot Token")
        sess = await self._ensure_session()
        eff_to = float(timeout_seconds if timeout_seconds is not None else max(10.0, self.config.rpc_timeout_seconds))
        timeout = aiohttp.ClientTimeout(total=eff_to)
        kwargs: dict[str, Any] = {"proxy": self.effective_proxy, "timeout": timeout}
        if form_data is not None:
            kwargs["data"] = form_data
        elif json_body is not None:
            kwargs["json"] = json_body
        async with sess.post(self._endpoint(method), **kwargs) as resp:
            payload = await resp.json(content_type=None)
            if resp.status == 200 and isinstance(payload, dict) and payload.get("ok"):
                return payload.get("result")
            desc = payload.get("description") if isinstance(payload, dict) else f"HTTP {resp.status}"
            raise RuntimeError(f"Telegram API {method} 失败 (HTTP {resp.status}): {desc}")

    def parse_message(self, raw_msg: dict[str, Any]) -> IncomingMessage | None:
        if not isinstance(raw_msg, dict):
            return None
        chat = raw_msg.get("chat")
        if not isinstance(chat, dict):
            return None
        chat_type = str(chat.get("type") or "").strip().lower()
        if chat_type == "private":
            scene = "private"
        elif chat_type in ("group", "supergroup"):
            scene = "group"
        else:
            return None

        sender = raw_msg.get("from")
        if not isinstance(sender, dict):
            return None
        user_id = str(sender.get("id") or "").strip()
        channel_id = str(chat.get("id") or "").strip()
        if not user_id or not channel_id:
            return None

        if sender.get("is_bot") and user_id != self.self_id:
            return None

        message_id = str(raw_msg.get("message_id") or "")
        sender_name = _sender_display_name(sender)
        raw_text = str(raw_msg.get("text") or raw_msg.get("caption") or "").strip()
        ts = float(raw_msg.get("date") or time.time())

        body_atts, body_tags = _extract_tg_attachments(raw_msg, source="message")
        text = raw_text
        if body_tags:
            tag_str = " ".join(body_tags)
            text = f"{text} {tag_str}".strip() if text else tag_str

        # Detect explicit @bot mentions via Telegram entities + fallback text match
        at_targets: list[str] = []
        bot_uname_low = self.bot_username.lower().lstrip("@")
        entities = raw_msg.get("entities") or raw_msg.get("caption_entities") or []
        mentioned_bot = False
        if isinstance(entities, list):
            for ent in entities:
                if not isinstance(ent, dict):
                    continue
                etype = str(ent.get("type") or "")
                offset = int(ent.get("offset") or 0)
                length = int(ent.get("length") or 0)
                frag = raw_text[offset : offset + length] if length > 0 else ""
                if etype == "mention" and bot_uname_low and frag.lower().lstrip("@") == bot_uname_low:
                    mentioned_bot = True
                elif etype == "text_mention":
                    u = ent.get("user")
                    if isinstance(u, dict) and self.self_id and str(u.get("id") or "") == self.self_id:
                        mentioned_bot = True
                elif etype == "bot_command" and bot_uname_low and f"@{bot_uname_low}" in frag.lower():
                    mentioned_bot = True
        if not mentioned_bot and bot_uname_low and f"@{bot_uname_low}" in raw_text.lower():
            mentioned_bot = True
        if mentioned_bot and self.self_id:
            at_targets.append(self.self_id)

        # Reply / quote extraction
        reply_to_msg_id: str | None = None
        reply_to_user_id: str | None = None
        reply_to_text: str | None = None
        reply_atts: list[MediaAttachment] = []
        reply_obj = raw_msg.get("reply_to_message")
        if isinstance(reply_obj, dict):
            rmid = reply_obj.get("message_id")
            if rmid is not None:
                reply_to_msg_id = str(rmid)
            rfrom = reply_obj.get("from")
            if isinstance(rfrom, dict) and rfrom.get("id") is not None:
                reply_to_user_id = str(rfrom["id"])
            r_text = str(reply_obj.get("text") or reply_obj.get("caption") or "").strip()
            r_atts, r_tags = _extract_tg_attachments(reply_obj, source="reply")
            reply_atts.extend(r_atts)
            if r_tags:
                r_tag_str = " ".join(r_tags)
                r_text = f"{r_text} {r_tag_str}".strip() if r_text else r_tag_str
            reply_to_text = r_text or None
            if reply_to_msg_id and not reply_to_text and self._message_lookup is not None:
                cached = self._message_lookup(self.adapter_id, reply_to_msg_id)
                if cached is not None:
                    reply_to_user_id = reply_to_user_id or cached.user_id
                    reply_to_text = cached.text

        if not text and not body_atts and not reply_to_msg_id:
            return None

        raw_segments: list[Segment] = []
        if text:
            raw_segments.append(Segment(type="text", data={"text": text}))

        return IncomingMessage(
            adapter_id=self.adapter_id,
            platform="telegram",
            self_id=self.self_id,
            scene=scene,  # type: ignore[arg-type]
            channel_id=channel_id,
            message_id=message_id,
            user_id=user_id,
            sender_name=sender_name,
            text=text,
            timestamp=ts,
            raw_segments=raw_segments,
            at_targets=at_targets,
            reply_to_msg_id=reply_to_msg_id,
            reply_to_user_id=reply_to_user_id,
            reply_to_text=reply_to_text,
            is_self=(bool(self.self_id) and user_id == self.self_id),
            raw_event=raw_msg,
            attachments=[*body_atts, *reply_atts],
        )

    async def resolve_image_source(self, attachment: MediaAttachment) -> dict[str, str]:
        if attachment.url:
            return {"url": attachment.url}
        if not attachment.file_id or not self.bot_token:
            return {}
        try:
            res = await self._api_call("getFile", json_body={"file_id": attachment.file_id}, timeout_seconds=12.0)
            file_path = str(res.get("file_path") or "").lstrip("/") if isinstance(res, dict) else ""
            if not file_path:
                return {}
            download_url = f"{self.api_base}/file/bot{self.bot_token}/{file_path}"
            try:
                sess = await self._ensure_session()
                timeout = aiohttp.ClientTimeout(total=20.0)
                async with sess.get(download_url, proxy=self.effective_proxy, timeout=timeout) as resp:
                    if resp.status == 200:
                        chunks = bytearray()
                        async for chunk in resp.content.iter_chunked(65536):
                            chunks.extend(chunk)
                            if len(chunks) > 20 * 1024 * 1024:
                                return {}
                        if chunks:
                            return {
                                "base64": base64.b64encode(bytes(chunks)).decode("ascii"),
                                "url": download_url,
                            }
            except Exception as dl_exc:
                logger.debug("[%s] Telegram 图片代理下载回退 URL: %s", self.adapter_id, dl_exc)
            return {"url": download_url}
        except Exception as exc:
            logger.debug("[%s] Telegram getFile(%s) 失败: %s", self.adapter_id, attachment.file_id[:16], exc)
        return {}

    async def start(self, on_message: OnMessageFn) -> None:
        self._stopped = False
        backoff = max(1.0, float(self.config.reconnect_initial_seconds or 2.0))
        max_backoff = max(backoff, float(self.config.reconnect_max_seconds or 60.0))
        extra = self.config.extra if isinstance(self.config.extra, dict) else {}
        try:
            poll_timeout = max(5, min(50, int(extra.get("poll_timeout_seconds", 25) or 25)))
        except (TypeError, ValueError):
            poll_timeout = 25

        while not self._stopped:
            if not self.bot_token:
                self.connected = False
                logger.warning("[%s] Telegram Bot Token 为空，等待配置更新...", self.adapter_id)
                await asyncio.sleep(min(10.0, max_backoff))
                continue
            try:
                me = await self._api_call("getMe", timeout_seconds=max(10.0, self.config.rpc_timeout_seconds))
                if isinstance(me, dict):
                    self.self_id = str(me.get("id") or self.self_id)
                    self.bot_username = str(me.get("username") or "")
                    first_name = str(me.get("first_name") or "")
                    privacy_off = bool(me.get("can_read_all_group_messages", False))
                    self.connected = True
                    backoff = max(1.0, float(self.config.reconnect_initial_seconds or 2.0))
                    logger.info(
                        "[%s] Telegram 机器人已上线: %s (@%s, id=%s, 群聊隐私关闭=%s, 代理=%s)",
                        self.adapter_id,
                        first_name or self.bot_username,
                        self.bot_username or "-",
                        self.self_id,
                        privacy_off,
                        self.effective_proxy or "直连",
                    )
                with contextlib.suppress(Exception):
                    await self._api_call("deleteWebhook", json_body={"drop_pending_updates": False}, timeout_seconds=10.0)

                consecutive_timeouts = 0
                while not self._stopped:
                    params: dict[str, Any] = {
                        "timeout": poll_timeout,
                        "allowed_updates": ["message"],
                    }
                    if self._offset > 0:
                        params["offset"] = self._offset
                    try:
                        updates = await self._api_call(
                            "getUpdates",
                            json_body=params,
                            timeout_seconds=poll_timeout + 20.0,
                        )
                    except (asyncio.TimeoutError, TimeoutError):
                        consecutive_timeouts += 1
                        if consecutive_timeouts < 3:
                            logger.debug(
                                "[%s] Telegram 长轮询等待超时 (%d/3)，继续下一轮轮询",
                                self.adapter_id,
                                consecutive_timeouts,
                            )
                            continue
                        raise
                    consecutive_timeouts = 0
                    if not isinstance(updates, list):
                        continue
                    for upd in updates:
                        if not isinstance(upd, dict):
                            continue
                        uid = int(upd.get("update_id") or 0)
                        if uid >= self._offset:
                            self._offset = uid + 1
                        if uid in self._seen_updates:
                            continue
                        self._seen_updates.add(uid)
                        if len(self._seen_updates) > 2000:
                            self._seen_updates = set(sorted(self._seen_updates)[-1000:])
                        raw_msg = upd.get("message")
                        if isinstance(raw_msg, dict):
                            parsed = self.parse_message(raw_msg)
                            if parsed is not None:
                                try:
                                    await on_message(parsed)
                                except Exception:
                                    logger.exception("[%s] 处理 Telegram 消息异常", self.adapter_id)
            except asyncio.CancelledError:
                self.connected = False
                raise
            except Exception as exc:
                self.connected = False
                if self._stopped:
                    break
                err_desc = str(exc).strip() or type(exc).__name__
                logger.warning("[%s] Telegram 长轮询连接异常: %s；%.1f 秒后重连", self.adapter_id, err_desc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(max_backoff, backoff * 2.0)

        self.connected = False

    async def stop(self) -> None:
        self._stopped = True
        self.connected = False
        if self._session is not None and not self._session.closed:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None

    @staticmethod
    def _coerce_chat_id(channel_id: str) -> int | str:
        s = str(channel_id or "").strip()
        try:
            return int(s)
        except ValueError:
            return s

    @staticmethod
    def _reply_params(target: IncomingMessage, quote_trigger: bool) -> dict[str, Any] | None:
        if not quote_trigger or not target.message_id:
            return None
        try:
            mid = int(target.message_id)
        except ValueError:
            return None
        return {"message_id": mid, "allow_sending_without_reply": True}

    async def send_reply(
        self,
        target: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        clean = (text or "").strip()
        if not clean:
            return None
        chat_id = self._coerce_chat_id(target.channel_id)
        chunks = [clean[i : i + 4000] for i in range(0, len(clean), 4000)]
        last_id: str | None = None
        for idx, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
            }
            if idx == 0:
                rp = self._reply_params(target, quote_trigger)
                if rp is not None:
                    payload["reply_parameters"] = rp
            res = await self._api_call("sendMessage", json_body=payload, timeout_seconds=max(15.0, self.config.rpc_timeout_seconds))
            if isinstance(res, dict) and res.get("message_id") is not None:
                last_id = str(res["message_id"])
        return last_id

    async def send_image(
        self,
        target: IncomingMessage,
        image: bytes | str | Path,
        *,
        caption: str = "",
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        chat_id = self._coerce_chat_id(target.channel_id)
        raw_bytes: bytes | None = None
        url_str: str | None = None
        filename = "image.png"

        if isinstance(image, bytes):
            raw_bytes = image
        elif isinstance(image, Path):
            raw_bytes = image.read_bytes()
            filename = image.name or filename
        else:
            s = str(image or "").strip()
            if s.lower().startswith(("http://", "https://")):
                url_str = s
            elif s.lower().startswith("base64://"):
                raw_bytes = base64.b64decode(s[len("base64://") :])
            else:
                p = Path(s)
                if p.is_file():
                    raw_bytes = p.read_bytes()
                    filename = p.name or filename
                else:
                    raise ValueError(f"无法识别的图片来源: {s[:80]}")

        is_gif = False
        if raw_bytes is not None:
            is_gif = raw_bytes[:6] in (b"GIF87a", b"GIF89a")
            if is_gif:
                filename = "animation.gif"
            elif raw_bytes[:3] == b"\xff\xd8\xff":
                filename = "image.jpg"
            elif raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
                filename = "image.webp"
        elif url_str:
            is_gif = url_str.lower().split("?", 1)[0].endswith(".gif")

        method = "sendAnimation" if is_gif else "sendPhoto"
        field_name = "animation" if is_gif else "photo"
        cap_trim = (caption or "").strip()[:1020]
        rp = self._reply_params(target, quote_trigger)

        if raw_bytes is not None:
            form = aiohttp.FormData()
            form.add_field("chat_id", str(chat_id))
            if cap_trim:
                form.add_field("caption", cap_trim)
            if rp is not None:
                import json as _json
                form.add_field("reply_parameters", _json.dumps(rp))
            form.add_field(field_name, raw_bytes, filename=filename)
            res = await self._api_call(method, form_data=form, timeout_seconds=45.0)
        else:
            payload: dict[str, Any] = {"chat_id": chat_id, field_name: url_str}
            if cap_trim:
                payload["caption"] = cap_trim
            if rp is not None:
                payload["reply_parameters"] = rp
            res = await self._api_call(method, json_body=payload, timeout_seconds=30.0)

        if isinstance(res, dict) and res.get("message_id") is not None:
            return str(res["message_id"])
        return None
