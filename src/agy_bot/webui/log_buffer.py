from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import uuid
from collections import deque
from typing import Any


def classify_log_tags(logger_name: str, message: str) -> list[str]:
    """Classify a log entry into UI filter tags: tool, subagent, mcp, plugin."""
    lname = (logger_name or "").lower()
    msg = message or ""
    low = msg.lower()
    tags: list[str] = []

    if (
        "[子代理" in msg
        or "子代理" in msg
        or "[subagent:" in low
        or "subagent:" in low
        or "后台任务" in msg
    ):
        tags.append("subagent")

    if (
        "[mcp]" in low
        or "mcp 服务器" in low
        or "mcp工具" in low
        or "mcp 工具" in low
        or "call_mcp_tool" in low
        or "list_resources" in low
        or "read_resource" in low
        or "mcp=" in low
    ):
        tags.append("mcp")

    if (
        "[工具]" in msg
        or "调用工具" in msg
        or "工具调用" in msg
        or "tool_use" in low
        or "call_mcp_tool" in low
    ):
        tags.append("tool")

    if (
        "[插件" in msg
        or "agy_bot.plugin" in lname
        or "agy_bot.plugins" in lname
        or "外部插件" in msg
    ):
        tags.append("plugin")

    return tags


class LogRingBufferHandler(logging.Handler):
    """In-memory ring buffer logging handler with async subscriber fan-out for WebUI SSE."""

    def __init__(self, max_entries: int = 1000) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._entries: deque[dict[str, Any]] = deque(maxlen=max(100, max_entries))
        self._seq = 0
        self.boot_id = uuid.uuid4().hex[:8]
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) if self.formatter else record.getMessage()
            raw_msg = record.getMessage()
            ts = dt.datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
            tags = classify_log_tags(record.name, raw_msg)
            with self._lock:
                self._seq += 1
                entry = {
                    "boot_id": self.boot_id,
                    "seq": self._seq,
                    "time": ts,
                    "level": record.levelname,
                    "logger": record.name,
                    "message": raw_msg,
                    "formatted": msg,
                    "tags": tags,
                }
                self._entries.append(entry)
                subs = list(self._subscribers)

            for loop, queue in subs:
                if loop.is_closed():
                    continue
                loop.call_soon_threadsafe(self._push_nowait, queue, entry)
        except Exception:
            self.handleError(record)

    @staticmethod
    def _push_nowait(queue: asyncio.Queue[dict[str, Any]], entry: dict[str, Any]) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            pass

    def get_entries(
        self,
        *,
        limit: int = 200,
        level: str = "",
        search: str = "",
        since_seq: int = 0,
        tag: str = "",
    ) -> list[dict[str, Any]]:
        lvl = level.strip().upper()
        needle = search.strip().lower()
        want_tag = tag.strip().lower()
        with self._lock:
            items = list(self._entries)
        filtered: list[dict[str, Any]] = []
        for entry in items:
            if since_seq and entry["seq"] <= since_seq:
                continue
            if lvl and lvl != "ALL" and entry["level"] != lvl:
                continue
            if want_tag and want_tag != "all" and want_tag not in (entry.get("tags") or ()):
                continue
            if needle and needle not in entry["formatted"].lower():
                continue
            filtered.append(entry)
        if limit > 0 and len(filtered) > limit:
            return filtered[-limit:]
        return filtered

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add((loop, queue))
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers = {item for item in self._subscribers if item[1] is not queue}
