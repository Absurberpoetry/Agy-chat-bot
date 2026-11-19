from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PlatformConfig:
    id: str
    type: str = "onebot_v11"
    enabled: bool = True
    ws_url: str = "ws://127.0.0.1:3001"
    access_token: str = ""
    self_id: str = ""
    group_allowlist: list[str] = field(default_factory=list)
    group_blocklist: list[str] = field(default_factory=list)
    private_chat_enabled: bool = False
    private_allowlist: list[str] = field(default_factory=list)
    reconnect_initial_seconds: float = 2.0
    reconnect_max_seconds: float = 60.0
    rpc_timeout_seconds: float = 5.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MentionTriggerConfig:
    enabled: bool = True
    allow_at_all: bool = False
    include_quote_at: bool = False
    unknown_reply_at_behavior: str = "mention"


@dataclass(slots=True)
class QuoteTriggerConfig:
    enabled: bool = True


@dataclass(slots=True)
class KeywordTriggerConfig:
    enabled: bool = True
    keywords: list[str] = field(default_factory=list)
    regex_patterns: list[str] = field(default_factory=list)
    case_sensitive: bool = False
    exclude_keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DiceTriggerConfig:
    enabled: bool = True
    probability: float = 0.03
    min_message_length: int = 4
    cooldown_seconds: float = 180.0
    ignore_prefixes: list[str] = field(default_factory=lambda: ["/", "!", ".", "！"])


@dataclass(slots=True)
class ContextLLMTriggerConfig:
    enabled: bool = False
    min_unread_messages: int = 12
    min_interval_seconds: float = 240.0
    judge_context_window: int = 15
    cooldown_after_reply_seconds: float = 180.0


@dataclass(slots=True)
class TriggersConfig:
    mention: MentionTriggerConfig = field(default_factory=MentionTriggerConfig)
    quote: QuoteTriggerConfig = field(default_factory=QuoteTriggerConfig)
    keyword: KeywordTriggerConfig = field(default_factory=KeywordTriggerConfig)
    dice: DiceTriggerConfig = field(default_factory=DiceTriggerConfig)
    context_llm: ContextLLMTriggerConfig = field(default_factory=ContextLLMTriggerConfig)


@dataclass(slots=True)
class CollectorConfig:
    wait_triggers: list[str] = field(default_factory=lambda: ["mention", "quote", "keyword"])
    quiet_wait_seconds: float = 4.0
    max_wait_seconds: float = 15.0
    max_extra_messages: int = 5
    track_only_trigger_user: bool = True
    context_message_limit: int = 25
    history_buffer_size: int = 100


@dataclass(slots=True)
class RateLimitConfig:
    max_concurrent_requests: int = 1
    max_queue_size: int = 8
    user_cooldown_seconds: float = 12.0
    channel_min_interval_seconds: float = 3.0
    max_replies_per_channel_per_minute: int = 6


@dataclass(slots=True)
class ReplyConfig:
    quote_trigger_message: bool = True
    at_trigger_user: bool = False
    max_reply_chars: int = 800


@dataclass(slots=True)
class AgyConfig:
    mode: str = "agy"  # "agy" or "mock"
    executable: str = "auto"
    workspace_dir: str = "./bot_workspace"
    data_dir: str = "./data"
    model: str = ""
    judge_model: str = ""
    effort: str = ""
    agent: str = ""
    sandbox: bool = False
    max_warm_processes: int = 2
    max_turns_per_session: int = 20
    turn_timeout_seconds: float = 90.0
    judge_timeout_seconds: float = 30.0
    resume_on_restart: bool = True
    rate_limit_cooldown_seconds: float = 180.0
    fatal_error_pause_seconds: float = 3600.0


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(slots=True)
class BotConfig:
    bot_name: str = "Antigravity助手"
    platforms: list[PlatformConfig] = field(default_factory=list)
    triggers: TriggersConfig = field(default_factory=TriggersConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    agy: AgyConfig = field(default_factory=AgyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    base_dir: Path = field(default_factory=lambda: Path("."))

    def resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()


def _as_str_list(items: Any) -> list[str]:
    if not items:
        return []
    return [str(x) for x in items]


def load_config(path: str | Path) -> BotConfig:
    cfg_path = Path(path).resolve()
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    platforms: list[PlatformConfig] = []
    known_platform_keys = {
        "id",
        "type",
        "enabled",
        "ws_url",
        "access_token",
        "self_id",
        "group_allowlist",
        "group_blocklist",
        "private_chat_enabled",
        "private_allowlist",
        "reconnect_initial_seconds",
        "reconnect_max_seconds",
        "rpc_timeout_seconds",
    }
    for p in raw.get("platforms", []):
        platforms.append(
            PlatformConfig(
                id=str(p["id"]),
                type=str(p.get("type", "onebot_v11")),
                enabled=bool(p.get("enabled", True)),
                ws_url=str(p["ws_url"]),
                access_token=str(p.get("access_token", "")),
                self_id=str(p.get("self_id", "")),
                group_allowlist=_as_str_list(p.get("group_allowlist")),
                group_blocklist=_as_str_list(p.get("group_blocklist")),
                private_chat_enabled=bool(p.get("private_chat_enabled", False)),
                private_allowlist=_as_str_list(p.get("private_allowlist")),
                reconnect_initial_seconds=float(p.get("reconnect_initial_seconds", 2.0)),
                reconnect_max_seconds=float(p.get("reconnect_max_seconds", 60.0)),
                rpc_timeout_seconds=float(p.get("rpc_timeout_seconds", 5.0)),
                extra={k: v for k, v in p.items() if k not in known_platform_keys},
            )
        )

    tr = raw.get("triggers", {})
    m = tr.get("mention", {})
    q = tr.get("quote", {})
    kw = tr.get("keyword", {})
    dc = tr.get("dice", {})
    cl = tr.get("context_llm", {})

    triggers = TriggersConfig(
        mention=MentionTriggerConfig(
            enabled=bool(m.get("enabled", True)),
            allow_at_all=bool(m.get("allow_at_all", False)),
            include_quote_at=bool(m.get("include_quote_at", False)),
            unknown_reply_at_behavior=str(m.get("unknown_reply_at_behavior", "mention")),
        ),
        quote=QuoteTriggerConfig(
            enabled=bool(q.get("enabled", True)),
        ),
        keyword=KeywordTriggerConfig(
            enabled=bool(kw.get("enabled", True)),
            keywords=_as_str_list(kw.get("keywords")),
            regex_patterns=_as_str_list(kw.get("regex_patterns")),
            case_sensitive=bool(kw.get("case_sensitive", False)),
            exclude_keywords=_as_str_list(kw.get("exclude_keywords")),
        ),
        dice=DiceTriggerConfig(
            enabled=bool(dc.get("enabled", True)),
            probability=float(dc.get("probability", 0.03)),
            min_message_length=int(dc.get("min_message_length", 4)),
            cooldown_seconds=float(dc.get("cooldown_seconds", 180.0)),
            ignore_prefixes=_as_str_list(dc.get("ignore_prefixes", ["/", "!", ".", "！"])),
        ),
        context_llm=ContextLLMTriggerConfig(
            enabled=bool(cl.get("enabled", False)),
            min_unread_messages=int(cl.get("min_unread_messages", 12)),
            min_interval_seconds=float(cl.get("min_interval_seconds", 240.0)),
            judge_context_window=int(cl.get("judge_context_window", 15)),
            cooldown_after_reply_seconds=float(cl.get("cooldown_after_reply_seconds", 180.0)),
        ),
    )

    col = raw.get("collector", {})
    collector = CollectorConfig(
        wait_triggers=_as_str_list(col.get("wait_triggers", ["mention", "quote", "keyword"])),
        quiet_wait_seconds=float(col.get("quiet_wait_seconds", 4.0)),
        max_wait_seconds=float(col.get("max_wait_seconds", 15.0)),
        max_extra_messages=int(col.get("max_extra_messages", 5)),
        track_only_trigger_user=bool(col.get("track_only_trigger_user", True)),
        context_message_limit=int(col.get("context_message_limit", 25)),
        history_buffer_size=int(col.get("history_buffer_size", 100)),
    )

    rl = raw.get("rate_limit", {})
    rate_limit = RateLimitConfig(
        max_concurrent_requests=max(1, int(rl.get("max_concurrent_requests", 1))),
        max_queue_size=max(1, int(rl.get("max_queue_size", 8))),
        user_cooldown_seconds=float(rl.get("user_cooldown_seconds", 12.0)),
        channel_min_interval_seconds=float(rl.get("channel_min_interval_seconds", 3.0)),
        max_replies_per_channel_per_minute=max(1, int(rl.get("max_replies_per_channel_per_minute", 6))),
    )

    rep = raw.get("reply", {})
    reply = ReplyConfig(
        quote_trigger_message=bool(rep.get("quote_trigger_message", True)),
        at_trigger_user=bool(rep.get("at_trigger_user", False)),
        max_reply_chars=max(50, int(rep.get("max_reply_chars", 800))),
    )

    ag = raw.get("agy", {})
    agy = AgyConfig(
        mode=str(ag.get("mode", "agy")),
        executable=str(ag.get("executable", "auto")),
        workspace_dir=str(ag.get("workspace_dir", "./bot_workspace")),
        data_dir=str(ag.get("data_dir", "./data")),
        model=str(ag.get("model", "")),
        judge_model=str(ag.get("judge_model", "")),
        effort=str(ag.get("effort", "")),
        agent=str(ag.get("agent", "")),
        sandbox=bool(ag.get("sandbox", False)),
        max_warm_processes=max(1, int(ag.get("max_warm_processes", 2))),
        max_turns_per_session=max(1, int(ag.get("max_turns_per_session", 20))),
        turn_timeout_seconds=float(ag.get("turn_timeout_seconds", 90.0)),
        judge_timeout_seconds=float(ag.get("judge_timeout_seconds", 30.0)),
        resume_on_restart=bool(ag.get("resume_on_restart", True)),
        rate_limit_cooldown_seconds=float(ag.get("rate_limit_cooldown_seconds", 180.0)),
        fatal_error_pause_seconds=float(ag.get("fatal_error_pause_seconds", 3600.0)),
    )

    lg = raw.get("logging", {})
    logging_cfg = LoggingConfig(level=str(lg.get("level", "INFO")).upper())

    return BotConfig(
        bot_name=str(raw.get("bot_name", "Antigravity助手")),
        platforms=platforms,
        triggers=triggers,
        collector=collector,
        rate_limit=rate_limit,
        reply=reply,
        agy=agy,
        logging=logging_cfg,
        base_dir=cfg_path.parent,
    )
