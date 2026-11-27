from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import inspect
import ipaddress
import json
import logging
import re
import shutil
import socket
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .adapters import AdapterRegistry
from .channel_guard import (
    ChannelActivationStore,
    is_non_friend_private_message,
    is_strict_explicit_at_bot,
)
from .collector import ActiveWindowState, DebouncedReplyManager, GroupContextStore
from .config import BotConfig, ModelProfileConfig, PromptConfig, ScheduledTaskConfig, save_config
from .extensions import McpManager, SkillsManager
from .identities import IdentityResolver, ResolvedIdentity
from .media import (
    _ALLOWED_EXTS,
    _decode_base64_source,
    _fetch_http_bytes,
    _resolve_local_file,
    detect_image_ext,
    looks_like_image_bytes,
    materialize_snapshot_images,
    prune_media_cache,
    sanitize_reference_image,
)
from .models import ContextSnapshot, IncomingMessage, JudgeDecision, MediaAttachment, RunnerResult, TriggerType
from .persona_store import PersonaStore
from .usage_store import TokenUsageStore
from .plugins import PluginManager
from .plugins.manager import _normalize_reply as _normalize_plugin_reply
from .plugins.sdk import PLUGIN_SUBAGENT_CTX
from .proxy_guard import (
    ProbeOutcome,
    discover_proxy_controller,
    extract_local_proxy_port,
    fetch_selector_group,
    is_allowed_proxy_node,
    is_auth_required_error,
    is_geo_blocked_error,
    is_proxy_or_geo_error,
    select_best_working_node,
)
from .reply_formatter import apply_content_safety, compute_segment_delay, split_and_clean_reply
from .runner import BaseAgentRunner, build_runner
from .triggers import RateLimiter, ScheduledTriggerRunner, ScriptTriggerEvaluator, TriggerEngine
from .webui.log_buffer import LogRingBufferHandler

logger = logging.getLogger(__name__)

_BG_TASK_RE = re.compile(r"<agy_bg_task>\s*([\s\S]*?)\s*</agy_bg_task>", re.IGNORECASE)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\r\n]+)\)")
_BARE_IMAGE_URL_RE = re.compile(r"https?://[^\s<>'\"`)]+\.(?:png|jpe?g|gif|webp|bmp)(?:\?[^\s<>'\"`)]*)?", re.IGNORECASE)


def extract_bg_tasks(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Strip hidden `<agy_bg_task>{...}</agy_bg_task>` directives from main-agent output and return parsed specs."""
    if not text or "<agy_bg_task>" not in text.lower():
        return text, []
    tasks: list[dict[str, Any]] = []
    for match in _BG_TASK_RE.finditer(text):
        raw_json = match.group(1).strip()
        try:
            obj = json.loads(raw_json)
        except Exception:
            logger.warning("解析 <agy_bg_task> JSON 失败，已忽略: %s", raw_json[:200])
            continue
        if not isinstance(obj, dict):
            continue
        task_desc = str(obj.get("task") or obj.get("prompt") or obj.get("description") or "").strip()
        if not task_desc:
            continue
        raw_imgs = obj.get("images") or obj.get("image_paths") or []
        if isinstance(raw_imgs, str):
            raw_imgs = [raw_imgs]
        images = [str(x).strip() for x in raw_imgs if str(x).strip()] if isinstance(raw_imgs, list) else []
        tasks.append(
            {
                "mcp": str(obj.get("mcp") or obj.get("server") or "").strip(),
                "tool": str(obj.get("tool") or obj.get("tool_name") or "").strip(),
                "task": task_desc,
                "images": images,
                "model": str(obj.get("model") or "").strip(),
            }
        )
    cleaned = _BG_TASK_RE.sub("", text).strip()
    return cleaned, tasks


_MAX_LOCAL_IMAGE_BYTES = 25 * 1024 * 1024


def _looks_like_image_bytes(data: bytes) -> bool:
    return looks_like_image_bytes(data)


def _is_non_public_ip(ip_str: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


async def _is_safe_public_http_url(url: str) -> bool:
    """Reject loopback/private/link-local targets to mitigate SSRF when fetching model-supplied image URLs."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    if host in ("localhost", "ip6-localhost", "metadata.google.internal") or host.endswith((".local", ".internal", ".localhost")):
        return False
    if _is_non_public_ip(host):
        return False
    try:
        # Resolve DNS in worker thread to avoid blocking the event loop
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM),
            timeout=5.0,
        )
        for info in infos:
            addr = info[4][0]
            if _is_non_public_ip(addr):
                return False
    except Exception:
        # If DNS resolution fails locally (e.g., proxy-only DNS), only block explicit IP literals/local names checked above
        pass
    return True


def _safe_fromtimestamp(ts: float) -> dt.datetime:
    try:
        val = float(ts)
        if val > 1e11:
            val /= 1000.0
        if val < 86400.0 or val > 4102444800.0:
            return dt.datetime.now()
        return dt.datetime.fromtimestamp(val)
    except (OSError, OverflowError, ValueError, TypeError):
        return dt.datetime.now()


def _format_ts(ts: float) -> str:
    return _safe_fromtimestamp(ts).strftime("%H:%M:%S")


def _inject_image_paths(
    text: str,
    attachments: list[MediaAttachment],
    *,
    vision_enabled: bool,
) -> tuple[str, list[str]]:
    """Replace `[图片]` markers with materialized workspace paths (or ignore markers when vision is off)."""
    if not vision_enabled:
        return text.replace("[图片]", "[图片已忽略]"), []

    img_atts = [a for a in attachments if a.kind == "image"]
    if not img_atts:
        return text, []

    used_paths: list[str] = []
    remaining_tags: list[str] = []
    out = text
    cursor = 0

    for att in img_atts:
        summary_part = f"({att.summary})" if att.summary else ""
        if att.local_path:
            tag = f"[图片{summary_part}: {att.local_path}]"
            used_paths.append(att.local_path)
        else:
            tag = f"[图片{summary_part}]"

        idx = out.find("[图片]", cursor)
        if idx != -1:
            out = out[:idx] + tag + out[idx + len("[图片]") :]
            cursor = idx + len(tag)
        else:
            remaining_tags.append(tag)

    if remaining_tags:
        sep = " " if out and not out.endswith(("\n", " ")) else ""
        out = f"{out}{sep}{' '.join(remaining_tags)}"

    return out, used_paths


def _render_context_messages(
    messages: list[IncomingMessage],
    *,
    bot_name: str,
    snapshot: ContextSnapshot,
    pcfg: PromptConfig,
    vision_enabled: bool,
    audio_enabled: bool,
    identity_resolver: IdentityResolver | None = None,
    immutable_lines: bool = False,
) -> tuple[list[str], list[str]]:
    lines: list[str] = []
    all_image_paths: list[str] = []
    for m in messages:
        seq_prefix = f"[#{m.db_seq}]" if m.db_seq > 0 else ""
        if pcfg.datetime_system_prompt:
            prefix_str = f"{seq_prefix}[{_format_ts(m.timestamp)}] "
        else:
            prefix_str = f"{seq_prefix} " if seq_prefix else ""

        if m.is_self:
            speaker = f"{bot_name}(我)"
        else:
            badge = (
                identity_resolver.speaker_badge(m.user_id, adapter_id=snapshot.adapter_id)
                if (identity_resolver is not None and identity_resolver.enabled)
                else ""
            )
            badge_str = f"[身份:{badge}]" if badge else ""
            speaker = f"{m.sender_name}({m.user_id}){badge_str}"

        tag_str = ""
        if not immutable_lines:
            tags: list[str] = []
            if m.message_id and m.message_id in snapshot.trigger_message_ids:
                tags.append("触发消息")
            elif m.message_id and m.message_id in snapshot.followup_message_ids:
                tags.append("追发补充")
            tag_str = f" [{'/'.join(tags)}]" if tags else ""

        reply_atts = [a for a in m.attachments if a.source == "reply"]
        body_atts = [a for a in m.attachments if a.source != "reply"]

        quote_part = ""
        if m.reply_to_msg_id:
            who = "我" if m.is_quoted_bot else (m.reply_to_user_id or "某人")
            raw_quoted = m.reply_to_text or ""
            rendered_quoted, q_paths = _inject_image_paths(
                raw_quoted,
                reply_atts,
                vision_enabled=vision_enabled,
            )
            all_image_paths.extend(q_paths)
            if not audio_enabled:
                rendered_quoted = rendered_quoted.replace("[语音]", "[语音已忽略]")
            if rendered_quoted:
                if "\n" in rendered_quoted:
                    indented_q = "\n".join(f"  {qline}" for qline in rendered_quoted.splitlines())
                    quote_part = f" [引用 {who}:\n{indented_q}\n]"
                else:
                    quote_part = f' [引用 {who}: "{rendered_quoted}"]'
            else:
                quote_part = f" [引用 {who}]"

        msg_text, b_paths = _inject_image_paths(
            m.text,
            body_atts,
            vision_enabled=vision_enabled,
        )
        all_image_paths.extend(b_paths)
        if not audio_enabled:
            msg_text = msg_text.replace("[语音]", "[语音已忽略]")

        if "\n" in msg_text:
            lines.append(f"{prefix_str}{speaker}{tag_str}{quote_part}:\n{msg_text}")
        else:
            lines.append(f"{prefix_str}{speaker}{tag_str}{quote_part}: {msg_text}")
    return lines, all_image_paths


def _build_archive_hint(session_key: str, window_state: ActiveWindowState | None) -> str:
    if window_state is None or window_state.archived_count <= 0:
        return ""
    return (
        f"[历史归档提示] 当前群聊/会话共有 {window_state.archived_count} 条更早的历史消息"
        f"（序号 #{window_state.archived_min_seq} ~ #{window_state.archived_max_seq}，早于当前活跃窗口起点 #{window_state.active_min_seq}）"
        f"已因上下文压缩策略移出当前轮上下文，但均完整保存在 SQLite 数据库中（包含群友消息与你自己的历史回复）。"
        f"若当前问题需要回忆或引用更早的聊天内容、约定或你之前的发言，请主动调用 `chat_history` MCP 工具检索"
        f"（务必传入 `session_key=\"{session_key}\"`，例如 `get_older_messages(session_key=\"{session_key}\", before_seq={window_state.active_min_seq})` "
        f"或 `search_chat_history(session_key=\"{session_key}\", keyword=\"...\")`）。"
    )


def format_context_prompt(
    snapshot: ContextSnapshot,
    bot_name: str,
    prompt_cfg: PromptConfig | None = None,
    model_profile: ModelProfileConfig | None = None,
    identity: ResolvedIdentity | None = None,
    identity_resolver: IdentityResolver | None = None,
    window_state: ActiveWindowState | None = None,
) -> str:
    pcfg = prompt_cfg or PromptConfig()
    trigger_user = snapshot.trigger_message
    now_dt = _safe_fromtimestamp(snapshot.settled_at)
    date_str = now_dt.strftime("%Y-%m-%d")
    time_str = now_dt.strftime("%H:%M:%S")
    now_str = f"{date_str} {time_str}"
    trigger_labels = "+".join(snapshot.matched_triggers) if snapshot.matched_triggers else snapshot.primary_trigger

    messages = snapshot.messages
    if window_state is None and model_profile is not None and model_profile.max_context_messages > 0:
        messages = messages[-model_profile.max_context_messages :]

    vision_enabled = model_profile.enable_vision if model_profile is not None else True
    audio_enabled = model_profile.enable_audio if model_profile is not None else True

    lines, all_image_paths = _render_context_messages(
        messages,
        bot_name=bot_name,
        snapshot=snapshot,
        pcfg=pcfg,
        vision_enabled=vision_enabled,
        audio_enabled=audio_enabled,
        identity_resolver=identity_resolver,
        immutable_lines=(window_state is not None),
    )

    time_clause = f" | 截止时间={now_str}" if pcfg.datetime_system_prompt else ""
    modality_notes: list[str] = []
    if model_profile is not None:
        if not model_profile.enable_vision:
            modality_notes.append("当前模型未开启识图能力，请勿读取图片内容")
        if not model_profile.enable_audio:
            modality_notes.append("当前模型未开启语音理解能力")
    mod_clause = f"（{'；'.join(modality_notes)}）" if modality_notes else ""

    custom_tpl = ""
    if pcfg.prompt_template.strip():
        custom_tpl = (
            pcfg.prompt_template.strip()
            .replace("{{bot_name}}", bot_name)
            .replace("{{date}}", date_str)
            .replace("{{time}}", time_str)
        )

    id_block = ""
    if identity is not None and identity.active:
        id_block = identity.build_prompt_block(
            adapter_id=snapshot.adapter_id,
            user_id=trigger_user.user_id,
            sender_name=trigger_user.sender_name,
            scene=snapshot.scene,
            channel_id=snapshot.channel_id,
            effective_profile=model_profile,
        )

    # Prefix-cache optimized layout when SQLite active-window context mode is active:
    # static channel metadata + immutable message history at the very top, dynamic turn metadata at the tail.
    if window_state is not None:
        out: list[str] = []
        if pcfg.group_name_display:
            out.append(
                f"[聊天场景] 平台={snapshot.platform} | 实例={snapshot.adapter_id} | "
                f"类型={'群聊' if snapshot.scene == 'group' else '私聊'} | 会话ID={snapshot.channel_id} | "
                f"session_key={snapshot.session_key}"
            )
        out.append("--- 会话活跃上下文记录（按时间与序号正序） ---")
        out.extend(lines)
        out.append("")
        out.append("--- 本轮触发说明 ---")
        if custom_tpl:
            out.append(custom_tpl)
        if id_block:
            out.append(id_block)
        trig_seqs = [
            f"#{m.db_seq}"
            for m in messages
            if m.db_seq > 0 and m.message_id and m.message_id in snapshot.trigger_message_ids
        ]
        if not trig_seqs and trigger_user.db_seq > 0:
            trig_seqs = [f"#{trigger_user.db_seq}"]
        seq_clause = f" | 触发消息序号={','.join(trig_seqs)}" if trig_seqs else ""
        out.append(
            f"[上下文说明] 主要触发用户={trigger_user.sender_name}({trigger_user.user_id}) | "
            f"触发方式={trigger_labels} ({snapshot.trigger_reason}){seq_clause} | "
            f"追发补充条数={snapshot.extra_messages_collected}{time_clause}。"
            f"请作为「{bot_name}」结合上方上下文直接给出自然、简洁的回复内容{mod_clause}："
        )
        arch_hint = _build_archive_hint(snapshot.session_key, window_state)
        if arch_hint:
            out.append(arch_hint)
        if vision_enabled and all_image_paths:
            unique_paths = list(dict.fromkeys(all_image_paths))
            out.append(
                f"[图片附件说明] 上下文中包含 {len(unique_paths)} 张图片，均已下载至工作区相对路径"
                f"（见各消息中的 `[图片: .media_cache/...]` 标注）。"
                f"请直接调用 `read_file` 工具查看相关图片内容后再结合上下文自然回复。"
            )
        if snapshot.prompt_override.strip():
            out.append(f"[触发附加指令] {snapshot.prompt_override.strip()}")
        return "\n".join(out)

    header: list[str] = []
    if custom_tpl:
        header.append(custom_tpl)
        header.append("")

    if id_block:
        header.append(id_block)
        header.append("")

    if pcfg.group_name_display:
        header.append(
            f"[聊天场景] 平台={snapshot.platform} | 实例={snapshot.adapter_id} | "
            f"类型={'群聊' if snapshot.scene == 'group' else '私聊'} | 会话ID={snapshot.channel_id} | "
            f"触发方式={trigger_labels} ({snapshot.trigger_reason})"
        )

    header.append(
        f"[上下文说明] 主要触发用户={trigger_user.sender_name}({trigger_user.user_id}) | "
        f"追发补充条数={snapshot.extra_messages_collected}{time_clause}。"
        f"请作为「{bot_name}」结合以下上下文直接给出自然、简洁的回复内容{mod_clause}："
    )
    if vision_enabled and all_image_paths:
        unique_paths = list(dict.fromkeys(all_image_paths))
        header.append(
            f"[图片附件说明] 上下文中包含 {len(unique_paths)} 张图片，均已下载至工作区相对路径"
            f"（见各消息中的 `[图片: .media_cache/...]` 标注）。"
            f"请直接调用 `read_file` 工具查看相关图片内容后再结合上下文自然回复。"
        )
    header.append("")
    header.append("--- 最近消息上下文（按时间正序） ---")

    if snapshot.prompt_override.strip():
        lines.append("")
        lines.append(f"[触发附加指令] {snapshot.prompt_override.strip()}")

    return "\n".join(header + lines)


def format_delta_prompt(
    snapshot: ContextSnapshot,
    delta_messages: list[IncomingMessage],
    bot_name: str,
    prompt_cfg: PromptConfig | None = None,
    model_profile: ModelProfileConfig | None = None,
    identity: ResolvedIdentity | None = None,
    identity_resolver: IdentityResolver | None = None,
    window_state: ActiveWindowState | None = None,
) -> str:
    """Render an append-only incremental turn prompt for an already-seeded warm `agy` session
    so previous turns remain 100% intact for Gemini KV prefix cache hits."""
    pcfg = prompt_cfg or PromptConfig()
    trigger_user = snapshot.trigger_message
    now_dt = _safe_fromtimestamp(snapshot.settled_at)
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    trigger_labels = "+".join(snapshot.matched_triggers) if snapshot.matched_triggers else snapshot.primary_trigger

    vision_enabled = model_profile.enable_vision if model_profile is not None else True
    audio_enabled = model_profile.enable_audio if model_profile is not None else True

    lines, delta_image_paths = _render_context_messages(
        delta_messages,
        bot_name=bot_name,
        snapshot=snapshot,
        pcfg=pcfg,
        vision_enabled=vision_enabled,
        audio_enabled=audio_enabled,
        identity_resolver=identity_resolver,
        immutable_lines=True,
    )

    out: list[str] = [
        f"--- 新增会话消息（接续上文 | session_key={snapshot.session_key}） ---",
        *lines,
        "",
    ]
    if identity is not None and identity.active:
        id_block = identity.build_prompt_block(
            adapter_id=snapshot.adapter_id,
            user_id=trigger_user.user_id,
            sender_name=trigger_user.sender_name,
            scene=snapshot.scene,
            channel_id=snapshot.channel_id,
            effective_profile=model_profile,
        )
        if id_block:
            out.append(id_block)

    time_clause = f" | 截止时间={now_str}" if pcfg.datetime_system_prompt else ""
    trig_seq = f" | 触发序号=#{trigger_user.db_seq}" if trigger_user.db_seq > 0 else ""
    out.append(
        f"[本轮触发] 触发用户={trigger_user.sender_name}({trigger_user.user_id}) | "
        f"触发方式={trigger_labels} ({snapshot.trigger_reason}){trig_seq}{time_clause}。"
        f"请作为「{bot_name}」结合历史对话与以上新增消息自然、简洁地回复："
    )
    arch_hint = _build_archive_hint(snapshot.session_key, window_state)
    if arch_hint:
        out.append(arch_hint)
    if vision_enabled and delta_image_paths:
        unique_paths = list(dict.fromkeys(delta_image_paths))
        out.append(
            f"[图片附件说明] 本轮新增 {len(unique_paths)} 张图片（见上方 `[图片: .media_cache/...]` 标注），"
            f"请调用 `read_file` 查看后再回复。"
        )
    if snapshot.prompt_override.strip():
        out.append(f"[触发附加指令] {snapshot.prompt_override.strip()}")
    return "\n".join(out)


class BotApplication:
    """Wires forward-WS adapters, trigger engine, debouncer, agy runner, external plugins, and WebUI."""

    def __init__(
        self,
        config: BotConfig,
        *,
        registry: AdapterRegistry | None = None,
        runner: BaseAgentRunner | None = None,
        trigger_engine: TriggerEngine | None = None,
        log_handler: LogRingBufferHandler | None = None,
    ) -> None:
        self.config = config
        self._started_at = time.time()
        self._log_handler = log_handler
        self._webui_server: Any = None
        workspace_dir = config.resolve_path(config.agy.workspace_dir)
        data_dir = config.resolve_path(config.agy.data_dir)
        settings_file = (
            config.resolve_path(config.agy.settings_file)
            if config.agy.settings_file.strip()
            else None
        )
        self._workspace_dir = workspace_dir
        self._data_dir = data_dir
        subagent_workspace_dir = data_dir / "subagent_workspace"
        subagent_workspace_dir.mkdir(parents=True, exist_ok=True)
        self._subagent_workspace_dir = subagent_workspace_dir

        use_db_ctx = bool(config.collector.db_context_enabled)
        self.context_store = GroupContextStore(
            history_limit=max(config.collector.history_buffer_size, config.collector.compact_max_messages + 10),
            db_path=(data_dir / "chat_history.db") if use_db_ctx else None,
            persist_file=data_dir / "context_history.json",
        )
        self.registry = registry or AdapterRegistry()
        self.registry.build_adapters(
            config.platforms,
            message_lookup=self.context_store.lookup_message,
            on_group_ban=self.handle_group_ban,
            default_proxy=config.agy.proxy,
        )

        self.channel_guard = ChannelActivationStore(data_dir=data_dir)
        self.usage_store = TokenUsageStore(data_dir / "token_usage.db")
        self.skills_manager = SkillsManager(workspace_dir=workspace_dir)
        self.mcp_manager = McpManager(
            workspace_dir=workspace_dir,
            data_dir=data_dir,
            settings_file=settings_file,
            subagent_workspace_dir=subagent_workspace_dir,
        )
        self.mcp_manager.configure_builtin_history_server(
            db_path=self.context_store.db_path,
            enabled=use_db_ctx,
        )
        self.persona_store = PersonaStore(data_dir=data_dir, workspace_dir=workspace_dir)
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        if (
            (config.active_persona_id and self.persona_store.get(config.active_persona_id) is not None)
            or (not config.active_persona_id and self.persona_store.get_global_dialogs())
            or self.mcp_manager.get_usage_guides()
        ):
            self.sync_workspace_instructions()
        else:
            self.sync_subagent_workspace_instructions()

        self.runner = runner or build_runner(
            config.agy,
            config.rate_limit,
            workspace_dir=workspace_dir,
            data_dir=data_dir,
            settings_file=settings_file,
            subagent_workspace_dir=subagent_workspace_dir,
        )
        self.rate_limiter = RateLimiter(config.rate_limit)
        self.triggers = trigger_engine or TriggerEngine(config.triggers)
        self.triggers.set_context_judge(self._judge_context)
        self.identity_resolver = IdentityResolver(config.identities)
        self.script_triggers = ScriptTriggerEvaluator(
            config.triggers.script,
            base_dir=config.base_dir,
        )
        self.scheduler = ScheduledTriggerRunner(
            config.triggers.scheduled,
            timezone=config.prompt.timezone,
            on_fire=self._on_scheduled_task_fire,
        )

        self.debouncer = DebouncedReplyManager(
            config.collector,
            self.context_store,
            self._on_snapshot_ready,
        )

        plugins_dir = config.resolve_path(config.plugins.dir)
        self.plugin_manager = PluginManager(self, plugins_dir=plugins_dir, data_dir=data_dir)
        self._plugins_loaded = False
        self._proxy_failover_lock = asyncio.Lock()
        self._last_proxy_failover_mono = -1e9
        self._last_proxy_failover_ok_mono = -1e9
        self._geo_blocked_nodes: dict[str, float] = {}  # node_name -> monotonic expiry timestamp

    def _track_bg_task(self, task: asyncio.Task[Any]) -> None:
        self._bg_tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._bg_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                logger.error("后台任务 %s 未捕获异常: %s", t.get_name(), exc, exc_info=exc)

        task.add_done_callback(_done)

    async def ensure_plugins_loaded(self) -> None:
        if not self._plugins_loaded:
            await self.plugin_manager.load_all()
            self._plugins_loaded = True

    def _runner_session_key(self, snapshot: ContextSnapshot) -> str:
        if self.config.session.unique_session and snapshot.scene == "group":
            return f"{snapshot.session_key}:u:{snapshot.trigger_message.user_id}"
        return snapshot.session_key

    async def _judge_context(
        self,
        session_key: str,
        messages: list[IncomingMessage],
    ) -> JudgeDecision:
        return await self.runner.judge_context(session_key, messages)

    def _match_command_prefix(self, text: str) -> tuple[str, str] | None:
        prefixes = self.config.triggers.command_prefixes or ["/"]
        for p in prefixes:
            if p and text.startswith(p):
                body = text[len(p) :].strip()
                if not body:
                    return None
                parts = body.split(None, 1)
                return parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        return None

    async def _handle_admin_command(self, msg: IncomingMessage, *, identity: ResolvedIdentity | None = None) -> bool:
        """Handle built-in admin commands (/status, /reset, /model, /persona, /plugins, /help)."""
        if self.config.triggers.disable_builtin_commands:
            return False
        is_cfg_admin = bool(self.config.admins and msg.user_id in self.config.admins)
        is_id_admin = bool(identity is not None and identity.active and identity.allow_bot_commands)
        if not (is_cfg_admin or is_id_admin):
            return False
        parsed = self._match_command_prefix(msg.text.strip())
        if parsed is None:
            return False

        cmd, arg = parsed
        adapter = self.registry.get(msg.adapter_id)
        if adapter is None:
            return False

        if cmd == "status":
            st = self.runner.get_status()
            pl_cnt = sum(1 for p in self.plugin_manager.list_plugins() if p.get("loaded"))
            reply = (
                f"状态正常 | 模型: {st.get('model') or 'default'}\n"
                f"备用模型: {', '.join(st.get('fallback_models') or []) or '无'}\n"
                f"当前人设: {self.config.active_persona_id or '默认助手'} | 插件: {pl_cnt} 已启用\n"
                f"暖会话数: {len(st.get('warm_sessions') or [])} | 熔断: {'开启' if st.get('circuit_open') else '正常'}"
            )
            await adapter.send_reply(msg, reply, quote_trigger=True, at_user=False)
            return True

        if cmd == "reset":
            target_key = (
                f"{msg.session_key}:u:{msg.user_id}"
                if (self.config.session.unique_session and msg.scene == "group")
                else msg.session_key
            )
            cleared = await self.runner.reset_sessions(target_key)
            if target_key == msg.session_key:
                self.context_store.clear(msg.session_key)
            await adapter.send_reply(
                msg,
                f"已重置本会话记忆 (清理 {cleared} 个会话)。",
                quote_trigger=True,
                at_user=False,
            )
            return True

        if cmd == "model":
            if not arg:
                await adapter.send_reply(
                    msg,
                    f"当前模型: {self.config.agy.model or 'default'}；用法: /model <模型slug>",
                    quote_trigger=True,
                    at_user=False,
                )
                return True
            self.config.agy.model = arg
            settings_file = (
                self.config.resolve_path(self.config.agy.settings_file)
                if self.config.agy.settings_file.strip()
                else None
            )
            await self.runner.update_config(self.config.agy, self.config.rate_limit, settings_file=settings_file)
            if self.config.config_path is not None:
                save_config(self.config)
            await adapter.send_reply(msg, f"已切换主模型为: {arg}", quote_trigger=True, at_user=False)
            return True

        if cmd == "persona":
            if not arg:
                names = [p.persona_id for p in self.persona_store.list_personas()]
                await adapter.send_reply(
                    msg,
                    f"当前人设: {self.config.active_persona_id or '默认助手'} | 可选: {', '.join(names) or '无'}",
                    quote_trigger=True,
                    at_user=False,
                )
                return True
            if arg.lower() in ("default", "none", "默认"):
                await self.apply_active_persona("")
                await adapter.send_reply(msg, "已切回默认助手人设并重置会话。", quote_trigger=True, at_user=False)
                return True
            if self.persona_store.get(arg) is None:
                await adapter.send_reply(msg, f"未找到人设: {arg}", quote_trigger=True, at_user=False)
                return True
            await self.apply_active_persona(arg)
            await adapter.send_reply(msg, f"已切换人设为: {arg} 并重置会话。", quote_trigger=True, at_user=False)
            return True

        if cmd == "plugins":
            if arg.lower() == "reload":
                await self.plugin_manager.load_all()
                cnt = sum(1 for p in self.plugin_manager.list_plugins() if p.get("loaded"))
                await adapter.send_reply(msg, f"已热重载外部插件，当前启用 {cnt} 个。", quote_trigger=True, at_user=False)
                return True
            items = self.plugin_manager.list_plugins()
            if not items:
                await adapter.send_reply(msg, "暂无外部插件。", quote_trigger=True, at_user=False)
                return True
            lines = [
                f"- {p['id']} ({'启用' if p['loaded'] else '停用/异常'}): {p['description'] or p['name']}"
                for p in items
            ]
            await adapter.send_reply(msg, "已发现插件：\n" + "\n".join(lines), quote_trigger=True, at_user=False)
            return True

        if cmd == "help":
            prefix = (self.config.triggers.command_prefixes or ["/"])[0]
            lines = [
                f"内置管理命令: {prefix}status | {prefix}reset | {prefix}model | {prefix}persona | {prefix}plugins [reload]",
            ]
            pcmds = self.plugin_manager.list_registered_commands()
            if pcmds:
                lines.append("插件命令: " + " | ".join(f"{prefix}{c['name']}" for c in pcmds))
            await adapter.send_reply(msg, "\n".join(lines), quote_trigger=True, at_user=False)
            return True

        return False

    def sync_subagent_workspace_instructions(self) -> Path:
        """Render and sync the isolated subagent workspace GEMINI.md + MCP config."""
        self.mcp_manager._sync_subagent_mcp_config()
        return self.persona_store.sync_to_subagent_workspace(
            self._subagent_workspace_dir,
            self.config.agy.subagent_persona_id,
            active_persona_id=self.config.active_persona_id,
            bot_name=self.config.bot_name,
            mcp_guides=self.mcp_manager.get_usage_guides(),
            allow_command_execution=self.config.agy.allow_command_execution,
        )

    def sync_workspace_instructions(self) -> Path:
        """Render and sync persona + global dialogs + enabled MCP usage guides into bot_workspace/GEMINI.md and subagent workspace."""
        main_path = self.persona_store.sync_to_workspace(
            self.config.active_persona_id,
            bot_name=self.config.bot_name,
            segmented_hint=self.config.reply.segmented_reply.enabled,
            mcp_guides=self.mcp_manager.get_usage_guides(),
            bg_subagent_enabled=self.config.agy.subagent_enabled,
            allow_command_execution=self.config.agy.allow_command_execution,
        )
        self.sync_subagent_workspace_instructions()
        return main_path

    async def reset_warm_sessions(self, session_key: str | None = None) -> int:
        """Recycle warm CLI subprocesses without wiping persisted conversation IDs or chat context."""
        try:
            params = inspect.signature(self.runner.reset_sessions).parameters
            if "clear_saved" in params:
                return await self.runner.reset_sessions(session_key, clear_saved=False)
        except (TypeError, ValueError):
            pass
        return await self.runner.reset_sessions(session_key)

    async def purge_chat_data(self, session_key: str | None = None) -> dict[str, int]:
        """Permanently purge active context, SQLite archive rows, pending debounce, and CLI sessions."""
        if session_key:
            self.debouncer.cancel_session(session_key)
        else:
            for sk in list(self.debouncer._pending.keys()) + list(self.debouncer._session_dispatch_tasks.keys()):
                self.debouncer.cancel_session(sk)

        deleted_messages = self.context_store.clear(session_key, delete_archive=True)
        reset_cli = await self.runner.reset_sessions(session_key, clear_saved=True)
        logger.info(
            "已彻底清除对话数据 (scope=%s, 删除消息=%d 条, 重置 CLI 会话=%d 个)",
            session_key or "ALL",
            deleted_messages,
            reset_cli,
        )
        return {
            "deleted_messages": deleted_messages,
            "reset_cli_sessions": reset_cli,
        }

    async def apply_active_persona(self, persona_id: str) -> None:
        pid = persona_id.strip()
        if pid and self.persona_store.get(pid) is None:
            raise ValueError(f"人设不存在: {pid}")
        self.config.active_persona_id = pid
        self.sync_workspace_instructions()
        if self.config.config_path is not None:
            save_config(self.config)
        await self.runner.reset_sessions()
        self.context_store.clear()
        logger.info("已同步工作区 GEMINI.md，当前激活人设: %s", pid or "(默认助手)")

    async def reload_config(self, new_cfg: BotConfig) -> dict[str, Any]:
        old_cfg = self.config
        self.config = new_cfg

        logging.getLogger().setLevel(getattr(logging, new_cfg.logging.level, logging.INFO))

        self.rate_limiter = RateLimiter(new_cfg.rate_limit)
        self.triggers = TriggerEngine(new_cfg.triggers)
        self.triggers.set_context_judge(self._judge_context)
        self.identity_resolver.update_config(new_cfg.identities)
        self.script_triggers.update_config(new_cfg.triggers.script, base_dir=new_cfg.base_dir)
        self.scheduler.update_config(new_cfg.triggers.scheduled, timezone=new_cfg.prompt.timezone)
        self.debouncer._config = new_cfg.collector

        platform_restart_needed = False
        sync_fn = getattr(self.registry, "sync_adapters", None)
        if callable(sync_fn):
            await sync_fn(new_cfg.platforms, default_proxy=new_cfg.agy.proxy)
        else:
            new_plat_map = {p.id: p for p in new_cfg.platforms}
            for aid, adapter in self.registry.adapters.items():
                np = new_plat_map.get(aid)
                if np is None or not np.enabled or np.ws_url != adapter.config.ws_url or np.access_token != adapter.config.access_token:
                    platform_restart_needed = True
                else:
                    adapter.config = np

        settings_file = (
            new_cfg.resolve_path(new_cfg.agy.settings_file)
            if new_cfg.agy.settings_file.strip()
            else None
        )
        self.mcp_manager.settings_file = settings_file

        db_mode_changed = new_cfg.collector.db_context_enabled != old_cfg.collector.db_context_enabled
        if db_mode_changed:
            self.context_store.close()
            use_db_ctx = bool(new_cfg.collector.db_context_enabled)
            self.context_store = GroupContextStore(
                history_limit=max(new_cfg.collector.history_buffer_size, new_cfg.collector.compact_max_messages + 10),
                db_path=(self._data_dir / "chat_history.db") if use_db_ctx else None,
                persist_file=self._data_dir / "context_history.json",
            )
            self.debouncer._context_store = self.context_store
            for ad in self.registry.adapters.values():
                ad._message_lookup = self.context_store.lookup_message
            self.mcp_manager.configure_builtin_history_server(
                db_path=self.context_store.db_path,
                enabled=use_db_ctx,
            )
            await self.reset_warm_sessions()

        if (
            db_mode_changed
            or new_cfg.active_persona_id != old_cfg.active_persona_id
            or new_cfg.bot_name != old_cfg.bot_name
            or new_cfg.reply.segmented_reply.enabled != old_cfg.reply.segmented_reply.enabled
            or new_cfg.agy.subagent_enabled != old_cfg.agy.subagent_enabled
            or new_cfg.agy.subagent_persona_id != old_cfg.agy.subagent_persona_id
            or new_cfg.agy.allow_command_execution != old_cfg.agy.allow_command_execution
        ):
            if not new_cfg.active_persona_id or self.persona_store.get(new_cfg.active_persona_id) is not None:
                self.sync_workspace_instructions()
            else:
                self.sync_subagent_workspace_instructions()

        if (
            new_cfg.plugins.enabled != old_cfg.plugins.enabled
            or new_cfg.plugins.dir != old_cfg.plugins.dir
            or new_cfg.plugins.disabled != old_cfg.plugins.disabled
        ):
            self.plugin_manager.set_plugins_dir(new_cfg.resolve_path(new_cfg.plugins.dir))
            await self.plugin_manager.load_all()
            self._plugins_loaded = True

        await self.runner.update_config(
            new_cfg.agy,
            new_cfg.rate_limit,
            settings_file=settings_file,
        )

        logger.info("配置热重载完成 (model=%s, persona=%s)", new_cfg.agy.model, new_cfg.active_persona_id or "默认")
        return {
            "platform_restart_needed": platform_restart_needed,
            "webui_restart_needed": (
                new_cfg.webui.port != old_cfg.webui.port or new_cfg.webui.host != old_cfg.webui.host
            ),
        }

    def get_runtime_status(self) -> dict[str, Any]:
        plugins = self.plugin_manager.list_plugins()
        return {
            "bot_name": self.config.bot_name,
            "active_persona_id": self.config.active_persona_id,
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "admins": list(self.config.admins),
            "platforms": self.registry.get_status(),
            "runner": self.runner.get_status(),
            "segmented_reply_enabled": self.config.reply.segmented_reply.enabled,
            "plugins_total": len(plugins),
            "plugins_loaded": sum(1 for p in plugins if p.get("loaded")),
            "skills_total": len(self.skills_manager.list_skills()),
            "skills_enabled": sum(1 for s in self.skills_manager.list_skills() if s.get("enabled")),
            "mcp_total": len(self.mcp_manager.list_servers()),
            "mcp_enabled": sum(1 for m in self.mcp_manager.list_servers() if m.get("enabled")),
            "subagent_enabled": self.config.agy.subagent_enabled,
            "active_bg_tasks": len(self._bg_tasks),
            "channel_guard": self.channel_guard.get_summary(),
            "triggers_enabled": {
                "mention": self.config.triggers.mention.enabled,
                "quote": self.config.triggers.quote.enabled,
                "keyword": self.config.triggers.keyword.enabled,
                "dice": self.config.triggers.dice.enabled,
                "context_llm": self.config.triggers.context_llm.enabled,
                "scheduled": self.config.triggers.scheduled.enabled,
                "script": self.config.triggers.script.enabled,
            },
            "scheduled_tasks_count": len(self.config.triggers.scheduled.tasks),
            "script_rules_count": len(self.config.triggers.script.rules),
        }

    async def _send_direct_reply(
        self,
        msg: IncomingMessage,
        text: str,
        *,
        quote: bool = True,
        at: bool = False,
    ) -> None:
        adapter = self.registry.get(msg.adapter_id)
        if adapter is None or not text.strip():
            return
        if not self.config.reply.segmented_reply.only_llm_result and self.config.reply.segmented_reply.enabled:
            segments = split_and_clean_reply(text, self.config.reply)
            for idx, seg in enumerate(segments):
                if idx > 0:
                    delay = compute_segment_delay(self.config.reply.segmented_reply, text=seg)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if not self.channel_guard.is_activated(msg.session_key):
                        return
                try:
                    await adapter.send_reply(msg, seg, quote_trigger=(quote and idx == 0), at_user=(at and idx == 0))
                except Exception as exc:
                    logger.warning("[%s] 直接回复发送失败: %s", msg.session_key, exc)
                    break
            return
        try:
            await adapter.send_reply(msg, text, quote_trigger=quote, at_user=at)
        except Exception as exc:
            logger.warning("[%s] 直接回复发送失败: %s", msg.session_key, exc)

    def _resolve_reply_style(self, trigger_type: str) -> tuple[bool, bool]:
        """Resolve (quote_trigger, at_user) for a specific trigger type with fallback to ReplyConfig."""
        tr_map = {
            "mention": self.config.triggers.mention,
            "quote": self.config.triggers.quote,
            "keyword": self.config.triggers.keyword,
            "dice": self.config.triggers.dice,
            "context_llm": self.config.triggers.context_llm,
            "script": self.config.triggers.script,
        }
        tr_cfg = tr_map.get(trigger_type)
        q_opt = getattr(tr_cfg, "quote_reply", None) if tr_cfg is not None else None
        a_opt = getattr(tr_cfg, "at_user", None) if tr_cfg is not None else None
        quote = self.config.reply.quote_trigger_message if q_opt is None else bool(q_opt)
        at_u = self.config.reply.at_trigger_user if a_opt is None else bool(a_opt)
        return quote, at_u

    async def _try_auto_proxy_failover(self, error_text: str) -> bool:
        """If Antigravity failed due to timeout/proxy/geo-block on a local proxy port, switch to the
        lowest-latency non-geo-blocked node among allowed Taiwan(AI)/Japan(AI)/US-Phoenix nodes."""
        if not is_proxy_or_geo_error(error_text):
            return False
        port = extract_local_proxy_port(self.config.agy.proxy)
        if port is None:
            return False
        now_mono = time.monotonic()
        if (now_mono - self._last_proxy_failover_mono) < 30.0:
            return (now_mono - self._last_proxy_failover_ok_mono) < 30.0

        async with self._proxy_failover_lock:
            now_mono = time.monotonic()
            if (now_mono - self._last_proxy_failover_mono) < 30.0:
                return (now_mono - self._last_proxy_failover_ok_mono) < 30.0
            loop = asyncio.get_running_loop()
            ctl = await loop.run_in_executor(None, discover_proxy_controller, port)
            if ctl is None:
                return False

            # Expire stale geo-block records (30 min TTL) and record current node if this failure was geo-blocked
            self._geo_blocked_nodes = {k: exp for k, exp in self._geo_blocked_nodes.items() if exp > now_mono}
            if is_geo_blocked_error(error_text):
                try:
                    _, cur_node, _ = await fetch_selector_group(ctl)
                    if cur_node:
                        self._geo_blocked_nodes[cur_node] = now_mono + 1800.0
                except Exception:
                    pass

            logger.warning(
                "检测到上游网络/地区异常 (%s)，自动开始在端口 %d 的白名单节点（台湾带AI / 日本带AI / 美国凤凰城）中筛选无地区限制且延迟最低的节点...",
                error_text[:120],
                port,
            )

            async def _probe_upstream() -> ProbeOutcome:
                t0 = time.monotonic()
                await self.runner.reset_sessions("proxy_guard:probe")
                # Strictly probe the primary model WITHOUT fallback so geo-blocked nodes are never misclassified as healthy
                try:
                    probe_res = await asyncio.wait_for(
                        self._ask_runner(
                            "proxy_guard:probe",
                            "只回复ok两个字母",
                            allow_fallback=False,
                        ),
                        timeout=25.0,
                    )
                except asyncio.TimeoutError:
                    ms = int((time.monotonic() - t0) * 1000)
                    return ProbeOutcome(ok=False, elapsed_ms=ms, error="探针超时(25s)", geo_blocked=False)
                ms = int((time.monotonic() - t0) * 1000)
                if probe_res.ok and (probe_res.response or "").strip():
                    return ProbeOutcome(ok=True, elapsed_ms=ms, response=probe_res.response[:40], geo_blocked=False)
                err = probe_res.error or "空响应"
                return ProbeOutcome(
                    ok=False,
                    elapsed_ms=ms,
                    error=err,
                    geo_blocked=is_geo_blocked_error(err),
                )

            try:
                fo = await select_best_working_node(
                    ctl,
                    probe_fn=_probe_upstream,
                    skip_current_if_broken=True,
                    node_filter=is_allowed_proxy_node,
                    geo_blocked_nodes=set(self._geo_blocked_nodes.keys()),
                )
            except Exception as exc:
                logger.warning("自动代理节点切换异常: %s", exc)
                self._last_proxy_failover_mono = time.monotonic()
                return False

            done_mono = time.monotonic()
            self._last_proxy_failover_mono = done_mono
            for gnode in fo.geo_blocked_nodes:
                self._geo_blocked_nodes[gnode] = done_mono + 1800.0
            if fo.ok:
                self._last_proxy_failover_ok_mono = done_mono
                self._geo_blocked_nodes.pop(fo.selected_node, None)
                await self.reset_warm_sessions()
                logger.info(
                    "代理节点自动切换成功: 策略组「%s」 %s -> %s (白名单=%d/%d, 测速 %sms，上游主模型响应 %sms)",
                    fo.group,
                    fo.previous_node,
                    fo.selected_node,
                    fo.filtered_nodes,
                    fo.total_nodes,
                    fo.selected_delay_ms,
                    fo.probe_elapsed_ms,
                )
                return True

            logger.warning("代理节点自动切换未找到可用节点: %s", fo.error)
            return False

    def record_token_usage(
        self,
        *,
        session_key: str,
        result: RunnerResult,
        scene: str = "",
        channel_id: str = "",
        display_name: str = "",
        trigger_type: str = "",
        context_messages: int = 0,
        context_tokens: int = 0,
    ) -> dict[str, Any] | None:
        """Persist per-request token & latency metrics to TokenUsageStore without interrupting chat flow."""
        if not result.ok:
            return None
        try:
            return self.usage_store.record(
                session_key=session_key,
                result=result,
                scene=scene,
                channel_id=channel_id,
                display_name=display_name,
                model=result.model or self.config.agy.model or "default",
                trigger_type=trigger_type,
                context_messages=context_messages,
                context_tokens=context_tokens,
            )
        except Exception as exc:
            logger.debug("记录 Token 用量失败 (%s): %s", session_key, exc)
            return None

    async def _ask_runner(
        self,
        runner_key: str,
        prompt: str,
        *,
        model_override: str | None = None,
        profile_override: ModelProfileConfig | None = None,
        allow_fallback: bool = True,
        defer_geo_fallback: bool = False,
        delta_prompt: str | None = None,
        delta_base_seq: int = 0,
        window_min_seq: int | None = None,
        latest_seq: int = 0,
    ) -> Any:
        kwargs: dict[str, Any] = {}
        try:
            params = inspect.signature(self.runner.ask).parameters
            if "model_override" in params:
                kwargs["model_override"] = model_override
            if "profile_override" in params:
                kwargs["profile_override"] = profile_override
            if "allow_fallback" in params:
                kwargs["allow_fallback"] = allow_fallback
            if "defer_geo_fallback" in params:
                kwargs["defer_geo_fallback"] = defer_geo_fallback
            if "window_min_seq" in params:
                kwargs["window_min_seq"] = window_min_seq
                kwargs["latest_seq"] = latest_seq
                kwargs["delta_prompt"] = delta_prompt
                kwargs["delta_base_seq"] = delta_base_seq
        except (TypeError, ValueError):
            kwargs = {}
        if kwargs:
            return await self.runner.ask(runner_key, prompt, **kwargs)
        return await self.runner.ask(runner_key, prompt)

    async def handle_group_ban(
        self,
        adapter_id: str,
        group_id: str,
        is_banned: bool,
        duration_seconds: float = 0.0,
        reason: str = "",
    ) -> None:
        """Handle group mute/unmute events: muting resets the group back to unactivated anti-disturb mode."""
        session_key = ChannelActivationStore.make_key(adapter_id, "group", group_id)
        if is_banned:
            self.channel_guard.on_group_muted(
                adapter_id,
                group_id,
                duration_seconds=duration_seconds,
            )
            self.debouncer.cancel_session(session_key)
            self.context_store.reset_ambient_unread(session_key)
            logger.warning(
                "[%s] 检测到机器人在该群被禁言 (%s)，已自动恢复防打扰静默状态（解禁后需重新被显式 @ 才恢复发言）",
                session_key,
                reason or f"duration={int(duration_seconds)}s",
            )
        else:
            self.channel_guard.on_group_unmuted(adapter_id, group_id)
            logger.info(
                "[%s] 检测到该群已解除禁言 (%s)，继续保持防打扰静默状态直至再次被显式 @",
                session_key,
                reason or "lift_ban",
            )

    async def handle_message(self, msg: IncomingMessage) -> None:
        adapter = self.registry.get(msg.adapter_id)
        if adapter is not None and not adapter.is_channel_allowed(msg.scene, msg.channel_id, msg.user_id):
            return
        if is_non_friend_private_message(msg):
            logger.debug("[%s] 忽略非好友临时私聊消息 (user=%s)", msg.session_key, msg.user_id)
            return

        is_new = self.context_store.add_message(msg, count_ambient=False)
        if not is_new:
            logger.debug("[%s] 忽略重复 message_id=%s", msg.session_key, msg.message_id)
            return
        if msg.is_self:
            return

        # Anti-disturbance activation guard:
        # - Private: activated automatically when a friend proactively sends a DM.
        # - Group: stays silent (no proactive replies/keywords/quotes/plugins) until explicitly `@bot`;
        #   also resets back to this unactivated silent state whenever the bot is muted in that group.
        if msg.scene == "private":
            if self.channel_guard.activate_private(msg.adapter_id, msg.channel_id):
                logger.info("[%s] 收到好友私聊消息，已激活该私聊会话", msg.session_key)
        else:
            if self.channel_guard.is_muted(msg.session_key):
                return
            if not self.channel_guard.is_activated(msg.session_key):
                if is_strict_explicit_at_bot(msg):
                    self.channel_guard.activate_group(msg.adapter_id, msg.channel_id)
                    logger.info("[%s] 群内收到显式 @机器人，已唤醒并激活该群发言权限", msg.session_key)
                else:
                    return

        identity = self.identity_resolver.resolve(
            msg.scene,
            msg.channel_id,
            msg.user_id,
            adapter_id=msg.adapter_id,
        )

        if await self._handle_admin_command(msg, identity=identity):
            return

        # Plugin commands & on_message hooks (gated by identity.allow_plugins when identities enabled)
        if (not identity.active) or identity.allow_plugins:
            cmd_handled, cmd_reply = await self.plugin_manager.dispatch_command(msg)
            if cmd_handled:
                if cmd_reply and cmd_reply.text:
                    await self._send_direct_reply(msg, cmd_reply.text, quote=cmd_reply.quote, at=cmd_reply.at)
                return

            hook_reply = await self.plugin_manager.dispatch_on_message(msg)
            if hook_reply is not None:
                if hook_reply.text:
                    await self._send_direct_reply(msg, hook_reply.text, quote=hook_reply.quote, at=hook_reply.at)
                if hook_reply.stop:
                    return

        # Content safety input check
        if self.config.content_safety.enabled and self.config.content_safety.check_input:
            ok, cleaned, hit = apply_content_safety(msg.text, self.config.content_safety)
            if not ok:
                logger.warning("[%s] 输入触发敏感词拦截 (%s)", msg.session_key, hit)
                if self.config.content_safety.block_reply.strip():
                    await self._send_direct_reply(msg, self.config.content_safety.block_reply.strip())
                return
            msg.text = cleaned

        fast_trigger = self.triggers.evaluate_fast(msg)
        is_admin = bool(
            (self.config.admins and msg.user_id in self.config.admins)
            or (identity.active and identity.allow_bot_commands)
        )
        bypass_rl = bool(
            (is_admin and self.config.triggers.admin_bypass_rate_limit)
            or (identity.active and identity.bypass_rate_limit)
        )

        if not fast_trigger.triggered and self.config.triggers.script.enabled:
            recent_cnt = len(
                self.context_store.get_recent(
                    msg.session_key,
                    limit=self.config.collector.context_message_limit,
                )
            )
            script_trigger = await self.script_triggers.evaluate(
                msg,
                is_admin=is_admin,
                recent_count=recent_cnt,
            )
            if not self.channel_guard.is_activated(msg.session_key):
                return
            if script_trigger.triggered:
                if script_trigger.direct_reply:
                    if not bypass_rl:
                        allowed, rl_reason = self.rate_limiter.can_trigger(msg)
                        if not allowed:
                            logger.info(
                                "[%s] 用户 %s 脚本触发直发被限流跳过 (%s)",
                                msg.session_key,
                                msg.user_id,
                                rl_reason,
                            )
                            return
                    self.rate_limiter.record_trigger(msg)
                    logger.info("[%s] 脚本触发直接回复 (%s)", msg.session_key, script_trigger.reason)
                    quote_flag, at_flag = self._resolve_reply_style("script")
                    await self._send_direct_reply(
                        msg,
                        script_trigger.direct_reply,
                        quote=quote_flag,
                        at=at_flag,
                    )
                    self.rate_limiter.record_reply_sent(msg.session_key)
                    self.context_store.reset_ambient_unread(msg.session_key)
                    return
                fast_trigger = script_trigger

        if self.debouncer.has_pending(msg.session_key):
            if fast_trigger.triggered and fast_trigger.trigger_type in ("mention", "quote", "keyword"):
                self.debouncer.on_trigger(msg, fast_trigger)
            else:
                self.debouncer.handle_followup_message(msg, is_new_trigger=False)
            return

        if fast_trigger.triggered:
            if not bypass_rl:
                allowed, rl_reason = self.rate_limiter.can_trigger(msg)
                if not allowed:
                    logger.info(
                        "[%s] 用户 %s 触发 %s 被限流跳过 (%s)",
                        msg.session_key,
                        msg.user_id,
                        fast_trigger.trigger_type,
                        rl_reason,
                    )
                    return

            circuit_open, rem = self.runner.is_circuit_open()
            if circuit_open:
                logger.warning("[%s] Antigravity 熔断冷却中 (%.0fs)，忽略触发", msg.session_key, rem)
                return

            self.rate_limiter.record_trigger(msg)
            self.context_store.reset_ambient_unread(msg.session_key)
            self.debouncer.on_trigger(msg, fast_trigger)
            return

        unread = self.context_store.increment_ambient(msg.session_key)

        circuit_open, _ = self.runner.is_circuit_open()
        if circuit_open or not self.config.triggers.context_llm.enabled:
            return

        if not bypass_rl:
            allowed, _ = self.rate_limiter.can_trigger(msg)
            if not allowed:
                return

        recent = self.context_store.get_recent(
            msg.session_key,
            limit=self.config.triggers.context_llm.judge_context_window,
        )
        ctx_trigger = await self.triggers.evaluate_context_llm(
            msg,
            unread_ambient_count=unread,
            recent_messages=recent,
        )
        if not self.channel_guard.is_activated(msg.session_key):
            return
        if ctx_trigger.triggered:
            self.rate_limiter.record_trigger(msg)
            self.context_store.reset_ambient_unread(msg.session_key)
            self.debouncer.on_trigger(msg, ctx_trigger)

    async def _on_snapshot_ready(self, snapshot: ContextSnapshot) -> None:
        if not self.channel_guard.is_activated(snapshot.session_key):
            logger.info("[%s] 该会话未激活或已因禁言重置静默，取消本次回复", snapshot.session_key)
            return
        circuit_open, rem = self.runner.is_circuit_open()
        if circuit_open:
            logger.warning("[%s] 熔断冷却中 (%.0fs)，取消本次推理", snapshot.session_key, rem)
            return

        identity = self.identity_resolver.resolve(
            snapshot.scene,
            snapshot.channel_id,
            snapshot.trigger_message.user_id,
            adapter_id=snapshot.adapter_id,
        )
        base_profile = self.config.agy.resolve_model_profile(self.config.agy.model)
        active_profile = identity.build_effective_model_profile(base_profile)
        runner_key = self._runner_session_key(snapshot)

        window_state: ActiveWindowState | None = None
        if self.config.collector.db_context_enabled and self.context_store.db_path is not None:
            ccfg = self.config.collector
            eff_max_msgs = (
                active_profile.compact_max_messages
                if (active_profile is not None and active_profile.compact_max_messages > 0)
                else (
                    active_profile.max_context_messages
                    if (active_profile is not None and active_profile.max_context_messages > 0)
                    else ccfg.compact_max_messages
                )
            )
            eff_drop_cnt = (
                active_profile.compact_drop_by_count
                if (active_profile is not None and active_profile.compact_drop_by_count > 0)
                else ccfg.compact_drop_by_count
            )
            eff_max_tok = (
                active_profile.compact_max_tokens
                if (active_profile is not None and active_profile.compact_max_tokens > 0)
                else ccfg.compact_max_tokens
            )
            eff_max_bytes = (
                active_profile.compact_max_bytes
                if (active_profile is not None and active_profile.compact_max_bytes > 0)
                else ccfg.compact_max_bytes
            )
            eff_drop_tok = (
                active_profile.compact_drop_by_tokens
                if (active_profile is not None and active_profile.compact_drop_by_tokens > 0)
                else ccfg.compact_drop_by_tokens
            )
            window_state = self.context_store.get_active_window_and_compact(
                snapshot.session_key,
                cursor_key=runner_key,
                max_messages=eff_max_msgs,
                drop_by_count=eff_drop_cnt,
                max_tokens=eff_max_tok,
                max_bytes=eff_max_bytes,
                drop_by_tokens=eff_drop_tok,
            )
            if window_state.compacted:
                logger.info(
                    "[%s] 触发上下文压缩策略 (%s): 已将最早 %d 条消息归档移出当前窗口 (当前窗口 #%d~#%d 共 %d 条 / 约 %d tok / %d B，数据库历史归档共 %d 条)",
                    runner_key,
                    " + ".join(window_state.compact_reasons),
                    window_state.dropped_count,
                    window_state.active_min_seq,
                    window_state.latest_seq,
                    len(window_state.messages),
                    window_state.total_tokens,
                    window_state.total_bytes,
                    window_state.archived_count,
                )
            if window_state.messages:
                trig = snapshot.trigger_message
                merged_msgs: list[IncomingMessage] = []
                matched_trig = False
                for wm in window_state.messages:
                    is_trig_row = trig is not None and (
                        (trig.db_seq > 0 and wm.db_seq == trig.db_seq)
                        or (bool(trig.message_id) and wm.message_id == trig.message_id)
                    )
                    if is_trig_row:
                        if trig.db_seq <= 0 and wm.db_seq > 0:
                            trig.db_seq = wm.db_seq
                        merged_msgs.append(trig)
                        matched_trig = True
                    else:
                        merged_msgs.append(wm)
                if trig is not None and not matched_trig:
                    merged_msgs.append(trig)
                snapshot.messages = merged_msgs
            elif snapshot.trigger_message is not None and snapshot.trigger_message not in snapshot.messages:
                snapshot.messages.append(snapshot.trigger_message)

        effective_vision = active_profile.enable_vision if active_profile is not None else True
        if effective_vision:
            adapter_for_media = self.registry.get(snapshot.adapter_id)
            media_messages = (
                snapshot.messages[-active_profile.max_context_messages :]
                if (window_state is None and active_profile is not None and active_profile.max_context_messages > 0)
                else snapshot.messages
            )
            downloaded = await materialize_snapshot_images(
                media_messages,
                workspace_dir=self._workspace_dir,
                adapter=adapter_for_media,
                proxy=self.config.agy.proxy,
            )
            if downloaded:
                logger.info("[%s] 已准备 %d 张图片供模型查看", snapshot.session_key, len(downloaded))
                if window_state is not None:
                    for m in media_messages:
                        if any(a.local_path for a in m.attachments):
                            self.context_store.update_message_attachments(m)

        prompt = format_context_prompt(
            snapshot,
            self.config.bot_name,
            self.config.prompt,
            model_profile=active_profile,
            identity=identity,
            identity_resolver=self.identity_resolver,
            window_state=window_state,
        )
        if (not identity.active) or identity.allow_plugins:
            prompt = await self.plugin_manager.dispatch_before_llm(snapshot, prompt)

        delta_prompt: str | None = None
        delta_base_seq = 0
        delta_count = 0
        if window_state is not None and window_state.latest_seq > 0:
            peek_fn = getattr(self.runner, "peek_warm_cursor", None)
            warm_cur = peek_fn(runner_key) if callable(peek_fn) else None
            if warm_cur is not None:
                warm_min_seq, warm_last_seq, warm_self_seqs = warm_cur
                if warm_min_seq == window_state.active_min_seq and 0 < warm_last_seq < window_state.latest_seq:
                    delta_msgs = [
                        m
                        for m in snapshot.messages
                        if m.db_seq > warm_last_seq and m.db_seq not in warm_self_seqs
                    ]
                    if delta_msgs:
                        delta_base_seq = warm_last_seq
                        delta_count = len(delta_msgs)
                        delta_prompt = format_delta_prompt(
                            snapshot,
                            delta_msgs,
                            self.config.bot_name,
                            self.config.prompt,
                            model_profile=active_profile,
                            identity=identity,
                            identity_resolver=self.identity_resolver,
                            window_state=window_state,
                        )
                        if (not identity.active) or identity.allow_plugins:
                            delta_prompt = await self.plugin_manager.dispatch_before_llm(snapshot, delta_prompt)

        id_note = f"，身份={identity.user_label}" if identity.active else ""
        if delta_prompt:
            ctx_note = f"，热会话增量缓存 +{delta_count} 条 [基准 #{delta_base_seq}，窗口 {len(snapshot.messages)} 条，归档 {window_state.archived_count if window_state else 0} 条]"
        elif window_state is not None:
            ctx_note = f"，数据库窗口={len(snapshot.messages)} 条 / 约 {window_state.total_tokens} tok，归档={window_state.archived_count} 条"
        else:
            ctx_note = f"，上下文={len(snapshot.messages)} 条"
        logger.info(
            "[%s] 开始调用 Antigravity (%s%s%s，追发=%d 条)",
            runner_key,
            snapshot.primary_trigger,
            id_note,
            ctx_note,
            snapshot.extra_messages_collected,
        )
        has_local_proxy = extract_local_proxy_port(self.config.agy.proxy) is not None
        win_min_arg = window_state.active_min_seq if window_state is not None else None
        win_latest_arg = window_state.latest_seq if window_state is not None else 0
        prof_ov = active_profile if (identity.active and identity.has_tool_override()) else None
        result = await self._ask_runner(
            runner_key,
            prompt,
            profile_override=prof_ov,
            defer_geo_fallback=has_local_proxy,
            delta_prompt=delta_prompt,
            delta_base_seq=delta_base_seq,
            window_min_seq=win_min_arg,
            latest_seq=win_latest_arg,
        )
        if not result.ok and is_proxy_or_geo_error(result.error):
            switched = await self._try_auto_proxy_failover(result.error)
            if switched:
                logger.info("[%s] 已切换至可用低延迟代理节点，自动重试本次推理", runner_key)
                result = await self._ask_runner(
                    runner_key,
                    prompt,
                    profile_override=prof_ov,
                    window_min_seq=win_min_arg,
                    latest_seq=win_latest_arg,
                )
            elif has_local_proxy and is_geo_blocked_error(result.error) and self.config.agy.fallback_models:
                # Failover couldn't find a cleaner node; allow fallback model as last resort
                result = await self._ask_runner(
                    runner_key,
                    prompt,
                    profile_override=prof_ov,
                    allow_fallback=True,
                    defer_geo_fallback=False,
                    window_min_seq=win_min_arg,
                    latest_seq=win_latest_arg,
                )
        quote_flag, at_flag = self._resolve_reply_style(snapshot.primary_trigger)
        if not result.ok:
            if is_auth_required_error(result.error):
                logger.error(
                    "[%s] Antigravity CLI 当前未登录或凭证已失效 (%s) —— 并非代理故障，请前往 WebUI「Agy CLI」页面重新登录后即可恢复",
                    runner_key,
                    result.error,
                )
            else:
                logger.error("[%s] Antigravity 推理失败: %s", runner_key, result.error)
            if self.config.prompt.custom_error_message.strip():
                await self._send_direct_reply(
                    snapshot.trigger_message,
                    self.config.prompt.custom_error_message.strip(),
                    quote=quote_flag,
                    at=at_flag,
                )
            return

        sess_display = (
            (snapshot.trigger_message.sender_name or snapshot.channel_id)
            if snapshot.scene == "private"
            else snapshot.channel_id
        )
        self.record_token_usage(
            session_key=snapshot.session_key,
            result=result,
            scene=snapshot.scene,
            channel_id=snapshot.channel_id,
            display_name=sess_display,
            trigger_type=snapshot.primary_trigger,
            context_messages=len(snapshot.messages),
            context_tokens=(window_state.total_tokens if window_state is not None else 0),
        )

        if not result.response:
            logger.info("[%s] Antigravity 返回空内容，跳过发送", runner_key)
            return

        reply_text = (
            await self.plugin_manager.dispatch_after_llm(snapshot, result.response)
            if ((not identity.active) or identity.allow_plugins)
            else result.response
        )

        reply_text, bg_tasks = extract_bg_tasks(reply_text)
        if not self.channel_guard.is_activated(snapshot.session_key):
            logger.info("[%s] 推理完成后检测到该会话已重置为静默/禁言态，取消后台任务与回复", runner_key)
            return
        if bg_tasks and self.config.agy.subagent_enabled:
            for spec in bg_tasks:
                self._spawn_bg_subagent_task(
                    snapshot=snapshot,
                    runner_key=runner_key,
                    task_spec=spec,
                    quote_flag=quote_flag,
                    at_flag=at_flag,
                )
            if not reply_text.strip():
                reply_text = "收到，已交给后台子代理处理，完成后马上发出来，期间可以继续正常聊天~"

        if self.config.content_safety.enabled and self.config.content_safety.check_output:
            ok, reply_text, hit = apply_content_safety(reply_text, self.config.content_safety)
            if not ok:
                logger.warning("[%s] 输出触发敏感词拦截 (%s)", runner_key, hit)
                if self.config.content_safety.block_reply.strip():
                    await self._send_direct_reply(snapshot.trigger_message, self.config.content_safety.block_reply.strip())
                return

        segments = split_and_clean_reply(reply_text, self.config.reply)
        if not segments:
            logger.info("[%s] 回复经清理后为空，跳过发送", runner_key)
            return

        adapter = self.registry.get(snapshot.adapter_id)
        if adapter is None:
            logger.error("[%s] 未找到适配器 %s，无法发送回复", runner_key, snapshot.adapter_id)
            return
        if not self.channel_guard.is_activated(snapshot.session_key):
            logger.info("[%s] 发送前检测到该会话已重置为静默/禁言态，跳过发送", runner_key)
            return

        self_id = getattr(adapter, "self_id", snapshot.trigger_message.self_id) or snapshot.trigger_message.self_id
        delivered_segments: list[tuple[str, str]] = []
        for idx, seg_text in enumerate(segments):
            if idx > 0:
                delay = compute_segment_delay(self.config.reply.segmented_reply, text=seg_text)
                if delay > 0:
                    await asyncio.sleep(delay)
                if not self.channel_guard.is_activated(snapshot.session_key):
                    logger.info("[%s] 分段发送中途检测到该会话已重置为静默/禁言态，中止后续分段", runner_key)
                    break

            img_refs = [m.strip() for m in _MD_IMAGE_RE.findall(seg_text) if m.strip()]
            sent_id: str | None = None
            delivered = False
            if img_refs and hasattr(adapter, "send_image"):
                clean_caption = _MD_IMAGE_RE.sub("", seg_text).strip()
                sent_any_img = False
                for img_idx, img_ref in enumerate(img_refs):
                    img_bytes = await self._load_reply_image_bytes(img_ref)
                    if img_bytes:
                        try:
                            sent_id = await adapter.send_image(
                                snapshot.trigger_message,
                                img_bytes,
                                caption=clean_caption if img_idx == 0 else "",
                                quote_trigger=(quote_flag and idx == 0 and img_idx == 0),
                                at_user=(at_flag and idx == 0 and img_idx == 0),
                            )
                            sent_any_img = True
                            delivered = True
                        except Exception as exc:
                            logger.warning("[%s] 自动发送回复图片失败 (%s): %s", runner_key, img_ref, exc)
                if not sent_any_img:
                    try:
                        sent_id = await adapter.send_reply(
                            snapshot.trigger_message,
                            seg_text,
                            quote_trigger=(quote_flag and idx == 0),
                            at_user=(at_flag and idx == 0),
                        )
                        delivered = True
                    except Exception as exc:
                        logger.warning("[%s] 发送回复分段失败: %s", runner_key, exc)
                        break
            else:
                try:
                    sent_id = await adapter.send_reply(
                        snapshot.trigger_message,
                        seg_text,
                        quote_trigger=(quote_flag and idx == 0),
                        at_user=(at_flag and idx == 0),
                    )
                    delivered = True
                except Exception as exc:
                    logger.warning("[%s] 发送回复分段失败: %s", runner_key, exc)
                    break
            if not delivered:
                continue
            delivered_segments.append((sent_id or "", seg_text))
            if sent_id:
                self.context_store.cache_message(
                    adapter_id=snapshot.adapter_id,
                    message_id=sent_id,
                    user_id=self_id,
                    sender_name=self.config.bot_name,
                    text=seg_text,
                    timestamp=time.time(),
                )

        sent_count = len(delivered_segments)
        if sent_count > 0:
            combined_reply_text = "\n".join(seg for _, seg in delivered_segments)
            last_sent_id = next((sid for sid, _ in reversed(delivered_segments) if sid), "")
            self_msg = IncomingMessage(
                adapter_id=snapshot.adapter_id,
                platform=snapshot.platform,
                self_id=self_id,
                scene=snapshot.scene,
                channel_id=snapshot.channel_id,
                message_id=last_sent_id,
                user_id=self_id,
                sender_name=self.config.bot_name,
                text=combined_reply_text,
                timestamp=time.time(),
                is_self=True,
            )
            self.context_store.add_message(self_msg, count_ambient=False)
            if sent_count > 1:
                for sid, seg_t in delivered_segments:
                    if sid:
                        self.context_store.cache_message(
                            adapter_id=snapshot.adapter_id,
                            message_id=sid,
                            user_id=self_id,
                            sender_name=self.config.bot_name,
                            text=seg_t,
                            timestamp=self_msg.timestamp,
                        )
            notify_fn = getattr(self.runner, "notify_self_reply_seq", None)
            if callable(notify_fn) and self_msg.db_seq > 0:
                notify_fn(runner_key, self_msg.db_seq)
            self.rate_limiter.record_reply_sent(snapshot.session_key)
            self.context_store.reset_ambient_unread(snapshot.session_key)
            self.context_store.flush()
            logger.info(
                "[%s] 回复已发送 (%d 段, 共 %d 字)",
                runner_key,
                sent_count,
                sum(len(s) for _, s in delivered_segments),
            )

    async def _load_reply_image_bytes(self, ref: str) -> bytes | None:
        """Load image bytes from safe public HTTP(S) URL, data/base64 URI, or verified local image file."""
        src = (ref or "").strip().strip("<>").strip("'\"")
        if not src:
            return None
        if src.lower().startswith(("http://", "https://")):
            if not await _is_safe_public_http_url(src):
                logger.warning("拒绝从内网/本机地址加载回复图片 (SSRF 拦截): %s", src[:120])
                return None
            data, _ = await _fetch_http_bytes(src, proxy=self.config.agy.proxy, timeout_seconds=45.0)
            if data and _looks_like_image_bytes(data):
                return data
            return None
        decoded = _decode_base64_source(src)
        if decoded and _looks_like_image_bytes(decoded):
            return decoded

        # Local path (file://, absolute path, or workspace/subagent-workspace relative path) - verified image files only
        ws_resolved = self._workspace_dir.resolve()
        sub_ws_resolved = self._subagent_workspace_dir.resolve()
        local_p = _resolve_local_file(src)
        if local_p is None and not src.lower().startswith("file://"):
            for base_ws in (sub_ws_resolved, ws_resolved):
                rel_cand = (base_ws / src).resolve()
                if rel_cand.is_file():
                    local_p = rel_cand
                    break
        if local_p is not None:
            try:
                resolved_p = local_p.resolve()
                if resolved_p.stat().st_size > _MAX_LOCAL_IMAGE_BYTES:
                    logger.warning("跳过超大本地回复图片 (>25MB): %s", resolved_p)
                    return None
                data = resolved_p.read_bytes()
                if _looks_like_image_bytes(data):
                    return data
            except OSError as exc:
                logger.warning("读取本地回复图片失败 (%s): %s", local_p, exc)
        return None

    def _collect_snapshot_image_paths(self, snapshot: ContextSnapshot) -> list[str]:
        paths: list[str] = []
        seen: set[str] = set()
        # Prefer trigger message images first, then recent context images
        ordered_msgs = [snapshot.trigger_message] + [
            m for m in reversed(snapshot.messages) if m is not snapshot.trigger_message
        ]
        for m in ordered_msgs:
            for att in m.attachments:
                if att.kind == "image" and att.local_path and att.local_path not in seen:
                    seen.add(att.local_path)
                    paths.append(att.local_path)
        return paths

    def _spawn_bg_subagent_task(
        self,
        *,
        snapshot: ContextSnapshot,
        runner_key: str,
        task_spec: dict[str, Any],
        quote_flag: bool,
        at_flag: bool,
    ) -> asyncio.Task[Any]:
        task_id = uuid.uuid4().hex[:8]
        task = asyncio.create_task(
            self._run_bg_subagent_task(
                task_id=task_id,
                snapshot=snapshot,
                runner_key=runner_key,
                task_spec=task_spec,
                quote_flag=quote_flag,
                at_flag=at_flag,
            ),
            name=f"agy-bg-subagent-{task_id}",
        )
        self._track_bg_task(task)
        return task

    def _stage_subagent_reference_images(self, req_images: list[str]) -> list[str]:
        """Stage reference images into the isolated subagent workspace `.media_cache/` as clean,
        metadata-stripped standard PNG/JPG/WEBP/GIF files with verified extensions so both `read_file`
        and strict MCP uploaders accept them on the very first try.
        Strictly rejects non-image files and oversized files (>25MB)."""
        staged: list[str] = []
        main_ws_resolved = self._workspace_dir.resolve()
        sub_ws_resolved = self._subagent_workspace_dir.resolve()
        cache_dir = sub_ws_resolved / ".media_cache"
        wrote_any = False
        for raw in req_images:
            p_str = str(raw or "").strip()
            if not p_str:
                continue
            if p_str.lower().startswith(("http://", "https://")):
                staged.append(p_str)
                continue
            local_p = _resolve_local_file(p_str)
            if local_p is None and not p_str.lower().startswith("file://"):
                for base_ws in (main_ws_resolved, sub_ws_resolved):
                    cand = (base_ws / p_str).resolve()
                    if cand.is_file():
                        local_p = cand
                        break
            if local_p is None:
                continue

            try:
                resolved_p = local_p.resolve()
                inside_sub_ws = resolved_p.is_relative_to(sub_ws_resolved)
                inside_main_ws = resolved_p.is_relative_to(main_ws_resolved)
                if resolved_p.stat().st_size > _MAX_LOCAL_IMAGE_BYTES:
                    logger.warning("拒绝暂存超大参考图 (>25MB): %s", resolved_p)
                    continue
                raw_bytes = resolved_p.read_bytes()
                if not _looks_like_image_bytes(raw_bytes):
                    logger.warning("拒绝将非图片文件暂存为子代理参考图: %s", resolved_p)
                    continue
                clean_bytes, real_ext = sanitize_reference_image(raw_bytes, hint_name=resolved_p.name)
                use_ext = real_ext if real_ext in _ALLOWED_EXTS else ".png"
                cur_ext = resolved_p.suffix.lower()
                ext_matches = (cur_ext == use_ext) or ({cur_ext, use_ext} == {".jpg", ".jpeg"})

                if inside_sub_ws and ext_matches and clean_bytes == raw_bytes:
                    staged.append(resolved_p.relative_to(sub_ws_resolved).as_posix())
                    continue

                cache_dir.mkdir(parents=True, exist_ok=True)
                if (
                    inside_main_ws
                    and ext_matches
                    and clean_bytes == raw_bytes
                    and resolved_p.relative_to(main_ws_resolved).as_posix().startswith(".media_cache/")
                ):
                    dst = cache_dir / resolved_p.name
                else:
                    sig = hashlib.sha1(clean_bytes).hexdigest()[:12]
                    dst = cache_dir / f"subref_{sig}{use_ext}"

                if not dst.is_file() or dst.stat().st_size != len(clean_bytes):
                    dst.write_bytes(clean_bytes)
                    wrote_any = True
                staged.append(f".media_cache/{dst.name}")
            except Exception as exc:
                logger.warning("标准化子代理参考图失败 (%s): %s", p_str, exc)
        if wrote_any:
            prune_media_cache(cache_dir)
        return staged

    def _build_bg_subagent_prompt(
        self,
        *,
        task_id: str,
        sender_label: str,
        mcp_id: str,
        tool_name: str,
        task_desc: str,
        staged_images: list[str],
    ) -> str:
        mcp_guide_text = ""
        if mcp_id:
            for g in self.mcp_manager.get_usage_guides():
                if g["id"] == mcp_id and g.get("usage_prompt"):
                    mcp_guide_text = g["usage_prompt"]
                    break
        img_block = "\n".join(f"  - {p}" for p in staged_images) if staged_images else "  - 无参考图片"
        prompt_parts = [
            "[后台子代理执行模式 - 独立执行耗时任务，禁止输出 <agy_bg_task>]",
            "你是独立运行的后台执行子代理，负责在后台完成主代理派发的任务（不阻塞主聊）。请严格遵守：",
            "1. **绝对禁止**输出 `<agy_bg_task>` 标签；若指定了 MCP 则直接调用对应 MCP 工具，若为前端/代码绘图等非 MCP 任务则直接在工作区写代码并渲染导出为 `.png`/`.jpg` 位图文件。",
            "2. 下方列出的本地参考图均已按真实文件头校准扩展名并放入工作区 `.media_cache/`（可直接读取和上传，切勿反复重命名或格式转换）；如有多张图，可精选 1~3 张调用 `read_file` 了解外貌风格即可。",
            '3. 若 MCP 需要本机执行 curl 上传参考图：在 Windows PowerShell 中**务必使用 `curl.exe -sS -F "file=@.media_cache/xxx.png" "上传地址"`**（不要用 PowerShell 的 `curl` 别名；多图直接重复 `-F` 一次传完，下方参考图已自动清洗元数据并校准扩展名，可直接一次上传成功）。',
            "4. 若该 MCP 包含多个可用模型，请严格遵守 MCP 调用规范；拿到参考图 ID 后立即调用生图工具（如返回 `taskId` 则耐心轮询 `get_image_task` 直到完成）。若首选生图模型因内容策略审核拒绝或报错失败，请自动换用该 MCP 中另一个合适的可用模型重试一次。",
            "5. 生成图片成功后，务必在回复中使用 Markdown 格式输出生成图片的 URL 或本地 `.png`/`.jpg` 路径，例如 `![图片](https://...)` 或 `![图片](logo.png)`，并附上一句简短结果摘要（说明使用的方案/模型和画面内容）；无论成功或失败，都必须输出文字结果摘要（若失败请附具体错误码和报错原文），切勿返回空内容。",
            "",
            "[任务派发详情]",
            f"- 任务编号：{task_id}",
            f"- 发起用户：{sender_label}",
            f"- 目标 MCP：{mcp_id or '无/按需自动选择（若需求禁止AI生图或要求代码/前端绘图则直接用本地代码渲染导出PNG）'}",
            f"- 指定工具：{tool_name or '按需自动选择'}",
            f"- 任务需求：{task_desc}",
            f"- 参考图片（工作区相对路径，已校准扩展名）：\n{img_block}",
        ]
        if mcp_guide_text:
            prompt_parts.extend(["", f"[该 MCP 专属调用规范 ({mcp_id})]", mcp_guide_text])
        return "\n".join(prompt_parts)

    async def run_subagent_preview(
        self,
        *,
        task_desc: str,
        mcp_id: str = "",
        tool_name: str = "",
        images: list[str] | None = None,
        model_override: str | None = None,
    ) -> dict[str, Any]:
        """Run the full background subagent pipeline (ref staging -> subagent -> image fetch) for WebUI testing."""
        task_id = f"pg-{uuid.uuid4().hex[:6]}"
        staged = self._stage_subagent_reference_images(list(images or []))
        sub_prompt = self._build_bg_subagent_prompt(
            task_id=task_id,
            sender_label="WebUI测试",
            mcp_id=mcp_id,
            tool_name=tool_name,
            task_desc=task_desc,
            staged_images=staged,
        )
        t0 = time.monotonic()
        res = await self.runner.run_subagent_task(
            task_id,
            sub_prompt,
            model_override=(model_override or "").strip() or None,
            timeout_seconds=self.config.agy.subagent_timeout_seconds,
        )
        if res.ok:
            self.record_token_usage(
                session_key=f"subagent:{task_id}",
                result=res,
                scene="subagent",
                channel_id=task_id,
                display_name=f"子代理测试 ({mcp_id or 'auto'})",
                trigger_type="subagent_preview",
                context_messages=1,
            )
        raw_text, _ = extract_bg_tasks((res.response if res.ok else "") or "")
        img_refs = [m.strip() for m in _MD_IMAGE_RE.findall(raw_text) if m.strip()]
        if not img_refs:
            img_refs = [u.strip() for u in _BARE_IMAGE_URL_RE.findall(raw_text) if u.strip()]
        img_sizes: list[int] = []
        seen_refs: set[str] = set()
        for ref in img_refs:
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            b = await self._load_reply_image_bytes(ref)
            if b:
                img_sizes.append(len(b))
        return {
            "ok": bool(res.ok),
            "task_id": task_id,
            "staged_images": staged,
            "response": raw_text,
            "error": res.error,
            "image_refs": list(seen_refs),
            "image_count": len(img_sizes),
            "image_sizes": img_sizes,
            "elapsed_seconds": round(time.monotonic() - t0, 2),
        }

    async def _run_bg_subagent_task(
        self,
        *,
        task_id: str,
        snapshot: ContextSnapshot,
        runner_key: str,
        task_spec: dict[str, Any],
        quote_flag: bool,
        at_flag: bool,
    ) -> None:
        mcp_id = str(task_spec.get("mcp") or "").strip()
        tool_name = str(task_spec.get("tool") or "").strip()
        task_desc = str(task_spec.get("task") or "").strip()
        req_images = list(task_spec.get("images") or [])
        if not req_images:
            req_images = self._collect_snapshot_image_paths(snapshot)
        staged_images = self._stage_subagent_reference_images(req_images)
        model_override = str(task_spec.get("model") or "").strip() or None

        trigger_user = snapshot.trigger_message
        sub_prompt = self._build_bg_subagent_prompt(
            task_id=task_id,
            sender_label=f"{trigger_user.sender_name}({trigger_user.user_id})",
            mcp_id=mcp_id,
            tool_name=tool_name,
            task_desc=task_desc,
            staged_images=staged_images,
        )
        logger.info(
            "[%s] [子代理:#%s] 主代理已派发后台任务 (mcp=%s, tool=%s, refs=%d, 需求=%s)",
            runner_key,
            task_id,
            mcp_id or "auto",
            tool_name or "auto",
            len(req_images),
            task_desc[:80],
        )

        try:
            sub_res = await self.runner.run_subagent_task(
                task_id,
                sub_prompt,
                model_override=model_override,
                timeout_seconds=self.config.agy.subagent_timeout_seconds,
            )
            if not sub_res.ok and is_proxy_or_geo_error(sub_res.error):
                switched = await self._try_auto_proxy_failover(sub_res.error)
                if switched:
                    logger.info("[%s] [子代理:#%s] 已切换低延迟代理节点，自动重试后台任务", runner_key, task_id)
                    sub_res = await self.runner.run_subagent_task(
                        task_id,
                        sub_prompt,
                        model_override=model_override,
                        timeout_seconds=self.config.agy.subagent_timeout_seconds,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[%s] 后台子代理 #%s 执行异常: %s", runner_key, task_id, exc)
            sub_res = None
            err_detail = str(exc)
        else:
            err_detail = sub_res.error if not sub_res.ok else ""
            if sub_res.ok:
                sub_display = (
                    (snapshot.trigger_message.sender_name or snapshot.channel_id)
                    if snapshot.scene == "private"
                    else snapshot.channel_id
                )
                self.record_token_usage(
                    session_key=snapshot.session_key,
                    result=sub_res,
                    scene=snapshot.scene,
                    channel_id=snapshot.channel_id,
                    display_name=sub_display,
                    trigger_type=f"subagent:{mcp_id or 'bg'}",
                    context_messages=1,
                )

        adapter = self.registry.get(snapshot.adapter_id)
        if adapter is None:
            logger.error("[%s] 后台子代理 #%s 完成但找不到适配器 %s", runner_key, task_id, snapshot.adapter_id)
            return

        images_bytes: list[bytes] = []
        raw_sub_text = (sub_res.response if (sub_res is not None and sub_res.ok) else "") or ""
        raw_sub_text, _ = extract_bg_tasks(raw_sub_text)

        if raw_sub_text:
            img_refs = [m.strip() for m in _MD_IMAGE_RE.findall(raw_sub_text) if m.strip()]
            if not img_refs:
                img_refs = [u.strip() for u in _BARE_IMAGE_URL_RE.findall(raw_sub_text) if u.strip()]
            seen_refs: set[str] = set()
            for ref in img_refs:
                if ref in seen_refs:
                    continue
                seen_refs.add(ref)
                b = await self._load_reply_image_bytes(ref)
                if b:
                    images_bytes.append(b)

        clean_summary = _MD_IMAGE_RE.sub("", raw_sub_text).strip()

        # Handoff result back to the main agent so it replies in-persona and records the outcome in memory
        final_caption = ""
        handed_ok = False
        if self.config.agy.subagent_handoff_to_main:
            logger.info(
                "[%s] [子代理:#%s] 执行结束 (ok=%s, 图片=%d张)，正在交回主代理组织回复...",
                runner_key,
                task_id,
                bool(sub_res is not None and sub_res.ok),
                len(images_bytes),
            )
            if sub_res is not None and sub_res.ok:
                handoff_prompt = (
                    "[后台子代理任务已完成 - 请用你当前的人设口吻简短回复群友，禁止输出 <agy_bg_task>]\n"
                    f"- 任务编号：{task_id}\n"
                    f"- 原发起用户：{trigger_user.sender_name}({trigger_user.user_id})\n"
                    f"- 原需求：{task_desc}\n"
                    f"- 子代理返回摘要：{clean_summary or '已完成生成'}\n"
                    f"- 已就绪图片数量：{len(images_bytes)} 张"
                    + ("（系统将自动随你的本条回复把真实图片发到聊天中，无需你再贴图片链接）" if images_bytes else "")
                    + "\n请用自然、符合人设的简短回复把结果告诉群友："
                )
            else:
                handoff_prompt = (
                    "[后台子代理任务执行失败 - 请用你当前的人设口吻简短告知群友，禁止输出 <agy_bg_task>]\n"
                    f"- 任务编号：{task_id}\n"
                    f"- 原发起用户：{trigger_user.sender_name}({trigger_user.user_id})\n"
                    f"- 原需求：{task_desc}\n"
                    f"- 失败原因：{err_detail or '未知错误'}\n"
                    "请用自然简短的口吻告知群友这次未能成功生成："
                )
            try:
                main_res = await self._ask_runner(runner_key, handoff_prompt)
                if main_res.ok:
                    ho_display = (
                        (snapshot.trigger_message.sender_name or snapshot.channel_id)
                        if snapshot.scene == "private"
                        else snapshot.channel_id
                    )
                    self.record_token_usage(
                        session_key=snapshot.session_key,
                        result=main_res,
                        scene=snapshot.scene,
                        channel_id=snapshot.channel_id,
                        display_name=ho_display,
                        trigger_type="subagent_handoff",
                        context_messages=len(snapshot.messages),
                    )
                if main_res.ok and main_res.response:
                    handed_text, _ = extract_bg_tasks(main_res.response)
                    handed_text = _MD_IMAGE_RE.sub("", handed_text).strip()
                    if self.config.content_safety.enabled and self.config.content_safety.check_output:
                        ok_cs, handed_text, _ = apply_content_safety(handed_text, self.config.content_safety)
                        if not ok_cs:
                            handed_text = ""
                    final_caption = handed_text
                    handed_ok = bool(final_caption)
            except Exception as exc:
                logger.warning("[%s] 后台子代理 #%s 回传主代理润色失败，降级直发: %s", runner_key, task_id, exc)

        if not final_caption:
            if sub_res is not None and sub_res.ok:
                final_caption = clean_summary or ("图片生成好啦~" if images_bytes else "")
            else:
                final_caption = f"后台任务执行失败：{err_detail or '未知错误'}"

        if not self.channel_guard.is_activated(snapshot.session_key):
            logger.info("[%s] 后台子代理 #%s 完成但该会话已因禁言重置为静默态，跳过发送", runner_key, task_id)
            return

        caption_segments = split_and_clean_reply(final_caption, self.config.reply) if final_caption else []
        self_id = getattr(adapter, "self_id", snapshot.trigger_message.self_id) or snapshot.trigger_message.self_id
        sent_id: str | None = None
        if images_bytes and hasattr(adapter, "send_image"):
            first_cap = caption_segments[0] if caption_segments else ""
            sent_any_img = False
            for img_idx, data in enumerate(images_bytes):
                if img_idx > 0 and not self.channel_guard.is_activated(snapshot.session_key):
                    break
                try:
                    sent_id = await adapter.send_image(
                        snapshot.trigger_message,
                        data,
                        caption=first_cap if img_idx == 0 else "",
                        quote_trigger=(quote_flag and img_idx == 0),
                        at_user=(at_flag and img_idx == 0),
                    )
                    sent_any_img = True
                except Exception as exc:
                    logger.warning("[%s] 后台子代理 #%s 发送图片失败: %s", runner_key, task_id, exc)
            remaining_segs = caption_segments[1:] if sent_any_img else caption_segments
            for s_idx, extra_seg in enumerate(remaining_segs):
                if s_idx > 0 or sent_any_img:
                    delay = compute_segment_delay(self.config.reply.segmented_reply, text=extra_seg)
                    if delay > 0:
                        await asyncio.sleep(delay)
                if not self.channel_guard.is_activated(snapshot.session_key):
                    break
                try:
                    sent_id = await adapter.send_reply(
                        snapshot.trigger_message,
                        extra_seg,
                        quote_trigger=(quote_flag and not sent_any_img and s_idx == 0),
                        at_user=(at_flag and not sent_any_img and s_idx == 0),
                    )
                except Exception as exc:
                    logger.warning("[%s] 后台子代理 #%s 发送文本失败: %s", runner_key, task_id, exc)
                    break
            record_text = f"{' '.join(caption_segments)} {'[图片]' if sent_any_img else ''}".strip()
        elif caption_segments:
            for idx, seg_text in enumerate(caption_segments):
                if idx > 0:
                    delay = compute_segment_delay(self.config.reply.segmented_reply, text=seg_text)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if not self.channel_guard.is_activated(snapshot.session_key):
                        break
                try:
                    sent_id = await adapter.send_reply(
                        snapshot.trigger_message,
                        seg_text,
                        quote_trigger=(quote_flag and idx == 0),
                        at_user=(at_flag and idx == 0),
                    )
                except Exception as exc:
                    logger.warning("[%s] 后台子代理 #%s 发送文本失败: %s", runner_key, task_id, exc)
                    break
            record_text = " ".join(caption_segments)
        else:
            return

        self_msg = IncomingMessage(
            adapter_id=snapshot.adapter_id,
            platform=snapshot.platform,
            self_id=self_id,
            scene=snapshot.scene,
            channel_id=snapshot.channel_id,
            message_id=sent_id or "",
            user_id=self_id,
            sender_name=self.config.bot_name,
            text=record_text,
            timestamp=time.time(),
            is_self=True,
        )
        self.context_store.add_message(self_msg, count_ambient=False)
        if handed_ok and self_msg.db_seq > 0:
            notify_fn = getattr(self.runner, "notify_self_reply_seq", None)
            if callable(notify_fn):
                notify_fn(runner_key, self_msg.db_seq)
        logger.info(
            "[%s] 后台子代理 #%s 已完成并交付结果 (images=%d, handoff=%s)",
            runner_key,
            task_id,
            len(images_bytes),
            self.config.agy.subagent_handoff_to_main,
        )

    def spawn_plugin_subagent_command(
        self,
        *,
        plugin_id: str,
        command_name: str,
        handler: Any,
        ctx: Any,
        msg: IncomingMessage,
        args: str,
    ) -> asyncio.Task[Any]:
        """Run a plugin command concurrently in a background subagent task without blocking main chat."""
        task_id = uuid.uuid4().hex[:8]
        task = asyncio.create_task(
            self._run_plugin_subagent_command(
                task_id=task_id,
                plugin_id=plugin_id,
                command_name=command_name,
                handler=handler,
                ctx=ctx,
                msg=msg,
                args=args,
            ),
            name=f"agy-plugin-subagent-{plugin_id}-{task_id}",
        )
        self._track_bg_task(task)
        return task

    async def _run_plugin_subagent_command(
        self,
        *,
        task_id: str,
        plugin_id: str,
        command_name: str,
        handler: Any,
        ctx: Any,
        msg: IncomingMessage,
        args: str,
    ) -> None:
        t0 = time.monotonic()
        logger.info(
            "[%s] [插件:%s][子代理:#%s] 后台并发任务已启动 (cmd=/%s)",
            msg.session_key,
            plugin_id,
            task_id,
            command_name,
        )
        token = PLUGIN_SUBAGENT_CTX.set(True)
        err_text = ""
        reply = None
        try:
            res = handler(ctx, msg, args)
            if inspect.isawaitable(res):
                res = await res
            reply = _normalize_plugin_reply(res, default_stop=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("[%s] [插件:%s][子代理:#%s] 命令 /%s 执行失败: %s", msg.session_key, plugin_id, task_id, command_name, exc)
            err_text = f"插件命令执行失败: {exc}"
        finally:
            PLUGIN_SUBAGENT_CTX.reset(token)

        adapter = self.registry.get(msg.adapter_id)
        if adapter is None:
            return

        raw_text = err_text if err_text else (reply.text if (reply is not None and reply.text) else "")
        if not raw_text.strip():
            logger.info(
                "[%s] [插件:%s][子代理:#%s] 后台任务执行完成 (cmd=/%s, 耗时 %.2fs，已由插件直发内容)",
                msg.session_key,
                plugin_id,
                task_id,
                command_name,
                time.monotonic() - t0,
            )
            return

        quote_flag = True if (reply is None or err_text) else bool(reply.quote)
        at_flag = False if (reply is None or err_text) else bool(reply.at)

        # Extract any Markdown images returned by the plugin text
        images_bytes: list[bytes] = []
        img_refs = [m.strip() for m in _MD_IMAGE_RE.findall(raw_text) if m.strip()]
        for ref in img_refs:
            b = await self._load_reply_image_bytes(ref)
            if b:
                images_bytes.append(b)
        clean_text = _MD_IMAGE_RE.sub("", raw_text).strip()

        final_text = clean_text
        if self.config.agy.subagent_handoff_to_main and not err_text:
            runner_key = (
                f"{msg.session_key}:u:{msg.user_id}"
                if (self.config.session.unique_session and msg.scene == "group")
                else msg.session_key
            )
            handoff_prompt = (
                "[后台插件子代理任务已完成 - 请用你当前的人设口吻简短转达结果给群友，禁止输出 <agy_bg_task>]\n"
                f"- 插件：{plugin_id} | 命令：/{command_name}\n"
                f"- 原发起用户：{msg.sender_name}({msg.user_id})\n"
                f"- 插件返回内容：{clean_text or '已完成'}\n"
                f"- 已就绪图片数量：{len(images_bytes)} 张"
                + ("（系统将自动随你的本条回复发送真实图片）" if images_bytes else "")
                + "\n请用自然、简洁且符合当前人设的口吻回复群友（保留关键结果信息）："
            )
            try:
                main_res = await self._ask_runner(runner_key, handoff_prompt)
                if main_res.ok:
                    plg_display = (
                        (msg.sender_name or msg.channel_id)
                        if msg.scene == "private"
                        else msg.channel_id
                    )
                    self.record_token_usage(
                        session_key=msg.session_key,
                        result=main_res,
                        scene=msg.scene,
                        channel_id=msg.channel_id,
                        display_name=plg_display,
                        trigger_type="plugin_handoff",
                    )
                if main_res.ok and main_res.response:
                    handed, _ = extract_bg_tasks(main_res.response)
                    handed = _MD_IMAGE_RE.sub("", handed).strip()
                    if handed:
                        final_text = handed
            except Exception as exc:
                logger.warning("[%s] 后台插件 #%s 回传主代理失败，降级直发: %s", msg.session_key, task_id, exc)

        if not self.channel_guard.is_activated(msg.session_key):
            logger.info("[%s] [插件:%s][子代理:#%s] 后台任务完成但该会话已因禁言重置为静默态，跳过发送", msg.session_key, plugin_id, task_id)
            return

        text_segments = split_and_clean_reply(final_text, self.config.reply) if final_text else []
        self_id = getattr(adapter, "self_id", msg.self_id) or msg.self_id
        sent_id: str | None = None
        if images_bytes and hasattr(adapter, "send_image"):
            first_cap = text_segments[0] if text_segments else ""
            sent_any_img = False
            for img_idx, data in enumerate(images_bytes):
                if img_idx > 0 and not self.channel_guard.is_activated(msg.session_key):
                    break
                try:
                    sent_id = await adapter.send_image(
                        msg,
                        data,
                        caption=first_cap if img_idx == 0 else "",
                        quote_trigger=(quote_flag and img_idx == 0),
                        at_user=(at_flag and img_idx == 0),
                    )
                    sent_any_img = True
                except Exception as exc:
                    logger.warning("[%s] [插件:%s][子代理:#%s] 发送图片失败: %s", msg.session_key, plugin_id, task_id, exc)
            remaining_segs = text_segments[1:] if sent_any_img else text_segments
            for s_idx, extra_seg in enumerate(remaining_segs):
                if s_idx > 0 or sent_any_img:
                    delay = compute_segment_delay(self.config.reply.segmented_reply, text=extra_seg)
                    if delay > 0:
                        await asyncio.sleep(delay)
                if not self.channel_guard.is_activated(msg.session_key):
                    break
                try:
                    sent_id = await adapter.send_reply(
                        msg,
                        extra_seg,
                        quote_trigger=(quote_flag and not sent_any_img and s_idx == 0),
                        at_user=(at_flag and not sent_any_img and s_idx == 0),
                    )
                except Exception as exc:
                    logger.warning("[%s] [插件:%s][子代理:#%s] 发送文本失败: %s", msg.session_key, plugin_id, task_id, exc)
                    break
            record_text = f"{' '.join(text_segments)} {'[图片]' if sent_any_img else ''}".strip()
        elif text_segments:
            for idx, seg_text in enumerate(text_segments):
                if idx > 0:
                    delay = compute_segment_delay(self.config.reply.segmented_reply, text=seg_text)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if not self.channel_guard.is_activated(msg.session_key):
                        break
                try:
                    sent_id = await adapter.send_reply(
                        msg,
                        seg_text,
                        quote_trigger=(quote_flag and idx == 0),
                        at_user=(at_flag and idx == 0),
                    )
                except Exception as exc:
                    logger.warning("[%s] [插件:%s][子代理:#%s] 发送文本失败: %s", msg.session_key, plugin_id, task_id, exc)
                    break
            record_text = " ".join(text_segments)
        else:
            record_text = ""

        self.context_store.add_message(
            IncomingMessage(
                adapter_id=msg.adapter_id,
                platform=msg.platform,
                self_id=self_id,
                scene=msg.scene,
                channel_id=msg.channel_id,
                message_id=sent_id or "",
                user_id=self_id,
                sender_name=self.config.bot_name,
                text=record_text,
                timestamp=time.time(),
                is_self=True,
            ),
            count_ambient=False,
        )
        logger.info(
            "[%s] [插件:%s][子代理:#%s] 后台任务完成并已发送 (cmd=/%s, 耗时 %.2fs, 图片 %d 张, 文本 %d 字)",
            msg.session_key,
            plugin_id,
            task_id,
            command_name,
            time.monotonic() - t0,
            len(images_bytes),
            len(final_text),
        )

    def _render_proactive_template(self, text: str, when: dt.datetime) -> str:
        weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return (
            (text or "")
            .replace("{{bot_name}}", self.config.bot_name)
            .replace("{{date}}", when.strftime("%Y-%m-%d"))
            .replace("{{time}}", when.strftime("%H:%M:%S"))
            .replace("{{weekday}}", weekdays[when.weekday()])
        )

    def _resolve_proactive_targets(
        self,
        *,
        adapter_id: str,
        scene: str,
        target_ids: list[str],
    ) -> list[tuple[str, str, str]]:
        chosen_adapter_id = adapter_id.strip()
        if not chosen_adapter_id:
            for aid, ad in self.registry.adapters.items():
                if ad.config.enabled:
                    chosen_adapter_id = aid
                    break
            if not chosen_adapter_id and self.registry.adapters:
                chosen_adapter_id = next(iter(self.registry.adapters.keys()))

        adapter = self.registry.get(chosen_adapter_id)
        if adapter is None:
            return []

        clean_targets = [t.strip() for t in target_ids if t and t.strip()]
        if not clean_targets:
            clean_targets = self.channel_guard.list_activated(adapter_id=chosen_adapter_id, scene=scene)
        else:
            # Anti-disturbance guard: only allow proactive delivery to already-activated channels
            clean_targets = [
                cid
                for cid in clean_targets
                if self.channel_guard.is_activated(ChannelActivationStore.make_key(chosen_adapter_id, scene, cid))
            ]

        return [(chosen_adapter_id, scene, cid) for cid in clean_targets]

    async def trigger_proactive(
        self,
        *,
        adapter_id: str = "",
        scene: str = "group",
        channel_id: str = "",
        mode: str = "llm",
        prompt: str = "",
        include_context: bool = True,
        trigger_type: TriggerType = "scheduled",
        reason: str = "proactive",
        at_qq: str = "",
    ) -> dict[str, Any]:
        """Proactively send a text or LLM-generated message to an activated target group/private channel."""
        clean_scene = "private" if scene == "private" else "group"
        targets = self._resolve_proactive_targets(
            adapter_id=adapter_id,
            scene=clean_scene,
            target_ids=[channel_id] if channel_id.strip() else [],
        )
        if not targets:
            return {
                "ok": False,
                "error": "未找到已激活的目标群聊/私聊（防打扰保护：需先有好友私聊或在群内显式 @机器人唤醒）",
                "sent_count": 0,
            }

        aid, sc, cid = targets[0]
        adapter = self.registry.get(aid)
        if adapter is None:
            return {"ok": False, "error": f"平台实例不存在: {aid}", "sent_count": 0}

        self_id = getattr(adapter, "self_id", adapter.config.self_id) or adapter.config.self_id or ""
        clean_at = at_qq.strip()
        synthetic_msg = IncomingMessage(
            adapter_id=aid,
            platform=adapter.platform_name,
            self_id=self_id,
            scene=sc,  # type: ignore[arg-type]
            channel_id=cid,
            message_id="",
            user_id=clean_at or "system",
            sender_name="定时/脚本触发",
            text=prompt,
            timestamp=time.time(),
            is_self=False,
        )

        clean_mode = "text" if mode == "text" else "llm"
        if clean_mode == "text":
            reply_text = prompt.strip()
            if not reply_text:
                return {"ok": False, "error": "发送文本不能为空", "sent_count": 0}
            segments = (
                split_and_clean_reply(reply_text, self.config.reply)
                if (not self.config.reply.segmented_reply.only_llm_result and self.config.reply.segmented_reply.enabled)
                else [reply_text]
            )
        else:
            circuit_open, rem = self.runner.is_circuit_open()
            if circuit_open:
                return {"ok": False, "error": f"Antigravity 熔断冷却中 ({rem:.0f}s)", "sent_count": 0}

            recent = (
                self.context_store.get_recent(synthetic_msg.session_key, limit=self.config.collector.context_message_limit)
                if include_context
                else []
            )
            snapshot = ContextSnapshot(
                session_key=synthetic_msg.session_key,
                adapter_id=aid,
                platform=adapter.platform_name,
                scene=sc,  # type: ignore[arg-type]
                channel_id=cid,
                primary_trigger=trigger_type,
                matched_triggers=[trigger_type],
                trigger_reason=reason,
                trigger_message=synthetic_msg,
                tracked_user_ids=["system"],
                extra_messages_collected=0,
                settled_at=time.time(),
                messages=recent,
                prompt_override=prompt.strip(),
            )
            active_profile = self.config.agy.resolve_model_profile(self.config.agy.model)
            effective_vision = active_profile.enable_vision if active_profile is not None else True
            if effective_vision and snapshot.messages:
                await materialize_snapshot_images(
                    snapshot.messages,
                    workspace_dir=self._workspace_dir,
                    adapter=adapter,
                    proxy=self.config.agy.proxy,
                )
            full_prompt = format_context_prompt(
                snapshot,
                self.config.bot_name,
                self.config.prompt,
                model_profile=active_profile,
            )
            full_prompt = await self.plugin_manager.dispatch_before_llm(snapshot, full_prompt)
            logger.info("[%s] 主动触发 Antigravity (%s / %s)", synthetic_msg.session_key, trigger_type, reason)
            result = await self.runner.ask(synthetic_msg.session_key, full_prompt)
            if not result.ok:
                logger.error("[%s] 主动触发推理失败: %s", synthetic_msg.session_key, result.error)
                return {"ok": False, "error": result.error or "推理失败", "sent_count": 0}
            self.record_token_usage(
                session_key=synthetic_msg.session_key,
                result=result,
                scene=sc,
                channel_id=cid,
                display_name=cid,
                trigger_type=trigger_type,
                context_messages=len(recent),
            )
            if not result.response:
                return {"ok": True, "reply": "", "segments": [], "sent_count": 0}

            reply_text = await self.plugin_manager.dispatch_after_llm(snapshot, result.response)
            if self.config.content_safety.enabled and self.config.content_safety.check_output:
                ok, reply_text, hit = apply_content_safety(reply_text, self.config.content_safety)
                if not ok:
                    logger.warning("[%s] 主动触发输出命中敏感词拦截 (%s)", synthetic_msg.session_key, hit)
                    return {"ok": False, "error": f"输出命中敏感词拦截: {hit}", "sent_count": 0}
            segments = split_and_clean_reply(reply_text, self.config.reply)

        sent_segments: list[str] = []
        for idx, seg_text in enumerate(segments):
            if not seg_text.strip():
                continue
            if idx > 0:
                delay = compute_segment_delay(self.config.reply.segmented_reply, text=seg_text)
                if delay > 0:
                    await asyncio.sleep(delay)
            if not self.channel_guard.is_activated(synthetic_msg.session_key):
                break
            try:
                sent_id = await adapter.send_reply(
                    synthetic_msg,
                    seg_text,
                    quote_trigger=False,
                    at_user=(bool(clean_at) and sc == "group" and idx == 0),
                )
            except Exception as exc:
                logger.warning("[%s] 主动触发消息发送失败: %s", synthetic_msg.session_key, exc)
                if not sent_segments:
                    return {"ok": False, "error": f"发送失败: {exc}", "sent_count": 0}
                break
            sent_segments.append(seg_text)
            self_msg = IncomingMessage(
                adapter_id=aid,
                platform=adapter.platform_name,
                self_id=self_id,
                scene=sc,  # type: ignore[arg-type]
                channel_id=cid,
                message_id=sent_id or "",
                user_id=self_id,
                sender_name=self.config.bot_name,
                text=seg_text,
                timestamp=time.time(),
                is_self=True,
            )
            self.context_store.add_message(self_msg, count_ambient=False)

        self.rate_limiter.record_reply_sent(synthetic_msg.session_key)
        self.context_store.reset_ambient_unread(synthetic_msg.session_key)
        return {
            "ok": True,
            "adapter_id": aid,
            "scene": sc,
            "channel_id": cid,
            "reply": reply_text,
            "segments": sent_segments,
            "sent_count": len(sent_segments),
        }

    async def _on_scheduled_task_fire(self, task: ScheduledTaskConfig, when: dt.datetime) -> int:
        rendered_prompt = self._render_proactive_template(task.prompt, when)
        targets = self._resolve_proactive_targets(
            adapter_id=task.adapter_id,
            scene=task.scene,
            target_ids=task.target_ids,
        )
        if not targets:
            logger.warning("定时任务 [%s] 未解析到任何发送目标，已跳过", task.id)
            return 0
        sent_targets = 0
        for aid, sc, cid in targets:
            res = await self.trigger_proactive(
                adapter_id=aid,
                scene=sc,
                channel_id=cid,
                mode=task.action_mode,
                prompt=rendered_prompt,
                include_context=task.include_context,
                trigger_type="scheduled",
                reason=f"scheduled:{task.name or task.id}",
                at_qq=task.at_qq,
            )
            if res.get("ok"):
                sent_targets += 1
        return sent_targets

    async def run(self) -> None:
        await self.ensure_plugins_loaded()
        self.scheduler.start()
        if self.config.webui.enabled:
            from .webui.server import WebUIServer

            self._webui_server = WebUIServer(self, self.config.webui, log_handler=self._log_handler)
            await self._webui_server.start()
        if not self.registry.adapters:
            logger.info(
                "当前尚未启用任何聊天客户端；请打开 WebUI 控制台 (http://127.0.0.1:%d) →「全局配置 → 平台」启用 QQ / Telegram / 通用 WS 客户端并点击「保存并热重载」即可在线生效。",
                self.config.webui.port,
            )
        try:
            await self.registry.run_all(self.handle_message)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        bg_pending = list(self._bg_tasks)
        for task in bg_pending:
            task.cancel()
        if bg_pending:
            with contextlib.suppress(Exception):
                await asyncio.gather(*bg_pending, return_exceptions=True)
        self._bg_tasks.clear()
        await self.scheduler.stop()
        await self.debouncer.shutdown()
        self.context_store.close()
        await self.plugin_manager.unload_all()
        await self.registry.stop_all()
        await self.runner.shutdown()
        if self._webui_server is not None:
            await self._webui_server.stop()
            self._webui_server = None
