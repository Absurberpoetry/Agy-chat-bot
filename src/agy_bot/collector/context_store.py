from __future__ import annotations

import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import CachedMessage, IncomingMessage, MediaAttachment

logger = logging.getLogger(__name__)


def estimate_text_tokens_and_bytes(text: str) -> tuple[int, int]:
    """Estimate Gemini token count and exact UTF-8 byte length for mixed CJK/ASCII chat text."""
    if not text:
        return 0, 0
    utf8_bytes = len(text.encode("utf-8"))
    cjk = 0
    other_chars = 0
    for ch in text:
        cp = ord(ch)
        if (
            0x4E00 <= cp <= 0x9FFF
            or 0x3400 <= cp <= 0x4DBF
            or 0x3000 <= cp <= 0x30FF
            or 0xFF00 <= cp <= 0xFFEF
            or 0xAC00 <= cp <= 0xD7AF
        ):
            cjk += 1
        else:
            other_chars += 1
    # CJK characters average ~0.75 tokens in Gemini SentencePiece; ASCII/code ~3.8 chars per token
    est_tok = max(1, int(round(cjk * 0.8 + other_chars / 3.8)))
    return est_tok, utf8_bytes


def estimate_message_metrics(msg: IncomingMessage) -> tuple[int, int]:
    """Compute (estimated_tokens, utf8_bytes) for a single chat message including quote & attachment tags."""
    header = f"[{msg.sender_name}({msg.user_id})]: "
    quote = f'[引用: "{msg.reply_to_text}"] ' if msg.reply_to_text else ""
    att_tags = " ".join(f"[图片:{a.local_path or a.file_name or 'img'}]" for a in msg.attachments)
    combined = f"{header}{quote}{msg.text} {att_tags}".strip()
    tok, b_len = estimate_text_tokens_and_bytes(combined)
    # Add small structural overhead per message line
    return tok + 6, b_len + 12


@dataclass(slots=True)
class ActiveWindowState:
    messages: list[IncomingMessage]
    active_min_seq: int = 0
    latest_seq: int = 0
    archived_count: int = 0
    archived_min_seq: int = 0
    archived_max_seq: int = 0
    total_tokens: int = 0
    total_bytes: int = 0
    compacted: bool = False
    dropped_count: int = 0
    compact_reasons: list[str] = field(default_factory=list)


def _serialize_attachment(att: MediaAttachment) -> dict[str, Any]:
    return {
        "kind": att.kind,
        "url": att.url,
        "file_id": att.file_id,
        "file_name": att.file_name,
        "source_path": att.source_path,
        "local_path": att.local_path,
        "summary": att.summary,
        "source": att.source,
    }


def _deserialize_attachment(raw: Any) -> MediaAttachment | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "image")
    if kind not in ("image", "audio", "video", "file"):
        kind = "image"
    src = str(raw.get("source") or "message")
    if src not in ("message", "reply", "forward"):
        src = "message"
    return MediaAttachment(
        kind=kind,  # type: ignore[arg-type]
        url=str(raw.get("url") or ""),
        file_id=str(raw.get("file_id") or ""),
        file_name=str(raw.get("file_name") or ""),
        source_path=str(raw.get("source_path") or ""),
        local_path=str(raw.get("local_path") or ""),
        summary=str(raw.get("summary") or ""),
        source=src,  # type: ignore[arg-type]
    )


def _serialize_message(msg: IncomingMessage) -> dict[str, Any]:
    return {
        "adapter_id": msg.adapter_id,
        "platform": msg.platform,
        "self_id": msg.self_id,
        "scene": msg.scene,
        "channel_id": msg.channel_id,
        "message_id": msg.message_id,
        "user_id": msg.user_id,
        "sender_name": msg.sender_name,
        "text": msg.text,
        "timestamp": msg.timestamp,
        "at_targets": list(msg.at_targets),
        "reply_to_msg_id": msg.reply_to_msg_id,
        "reply_to_user_id": msg.reply_to_user_id,
        "reply_to_text": msg.reply_to_text,
        "is_self": msg.is_self,
        "attachments": [_serialize_attachment(a) for a in msg.attachments],
        "db_seq": msg.db_seq,
    }


def _deserialize_message(raw: Any) -> IncomingMessage | None:
    if not isinstance(raw, dict):
        return None
    scene = str(raw.get("scene") or "group")
    if scene not in ("group", "private"):
        scene = "group"
    atts: list[MediaAttachment] = []
    for item in raw.get("attachments") or []:
        att = _deserialize_attachment(item)
        if att is not None:
            atts.append(att)
    try:
        ts = float(raw.get("timestamp") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    return IncomingMessage(
        adapter_id=str(raw.get("adapter_id") or ""),
        platform=str(raw.get("platform") or ""),
        self_id=str(raw.get("self_id") or ""),
        scene=scene,  # type: ignore[arg-type]
        channel_id=str(raw.get("channel_id") or ""),
        message_id=str(raw.get("message_id") or ""),
        user_id=str(raw.get("user_id") or ""),
        sender_name=str(raw.get("sender_name") or ""),
        text=str(raw.get("text") or ""),
        timestamp=ts,
        at_targets=[str(x) for x in (raw.get("at_targets") or []) if str(x)],
        reply_to_msg_id=raw.get("reply_to_msg_id") if isinstance(raw.get("reply_to_msg_id"), str) else None,
        reply_to_user_id=raw.get("reply_to_user_id") if isinstance(raw.get("reply_to_user_id"), str) else None,
        reply_to_text=raw.get("reply_to_text") if isinstance(raw.get("reply_to_text"), str) else None,
        is_self=bool(raw.get("is_self", False)),
        attachments=atts,
        db_seq=int(raw.get("db_seq") or 0),
    )


def _row_to_message(row: sqlite3.Row | tuple[Any, ...]) -> IncomingMessage:
    (
        seq_id,
        adapter_id,
        platform,
        self_id,
        scene,
        channel_id,
        message_id,
        user_id,
        sender_name,
        text,
        timestamp,
        at_targets_json,
        reply_to_msg_id,
        reply_to_user_id,
        reply_to_text,
        is_self,
        attachments_json,
    ) = row[:17]
    try:
        at_list = [str(x) for x in json.loads(at_targets_json or "[]") if str(x)]
    except Exception:
        at_list = []
    atts: list[MediaAttachment] = []
    try:
        for item in json.loads(attachments_json or "[]"):
            att = _deserialize_attachment(item)
            if att is not None:
                atts.append(att)
    except Exception:
        pass
    sc = str(scene or "group")
    if sc not in ("group", "private"):
        sc = "group"
    return IncomingMessage(
        adapter_id=str(adapter_id or ""),
        platform=str(platform or ""),
        self_id=str(self_id or ""),
        scene=sc,  # type: ignore[arg-type]
        channel_id=str(channel_id or ""),
        message_id=str(message_id or ""),
        user_id=str(user_id or ""),
        sender_name=str(sender_name or ""),
        text=str(text or ""),
        timestamp=float(timestamp or 0.0),
        at_targets=at_list,
        reply_to_msg_id=reply_to_msg_id if isinstance(reply_to_msg_id, str) and reply_to_msg_id else None,
        reply_to_user_id=reply_to_user_id if isinstance(reply_to_user_id, str) and reply_to_user_id else None,
        reply_to_text=reply_to_text if isinstance(reply_to_text, str) and reply_to_text else None,
        is_self=bool(is_self),
        attachments=atts,
        db_seq=int(seq_id or 0),
    )


_SELECT_MSG_COLS = (
    "seq_id, adapter_id, platform, self_id, scene, channel_id, message_id, "
    "user_id, sender_name, text, timestamp, at_targets_json, "
    "reply_to_msg_id, reply_to_user_id, reply_to_text, is_self, attachments_json, "
    "est_tokens, est_bytes"
)


class GroupContextStore:
    """SQLite-backed (with in-memory / JSON fallback) chat history & dual-trigger compaction store."""

    def __init__(
        self,
        *,
        history_limit: int = 100,
        msg_cache_limit: int = 5000,
        db_path: Path | None = None,
        persist_file: Path | None = None,
    ) -> None:
        self._history_limit = max(10, history_limit)
        self._msg_cache_limit = max(100, msg_cache_limit)
        if db_path is None and persist_file is not None and persist_file.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
            db_path = persist_file
            persist_file = None
        self._db_path: Path | None = db_path
        self._persist_file: Path | None = persist_file if db_path is None else None
        self._db_lock = threading.RLock()

        self._dirty = False
        self._last_save_mono = -1e9
        self._channels: dict[str, deque[IncomingMessage]] = defaultdict(
            lambda: deque(maxlen=self._history_limit)
        )
        self._msg_cache: OrderedDict[tuple[str, str], CachedMessage] = OrderedDict()
        self._ambient_unread: dict[str, int] = defaultdict(int)

        if self._db_path is not None:
            self._init_sqlite()
            if persist_file is not None and persist_file.exists():
                self._migrate_legacy_json(persist_file)
            else:
                sibling_json = self._db_path.parent / "context_history.json"
                if sibling_json.exists():
                    self._migrate_legacy_json(sibling_json)
            self._warm_recent_from_sqlite()
        elif self._persist_file is not None:
            self._load_from_json()

    @property
    def db_path(self) -> Path | None:
        return self._db_path

    @contextlib.contextmanager
    def _db_conn(self) -> Any:
        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=15.0)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def _init_sqlite(self) -> None:
        with self._db_lock, self._db_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    adapter_id TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    self_id TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    user_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL,
                    text TEXT NOT NULL,
                    timestamp REAL NOT NULL,
                    at_targets_json TEXT NOT NULL DEFAULT '[]',
                    reply_to_msg_id TEXT,
                    reply_to_user_id TEXT,
                    reply_to_text TEXT,
                    is_self INTEGER NOT NULL DEFAULT 0,
                    attachments_json TEXT NOT NULL DEFAULT '[]',
                    est_tokens INTEGER NOT NULL DEFAULT 1,
                    est_bytes INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session_seq ON messages(session_key, seq_id);
                CREATE INDEX IF NOT EXISTS idx_messages_adapter_msgid ON messages(adapter_id, message_id);
                CREATE INDEX IF NOT EXISTS idx_messages_session_ts ON messages(session_key, timestamp);

                CREATE TABLE IF NOT EXISTS session_cursors (
                    cursor_key TEXT PRIMARY KEY,
                    session_key TEXT NOT NULL,
                    active_min_seq INTEGER NOT NULL DEFAULT 0,
                    compaction_count INTEGER NOT NULL DEFAULT 0,
                    total_dropped_messages INTEGER NOT NULL DEFAULT 0,
                    last_compacted_at REAL NOT NULL DEFAULT 0
                );
                """
            )
            conn.commit()

    def _migrate_legacy_json(self, json_file: Path) -> None:
        """One-time non-destructive import of legacy context_history.json into empty SQLite DB."""
        if self._db_path is None:
            return
        with self._db_lock, self._db_conn() as conn:
            row = conn.execute("SELECT COUNT(1) FROM messages").fetchone()
            if row and int(row[0]) > 0:
                return
            try:
                raw = json.loads(json_file.read_text(encoding="utf-8"))
                ch_map = raw.get("channels") if isinstance(raw, dict) and isinstance(raw.get("channels"), dict) else raw
                if not isinstance(ch_map, dict):
                    return
                imported = 0
                for items in ch_map.values():
                    if not isinstance(items, list):
                        continue
                    for entry in items:
                        msg = _deserialize_message(entry)
                        if msg is not None and msg.adapter_id and msg.channel_id:
                            self._insert_sqlite_message(conn, msg)
                            imported += 1
                conn.commit()
                if imported:
                    logger.info("已将旧版 %s 中的 %d 条记录迁移至 SQLite 数据库", json_file.name, imported)
            except Exception as exc:
                logger.warning("迁移旧版上下文文件 %s 失败: %s", json_file, exc)

    def _warm_recent_from_sqlite(self) -> None:
        if self._db_path is None:
            return
        with self._db_lock, self._db_conn() as conn:
            skeys = [r[0] for r in conn.execute("SELECT DISTINCT session_key FROM messages").fetchall()]
            total_loaded = 0
            for sk in skeys:
                cur_row = conn.execute(
                    "SELECT active_min_seq FROM session_cursors WHERE cursor_key = ?",
                    (sk,),
                ).fetchone()
                min_seq = int(cur_row[0]) if cur_row else 0
                rows = conn.execute(
                    f"SELECT {_SELECT_MSG_COLS} FROM messages "
                    "WHERE session_key = ? AND seq_id >= ? ORDER BY seq_id DESC LIMIT ?",
                    (sk, min_seq, self._history_limit),
                ).fetchall()
                buf = self._channels[sk]
                for r in reversed(rows):
                    msg = _row_to_message(r)
                    buf.append(msg)
                    total_loaded += 1
                    if msg.message_id:
                        self.cache_message(
                            adapter_id=msg.adapter_id,
                            message_id=msg.message_id,
                            user_id=msg.user_id,
                            sender_name=msg.sender_name,
                            text=msg.text,
                            timestamp=msg.timestamp,
                            attachments=msg.attachments,
                        )
            if total_loaded:
                logger.info("已从 SQLite 数据库预热 %d 个会话共 %d 条活跃上下文消息", len(skeys), total_loaded)

    def _insert_sqlite_message(self, conn: sqlite3.Connection, msg: IncomingMessage) -> int:
        est_tok, est_bytes = estimate_message_metrics(msg)
        role = "assistant" if msg.is_self else "user"
        cur = conn.execute(
            """
            INSERT INTO messages (
                session_key, adapter_id, platform, self_id, scene, channel_id,
                message_id, role, user_id, sender_name, text, timestamp,
                at_targets_json, reply_to_msg_id, reply_to_user_id, reply_to_text,
                is_self, attachments_json, est_tokens, est_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.session_key,
                msg.adapter_id,
                msg.platform,
                msg.self_id,
                msg.scene,
                msg.channel_id,
                msg.message_id or "",
                role,
                msg.user_id,
                msg.sender_name,
                msg.text,
                float(msg.timestamp),
                json.dumps(list(msg.at_targets), ensure_ascii=False),
                msg.reply_to_msg_id,
                msg.reply_to_user_id,
                msg.reply_to_text,
                1 if msg.is_self else 0,
                json.dumps([_serialize_attachment(a) for a in msg.attachments], ensure_ascii=False),
                est_tok,
                est_bytes,
            ),
        )
        seq_id = int(cur.lastrowid or 0)
        msg.db_seq = seq_id
        return seq_id

    def update_message_attachments(self, msg: IncomingMessage) -> None:
        """Persist materialized local image paths back to SQLite for future turns & archive search."""
        if msg.message_id:
            self.cache_message(
                adapter_id=msg.adapter_id,
                message_id=msg.message_id,
                user_id=msg.user_id,
                sender_name=msg.sender_name,
                text=msg.text,
                timestamp=msg.timestamp,
                attachments=msg.attachments,
            )
        if self._db_path is None or msg.db_seq <= 0:
            return
        est_tok, est_bytes = estimate_message_metrics(msg)
        with self._db_lock, contextlib.suppress(Exception), self._db_conn() as conn:
            conn.execute(
                "UPDATE messages SET attachments_json = ?, est_tokens = ?, est_bytes = ? WHERE seq_id = ?",
                (
                    json.dumps([_serialize_attachment(a) for a in msg.attachments], ensure_ascii=False),
                    est_tok,
                    est_bytes,
                    msg.db_seq,
                ),
            )
            conn.commit()

    def close(self) -> None:
        self.flush()

    def _load_from_json(self) -> None:
        if self._persist_file is None or not self._persist_file.exists():
            return
        try:
            raw = json.loads(self._persist_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            ch_map = raw.get("channels") if isinstance(raw.get("channels"), dict) else raw
            for sk, items in ch_map.items():
                if not isinstance(items, list):
                    continue
                buf = self._channels[str(sk)]
                for entry in items[-self._history_limit :]:
                    msg = _deserialize_message(entry)
                    if msg is None or not msg.adapter_id or not msg.channel_id:
                        continue
                    buf.append(msg)
                    if msg.message_id:
                        self.cache_message(
                            adapter_id=msg.adapter_id,
                            message_id=msg.message_id,
                            user_id=msg.user_id,
                            sender_name=msg.sender_name,
                            text=msg.text,
                            timestamp=msg.timestamp,
                            attachments=msg.attachments,
                        )
        except Exception as exc:
            logger.warning("读取上下文历史文件 %s 失败: %s", self._persist_file, exc)

    def flush(self) -> None:
        if self._db_path is not None:
            return
        if self._persist_file is None or not self._dirty:
            return
        self._persist_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": time.time(),
            "channels": {
                sk: [_serialize_message(m) for m in buf]
                for sk, buf in self._channels.items()
                if buf
            },
        }
        tmp = self._persist_file.with_name(f".{self._persist_file.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._persist_file)
            self._dirty = False
            self._last_save_mono = time.monotonic()
        except Exception as exc:
            logger.warning("保存上下文历史文件 %s 失败: %s", self._persist_file, exc)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def _maybe_persist_json(self) -> None:
        if self._persist_file is None:
            return
        self._dirty = True
        if (time.monotonic() - self._last_save_mono) >= 1.5:
            self.flush()

    def _purge_msg_cache_for_session(self, session_key: str | None, msg_keys: list[tuple[str, str]] | None = None) -> None:
        if session_key is None:
            self._msg_cache.clear()
            return
        if msg_keys:
            for k in msg_keys:
                self._msg_cache.pop(k, None)

    def _purge_legacy_json_file(self, session_key: str | None) -> None:
        if self._db_path is None:
            return
        legacy = self._db_path.parent / "context_history.json"
        if not legacy.is_file():
            return
        with contextlib.suppress(Exception):
            if session_key is None:
                legacy.unlink(missing_ok=True)
                return
            raw = json.loads(legacy.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            ch_map = raw.get("channels") if isinstance(raw.get("channels"), dict) else raw
            if isinstance(ch_map, dict) and session_key in ch_map:
                ch_map.pop(session_key, None)
                tmp = legacy.with_name(f".{legacy.name}.{uuid.uuid4().hex[:8]}.tmp")
                tmp.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, legacy)

    def clear(self, session_key: str | None = None, *, delete_archive: bool = False) -> int:
        """Reset active context window for one session or all sessions.

        By default in SQLite mode, advances `active_min_seq` past the latest row so active
        context starts completely fresh while preserving historical archive rows in SQLite.
        When `delete_archive=True`, permanently deletes all SQLite rows, cursors, memory buffers,
        quote cache entries, and legacy JSON entries for the target session(s).
        """
        if self._db_path is not None:
            with self._db_lock, self._db_conn() as conn:
                if session_key:
                    old_buf = self._channels.pop(session_key, None)
                    existed = 1 if old_buf else 0
                    self._ambient_unread.pop(session_key, None)
                    if delete_archive:
                        cache_keys = [
                            (str(r[0]), str(r[1]))
                            for r in conn.execute(
                                "SELECT adapter_id, message_id FROM messages WHERE session_key = ? AND message_id != ''",
                                (session_key,),
                            ).fetchall()
                        ]
                        if old_buf:
                            cache_keys.extend((m.adapter_id, m.message_id) for m in old_buf if m.message_id)
                        self._purge_msg_cache_for_session(session_key, cache_keys)
                        cur = conn.execute("DELETE FROM messages WHERE session_key = ?", (session_key,))
                        conn.execute(
                            "DELETE FROM session_cursors WHERE session_key = ? OR cursor_key = ?",
                            (session_key, session_key),
                        )
                        existed = max(existed, int(cur.rowcount or 0))
                        conn.commit()
                        self._purge_legacy_json_file(session_key)
                        return existed
                    else:
                        row = conn.execute(
                            "SELECT COALESCE(MAX(seq_id), 0) FROM messages WHERE session_key = ?",
                            (session_key,),
                        ).fetchone()
                        max_seq = int(row[0]) if row else 0
                        if max_seq > 0:
                            existed = 1
                            now = time.time()
                            conn.execute(
                                """
                                INSERT INTO session_cursors (cursor_key, session_key, active_min_seq, last_compacted_at)
                                VALUES (?, ?, ?, ?)
                                ON CONFLICT(cursor_key) DO UPDATE SET
                                    active_min_seq = excluded.active_min_seq,
                                    last_compacted_at = excluded.last_compacted_at
                                """,
                                (session_key, session_key, max_seq + 1, now),
                            )
                            conn.execute(
                                "UPDATE session_cursors SET active_min_seq = ?, last_compacted_at = ? WHERE session_key = ?",
                                (max_seq + 1, now, session_key),
                            )
                    conn.commit()
                    return existed

                count = len(self._channels)
                self._channels.clear()
                self._ambient_unread.clear()
                if delete_archive:
                    self._purge_msg_cache_for_session(None)
                    cur = conn.execute("DELETE FROM messages")
                    conn.execute("DELETE FROM session_cursors")
                    count = max(count, int(cur.rowcount or 0))
                    conn.commit()
                    self._purge_legacy_json_file(None)
                    return count
                else:
                    srows = conn.execute(
                        "SELECT session_key, MAX(seq_id) FROM messages GROUP BY session_key"
                    ).fetchall()
                    now = time.time()
                    count = max(count, len(srows))
                    for sk, mseq in srows:
                        next_seq = int(mseq or 0) + 1
                        conn.execute(
                            """
                            INSERT INTO session_cursors (cursor_key, session_key, active_min_seq, last_compacted_at)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(cursor_key) DO UPDATE SET
                                active_min_seq = excluded.active_min_seq,
                                last_compacted_at = excluded.last_compacted_at
                            """,
                            (str(sk), str(sk), next_seq, now),
                        )
                        conn.execute(
                            "UPDATE session_cursors SET active_min_seq = ?, last_compacted_at = ? WHERE session_key = ?",
                            (next_seq, now, str(sk)),
                        )
                conn.commit()
                return count

        if session_key:
            old_buf = self._channels.pop(session_key, None)
            existed = len(old_buf) if old_buf else 0
            self._ambient_unread.pop(session_key, None)
            if delete_archive and old_buf:
                self._purge_msg_cache_for_session(
                    session_key,
                    [(m.adapter_id, m.message_id) for m in old_buf if m.message_id],
                )
            if existed:
                self._dirty = True
                self.flush()
            return existed
        count = sum(len(b) for b in self._channels.values()) if delete_archive else len(self._channels)
        self._channels.clear()
        self._ambient_unread.clear()
        if delete_archive:
            self._purge_msg_cache_for_session(None)
        self._dirty = True
        self.flush()
        return count

    def add_message(self, msg: IncomingMessage, *, count_ambient: bool = False) -> bool:
        """Add message to rolling buffer and SQLite store. Returns True if new, False if duplicate."""
        buf = self._channels[msg.session_key]
        is_duplicate = bool(msg.message_id and any(m.message_id == msg.message_id for m in buf))
        if not is_duplicate and self._db_path is not None:
            with self._db_lock, self._db_conn() as conn:
                if msg.message_id and not msg.is_self:
                    dup_row = conn.execute(
                        "SELECT seq_id FROM messages WHERE session_key = ? AND message_id = ? LIMIT 1",
                        (msg.session_key, msg.message_id),
                    ).fetchone()
                    if dup_row is not None:
                        is_duplicate = True
                if not is_duplicate:
                    self._insert_sqlite_message(conn, msg)
                    conn.commit()

        if not is_duplicate:
            buf.append(msg)

        if msg.message_id:
            self.cache_message(
                adapter_id=msg.adapter_id,
                message_id=msg.message_id,
                user_id=msg.user_id,
                sender_name=msg.sender_name,
                text=msg.text,
                timestamp=msg.timestamp,
                attachments=msg.attachments,
            )
        if not is_duplicate and count_ambient and not msg.is_self:
            self._ambient_unread[msg.session_key] += 1
        if not is_duplicate and self._db_path is None:
            self._maybe_persist_json()
        return not is_duplicate

    def cache_message(
        self,
        *,
        adapter_id: str,
        message_id: str,
        user_id: str,
        sender_name: str,
        text: str,
        timestamp: float,
        attachments: list[MediaAttachment] | None = None,
    ) -> None:
        if not message_id:
            return
        key = (adapter_id, message_id)
        if key in self._msg_cache:
            self._msg_cache.move_to_end(key)
        self._msg_cache[key] = CachedMessage(
            message_id=message_id,
            user_id=user_id,
            sender_name=sender_name,
            text=text,
            timestamp=timestamp,
            attachments=list(attachments) if attachments else [],
        )
        while len(self._msg_cache) > self._msg_cache_limit:
            self._msg_cache.popitem(last=False)

    def lookup_message(self, adapter_id: str, message_id: str) -> CachedMessage | None:
        if not message_id:
            return None
        key = (adapter_id, message_id)
        item = self._msg_cache.get(key)
        if item is not None:
            self._msg_cache.move_to_end(key)
            return item
        # Fallback to SQLite lookup so quoting an old historical message works across restarts
        if self._db_path is not None:
            with self._db_lock, self._db_conn() as conn:
                row = conn.execute(
                    f"SELECT {_SELECT_MSG_COLS} FROM messages "
                    "WHERE adapter_id = ? AND message_id = ? ORDER BY seq_id DESC LIMIT 1",
                    (adapter_id, message_id),
                ).fetchone()
                if row is not None:
                    msg = _row_to_message(row)
                    self.cache_message(
                        adapter_id=msg.adapter_id,
                        message_id=msg.message_id,
                        user_id=msg.user_id,
                        sender_name=msg.sender_name,
                        text=msg.text,
                        timestamp=msg.timestamp,
                        attachments=msg.attachments,
                    )
                    return self._msg_cache.get(key)
        return None

    def get_active_window_and_compact(
        self,
        session_key: str,
        *,
        cursor_key: str = "",
        max_messages: int = 60,
        drop_by_count: int = 20,
        max_tokens: int = 12000,
        max_bytes: int = 0,
        drop_by_tokens: int = 20,
    ) -> ActiveWindowState:
        """Read active window (`seq_id >= active_min_seq`) from SQLite and apply dual-trigger compaction.

        Compaction triggers when EITHER:
          1. Active message count >= `max_messages` (drops `drop_by_count` oldest messages), OR
          2. Active estimated tokens >= `max_tokens` OR UTF-8 bytes >= `max_bytes`
             (drops in steps of `drop_by_tokens` oldest messages until below threshold).
        Dropped messages remain permanently archived in SQLite `messages` for on-demand lookup.
        """
        eff_cursor = cursor_key or session_key
        if self._db_path is None:
            msgs = self.get_recent(session_key, limit=max_messages if max_messages > 0 else self._history_limit)
            metrics = [estimate_message_metrics(m) for m in msgs]
            return ActiveWindowState(
                messages=msgs,
                active_min_seq=0,
                latest_seq=len(msgs),
                total_tokens=sum(x[0] for x in metrics),
                total_bytes=sum(x[1] for x in metrics),
            )

        with self._db_lock, self._db_conn() as conn:
            cur_row = conn.execute(
                "SELECT active_min_seq FROM session_cursors WHERE cursor_key = ?",
                (eff_cursor,),
            ).fetchone()
            if cur_row is None and eff_cursor != session_key:
                cur_row = conn.execute(
                    "SELECT active_min_seq FROM session_cursors WHERE cursor_key = ?",
                    (session_key,),
                ).fetchone()
            active_min_seq = int(cur_row[0]) if cur_row is not None else 0

            rows = conn.execute(
                f"SELECT {_SELECT_MSG_COLS} FROM messages "
                "WHERE session_key = ? AND seq_id >= ? ORDER BY seq_id ASC",
                (session_key, active_min_seq),
            ).fetchall()

            compacted = False
            dropped_count = 0
            reasons: list[str] = []

            if len(rows) > 1:
                tok_list = [max(1, int(r[17] or 1)) for r in rows]
                byte_list = [max(1, int(r[18] or 1)) for r in rows]
                cur_cnt = len(rows)
                cur_tok = sum(tok_list)
                cur_bytes = sum(byte_list)

                hit_count = max_messages > 0 and cur_cnt >= max_messages
                hit_tok = max_tokens > 0 and cur_tok >= max_tokens
                hit_bytes = max_bytes > 0 and cur_bytes >= max_bytes

                if hit_count or hit_tok or hit_bytes:
                    drop_n = 0
                    if hit_count:
                        step_c = max(1, drop_by_count)
                        n_c = step_c
                        while (cur_cnt - n_c) >= max_messages and n_c < cur_cnt - 1:
                            n_c += step_c
                        drop_n = max(drop_n, n_c)
                        reasons.append(f"条数 {cur_cnt}>={max_messages}")

                    if hit_tok or hit_bytes:
                        step_t = max(1, drop_by_tokens)
                        n_t = step_t
                        while n_t < cur_cnt - 1:
                            rem_tok = sum(tok_list[n_t:])
                            rem_bytes = sum(byte_list[n_t:])
                            tok_ok = (max_tokens <= 0) or (rem_tok < max_tokens)
                            bytes_ok = (max_bytes <= 0) or (rem_bytes < max_bytes)
                            if tok_ok and bytes_ok:
                                break
                            n_t += step_t
                        drop_n = max(drop_n, n_t)
                        if hit_tok:
                            reasons.append(f"Token {cur_tok}>={max_tokens}")
                        if hit_bytes:
                            reasons.append(f"字节 {cur_bytes}>={max_bytes}")

                    drop_n = min(drop_n, cur_cnt - 1)
                    if drop_n > 0:
                        active_min_seq = int(rows[drop_n][0])
                        rows = rows[drop_n:]
                        compacted = True
                        dropped_count = drop_n
                        now = time.time()
                        conn.execute(
                            """
                            INSERT INTO session_cursors (
                                cursor_key, session_key, active_min_seq,
                                compaction_count, total_dropped_messages, last_compacted_at
                            ) VALUES (?, ?, ?, 1, ?, ?)
                            ON CONFLICT(cursor_key) DO UPDATE SET
                                active_min_seq = excluded.active_min_seq,
                                compaction_count = session_cursors.compaction_count + 1,
                                total_dropped_messages = session_cursors.total_dropped_messages + excluded.total_dropped_messages,
                                last_compacted_at = excluded.last_compacted_at
                            """,
                            (eff_cursor, session_key, active_min_seq, dropped_count, now),
                        )
                        conn.commit()
                        buf = self._channels.get(session_key)
                        if buf is not None:
                            kept = [m for m in buf if m.db_seq == 0 or m.db_seq >= active_min_seq]
                            buf.clear()
                            buf.extend(kept[-self._history_limit :])

            arch_row = conn.execute(
                "SELECT COUNT(1), COALESCE(MIN(seq_id), 0), COALESCE(MAX(seq_id), 0) "
                "FROM messages WHERE session_key = ? AND seq_id < ?",
                (session_key, active_min_seq),
            ).fetchone()
            arch_cnt = int(arch_row[0] or 0) if arch_row else 0
            arch_min = int(arch_row[1] or 0) if arch_row else 0
            arch_max = int(arch_row[2] or 0) if arch_row else 0

            msgs = [_row_to_message(r) for r in rows]
            total_tok = sum(max(1, int(r[17] or 1)) for r in rows)
            total_b = sum(max(1, int(r[18] or 1)) for r in rows)
            latest_seq = int(rows[-1][0]) if rows else max(0, active_min_seq - 1)

            return ActiveWindowState(
                messages=msgs,
                active_min_seq=active_min_seq,
                latest_seq=latest_seq,
                archived_count=arch_cnt,
                archived_min_seq=arch_min,
                archived_max_seq=arch_max,
                total_tokens=total_tok,
                total_bytes=total_b,
                compacted=compacted,
                dropped_count=dropped_count,
                compact_reasons=reasons,
            )

    def get_messages_after_seq(self, session_key: str, after_seq: int) -> list[IncomingMessage]:
        """Fetch incremental messages (`seq_id > after_seq`) from SQLite in chronological order."""
        if self._db_path is None:
            return [m for m in self._channels.get(session_key, ()) if m.db_seq > after_seq]
        with self._db_lock, self._db_conn() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT_MSG_COLS} FROM messages "
                "WHERE session_key = ? AND seq_id > ? ORDER BY seq_id ASC",
                (session_key, int(after_seq)),
            ).fetchall()
            return [_row_to_message(r) for r in rows]

    def get_recent(self, session_key: str, limit: int = 25) -> list[IncomingMessage]:
        buf = self._channels.get(session_key)
        if not buf or limit <= 0:
            return []
        if limit >= len(buf):
            return list(buf)
        return list(buf)[-limit:]

    def get_ambient_unread(self, session_key: str) -> int:
        return self._ambient_unread.get(session_key, 0)

    def increment_ambient(self, session_key: str) -> int:
        self._ambient_unread[session_key] += 1
        return self._ambient_unread[session_key]

    def reset_ambient_unread(self, session_key: str) -> None:
        self._ambient_unread[session_key] = 0

    def get_db_stats(self) -> dict[str, Any]:
        if self._db_path is None:
            return {"db_enabled": False, "db_path": "", "db_size_bytes": 0}
        size = 0
        for p in (
            self._db_path,
            Path(str(self._db_path) + "-wal"),
            Path(str(self._db_path) + "-shm"),
        ):
            with contextlib.suppress(OSError):
                if p.is_file():
                    size += p.stat().st_size
        return {"db_enabled": True, "db_path": str(self._db_path), "db_size_bytes": size}

    def list_sessions_summary(self) -> list[dict[str, Any]]:
        """List all group/private sessions with active/archived counts, token stats, and latest preview."""
        if self._db_path is not None:
            with self._db_lock, self._db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT
                        m.session_key,
                        MAX(m.adapter_id) AS adapter_id,
                        MAX(m.scene) AS scene,
                        MAX(m.channel_id) AS channel_id,
                        COUNT(1) AS total_messages,
                        COALESCE(SUM(m.est_tokens), 0) AS total_tokens,
                        COALESCE(SUM(m.est_bytes), 0) AS total_bytes,
                        MAX(m.seq_id) AS max_seq,
                        MAX(m.timestamp) AS last_ts
                    FROM messages m
                    GROUP BY m.session_key
                    ORDER BY last_ts DESC, max_seq DESC
                    """
                ).fetchall()

                cur_map: dict[str, tuple[int, int]] = {}
                for crow in conn.execute(
                    "SELECT session_key, MAX(active_min_seq), COALESCE(MAX(compaction_count), 0) "
                    "FROM session_cursors GROUP BY session_key"
                ).fetchall():
                    cur_map[str(crow[0])] = (int(crow[1] or 0), int(crow[2] or 0))

                items: list[dict[str, Any]] = []
                for r in rows:
                    sk = str(r[0])
                    adapter_id = str(r[1] or "")
                    scene = str(r[2] or "group")
                    channel_id = str(r[3] or "")
                    total_msgs = int(r[4] or 0)
                    total_tok = int(r[5] or 0)
                    total_bytes = int(r[6] or 0)
                    max_seq = int(r[7] or 0)
                    last_ts = float(r[8] or 0.0)
                    active_min_seq, compact_cnt = cur_map.get(sk, (0, 0))

                    act_row = conn.execute(
                        "SELECT COUNT(1), COALESCE(SUM(est_tokens), 0), COALESCE(SUM(est_bytes), 0) "
                        "FROM messages WHERE session_key = ? AND seq_id >= ?",
                        (sk, active_min_seq),
                    ).fetchone()
                    active_msgs = int(act_row[0] or 0) if act_row else 0
                    active_tok = int(act_row[1] or 0) if act_row else 0
                    active_bytes = int(act_row[2] or 0) if act_row else 0
                    archived_msgs = max(0, total_msgs - active_msgs)

                    last_row = conn.execute(
                        "SELECT role, sender_name, user_id, text FROM messages WHERE seq_id = ?",
                        (max_seq,),
                    ).fetchone()
                    last_role = str(last_row[0] or "user") if last_row else "user"
                    last_sender = str(last_row[1] or "") if last_row else ""
                    last_text = str(last_row[3] or "").strip() if last_row else ""

                    display_name = channel_id
                    if scene == "private":
                        peer_row = conn.execute(
                            "SELECT sender_name FROM messages "
                            "WHERE session_key = ? AND is_self = 0 AND sender_name != '' "
                            "ORDER BY seq_id DESC LIMIT 1",
                            (sk,),
                        ).fetchone()
                        if peer_row and str(peer_row[0] or "").strip():
                            display_name = str(peer_row[0]).strip()

                    items.append(
                        {
                            "session_key": sk,
                            "adapter_id": adapter_id,
                            "scene": scene,
                            "channel_id": channel_id,
                            "display_name": display_name,
                            "total_messages": total_msgs,
                            "active_messages": active_msgs,
                            "archived_messages": archived_msgs,
                            "active_tokens": active_tok,
                            "active_bytes": active_bytes,
                            "total_tokens": total_tok,
                            "total_bytes": total_bytes,
                            "compaction_count": compact_cnt,
                            "last_timestamp": last_ts,
                            "last_role": last_role,
                            "last_sender": last_sender,
                            "last_preview": last_text[:100],
                        }
                    )
                return items

        # Fallback: in-memory / JSON mode
        items = []
        for sk, buf in self._channels.items():
            if not buf:
                continue
            msgs = list(buf)
            last = msgs[-1]
            tot_tok = 0
            tot_bytes = 0
            for m in msgs:
                tk, bt = estimate_message_metrics(m)
                tot_tok += tk
                tot_bytes += bt
            display_name = last.channel_id
            if last.scene == "private":
                for m in reversed(msgs):
                    if not m.is_self and m.sender_name:
                        display_name = m.sender_name
                        break
            items.append(
                {
                    "session_key": sk,
                    "adapter_id": last.adapter_id,
                    "scene": last.scene,
                    "channel_id": last.channel_id,
                    "display_name": display_name,
                    "total_messages": len(msgs),
                    "active_messages": len(msgs),
                    "archived_messages": 0,
                    "active_tokens": tot_tok,
                    "active_bytes": tot_bytes,
                    "total_tokens": tot_tok,
                    "total_bytes": tot_bytes,
                    "compaction_count": 0,
                    "last_timestamp": float(last.timestamp or 0.0),
                    "last_role": "assistant" if last.is_self else "user",
                    "last_sender": last.sender_name,
                    "last_preview": (last.text or "").strip()[:100],
                }
            )
        items.sort(key=lambda x: x["last_timestamp"], reverse=True)
        return items

    def get_session_messages_preview(self, session_key: str, limit: int = 40) -> list[dict[str, Any]]:
        """Return latest messages for a session (chronological order) with active/archived flag."""
        eff_limit = max(1, min(200, int(limit or 40)))
        if self._db_path is not None:
            with self._db_lock, self._db_conn() as conn:
                cur_row = conn.execute(
                    "SELECT MAX(active_min_seq) FROM session_cursors WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                active_min_seq = int(cur_row[0] or 0) if cur_row else 0
                rows = conn.execute(
                    "SELECT seq_id, role, user_id, sender_name, text, timestamp, reply_to_text, is_self "
                    "FROM messages WHERE session_key = ? ORDER BY seq_id DESC LIMIT ?",
                    (session_key, eff_limit),
                ).fetchall()
                out: list[dict[str, Any]] = []
                for r in reversed(rows):
                    seq_id = int(r[0] or 0)
                    out.append(
                        {
                            "seq_id": seq_id,
                            "role": str(r[1] or "user"),
                            "user_id": str(r[2] or ""),
                            "sender_name": str(r[3] or ""),
                            "text": str(r[4] or ""),
                            "timestamp": float(r[5] or 0.0),
                            "reply_to_text": str(r[6] or ""),
                            "is_self": bool(r[7]),
                            "is_archived": active_min_seq > 0 and seq_id < active_min_seq,
                        }
                    )
                return out

        buf = self._channels.get(session_key)
        if not buf:
            return []
        out = []
        for idx, m in enumerate(list(buf)[-eff_limit:], start=1):
            out.append(
                {
                    "seq_id": m.db_seq or idx,
                    "role": "assistant" if m.is_self else "user",
                    "user_id": m.user_id,
                    "sender_name": m.sender_name,
                    "text": m.text,
                    "timestamp": float(m.timestamp or 0.0),
                    "reply_to_text": m.reply_to_text or "",
                    "is_self": bool(m.is_self),
                    "is_archived": False,
                }
            )
        return out


# ============================================================================
# Shared Read-Only SQLite Archive Query Helpers (used by chat_history MCP)
# ============================================================================
def open_readonly_history_db(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"历史数据库尚未生成: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def format_db_history_rows(rows: list[tuple[Any, ...]], *, bot_label: str = "机器人(我)") -> str:
    if not rows:
        return "未查询到符合条件的历史消息记录。"
    lines: list[str] = []
    for r in rows:
        seq_id, role, user_id, sender_name, text, ts, reply_to_text, attachments_json = r
        try:
            t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts or 0)))
        except Exception:
            t_str = "-"
        who = bot_label if role == "assistant" else f"{sender_name}({user_id})"
        q_part = f' [引用: "{reply_to_text[:80]}"]' if reply_to_text else ""
        att_part = ""
        try:
            atts = json.loads(attachments_json or "[]")
            if isinstance(atts, list) and atts:
                paths = [str(a.get("local_path") or a.get("file_name") or "图片") for a in atts if isinstance(a, dict)]
                if paths:
                    att_part = " " + " ".join(f"[附件:{p}]" for p in paths)
        except Exception:
            pass
        clean_text = (text or "").strip()
        lines.append(f"[#{seq_id}][{t_str}] {who}{q_part}: {clean_text}{att_part}")
    return "\n".join(lines)
