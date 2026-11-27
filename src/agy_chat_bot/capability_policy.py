from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from .config import (
        CapabilityOverridesConfig,
        CollectorConfig,
        ModelProfileConfig,
        TriggersConfig,
    )

CapabilityKind = Literal["plugins", "mcp", "skills"]

PRIORITY_LABELS: tuple[tuple[str, str], ...] = (
    ("client", "客户端"),
    ("session", "群聊/私聊"),
    ("model", "独立模型"),
    ("global", "全局"),
)


def normalize_capability_kind(kind: str) -> CapabilityKind:
    low = (kind or "").strip().lower()
    if low in ("plugin", "plugins"):
        return "plugins"
    if low in ("skill", "skills"):
        return "skills"
    if low in ("mcp", "mcps"):
        return "mcp"
    raise ValueError(f"不支持的能力类型: {kind}")


def normalize_override_bucket(raw: Any) -> dict[str, dict[str, bool]]:
    """Normalize a single scope's override map into {'plugins': {...}, 'mcp': {...}, 'skills': {...}}."""
    out: dict[str, dict[str, bool]] = {"plugins": {}, "mcp": {}, "skills": {}}
    if not isinstance(raw, dict):
        return out
    for src_key, canon_key in (
        ("plugins", "plugins"),
        ("plugin", "plugins"),
        ("mcp", "mcp"),
        ("mcps", "mcp"),
        ("skills", "skills"),
        ("skill", "skills"),
    ):
        sub = raw.get(src_key)
        if not isinstance(sub, dict):
            continue
        for item_id, val in sub.items():
            iid = str(item_id or "").strip()
            if not iid or val is None or val == "":
                continue
            if isinstance(val, str):
                vlow = val.strip().lower()
                if vlow in ("inherit", "default", "null", "none"):
                    continue
                out[canon_key][iid] = vlow in ("true", "1", "yes", "on")
            else:
                out[canon_key][iid] = bool(val)
    return out


def _lookup_session_bucket(
    sessions_map: dict[str, dict[str, dict[str, bool]]],
    *,
    session_key: str = "",
    adapter_id: str = "",
    scene: str = "",
    channel_id: str = "",
) -> dict[str, dict[str, bool]] | None:
    if not sessions_map:
        return None
    candidates: list[str] = []
    sk = (session_key or "").strip()
    aid = (adapter_id or "").strip()
    sc = (scene or "").strip().lower()
    cid = (channel_id or "").strip()
    if sk:
        candidates.append(sk)
    if aid and sc and cid:
        composed = f"{aid}:{sc}:{cid}"
        if composed not in candidates:
            candidates.append(composed)
    if sc and cid:
        candidates.append(f"{sc}:{cid}")
    if cid:
        candidates.append(cid)
    for cand in candidates:
        bucket = sessions_map.get(cand)
        if isinstance(bucket, dict):
            return bucket
    return None


def resolve_capability_enabled(
    kind: str,
    item_id: str,
    *,
    global_enabled: bool,
    capability_overrides: CapabilityOverridesConfig | None = None,
    model_profile: ModelProfileConfig | None = None,
    adapter_id: str = "",
    session_key: str = "",
    scene: str = "",
    channel_id: str = "",
    legacy_plugin_allowed: bool | None = None,
) -> tuple[bool, str]:
    """Resolve effective enabled state with strict precedence:
    Client (adapter) > Group/Private Session > Model Profile > Global.

    Returns `(enabled, matched_layer)` where `matched_layer` is one of
    `'client' | 'session' | 'model' | 'global'`.
    """
    canon = normalize_capability_kind(kind)
    iid = (item_id or "").strip()
    if not iid:
        return bool(global_enabled), "global"

    # 1. Highest priority: Client (adapter_id)
    if capability_overrides is not None and adapter_id:
        client_bucket = capability_overrides.clients.get(adapter_id.strip())
        if isinstance(client_bucket, dict):
            kmap = client_bucket.get(canon)
            if isinstance(kmap, dict) and iid in kmap:
                return bool(kmap[iid]), "client"

    # 2. Second priority: Specific Group / Private Chat Session
    if capability_overrides is not None:
        sess_bucket = _lookup_session_bucket(
            capability_overrides.sessions,
            session_key=session_key,
            adapter_id=adapter_id,
            scene=scene,
            channel_id=channel_id,
        )
        if isinstance(sess_bucket, dict):
            kmap = sess_bucket.get(canon)
            if isinstance(kmap, dict) and iid in kmap:
                return bool(kmap[iid]), "session"

    # 3. Third priority: Independent Model Profile
    if model_profile is not None:
        if canon == "mcp":
            mmap = model_profile.mcp_overrides
        elif canon == "skills":
            mmap = model_profile.skill_overrides
        else:
            mmap = model_profile.plugin_overrides
        if isinstance(mmap, dict) and iid in mmap:
            return bool(mmap[iid]), "model"

    # 4. Lowest priority: Global (with legacy plugin scope compatibility when no higher override matched)
    if canon == "plugins" and legacy_plugin_allowed is not None:
        return bool(global_enabled and legacy_plugin_allowed), "global"
    return bool(global_enabled), "global"


def has_any_override_enabling(
    kind: str,
    item_id: str,
    *,
    capability_overrides: CapabilityOverridesConfig | None = None,
    model_profiles: dict[str, ModelProfileConfig] | None = None,
) -> bool:
    """Return True if any Client / Session / Model Profile explicitly enables `item_id`."""
    canon = normalize_capability_kind(kind)
    iid = (item_id or "").strip()
    if not iid:
        return False
    if capability_overrides is not None:
        for bucket in capability_overrides.clients.values():
            if isinstance(bucket, dict) and bucket.get(canon, {}).get(iid) is True:
                return True
        for bucket in capability_overrides.sessions.values():
            if isinstance(bucket, dict) and bucket.get(canon, {}).get(iid) is True:
                return True
    if model_profiles:
        for prof in model_profiles.values():
            if canon == "mcp" and prof.mcp_overrides.get(iid) is True:
                return True
            if canon == "skills" and prof.skill_overrides.get(iid) is True:
                return True
            if canon == "plugins" and prof.plugin_overrides.get(iid) is True:
                return True
    return False


def resolve_effective_capabilities_map(
    kind: str,
    global_items: dict[str, bool],
    *,
    capability_overrides: CapabilityOverridesConfig | None = None,
    model_profile: ModelProfileConfig | None = None,
    adapter_id: str = "",
    session_key: str = "",
    scene: str = "",
    channel_id: str = "",
) -> dict[str, bool]:
    """Compute effective `{item_id: bool}` map for all known items in a category."""
    out: dict[str, bool] = {}
    for iid, g_en in sorted(global_items.items()):
        eff, _ = resolve_capability_enabled(
            kind,
            iid,
            global_enabled=g_en,
            capability_overrides=capability_overrides,
            model_profile=model_profile,
            adapter_id=adapter_id,
            session_key=session_key,
            scene=scene,
            channel_id=channel_id,
        )
        out[iid] = eff
    return out


def resolve_group_override_entry(
    overrides: dict[str, dict[str, Any]],
    *,
    session_key: str = "",
    channel_id: str = "",
) -> dict[str, Any]:
    """Look up a group-specific override dict by exact `session_key`, `group:<id>`, or raw `channel_id`."""
    if not overrides:
        return {}
    sk = (session_key or "").strip()
    cid = (channel_id or "").strip()
    if sk and sk in overrides and isinstance(overrides[sk], dict):
        return overrides[sk]
    if cid:
        gkey = f"group:{cid}"
        if gkey in overrides and isinstance(overrides[gkey], dict):
            return overrides[gkey]
        if cid in overrides and isinstance(overrides[cid], dict):
            return overrides[cid]
    return {}


def resolve_group_trigger_override(
    triggers_cfg: TriggersConfig,
    *,
    session_key: str = "",
    channel_id: str = "",
) -> dict[str, Any]:
    return resolve_group_override_entry(
        triggers_cfg.group_overrides,
        session_key=session_key,
        channel_id=channel_id,
    )


def resolve_group_compact_override(
    collector_cfg: CollectorConfig,
    *,
    session_key: str = "",
    channel_id: str = "",
) -> dict[str, Any]:
    return resolve_group_override_entry(
        collector_cfg.group_compact_overrides,
        session_key=session_key,
        channel_id=channel_id,
    )
