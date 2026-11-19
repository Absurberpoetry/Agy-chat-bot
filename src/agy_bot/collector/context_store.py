from __future__ import annotations

from collections import OrderedDict, defaultdict, deque

from ..models import CachedMessage, IncomingMessage


class GroupContextStore:
    """In-memory rolling context per channel plus bounded message-ID lookup cache."""

    def __init__(self, *, history_limit: int = 100, msg_cache_limit: int = 5000) -> None:
        self._history_limit = max(10, history_limit)
        self._msg_cache_limit = max(100, msg_cache_limit)
        self._channels: dict[str, deque[IncomingMessage]] = defaultdict(
            lambda: deque(maxlen=self._history_limit)
        )
        self._msg_cache: OrderedDict[tuple[str, str], CachedMessage] = OrderedDict()
        self._ambient_unread: dict[str, int] = defaultdict(int)

    def add_message(self, msg: IncomingMessage, *, count_ambient: bool = False) -> None:
        buf = self._channels[msg.session_key]
        is_duplicate = bool(msg.message_id and any(m.message_id == msg.message_id for m in buf))
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
            )
        if not is_duplicate and count_ambient and not msg.is_self:
            self._ambient_unread[msg.session_key] += 1

    def cache_message(
        self,
        *,
        adapter_id: str,
        message_id: str,
        user_id: str,
        sender_name: str,
        text: str,
        timestamp: float,
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
        )
        while len(self._msg_cache) > self._msg_cache_limit:
            self._msg_cache.popitem(last=False)

    def lookup_message(self, adapter_id: str, message_id: str) -> CachedMessage | None:
        key = (adapter_id, message_id)
        item = self._msg_cache.get(key)
        if item is not None:
            self._msg_cache.move_to_end(key)
        return item

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
