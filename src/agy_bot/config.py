from __future__ import annotations

import json
import os
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
    user_blocklist: list[str] = field(default_factory=list)
    private_blocklist: list[str] = field(default_factory=list)
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
    quote_reply: bool | None = None
    at_user: bool | None = None


@dataclass(slots=True)
class QuoteTriggerConfig:
    enabled: bool = True
    quote_reply: bool | None = None
    at_user: bool | None = None


@dataclass(slots=True)
class KeywordTriggerConfig:
    enabled: bool = True
    keywords: list[str] = field(default_factory=list)
    regex_patterns: list[str] = field(default_factory=list)
    case_sensitive: bool = False
    exclude_keywords: list[str] = field(default_factory=list)
    quote_reply: bool | None = None
    at_user: bool | None = None


@dataclass(slots=True)
class DiceTriggerConfig:
    enabled: bool = True
    probability: float = 0.03
    min_message_length: int = 4
    cooldown_seconds: float = 180.0
    ignore_prefixes: list[str] = field(default_factory=lambda: ["/", "!", ".", "！"])
    quote_reply: bool | None = None
    at_user: bool | None = None


@dataclass(slots=True)
class ContextLLMTriggerConfig:
    enabled: bool = False
    min_unread_messages: int = 12
    min_interval_seconds: float = 240.0
    judge_context_window: int = 15
    cooldown_after_reply_seconds: float = 180.0
    quote_reply: bool | None = None
    at_user: bool | None = None


@dataclass(slots=True)
class ScheduledTaskConfig:
    id: str = ""
    name: str = ""
    enabled: bool = True
    schedule_type: str = "cron"  # "cron" or "interval"
    cron: str = "0 8 * * *"
    interval_seconds: float = 3600.0
    adapter_id: str = ""
    scene: str = "group"  # "group" or "private"
    target_ids: list[str] = field(default_factory=list)
    action_mode: str = "llm"  # "llm" or "text"
    prompt: str = ""
    include_context: bool = True
    at_qq: str = ""  # Optional QQ ID or "all" to @ in group


@dataclass(slots=True)
class ScheduledTriggerConfig:
    enabled: bool = False
    tasks: list[ScheduledTaskConfig] = field(default_factory=list)


@dataclass(slots=True)
class ScriptRuleConfig:
    id: str = ""
    name: str = ""
    enabled: bool = True
    mode: str = "python_expr"  # "python_expr", "python_file", "shell_cmd"
    expr: str = ""
    script_path: str = ""
    command: str = ""
    action_mode: str = "llm"  # "llm" or "text"
    prompt_override: str = ""
    reply_text: str = ""
    cooldown_seconds: float = 10.0
    timeout_seconds: float = 5.0


@dataclass(slots=True)
class ScriptTriggerConfig:
    enabled: bool = False
    webhook_token: str = ""
    rules: list[ScriptRuleConfig] = field(default_factory=list)
    quote_reply: bool | None = None
    at_user: bool | None = None


@dataclass(slots=True)
class IdentityDefaultPolicyConfig:
    label: str = "普通群友"
    reply_style: str = ""
    allowed_tasks: str = ""
    allow_bot_commands: bool = False
    allow_plugins: bool = True
    bypass_rate_limit: bool = False
    allow_web_search: bool | None = None
    allow_url_read: bool | None = None
    allow_file_tools: bool | None = None
    allow_vision: bool | None = None
    allow_audio: bool | None = None


@dataclass(slots=True)
class IdentityProfileConfig:
    id: str = ""
    name: str = ""
    adapter_id: str = ""  # "" = all connected clients, or specific platform instance ID
    target_type: str = "user"  # "user" or "group"
    target_ids: list[str] = field(default_factory=list)
    is_admin: bool = False
    label: str = ""
    enabled: bool = True
    reply_style: str = ""
    allowed_tasks: str = ""
    allow_bot_commands: bool = False
    allow_plugins: bool = True
    bypass_rate_limit: bool = False
    allow_web_search: bool | None = None
    allow_url_read: bool | None = None
    allow_file_tools: bool | None = None
    allow_vision: bool | None = None
    allow_audio: bool | None = None


@dataclass(slots=True)
class IdentitiesConfig:
    enabled: bool = False
    strict_anti_spoof: bool = True
    default_policy: IdentityDefaultPolicyConfig = field(default_factory=IdentityDefaultPolicyConfig)
    profiles: list[IdentityProfileConfig] = field(default_factory=list)


@dataclass(slots=True)
class TriggersConfig:
    mention: MentionTriggerConfig = field(default_factory=MentionTriggerConfig)
    quote: QuoteTriggerConfig = field(default_factory=QuoteTriggerConfig)
    keyword: KeywordTriggerConfig = field(default_factory=KeywordTriggerConfig)
    dice: DiceTriggerConfig = field(default_factory=DiceTriggerConfig)
    context_llm: ContextLLMTriggerConfig = field(default_factory=ContextLLMTriggerConfig)
    scheduled: ScheduledTriggerConfig = field(default_factory=ScheduledTriggerConfig)
    script: ScriptTriggerConfig = field(default_factory=ScriptTriggerConfig)
    private_needs_wake: bool = False
    disable_builtin_commands: bool = False
    command_prefixes: list[str] = field(default_factory=lambda: ["/"])
    admin_bypass_rate_limit: bool = False


@dataclass(slots=True)
class SessionConfig:
    unique_session: bool = False


@dataclass(slots=True)
class PromptConfig:
    datetime_system_prompt: bool = True
    group_name_display: bool = True
    timezone: str = "Asia/Shanghai"
    prompt_template: str = ""
    custom_error_message: str = ""


@dataclass(slots=True)
class ContentSafetyConfig:
    enabled: bool = False
    check_input: bool = True
    check_output: bool = True
    action: str = "block"  # "block" or "mask"
    blocked_keywords: list[str] = field(default_factory=list)
    block_reply: str = ""


@dataclass(slots=True)
class PluginsConfig:
    enabled: bool = True
    dir: str = "./plugins"
    disabled: list[str] = field(default_factory=list)
    configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    scopes: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(slots=True)
class CollectorConfig:
    wait_triggers: list[str] = field(default_factory=lambda: ["mention", "quote", "keyword"])
    quiet_wait_seconds: float = 4.0
    max_wait_seconds: float = 15.0
    max_extra_messages: int = 5
    track_only_trigger_user: bool = True
    context_message_limit: int = 25
    history_buffer_size: int = 100
    db_context_enabled: bool = True
    compact_max_messages: int = 60
    compact_drop_by_count: int = 20
    compact_max_tokens: int = 12000
    compact_max_bytes: int = 0
    compact_drop_by_tokens: int = 20


@dataclass(slots=True)
class RateLimitConfig:
    max_concurrent_requests: int = 1
    max_queue_size: int = 8
    user_cooldown_seconds: float = 12.0
    channel_min_interval_seconds: float = 3.0
    max_replies_per_channel_per_minute: int = 6


@dataclass(slots=True)
class SegmentedReplyConfig:
    enabled: bool = False
    only_llm_result: bool = True
    interval_method: str = "random"  # "random", "fixed", or "log"
    interval: str = "1.5,3.5"
    log_base: float = 2.6
    words_count_threshold: int = 10000
    split_mode: str = "words"  # "words" or "regex"
    regex: str = r"[\s\S]+?(?:\|\|\||$)"
    split_words: list[str] = field(default_factory=lambda: ["|||"])
    content_cleanup_rule: str = ""


@dataclass(slots=True)
class ReplyConfig:
    quote_trigger_message: bool = True
    at_trigger_user: bool = False
    reply_prefix: str = ""
    max_reply_chars: int = 800
    segmented_reply: SegmentedReplyConfig = field(default_factory=SegmentedReplyConfig)


@dataclass(slots=True)
class ModelProfileConfig:
    name: str = ""  # Custom display name for this model preset (empty = show model slug)
    model: str = ""  # Actual agy CLI model slug (empty = legacy mode where profile dict key is the slug)
    effort: str = ""  # "", "low", "medium", "high", "xhigh", "max" -> agy --effort
    max_context_messages: int = 0  # 0 = inherit collector.context_message_limit
    max_turns: int = 0  # 0 = inherit agy.max_turns_per_session
    compact_max_messages: int = 0  # 0 = inherit collector.compact_max_messages
    compact_drop_by_count: int = 0  # 0 = inherit collector.compact_drop_by_count
    compact_max_tokens: int = 0  # 0 = inherit collector.compact_max_tokens
    compact_max_bytes: int = 0  # 0 = inherit collector.compact_max_bytes
    compact_drop_by_tokens: int = 0  # 0 = inherit collector.compact_drop_by_tokens
    execution_mode: str = ""  # "", "auto", "plan", "accept-edits" -> agy --mode
    sandbox: bool | None = None  # None = inherit agy.sandbox
    disable_slash_commands: bool = False  # -> agy --disable-slash-commands
    enable_web_search: bool = True
    enable_url_read: bool = False
    enable_vision: bool = True
    enable_audio: bool = False
    enable_file_tools: bool = False
    enable_command: bool = False


def strip_model_effort_suffix(model_id: str) -> tuple[str, str]:
    """Split a model slug like 'gemini-3.8-flash-medium' into ('gemini-3.8-flash', 'medium')."""
    raw = (model_id or "").strip()
    for suffix in ("-xhigh", "-medium", "-high", "-low", "-max"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            return raw[: -len(suffix)], suffix[1:]
    return raw, ""


@dataclass(slots=True)
class AgyConfig:
    mode: str = "agy"  # "agy" or "mock"
    executable: str = "auto"
    workspace_dir: str = "./bot_workspace"
    data_dir: str = "./data"
    settings_file: str = ""
    proxy: str = ""
    model: str = ""
    fallback_models: list[str] = field(default_factory=list)
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
    subagent_enabled: bool = True
    subagent_model: str = ""
    subagent_persona_id: str = ""  # "", "__follow_main__", or a PersonaStore persona_id
    subagent_max_concurrent: int = 2
    subagent_timeout_seconds: float = 240.0
    subagent_handoff_to_main: bool = True
    allow_command_execution: bool = False
    model_profiles: dict[str, ModelProfileConfig] = field(default_factory=dict)

    def resolve_model_target(self, model_ref: str) -> tuple[str, ModelProfileConfig | None]:
        """Resolve a named preset key, display name, or raw model slug into (cli_model_slug, profile)."""
        key = (model_ref or "").strip()
        if not key:
            return "", None
        if key in self.model_profiles:
            prof = self.model_profiles[key]
            return (prof.model.strip() or key), prof
        for pk, prof in self.model_profiles.items():
            if prof.name and prof.name.strip() == key:
                return (prof.model.strip() or pk), prof
        base, _ = strip_model_effort_suffix(key)
        if base and base in self.model_profiles:
            prof = self.model_profiles[base]
            return key, prof
        return key, None

    def resolve_model_profile(self, model_id: str) -> ModelProfileConfig | None:
        return self.resolve_model_target(model_id)[1]


@dataclass(slots=True)
class WebUIConfig:
    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8618
    username: str = "admin"
    password: str = "admin123"
    log_buffer_size: int = 1000


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(slots=True)
class BotConfig:
    bot_name: str = "Antigravity助手"
    admins: list[str] = field(default_factory=list)
    active_persona_id: str = ""
    platforms: list[PlatformConfig] = field(default_factory=list)
    triggers: TriggersConfig = field(default_factory=TriggersConfig)
    identities: IdentitiesConfig = field(default_factory=IdentitiesConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    prompt: PromptConfig = field(default_factory=PromptConfig)
    content_safety: ContentSafetyConfig = field(default_factory=ContentSafetyConfig)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    reply: ReplyConfig = field(default_factory=ReplyConfig)
    agy: AgyConfig = field(default_factory=AgyConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    base_dir: Path = field(default_factory=lambda: Path("."))
    config_path: Path | None = None

    def resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()


def _as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _as_str_list(items: Any) -> list[str]:
    if not items:
        return []
    if isinstance(items, str):
        s = items.strip()
        return [s] if s else []
    if isinstance(items, (list, tuple, set)):
        return [str(x) for x in items if x is not None]
    return [str(items)]


def _as_opt_bool(val: Any) -> bool | None:
    if val is None or val == "":
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("inherit", "default", "null", "none", ""):
            return None
        return low in ("true", "1", "yes", "on")
    return bool(val)


def load_config_from_dict(
    raw: dict[str, Any],
    *,
    base_dir: Path,
    config_path: Path | None = None,
) -> BotConfig:
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
        "user_blocklist",
        "private_chat_enabled",
        "private_allowlist",
        "private_blocklist",
        "reconnect_initial_seconds",
        "reconnect_max_seconds",
        "rpc_timeout_seconds",
        "extra",
    }
    for p in (raw.get("platforms") or []):
        if not isinstance(p, dict):
            continue
        merged_extra: dict[str, Any] = {}
        raw_extra = p.get("extra")
        # Unwrap accidental historical extra.extra nesting cleanly
        while isinstance(raw_extra, dict) and isinstance(raw_extra.get("extra"), dict) and len(raw_extra) == 1:
            raw_extra = raw_extra["extra"]
        if isinstance(raw_extra, dict):
            for ek, ev in raw_extra.items():
                if ek == "extra" and isinstance(ev, dict):
                    merged_extra.update(ev)
                else:
                    merged_extra[ek] = ev
        for k, v in p.items():
            if k not in known_platform_keys:
                merged_extra[k] = v
        platforms.append(
            PlatformConfig(
                id=str(p["id"]),
                type=str(p.get("type", "onebot_v11")),
                enabled=bool(p.get("enabled", True)),
                ws_url=str(p.get("ws_url", "")),
                access_token=str(p.get("access_token", "")),
                self_id=str(p.get("self_id", "")),
                user_blocklist=_as_str_list(p.get("user_blocklist")),
                private_blocklist=_as_str_list(p.get("private_blocklist")),
                reconnect_initial_seconds=float(p.get("reconnect_initial_seconds", 2.0)),
                reconnect_max_seconds=float(p.get("reconnect_max_seconds", 60.0)),
                rpc_timeout_seconds=float(p.get("rpc_timeout_seconds", 5.0)),
                extra=merged_extra,
            )
        )

    tr = _as_dict(raw.get("triggers"))
    m = _as_dict(tr.get("mention"))
    q = _as_dict(tr.get("quote"))
    kw = _as_dict(tr.get("keyword"))
    dc = _as_dict(tr.get("dice"))
    cl = _as_dict(tr.get("context_llm"))
    sch_raw = _as_dict(tr.get("scheduled"))
    scr_raw = _as_dict(tr.get("script"))

    scheduled_tasks: list[ScheduledTaskConfig] = []
    for idx, item in enumerate(sch_raw.get("tasks") or []):
        if not isinstance(item, dict):
            continue
        tid = str(item.get("id", "") or f"task_{idx + 1}").strip()
        stype = str(item.get("schedule_type", "cron") or "cron").strip().lower()
        if stype not in ("cron", "interval"):
            stype = "cron"
        amode = str(item.get("action_mode", "llm") or "llm").strip().lower()
        if amode not in ("llm", "text"):
            amode = "llm"
        scene = str(item.get("scene", "group") or "group").strip().lower()
        if scene not in ("group", "private"):
            scene = "group"
        scheduled_tasks.append(
            ScheduledTaskConfig(
                id=tid,
                name=str(item.get("name", "") or tid).strip(),
                enabled=bool(item.get("enabled", True)),
                schedule_type=stype,
                cron=str(item.get("cron", "0 8 * * *") or "0 8 * * *").strip(),
                interval_seconds=max(10.0, float(item.get("interval_seconds", 3600.0) or 3600.0)),
                adapter_id=str(item.get("adapter_id", "") or "").strip(),
                scene=scene,
                target_ids=_as_str_list(item.get("target_ids")),
                action_mode=amode,
                prompt=str(item.get("prompt", "") or ""),
                include_context=bool(item.get("include_context", True)),
                at_qq=str(item.get("at_qq", "") or "").strip(),
            )
        )

    script_rules: list[ScriptRuleConfig] = []
    for idx, item in enumerate(scr_raw.get("rules") or []):
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id", "") or f"rule_{idx + 1}").strip()
        rmode = str(item.get("mode", "python_expr") or "python_expr").strip().lower()
        if rmode not in ("python_expr", "python_file", "shell_cmd"):
            rmode = "python_expr"
        amode = str(item.get("action_mode", "llm") or "llm").strip().lower()
        if amode not in ("llm", "text"):
            amode = "llm"
        script_rules.append(
            ScriptRuleConfig(
                id=rid,
                name=str(item.get("name", "") or rid).strip(),
                enabled=bool(item.get("enabled", True)),
                mode=rmode,
                expr=str(item.get("expr", "") or ""),
                script_path=str(item.get("script_path", "") or "").strip(),
                command=str(item.get("command", "") or "").strip(),
                action_mode=amode,
                prompt_override=str(item.get("prompt_override", "") or ""),
                reply_text=str(item.get("reply_text", "") or ""),
                cooldown_seconds=max(0.0, float(item.get("cooldown_seconds", 10.0) or 0.0)),
                timeout_seconds=max(1.0, float(item.get("timeout_seconds", 5.0) or 5.0)),
            )
        )

    triggers = TriggersConfig(
        mention=MentionTriggerConfig(
            enabled=bool(m.get("enabled", True)),
            allow_at_all=bool(m.get("allow_at_all", False)),
            include_quote_at=bool(m.get("include_quote_at", False)),
            unknown_reply_at_behavior=str(m.get("unknown_reply_at_behavior", "mention")),
            quote_reply=_as_opt_bool(m.get("quote_reply")),
            at_user=_as_opt_bool(m.get("at_user")),
        ),
        quote=QuoteTriggerConfig(
            enabled=bool(q.get("enabled", True)),
            quote_reply=_as_opt_bool(q.get("quote_reply")),
            at_user=_as_opt_bool(q.get("at_user")),
        ),
        keyword=KeywordTriggerConfig(
            enabled=bool(kw.get("enabled", True)),
            keywords=_as_str_list(kw.get("keywords")),
            regex_patterns=_as_str_list(kw.get("regex_patterns")),
            case_sensitive=bool(kw.get("case_sensitive", False)),
            exclude_keywords=_as_str_list(kw.get("exclude_keywords")),
            quote_reply=_as_opt_bool(kw.get("quote_reply")),
            at_user=_as_opt_bool(kw.get("at_user")),
        ),
        dice=DiceTriggerConfig(
            enabled=bool(dc.get("enabled", True)),
            probability=float(dc.get("probability", 0.03)),
            min_message_length=int(dc.get("min_message_length", 4)),
            cooldown_seconds=float(dc.get("cooldown_seconds", 180.0)),
            ignore_prefixes=_as_str_list(dc.get("ignore_prefixes", ["/", "!", ".", "！"])),
            quote_reply=_as_opt_bool(dc.get("quote_reply")),
            at_user=_as_opt_bool(dc.get("at_user")),
        ),
        context_llm=ContextLLMTriggerConfig(
            enabled=bool(cl.get("enabled", False)),
            min_unread_messages=max(1, int(cl.get("min_unread_messages", 12))),
            min_interval_seconds=max(0.0, float(cl.get("min_interval_seconds", 240.0))),
            judge_context_window=max(1, int(cl.get("judge_context_window", 15))),
            cooldown_after_reply_seconds=max(0.0, float(cl.get("cooldown_after_reply_seconds", 180.0))),
            quote_reply=_as_opt_bool(cl.get("quote_reply")),
            at_user=_as_opt_bool(cl.get("at_user")),
        ),
        scheduled=ScheduledTriggerConfig(
            enabled=bool(sch_raw.get("enabled", False)),
            tasks=scheduled_tasks,
        ),
        script=ScriptTriggerConfig(
            enabled=bool(scr_raw.get("enabled", False)),
            webhook_token=str(scr_raw.get("webhook_token", "") or "").strip(),
            rules=script_rules,
            quote_reply=_as_opt_bool(scr_raw.get("quote_reply")),
            at_user=_as_opt_bool(scr_raw.get("at_user")),
        ),
        private_needs_wake=bool(tr.get("private_needs_wake", False)),
        disable_builtin_commands=bool(tr.get("disable_builtin_commands", False)),
        command_prefixes=_as_str_list(tr.get("command_prefixes")) or ["/"],
        admin_bypass_rate_limit=bool(tr.get("admin_bypass_rate_limit", False)),
    )

    id_raw = raw.get("identities") or {}
    id_def_raw = id_raw.get("default_policy") or {}
    id_profiles: list[IdentityProfileConfig] = []
    for idx, item in enumerate(id_raw.get("profiles") or []):
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id", "") or f"id_{idx + 1}").strip()
        ttype = str(item.get("target_type", "user") or "user").strip().lower()
        if ttype not in ("user", "group"):
            ttype = "user"
        legacy_role = str(item.get("role", "") or "").strip().lower()
        is_admin_val = (
            bool(item["is_admin"])
            if "is_admin" in item
            else (legacy_role in ("owner", "admin") or bool(item.get("allow_bot_commands", False)))
        )
        label = str(item.get("label", "") or item.get("name", "") or ("管理员" if is_admin_val else "成员")).strip()
        id_profiles.append(
            IdentityProfileConfig(
                id=pid,
                name=str(item.get("name", "") or label or pid).strip(),
                adapter_id=str(item.get("adapter_id", "") or "").strip(),
                target_type=ttype,
                target_ids=[x.strip() for x in _as_str_list(item.get("target_ids")) if x.strip()],
                is_admin=is_admin_val,
                label=label,
                enabled=bool(item.get("enabled", True)),
                reply_style=str(item.get("reply_style", "") or ""),
                allowed_tasks=str(item.get("allowed_tasks", "") or ""),
                allow_bot_commands=bool(item.get("allow_bot_commands", is_admin_val)),
                allow_plugins=bool(item.get("allow_plugins", True)),
                bypass_rate_limit=bool(item.get("bypass_rate_limit", False)),
                allow_web_search=_as_opt_bool(item.get("allow_web_search")),
                allow_url_read=_as_opt_bool(item.get("allow_url_read")),
                allow_file_tools=_as_opt_bool(item.get("allow_file_tools")),
                allow_vision=_as_opt_bool(item.get("allow_vision")),
                allow_audio=_as_opt_bool(item.get("allow_audio")),
            )
        )

    identities = IdentitiesConfig(
        enabled=bool(id_raw.get("enabled", False)),
        strict_anti_spoof=bool(id_raw.get("strict_anti_spoof", True)),
        default_policy=IdentityDefaultPolicyConfig(
            label=str(id_def_raw.get("label", "普通群友") or "普通群友").strip(),
            reply_style=str(id_def_raw.get("reply_style", "") or ""),
            allowed_tasks=str(id_def_raw.get("allowed_tasks", "") or ""),
            allow_bot_commands=bool(id_def_raw.get("allow_bot_commands", False)),
            allow_plugins=bool(id_def_raw.get("allow_plugins", True)),
            bypass_rate_limit=bool(id_def_raw.get("bypass_rate_limit", False)),
            allow_web_search=_as_opt_bool(id_def_raw.get("allow_web_search")),
            allow_url_read=_as_opt_bool(id_def_raw.get("allow_url_read")),
            allow_file_tools=_as_opt_bool(id_def_raw.get("allow_file_tools")),
            allow_vision=_as_opt_bool(id_def_raw.get("allow_vision")),
            allow_audio=_as_opt_bool(id_def_raw.get("allow_audio")),
        ),
        profiles=id_profiles,
    )

    sess_raw = raw.get("session") or {}
    session = SessionConfig(
        unique_session=bool(sess_raw.get("unique_session", False)),
    )

    prm_raw = raw.get("prompt") or {}
    prompt = PromptConfig(
        datetime_system_prompt=bool(prm_raw.get("datetime_system_prompt", True)),
        group_name_display=bool(prm_raw.get("group_name_display", True)),
        timezone=str(prm_raw.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
        prompt_template=str(prm_raw.get("prompt_template", "")),
        custom_error_message=str(prm_raw.get("custom_error_message", "")),
    )

    cs_raw = raw.get("content_safety") or {}
    content_safety = ContentSafetyConfig(
        enabled=bool(cs_raw.get("enabled", False)),
        check_input=bool(cs_raw.get("check_input", True)),
        check_output=bool(cs_raw.get("check_output", True)),
        action=str(cs_raw.get("action", "block") or "block"),
        blocked_keywords=_as_str_list(cs_raw.get("blocked_keywords")),
        block_reply=str(cs_raw.get("block_reply", "")),
    )

    pl_raw = raw.get("plugins") or {}
    raw_pl_cfgs = pl_raw.get("configs") or {}
    raw_pl_scopes = pl_raw.get("scopes") or {}
    plugins = PluginsConfig(
        enabled=bool(pl_raw.get("enabled", True)),
        dir=str(pl_raw.get("dir", "./plugins") or "./plugins"),
        disabled=_as_str_list(pl_raw.get("disabled")),
        configs={str(k): dict(v) for k, v in raw_pl_cfgs.items() if isinstance(v, dict)},
        scopes={str(k): dict(v) for k, v in raw_pl_scopes.items() if isinstance(v, dict)},
    )

    col = _as_dict(raw.get("collector"))
    collector = CollectorConfig(
        wait_triggers=_as_str_list(col.get("wait_triggers", ["mention", "quote", "keyword"])),
        quiet_wait_seconds=max(0.0, float(col.get("quiet_wait_seconds", 4.0))),
        max_wait_seconds=max(0.0, float(col.get("max_wait_seconds", 15.0))),
        max_extra_messages=max(0, int(col.get("max_extra_messages", 5))),
        track_only_trigger_user=bool(col.get("track_only_trigger_user", True)),
        context_message_limit=max(1, int(col.get("context_message_limit", 25))),
        history_buffer_size=max(1, int(col.get("history_buffer_size", 100))),
        db_context_enabled=bool(col.get("db_context_enabled", True)),
        compact_max_messages=max(0, int(col.get("compact_max_messages", 60) or 0)),
        compact_drop_by_count=max(1, int(col.get("compact_drop_by_count", 20) or 1)),
        compact_max_tokens=max(0, int(col.get("compact_max_tokens", 12000) or 0)),
        compact_max_bytes=max(0, int(col.get("compact_max_bytes", 0) or 0)),
        compact_drop_by_tokens=max(1, int(col.get("compact_drop_by_tokens", 20) or 1)),
    )

    rl = _as_dict(raw.get("rate_limit"))
    rate_limit = RateLimitConfig(
        max_concurrent_requests=max(1, int(rl.get("max_concurrent_requests", 1))),
        max_queue_size=max(1, int(rl.get("max_queue_size", 8))),
        user_cooldown_seconds=max(0.0, float(rl.get("user_cooldown_seconds", 12.0))),
        channel_min_interval_seconds=max(0.0, float(rl.get("channel_min_interval_seconds", 3.0))),
        max_replies_per_channel_per_minute=max(1, int(rl.get("max_replies_per_channel_per_minute", 6))),
    )

    rep = _as_dict(raw.get("reply"))
    seg_raw = _as_dict(rep.get("segmented_reply"))
    segmented_reply = SegmentedReplyConfig(
        enabled=bool(seg_raw.get("enabled", seg_raw.get("enable", False))),
        only_llm_result=bool(seg_raw.get("only_llm_result", True)),
        interval_method=str(seg_raw.get("interval_method", "random")),
        interval=str(seg_raw.get("interval", "1.5,3.5")),
        log_base=max(1.1, float(seg_raw.get("log_base", 2.6))),
        words_count_threshold=max(1, int(seg_raw.get("words_count_threshold", 10000))),
        split_mode=str(seg_raw.get("split_mode", "words")),
        regex=str(seg_raw.get("regex", r"[\s\S]+?(?:\|\|\||$)")),
        split_words=_as_str_list(seg_raw.get("split_words", ["|||"])),
        content_cleanup_rule=str(seg_raw.get("content_cleanup_rule", "")),
    )
    reply = ReplyConfig(
        quote_trigger_message=bool(rep.get("quote_trigger_message", True)),
        at_trigger_user=bool(rep.get("at_trigger_user", False)),
        reply_prefix=str(rep.get("reply_prefix", "")),
        max_reply_chars=max(50, int(rep.get("max_reply_chars", 800))),
        segmented_reply=segmented_reply,
    )

    ag = _as_dict(raw.get("agy"))
    raw_profiles = ag.get("model_profiles") or {}
    model_profiles: dict[str, ModelProfileConfig] = {}
    if isinstance(raw_profiles, dict):
        for mk, mv in raw_profiles.items():
            if not isinstance(mv, dict) or not str(mk).strip():
                continue
            sb_raw = mv.get("sandbox", None)
            sb_val = None if sb_raw is None else bool(sb_raw)
            model_profiles[str(mk).strip()] = ModelProfileConfig(
                name=str(mv.get("name", "") or "").strip(),
                model=str(mv.get("model", "") or "").strip(),
                effort=str(mv.get("effort", "") or "").strip(),
                max_context_messages=max(0, int(mv.get("max_context_messages", 0) or 0)),
                max_turns=max(0, int(mv.get("max_turns", 0) or 0)),
                compact_max_messages=max(0, int(mv.get("compact_max_messages", 0) or 0)),
                compact_drop_by_count=max(0, int(mv.get("compact_drop_by_count", 0) or 0)),
                compact_max_tokens=max(0, int(mv.get("compact_max_tokens", 0) or 0)),
                compact_max_bytes=max(0, int(mv.get("compact_max_bytes", 0) or 0)),
                compact_drop_by_tokens=max(0, int(mv.get("compact_drop_by_tokens", 0) or 0)),
                execution_mode=str(mv.get("execution_mode", "") or "").strip(),
                sandbox=sb_val,
                disable_slash_commands=bool(mv.get("disable_slash_commands", False)),
                enable_web_search=bool(mv.get("enable_web_search", True)),
                enable_url_read=bool(mv.get("enable_url_read", False)),
                enable_vision=bool(mv.get("enable_vision", True)),
                enable_audio=bool(mv.get("enable_audio", False)),
                enable_file_tools=bool(mv.get("enable_file_tools", False)),
                enable_command=bool(mv.get("enable_command", False)),
            )

    agy = AgyConfig(
        mode=str(ag.get("mode", "agy")),
        executable=str(ag.get("executable", "auto")),
        workspace_dir=str(ag.get("workspace_dir", "./bot_workspace")),
        data_dir=str(ag.get("data_dir", "./data")),
        settings_file=str(ag.get("settings_file", "")),
        proxy=str(ag.get("proxy", "")),
        model=str(ag.get("model", "")),
        fallback_models=_as_str_list(ag.get("fallback_models")),
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
        subagent_enabled=bool(ag.get("subagent_enabled", True)),
        subagent_model=str(ag.get("subagent_model", "") or "").strip(),
        subagent_persona_id=str(ag.get("subagent_persona_id", "") or "").strip(),
        subagent_max_concurrent=max(1, int(ag.get("subagent_max_concurrent", 2))),
        subagent_timeout_seconds=max(15.0, float(ag.get("subagent_timeout_seconds", 240.0))),
        subagent_handoff_to_main=bool(ag.get("subagent_handoff_to_main", True)),
        allow_command_execution=bool(ag.get("allow_command_execution", False)),
        model_profiles=model_profiles,
    )

    wu = _as_dict(raw.get("webui"))
    webui = WebUIConfig(
        enabled=bool(wu.get("enabled", wu.get("enable", False))),
        host=str(wu.get("host", "0.0.0.0")),
        port=int(wu.get("port", 8618)),
        username=str(wu.get("username", "admin")),
        password=str(wu.get("password", "admin123")),
        log_buffer_size=max(100, int(wu.get("log_buffer_size", 1000))),
    )

    lg = _as_dict(raw.get("logging"))
    logging_cfg = LoggingConfig(level=str(lg.get("level", "INFO")).upper())

    return BotConfig(
        bot_name=str(raw.get("bot_name", "Antigravity助手")),
        admins=_as_str_list(raw.get("admins")),
        active_persona_id=str(raw.get("active_persona_id", "")),
        platforms=platforms,
        triggers=triggers,
        identities=identities,
        session=session,
        prompt=prompt,
        content_safety=content_safety,
        plugins=plugins,
        collector=collector,
        rate_limit=rate_limit,
        reply=reply,
        agy=agy,
        webui=webui,
        logging=logging_cfg,
        base_dir=base_dir,
        config_path=config_path,
    )


def load_config(path: str | Path) -> BotConfig:
    cfg_path = Path(path).resolve()
    raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    return load_config_from_dict(raw, base_dir=cfg_path.parent, config_path=cfg_path)


def config_to_dict(cfg: BotConfig) -> dict[str, Any]:
    platforms_list: list[dict[str, Any]] = []
    for p in cfg.platforms:
        item: dict[str, Any] = {
            "id": p.id,
            "type": p.type,
            "enabled": p.enabled,
            "ws_url": p.ws_url,
            "access_token": p.access_token,
            "self_id": p.self_id,
            "user_blocklist": list(p.user_blocklist),
            "private_blocklist": list(p.private_blocklist),
            "reconnect_initial_seconds": p.reconnect_initial_seconds,
            "reconnect_max_seconds": p.reconnect_max_seconds,
            "rpc_timeout_seconds": p.rpc_timeout_seconds,
        }
        if p.extra:
            item["extra"] = dict(p.extra)
        platforms_list.append(item)

    return {
        "bot_name": cfg.bot_name,
        "admins": list(cfg.admins),
        "active_persona_id": cfg.active_persona_id,
        "platforms": platforms_list,
        "triggers": {
            "mention": {
                "enabled": cfg.triggers.mention.enabled,
                "allow_at_all": cfg.triggers.mention.allow_at_all,
                "include_quote_at": cfg.triggers.mention.include_quote_at,
                "unknown_reply_at_behavior": cfg.triggers.mention.unknown_reply_at_behavior,
                "quote_reply": cfg.triggers.mention.quote_reply,
                "at_user": cfg.triggers.mention.at_user,
            },
            "quote": {
                "enabled": cfg.triggers.quote.enabled,
                "quote_reply": cfg.triggers.quote.quote_reply,
                "at_user": cfg.triggers.quote.at_user,
            },
            "keyword": {
                "enabled": cfg.triggers.keyword.enabled,
                "keywords": list(cfg.triggers.keyword.keywords),
                "regex_patterns": list(cfg.triggers.keyword.regex_patterns),
                "case_sensitive": cfg.triggers.keyword.case_sensitive,
                "exclude_keywords": list(cfg.triggers.keyword.exclude_keywords),
                "quote_reply": cfg.triggers.keyword.quote_reply,
                "at_user": cfg.triggers.keyword.at_user,
            },
            "dice": {
                "enabled": cfg.triggers.dice.enabled,
                "probability": cfg.triggers.dice.probability,
                "min_message_length": cfg.triggers.dice.min_message_length,
                "cooldown_seconds": cfg.triggers.dice.cooldown_seconds,
                "ignore_prefixes": list(cfg.triggers.dice.ignore_prefixes),
                "quote_reply": cfg.triggers.dice.quote_reply,
                "at_user": cfg.triggers.dice.at_user,
            },
            "context_llm": {
                "enabled": cfg.triggers.context_llm.enabled,
                "min_unread_messages": cfg.triggers.context_llm.min_unread_messages,
                "min_interval_seconds": cfg.triggers.context_llm.min_interval_seconds,
                "judge_context_window": cfg.triggers.context_llm.judge_context_window,
                "cooldown_after_reply_seconds": cfg.triggers.context_llm.cooldown_after_reply_seconds,
                "quote_reply": cfg.triggers.context_llm.quote_reply,
                "at_user": cfg.triggers.context_llm.at_user,
            },
            "scheduled": {
                "enabled": cfg.triggers.scheduled.enabled,
                "tasks": [
                    {
                        "id": t.id,
                        "name": t.name,
                        "enabled": t.enabled,
                        "schedule_type": t.schedule_type,
                        "cron": t.cron,
                        "interval_seconds": t.interval_seconds,
                        "adapter_id": t.adapter_id,
                        "scene": t.scene,
                        "target_ids": list(t.target_ids),
                        "action_mode": t.action_mode,
                        "prompt": t.prompt,
                        "include_context": t.include_context,
                        "at_qq": t.at_qq,
                    }
                    for t in cfg.triggers.scheduled.tasks
                ],
            },
            "script": {
                "enabled": cfg.triggers.script.enabled,
                "webhook_token": cfg.triggers.script.webhook_token,
                "quote_reply": cfg.triggers.script.quote_reply,
                "at_user": cfg.triggers.script.at_user,
                "rules": [
                    {
                        "id": r.id,
                        "name": r.name,
                        "enabled": r.enabled,
                        "mode": r.mode,
                        "expr": r.expr,
                        "script_path": r.script_path,
                        "command": r.command,
                        "action_mode": r.action_mode,
                        "prompt_override": r.prompt_override,
                        "reply_text": r.reply_text,
                        "cooldown_seconds": r.cooldown_seconds,
                        "timeout_seconds": r.timeout_seconds,
                    }
                    for r in cfg.triggers.script.rules
                ],
            },
            "private_needs_wake": cfg.triggers.private_needs_wake,
            "disable_builtin_commands": cfg.triggers.disable_builtin_commands,
            "command_prefixes": list(cfg.triggers.command_prefixes),
            "admin_bypass_rate_limit": cfg.triggers.admin_bypass_rate_limit,
        },
        "identities": {
            "enabled": cfg.identities.enabled,
            "strict_anti_spoof": cfg.identities.strict_anti_spoof,
            "default_policy": {
                "label": cfg.identities.default_policy.label,
                "reply_style": cfg.identities.default_policy.reply_style,
                "allowed_tasks": cfg.identities.default_policy.allowed_tasks,
                "allow_bot_commands": cfg.identities.default_policy.allow_bot_commands,
                "allow_plugins": cfg.identities.default_policy.allow_plugins,
                "bypass_rate_limit": cfg.identities.default_policy.bypass_rate_limit,
                "allow_web_search": cfg.identities.default_policy.allow_web_search,
                "allow_url_read": cfg.identities.default_policy.allow_url_read,
                "allow_file_tools": cfg.identities.default_policy.allow_file_tools,
                "allow_vision": cfg.identities.default_policy.allow_vision,
                "allow_audio": cfg.identities.default_policy.allow_audio,
            },
            "profiles": [
                {
                    "id": p.id,
                    "name": p.name,
                    "adapter_id": p.adapter_id,
                    "target_type": p.target_type,
                    "target_ids": list(p.target_ids),
                    "is_admin": p.is_admin,
                    "label": p.label,
                    "enabled": p.enabled,
                    "reply_style": p.reply_style,
                    "allowed_tasks": p.allowed_tasks,
                    "allow_bot_commands": p.allow_bot_commands,
                    "allow_plugins": p.allow_plugins,
                    "bypass_rate_limit": p.bypass_rate_limit,
                    "allow_web_search": p.allow_web_search,
                    "allow_url_read": p.allow_url_read,
                    "allow_file_tools": p.allow_file_tools,
                    "allow_vision": p.allow_vision,
                    "allow_audio": p.allow_audio,
                }
                for p in cfg.identities.profiles
            ],
        },
        "session": {
            "unique_session": cfg.session.unique_session,
        },
        "prompt": {
            "datetime_system_prompt": cfg.prompt.datetime_system_prompt,
            "group_name_display": cfg.prompt.group_name_display,
            "timezone": cfg.prompt.timezone,
            "prompt_template": cfg.prompt.prompt_template,
            "custom_error_message": cfg.prompt.custom_error_message,
        },
        "content_safety": {
            "enabled": cfg.content_safety.enabled,
            "check_input": cfg.content_safety.check_input,
            "check_output": cfg.content_safety.check_output,
            "action": cfg.content_safety.action,
            "blocked_keywords": list(cfg.content_safety.blocked_keywords),
            "block_reply": cfg.content_safety.block_reply,
        },
        "plugins": {
            "enabled": cfg.plugins.enabled,
            "dir": cfg.plugins.dir,
            "disabled": list(cfg.plugins.disabled),
            "configs": {k: dict(v) for k, v in cfg.plugins.configs.items()},
            "scopes": {k: dict(v) for k, v in cfg.plugins.scopes.items()},
        },
        "collector": {
            "wait_triggers": list(cfg.collector.wait_triggers),
            "quiet_wait_seconds": cfg.collector.quiet_wait_seconds,
            "max_wait_seconds": cfg.collector.max_wait_seconds,
            "max_extra_messages": cfg.collector.max_extra_messages,
            "track_only_trigger_user": cfg.collector.track_only_trigger_user,
            "context_message_limit": cfg.collector.context_message_limit,
            "history_buffer_size": cfg.collector.history_buffer_size,
            "db_context_enabled": cfg.collector.db_context_enabled,
            "compact_max_messages": cfg.collector.compact_max_messages,
            "compact_drop_by_count": cfg.collector.compact_drop_by_count,
            "compact_max_tokens": cfg.collector.compact_max_tokens,
            "compact_max_bytes": cfg.collector.compact_max_bytes,
            "compact_drop_by_tokens": cfg.collector.compact_drop_by_tokens,
        },
        "rate_limit": {
            "max_concurrent_requests": cfg.rate_limit.max_concurrent_requests,
            "max_queue_size": cfg.rate_limit.max_queue_size,
            "user_cooldown_seconds": cfg.rate_limit.user_cooldown_seconds,
            "channel_min_interval_seconds": cfg.rate_limit.channel_min_interval_seconds,
            "max_replies_per_channel_per_minute": cfg.rate_limit.max_replies_per_channel_per_minute,
        },
        "reply": {
            "quote_trigger_message": cfg.reply.quote_trigger_message,
            "at_trigger_user": cfg.reply.at_trigger_user,
            "reply_prefix": cfg.reply.reply_prefix,
            "max_reply_chars": cfg.reply.max_reply_chars,
            "segmented_reply": {
                "enabled": cfg.reply.segmented_reply.enabled,
                "only_llm_result": cfg.reply.segmented_reply.only_llm_result,
                "interval_method": cfg.reply.segmented_reply.interval_method,
                "interval": cfg.reply.segmented_reply.interval,
                "log_base": cfg.reply.segmented_reply.log_base,
                "words_count_threshold": cfg.reply.segmented_reply.words_count_threshold,
                "split_mode": cfg.reply.segmented_reply.split_mode,
                "regex": cfg.reply.segmented_reply.regex,
                "split_words": list(cfg.reply.segmented_reply.split_words),
                "content_cleanup_rule": cfg.reply.segmented_reply.content_cleanup_rule,
            },
        },
        "agy": {
            "mode": cfg.agy.mode,
            "executable": cfg.agy.executable,
            "workspace_dir": cfg.agy.workspace_dir,
            "data_dir": cfg.agy.data_dir,
            "settings_file": cfg.agy.settings_file,
            "proxy": cfg.agy.proxy,
            "model": cfg.agy.model,
            "fallback_models": list(cfg.agy.fallback_models),
            "judge_model": cfg.agy.judge_model,
            "effort": cfg.agy.effort,
            "agent": cfg.agy.agent,
            "sandbox": cfg.agy.sandbox,
            "max_warm_processes": cfg.agy.max_warm_processes,
            "max_turns_per_session": cfg.agy.max_turns_per_session,
            "turn_timeout_seconds": cfg.agy.turn_timeout_seconds,
            "judge_timeout_seconds": cfg.agy.judge_timeout_seconds,
            "resume_on_restart": cfg.agy.resume_on_restart,
            "rate_limit_cooldown_seconds": cfg.agy.rate_limit_cooldown_seconds,
            "fatal_error_pause_seconds": cfg.agy.fatal_error_pause_seconds,
            "subagent_enabled": cfg.agy.subagent_enabled,
            "subagent_model": cfg.agy.subagent_model,
            "subagent_persona_id": cfg.agy.subagent_persona_id,
            "subagent_max_concurrent": cfg.agy.subagent_max_concurrent,
            "subagent_timeout_seconds": cfg.agy.subagent_timeout_seconds,
            "subagent_handoff_to_main": cfg.agy.subagent_handoff_to_main,
            "allow_command_execution": cfg.agy.allow_command_execution,
            "model_profiles": {
                k: {
                    "name": v.name,
                    "model": v.model,
                    "effort": v.effort,
                    "max_context_messages": v.max_context_messages,
                    "max_turns": v.max_turns,
                    "compact_max_messages": v.compact_max_messages,
                    "compact_drop_by_count": v.compact_drop_by_count,
                    "compact_max_tokens": v.compact_max_tokens,
                    "compact_max_bytes": v.compact_max_bytes,
                    "compact_drop_by_tokens": v.compact_drop_by_tokens,
                    "execution_mode": v.execution_mode,
                    "sandbox": v.sandbox,
                    "disable_slash_commands": v.disable_slash_commands,
                    "enable_web_search": v.enable_web_search,
                    "enable_url_read": v.enable_url_read,
                    "enable_vision": v.enable_vision,
                    "enable_audio": v.enable_audio,
                    "enable_file_tools": v.enable_file_tools,
                    "enable_command": v.enable_command,
                }
                for k, v in cfg.agy.model_profiles.items()
            },
        },
        "webui": {
            "enabled": cfg.webui.enabled,
            "host": cfg.webui.host,
            "port": cfg.webui.port,
            "username": cfg.webui.username,
            "password": cfg.webui.password,
            "log_buffer_size": cfg.webui.log_buffer_size,
        },
        "logging": {
            "level": cfg.logging.level,
        },
    }


def save_config(cfg: BotConfig, path: str | Path | None = None) -> Path:
    target = Path(path).resolve() if path is not None else cfg.config_path
    if target is None:
        raise ValueError("未指定 config.json 保存路径")
    data = config_to_dict(cfg)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target
