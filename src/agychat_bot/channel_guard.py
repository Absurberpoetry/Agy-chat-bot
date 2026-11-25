from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .models import IncomingMessage

logger = logging.getLogger(__name__)


def is_strict_explicit_at_bot(msg: IncomingMessage) -> bool:
    """Return True ONLY when a group message contains a real explicit `@bot` mention.

    Strictly rejects:
    - pure quotes/replies to the bot (whose auto-injected `@bot` is excluded by `explicit_at_bot_count`)
    - ambiguous quotes whose quoted author could not be resolved and only carry 1 `@bot`
    - `@all` mentions without explicitly `@`-ing the bot
    """
    if msg.explicit_at_bot_count <= 0:
        return False
    if msg.reply_to_msg_id and msg.reply_to_user_id is None and msg.at_bot_count <= 1:
        return False
    return True


def is_non_friend_private_message(msg: IncomingMessage) -> bool:
    """Return True if a private message explicitly comes from a non-friend temporary session."""
    if msg.scene != "private":
        return False
    raw = msg.raw_event if isinstance(msg.raw_event, dict) else {}
    sub_type = str(raw.get("sub_type") or "").strip().lower()
    return sub_type in ("group", "other", "stranger", "group_self")


class ChannelActivationStore:
    """Anti-disturbance guard persisting activated groups and friend private chats.

    Rules:
    1. By default, no group or private chat is activated: the bot never speaks proactively
       (no dice, no context_llm, no keyword/quote/script/scheduled/plugin unsolicited reply).
    2. A private chat is activated only when a friend initiates a private message.
    3. A group is activated ONLY when someone explicitly `@bot` in that group (quotes alone do NOT activate).
    4. If the bot is muted in a group, that group is immediately deactivated back to rule (1) and
       silenced during the mute duration; even after unmute, it stays silent until explicitly `@bot` again.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._file = self._data_dir / "activated_channels.json"
        self._groups: set[str] = set()
        self._privates: set[str] = set()
        self._muted_until: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self._file.is_file():
            return
        try:
            raw = json.loads(self._file.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                return
            self._groups = {str(x).strip() for x in (raw.get("groups") or []) if str(x).strip()}
            self._privates = {str(x).strip() for x in (raw.get("privates") or []) if str(x).strip()}
            raw_muted = raw.get("muted_until") or {}
            if isinstance(raw_muted, dict):
                now = time.time()
                for k, v in raw_muted.items():
                    try:
                        exp = float(v)
                    except (TypeError, ValueError):
                        continue
                    if exp > now:
                        self._muted_until[str(k).strip()] = exp
        except Exception as exc:
            logger.warning("读取防打扰激活会话状态失败 (%s): %s", self._file, exc)

    def _save(self) -> None:
        now = time.time()
        active_muted = {k: v for k, v in self._muted_until.items() if v > now}
        self._muted_until = active_muted
        payload = {
            "groups": sorted(self._groups),
            "privates": sorted(self._privates),
            "muted_until": active_muted,
        }
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self._file)

    @staticmethod
    def make_key(adapter_id: str, scene: str, channel_id: str) -> str:
        return f"{adapter_id}:{scene}:{channel_id}"

    def is_muted(self, session_key: str, *, now: float | None = None) -> bool:
        exp = self._muted_until.get(session_key)
        if exp is None:
            return False
        cur = time.time() if now is None else now
        if cur >= exp:
            self._muted_until.pop(session_key, None)
            self._save()
            return False
        return True

    def is_activated(self, session_key: str, *, now: float | None = None) -> bool:
        if self.is_muted(session_key, now=now):
            return False
        return session_key in self._groups or session_key in self._privates

    def activate_group(self, adapter_id: str, group_id: str, *, now: float | None = None) -> bool:
        key = self.make_key(adapter_id, "group", str(group_id).strip())
        if self.is_muted(key, now=now):
            return False
        if key in self._groups:
            return False
        self._groups.add(key)
        self._save()
        return True

    def activate_private(self, adapter_id: str, user_id: str) -> bool:
        key = self.make_key(adapter_id, "private", str(user_id).strip())
        if key in self._privates:
            return False
        self._privates.add(key)
        self._save()
        return True

    def on_group_muted(
        self,
        adapter_id: str,
        group_id: str,
        *,
        duration_seconds: float = 0.0,
        now: float | None = None,
    ) -> bool:
        """Immediately deactivate a group back to silent anti-disturb state when muted."""
        key = self.make_key(adapter_id, "group", str(group_id).strip())
        was_active = key in self._groups
        self._groups.discard(key)
        cur = time.time() if now is None else now
        eff_duration = float(duration_seconds) if duration_seconds > 0 else 120.0
        self._muted_until[key] = cur + min(eff_duration, 86400.0 * 30.0)
        self._save()
        return was_active

    def on_group_unmuted(self, adapter_id: str, group_id: str) -> None:
        """Clear mute timer on lift_ban, while KEEPING the group deactivated until next explicit @bot."""
        key = self.make_key(adapter_id, "group", str(group_id).strip())
        if key in self._muted_until:
            self._muted_until.pop(key, None)
            self._save()

    def list_activated(self, *, adapter_id: str = "", scene: str = "", now: float | None = None) -> list[str]:
        """Return channel_ids of currently activated (and unmuted) sessions matching optional filters."""
        pool: set[str] = set()
        if scene == "group":
            pool = set(self._groups)
        elif scene == "private":
            pool = set(self._privates)
        else:
            pool = self._groups | self._privates

        out: list[str] = []
        for key in sorted(pool):
            if self.is_muted(key, now=now):
                continue
            parts = key.split(":", 2)
            if len(parts) != 3:
                continue
            aid, sc, cid = parts
            if adapter_id and aid != adapter_id:
                continue
            if scene and sc != scene:
                continue
            if cid:
                out.append(cid)
        return out

    def get_summary(self, *, now: float | None = None) -> dict[str, Any]:
        cur = time.time() if now is None else now
        muted = sorted(k for k, exp in self._muted_until.items() if exp > cur)
        return {
            "activated_groups_count": len(self._groups),
            "activated_privates_count": len(self._privates),
            "activated_groups": sorted(self._groups),
            "activated_privates": sorted(self._privates),
            "muted_groups": muted,
        }
