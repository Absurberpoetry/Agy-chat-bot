from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import inspect
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..capability_policy import has_any_override_enabling, resolve_capability_enabled
from .sdk import BotPlugin, CommandMeta, HookMeta, PluginContext, PluginReply

if TYPE_CHECKING:
    from ..app import BotApplication
    from ..models import ContextSnapshot, IncomingMessage

logger = logging.getLogger(__name__)

_VALID_PLUGIN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-]{0,63}$")


@dataclass
class RegisteredCommand:
    plugin_id: str
    meta: CommandMeta
    handler: Any


@dataclass
class RegisteredHook:
    plugin_id: str
    meta: HookMeta
    handler: Any


def normalize_plugin_scope(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    mode = str(raw.get("mode", "all") or "all").strip().lower()
    if mode not in ("all", "groups", "exclude_groups"):
        mode = "all"
    gids_raw = raw.get("group_ids") or []
    group_ids = [str(x).strip() for x in gids_raw if str(x).strip()] if isinstance(gids_raw, list) else []
    raw_cmd_sub = raw.get("command_subagent")
    command_subagent: dict[str, bool] = {}
    if isinstance(raw_cmd_sub, dict):
        for ck, cv in raw_cmd_sub.items():
            cname = str(ck or "").strip().lstrip("/!#.").lower()
            if cname:
                command_subagent[cname] = bool(cv)
    return {
        "mode": mode,
        "adapter_id": str(raw.get("adapter_id", "") or "").strip(),
        "group_ids": group_ids,
        "allow_private": bool(raw.get("allow_private", True)),
        "use_subagent": bool(raw.get("use_subagent", False)),
        "command_subagent": command_subagent,
        "model": str(raw.get("model", "") or "").strip(),
        "persona_id": str(raw.get("persona_id", "") or "").strip(),
    }


@dataclass
class LoadedPluginRecord:
    plugin_id: str
    name: str
    version: str
    author: str
    description: str
    file_path: Path
    enabled: bool = True
    loaded: bool = False
    error: str = ""
    default_config: dict[str, Any] = field(default_factory=dict)
    config_schema: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    scope: dict[str, Any] = field(default_factory=lambda: normalize_plugin_scope(None))
    commands: list[CommandMeta] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    instance: BotPlugin | None = None
    ctx: PluginContext | None = None
    module_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.plugin_id,
            "name": self.name or self.plugin_id,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "file_path": str(self.file_path),
            "file_name": self.file_path.name,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "error": self.error,
            "default_config": copy.deepcopy(self.default_config),
            "config_schema": copy.deepcopy(self.config_schema),
            "config": copy.deepcopy(self.config),
            "scope": copy.deepcopy(self.scope),
            "commands": [
                {
                    "name": c.name,
                    "aliases": list(c.aliases),
                    "description": c.description,
                    "admin_only": c.admin_only,
                }
                for c in self.commands
            ],
            "hooks": list(self.hooks),
        }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class PluginManager:
    """Discovers, loads, hot-reloads, and dispatches hooks/commands for external plugins."""

    def __init__(self, app: BotApplication, plugins_dir: Path, data_dir: Path) -> None:
        self._app = app
        self.plugins_dir = plugins_dir
        self.data_dir = data_dir
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, LoadedPluginRecord] = {}
        self._commands: dict[str, RegisteredCommand] = {}
        self._hooks: dict[str, list[RegisteredHook]] = {
            "on_message": [],
            "on_before_llm": [],
            "on_after_llm": [],
        }
        self._load_lock = asyncio.Lock()

    def set_plugins_dir(self, plugins_dir: Path) -> None:
        self.plugins_dir = plugins_dir
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    def _discover_plugin_files(self) -> list[tuple[str, Path]]:
        if not self.plugins_dir.exists():
            return []
        found: list[tuple[str, Path]] = []
        seen: set[str] = set()
        for entry in sorted(self.plugins_dir.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("_") or entry.name.startswith("."):
                continue
            if entry.is_file() and entry.suffix == ".py":
                pid = entry.stem
                if pid not in seen:
                    seen.add(pid)
                    found.append((pid, entry))
            elif entry.is_dir():
                for candidate in ("main.py", "plugin.py", "__init__.py"):
                    fp = entry / candidate
                    if fp.is_file():
                        pid = entry.name
                        if pid not in seen:
                            seen.add(pid)
                            found.append((pid, fp))
                        break
        return found

    async def load_all(self) -> list[dict[str, Any]]:
        async with self._load_lock:
            await self._unload_all_unlocked()
            cfg = self._app.config.plugins
            if not cfg.enabled:
                logger.info("外部插件系统已关闭 (plugins.enabled=false)")
                return []

            disabled_set = set(cfg.disabled)
            for fallback_id, file_path in self._discover_plugin_files():
                await self._load_one_file(fallback_id, file_path, disabled_set=disabled_set)

            self._rebuild_dispatch_tables()
            loaded_cnt = sum(1 for r in self._records.values() if r.loaded)
            logger.info("外部插件加载完成: 发现 %d 个，已启用 %d 个", len(self._records), loaded_cnt)
            return self.list_plugins()

    async def unload_all(self) -> None:
        async with self._load_lock:
            await self._unload_all_unlocked()

    async def _unload_all_unlocked(self) -> None:
        for rec in list(self._records.values()):
            await self._unload_record(rec)
        self._records.clear()
        self._commands.clear()
        for k in self._hooks:
            self._hooks[k].clear()

    async def _unload_record(self, rec: LoadedPluginRecord) -> None:
        if rec.instance is not None:
            try:
                res = rec.instance.on_unload()
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                logger.warning("插件 %s on_unload 异常: %s", rec.plugin_id, exc)
            rec.instance = None
        if rec.module_name and rec.module_name in sys.modules:
            sys.modules.pop(rec.module_name, None)
        rec.loaded = False

    async def _load_one_file(
        self,
        fallback_id: str,
        file_path: Path,
        *,
        disabled_set: set[str],
    ) -> LoadedPluginRecord:
        path_hash = hashlib.sha1(str(file_path.resolve()).encode("utf-8", errors="replace")).hexdigest()[:8]
        module_name = f"agy_ext_plugin_{re.sub(r'[^a-zA-Z0-9_]', '_', fallback_id)}_{path_hash}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"无法读取模块: {file_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            plugin_cls = self._find_plugin_class(module)
            if plugin_cls is None:
                raise ValueError("未在插件文件中找到 BotPlugin 子类")

            plugin_id = str(getattr(plugin_cls, "id", "") or fallback_id).strip()
            if not _VALID_PLUGIN_ID.match(plugin_id):
                raise ValueError(f"插件 ID 非法（仅允许字母、数字、下划线和连字符）: {plugin_id!r}")
            if plugin_id in self._records:
                raise ValueError(f"存在重复的插件 ID「{plugin_id}」，已跳过冲突文件: {file_path.name}")
            name = (getattr(plugin_cls, "name", "") or plugin_id).strip()
            version = str(getattr(plugin_cls, "version", "1.0.0") or "1.0.0")
            author = str(getattr(plugin_cls, "author", "") or "")
            description = str(getattr(plugin_cls, "description", "") or "")
            default_cfg = copy.deepcopy(getattr(plugin_cls, "default_config", None) or {})
            raw_schema = getattr(plugin_cls, "config_schema", None)
            cfg_schema = copy.deepcopy(raw_schema) if isinstance(raw_schema, list) else []
            user_cfg = self._app.config.plugins.configs.get(plugin_id) or {}
            merged_cfg = _deep_merge(default_cfg, user_cfg)
            scope = normalize_plugin_scope(self._app.config.plugins.scopes.get(plugin_id))
            enabled = (plugin_id not in disabled_set) and (fallback_id not in disabled_set)
            override_enabled = (
                not enabled
                and has_any_override_enabling(
                    "plugins",
                    plugin_id,
                    capability_overrides=self._app.config.capability_overrides,
                    model_profiles=self._app.config.agy.model_profiles,
                )
            )

            rec = LoadedPluginRecord(
                plugin_id=plugin_id,
                name=name,
                version=version,
                author=author,
                description=description,
                file_path=file_path,
                enabled=enabled,
                loaded=False,
                default_config=default_cfg,
                config_schema=cfg_schema,
                config=merged_cfg,
                scope=scope,
                module_name=module_name,
            )

            if enabled or override_enabled:
                plugins_data_root = (self.data_dir / "plugins").resolve()
                pdata_dir = (plugins_data_root / plugin_id).resolve()
                if not pdata_dir.is_relative_to(plugins_data_root):
                    raise ValueError(f"插件数据目录越界: {plugin_id}")
                pdata_dir.mkdir(parents=True, exist_ok=True)
                ctx = PluginContext(
                    app=self._app,
                    plugin_id=plugin_id,
                    plugin_config=merged_cfg,
                    data_dir=pdata_dir,
                )
                instance = plugin_cls(ctx)
                res = instance.on_load()
                if inspect.isawaitable(res):
                    await res
                rec.ctx = ctx
                rec.instance = instance
                rec.loaded = True
                self._inspect_instance_metadata(rec, instance)
            else:
                sys.modules.pop(module_name, None)

            self._records[plugin_id] = rec
            return rec
        except Exception as exc:
            sys.modules.pop(module_name, None)
            logger.exception("加载外部插件失败 (%s): %s", file_path.name, exc)
            err_key = fallback_id if fallback_id not in self._records else f"{fallback_id}_{path_hash}"
            rec = LoadedPluginRecord(
                plugin_id=err_key,
                name=fallback_id,
                version="?",
                author="",
                description="",
                file_path=file_path,
                enabled=fallback_id not in disabled_set,
                loaded=False,
                error=str(exc),
                scope=normalize_plugin_scope(self._app.config.plugins.scopes.get(fallback_id)),
                module_name=module_name,
            )
            self._records[err_key] = rec
            return rec

    @staticmethod
    def _find_plugin_class(module: Any) -> type[BotPlugin] | None:
        explicit = getattr(module, "Plugin", None)
        if inspect.isclass(explicit) and issubclass(explicit, BotPlugin) and explicit is not BotPlugin:
            return explicit
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BotPlugin) and obj is not BotPlugin and obj.__module__ == module.__name__:
                return obj
        return None

    @staticmethod
    def _inspect_instance_metadata(rec: LoadedPluginRecord, instance: BotPlugin) -> None:
        cmds: list[CommandMeta] = []
        hook_names: list[str] = []
        for attr_name in dir(instance):
            if attr_name.startswith("__"):
                continue
            attr = getattr(instance, attr_name, None)
            if not callable(attr):
                continue
            for cm in getattr(attr, "_agy_commands", ()):
                cmds.append(cm)
            for hm in getattr(attr, "_agy_hooks", ()):
                hook_names.append(hm.kind)
        rec.commands = cmds
        rec.hooks = sorted(set(hook_names))

    def _rebuild_dispatch_tables(self) -> None:
        self._commands.clear()
        for k in self._hooks:
            self._hooks[k].clear()

        for rec in self._records.values():
            if not rec.loaded or rec.instance is None:
                continue
            inst = rec.instance
            for attr_name in dir(inst):
                if attr_name.startswith("__"):
                    continue
                attr = getattr(inst, attr_name, None)
                if not callable(attr):
                    continue
                for cm in getattr(attr, "_agy_commands", ()):
                    reg = RegisteredCommand(plugin_id=rec.plugin_id, meta=cm, handler=attr)
                    self._commands[cm.name] = reg
                    for alias in cm.aliases:
                        self._commands[alias] = reg
                for hm in getattr(attr, "_agy_hooks", ()):
                    if hm.kind in self._hooks:
                        self._hooks[hm.kind].append(
                            RegisteredHook(plugin_id=rec.plugin_id, meta=hm, handler=attr)
                        )

        for k in self._hooks:
            self._hooks[k].sort(key=lambda h: h.meta.priority)

    def list_plugins(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in sorted(self._records.values(), key=lambda x: x.plugin_id.lower())]

    def get_record(self, plugin_id: str) -> LoadedPluginRecord | None:
        return self._records.get(plugin_id)

    def list_registered_commands(self) -> list[dict[str, Any]]:
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, Any]] = []
        for reg in self._commands.values():
            key = (reg.plugin_id, reg.meta.name)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "plugin_id": reg.plugin_id,
                    "name": reg.meta.name,
                    "aliases": list(reg.meta.aliases),
                    "description": reg.meta.description,
                    "admin_only": reg.meta.admin_only,
                }
            )
        return sorted(out, key=lambda x: x["name"])

    async def set_enabled(self, plugin_id: str, enabled: bool) -> dict[str, Any]:
        if plugin_id not in self._records:
            raise ValueError(f"未找到插件: {plugin_id}")
        disabled = [x for x in self._app.config.plugins.disabled if x != plugin_id]
        if not enabled:
            disabled.append(plugin_id)
        self._app.config.plugins.disabled = disabled
        await self.load_all()
        rec = self._records.get(plugin_id)
        if rec is None:
            raise ValueError(f"未找到插件: {plugin_id}")
        return rec.to_dict()

    async def update_plugin_config(self, plugin_id: str, new_cfg: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(new_cfg, dict):
            raise ValueError("插件配置必须是 JSON 对象")
        rec = self._records.get(plugin_id)
        if rec is None:
            raise ValueError(f"未找到插件: {plugin_id}")
        self._app.config.plugins.configs[plugin_id] = copy.deepcopy(new_cfg)
        merged = _deep_merge(rec.default_config, new_cfg)
        rec.config = merged
        if rec.ctx is not None:
            rec.ctx.set_plugin_config(merged)
        if rec.instance is not None:
            try:
                res = rec.instance.on_config_update(merged)
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                logger.warning("插件 %s on_config_update 异常: %s", plugin_id, exc)
        return rec.to_dict()

    def get_plugin_scope(self, plugin_id: str) -> dict[str, Any]:
        rec = self._records.get(plugin_id)
        if rec is not None:
            return copy.deepcopy(rec.scope)
        return normalize_plugin_scope(self._app.config.plugins.scopes.get(plugin_id))

    async def update_plugin_scope(self, plugin_id: str, raw_scope: dict[str, Any]) -> dict[str, Any]:
        rec = self._records.get(plugin_id)
        if rec is None:
            raise ValueError(f"未找到插件: {plugin_id}")
        scope = normalize_plugin_scope(raw_scope)
        self._app.config.plugins.scopes[plugin_id] = copy.deepcopy(scope)
        rec.scope = scope
        return rec.to_dict()

    def is_plugin_allowed(
        self,
        plugin_id: str,
        *,
        adapter_id: str,
        scene: str,
        channel_id: str,
        session_key: str = "",
    ) -> bool:
        rec = self._records.get(plugin_id)
        global_en = (rec.enabled if rec is not None else (plugin_id not in self._app.config.plugins.disabled))

        scope = self.get_plugin_scope(plugin_id)
        legacy_ok = True
        s_adapter = scope.get("adapter_id", "")
        if s_adapter and adapter_id and s_adapter != adapter_id:
            legacy_ok = False
        elif scene == "private":
            legacy_ok = bool(scope.get("allow_private", True))
        else:
            mode = scope.get("mode", "all")
            gids = scope.get("group_ids") or []
            cid = str(channel_id or "").strip()
            if mode == "groups":
                legacy_ok = cid in gids
            elif mode == "exclude_groups":
                legacy_ok = cid not in gids

        model_profile = self._app.config.agy.resolve_model_profile(self._app.config.agy.model)
        eff_sk = session_key or (f"{adapter_id}:{scene}:{channel_id}" if (adapter_id and scene and channel_id) else "")
        allowed, _ = resolve_capability_enabled(
            "plugins",
            plugin_id,
            global_enabled=global_en,
            capability_overrides=self._app.config.capability_overrides,
            model_profile=model_profile,
            adapter_id=adapter_id,
            session_key=eff_sk,
            scene=scene,
            channel_id=channel_id,
            legacy_plugin_allowed=legacy_ok,
        )
        return allowed

    def should_use_subagent(self, plugin_id: str, command_name: str = "") -> bool:
        """Resolve whether a plugin (or a specific command of it) should run via background subagent."""
        scope = self.get_plugin_scope(plugin_id)
        cmd_map = scope.get("command_subagent") or {}
        if command_name:
            clean = command_name.strip().lstrip("/!#.").lower()
            if clean in cmd_map:
                return bool(cmd_map[clean])
        return bool(scope.get("use_subagent", False))

    def get_plugin_llm_options(self, plugin_id: str) -> dict[str, Any]:
        """Return effective per-plugin model, persona_id, and subagent defaults."""
        scope = self.get_plugin_scope(plugin_id)
        return {
            "use_subagent": bool(scope.get("use_subagent", False)),
            "model": str(scope.get("model", "") or "").strip(),
            "persona_id": str(scope.get("persona_id", "") or "").strip(),
        }

    async def import_plugin(self, filename: str, code: str, *, overwrite: bool = False) -> dict[str, Any]:
        raw_name = Path(filename or "").name
        if not raw_name.endswith(".py"):
            raise ValueError("目前支持导入 .py 格式的插件文件")
        stem = raw_name[:-3].strip()
        if not _VALID_PLUGIN_ID.match(stem):
            raise ValueError("文件名仅允许字母、数字、下划线和连字符，长度 1-64")
        target = self.plugins_dir / f"{stem}.py"
        if target.exists() and not overwrite:
            raise ValueError(f"插件文件 {target.name} 已存在（如需覆盖请勾选允许覆盖）")
        target.write_text(code, encoding="utf-8")
        await self.load_all()
        rec = self._records.get(stem) or next((r for r in self._records.values() if r.file_path == target), None)
        if rec is None:
            raise RuntimeError("插件导入后未能发现，请检查文件是否包含 BotPlugin 子类")
        return rec.to_dict()

    def read_source(self, plugin_id: str) -> tuple[Path, str]:
        rec = self._records.get(plugin_id)
        if rec is None or not rec.file_path.exists():
            raise FileNotFoundError(f"未找到插件源码: {plugin_id}")
        return rec.file_path, rec.file_path.read_text(encoding="utf-8")

    async def write_source(self, plugin_id: str, code: str) -> dict[str, Any]:
        rec = self._records.get(plugin_id)
        if rec is None:
            raise FileNotFoundError(f"未找到插件: {plugin_id}")
        rec.file_path.write_text(code, encoding="utf-8")
        await self.load_all()
        updated = self._records.get(plugin_id) or next(
            (r for r in self._records.values() if r.file_path == rec.file_path), None
        )
        if updated is None:
            raise RuntimeError("插件保存后未能重新发现")
        return updated.to_dict()

    async def create_plugin(self, plugin_id: str, code: str | None = None) -> dict[str, Any]:
        pid = plugin_id.strip()
        if not _VALID_PLUGIN_ID.match(pid):
            raise ValueError("插件 ID 仅允许字母、数字、下划线和连字符，长度 1-64")
        target = self.plugins_dir / f"{pid}.py"
        if target.exists():
            raise ValueError(f"插件文件已存在: {target.name}")
        content = code if (code is not None and code.strip()) else render_plugin_template(pid)
        target.write_text(content, encoding="utf-8")
        await self.load_all()
        rec = self._records.get(pid) or next((r for r in self._records.values() if r.file_path == target), None)
        if rec is None:
            raise RuntimeError("插件创建失败")
        return rec.to_dict()

    async def delete_plugin(self, plugin_id: str) -> bool:
        rec = self._records.get(plugin_id)
        if rec is None:
            return False
        await self._unload_record(rec)
        fp = rec.file_path
        if fp.exists():
            if fp.parent != self.plugins_dir and fp.parent.parent == self.plugins_dir:
                import shutil
                shutil.rmtree(fp.parent, ignore_errors=True)
            else:
                fp.unlink(missing_ok=True)
        self._app.config.plugins.disabled = [x for x in self._app.config.plugins.disabled if x != plugin_id]
        self._app.config.plugins.configs.pop(plugin_id, None)
        self._app.config.plugins.scopes.pop(plugin_id, None)
        await self.load_all()
        return True

    async def dispatch_command(self, msg: IncomingMessage) -> tuple[bool, PluginReply | None]:
        """Match and execute a registered plugin command. Returns (handled, reply)."""
        if not self._app.config.plugins.enabled or not self._commands:
            return False, None
        text = msg.text.strip()
        prefixes = self._app.config.triggers.command_prefixes or ["/"]
        matched_prefix = next((p for p in prefixes if p and text.startswith(p)), None)
        if matched_prefix is None:
            return False, None
        body = text[len(matched_prefix) :].strip()
        if not body:
            return False, None
        parts = body.split(None, 1)
        cmd_name = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""
        reg = self._commands.get(cmd_name)
        if reg is None:
            return False, None

        if not self.is_plugin_allowed(
            reg.plugin_id,
            adapter_id=msg.adapter_id,
            scene=msg.scene,
            channel_id=msg.channel_id,
        ):
            return False, None

        is_admin = msg.user_id in self._app.config.admins
        if not is_admin and hasattr(self._app, "identity_resolver"):
            id_res = self._app.identity_resolver.resolve(
                msg.scene, msg.channel_id, msg.user_id, adapter_id=msg.adapter_id
            )
            is_admin = bool(id_res.active and (id_res.is_admin or id_res.allow_bot_commands))
        if reg.meta.admin_only and not is_admin:
            return True, PluginReply(text="权限不足：仅管理员可执行此命令。", stop=True)

        rec = self._records.get(reg.plugin_id)
        if rec is None or rec.ctx is None:
            return False, None

        if (
            self._app.config.agy.subagent_enabled
            and self.should_use_subagent(reg.plugin_id, reg.meta.name)
            and hasattr(self._app, "spawn_plugin_subagent_command")
        ):
            logger.info(
                "[%s] [插件:%s] 触发命令 /%s (模式=后台子代理, 用户=%s(%s), 参数=%s)",
                msg.session_key,
                reg.plugin_id,
                reg.meta.name,
                msg.sender_name,
                msg.user_id,
                (args[:80] if args else "无"),
            )
            self._app.spawn_plugin_subagent_command(
                plugin_id=reg.plugin_id,
                command_name=reg.meta.name,
                handler=reg.handler,
                ctx=rec.ctx,
                msg=msg,
                args=args,
            )
            return True, None

        t0 = time.monotonic()
        logger.info(
            "[%s] [插件:%s] 触发命令 /%s (模式=主线程, 用户=%s(%s), 参数=%s)",
            msg.session_key,
            reg.plugin_id,
            reg.meta.name,
            msg.sender_name,
            msg.user_id,
            (args[:80] if args else "无"),
        )
        try:
            res = reg.handler(rec.ctx, msg, args)
            if inspect.isawaitable(res):
                res = await res
            norm = _normalize_reply(res, default_stop=True)
            elapsed = time.monotonic() - t0
            logger.info(
                "[%s] [插件:%s] 命令 /%s 执行完成 (耗时 %.2fs, 回复=%d字)",
                msg.session_key,
                reg.plugin_id,
                reg.meta.name,
                elapsed,
                len(norm.text) if norm is not None else 0,
            )
            return True, norm
        except Exception as exc:
            logger.exception("[%s] [插件:%s] 执行命令 /%s 失败: %s", msg.session_key, reg.plugin_id, cmd_name, exc)
            return True, PluginReply(text=f"插件命令执行失败: {exc}", stop=True)

    async def dispatch_on_message(self, msg: IncomingMessage) -> PluginReply | None:
        if not self._app.config.plugins.enabled:
            return None
        for hook in self._hooks["on_message"]:
            if not self.is_plugin_allowed(
                hook.plugin_id,
                adapter_id=msg.adapter_id,
                scene=msg.scene,
                channel_id=msg.channel_id,
            ):
                continue
            rec = self._records.get(hook.plugin_id)
            if rec is None or rec.ctx is None:
                continue
            try:
                res = hook.handler(rec.ctx, msg)
                if inspect.isawaitable(res):
                    res = await res
                reply = _normalize_reply(res, default_stop=False)
                if reply is not None and (reply.stop or reply.text):
                    logger.info(
                        "[%s] [插件:%s] on_message 钩子命中 (stop=%s, 回复=%d字)",
                        msg.session_key,
                        hook.plugin_id,
                        reply.stop,
                        len(reply.text or ""),
                    )
                    return reply
            except Exception as exc:
                logger.exception("[%s] [插件:%s] on_message 异常: %s", msg.session_key, hook.plugin_id, exc)
        return None

    async def dispatch_before_llm(self, snapshot: ContextSnapshot, prompt: str) -> str:
        if not self._app.config.plugins.enabled:
            return prompt
        current = prompt
        for hook in self._hooks["on_before_llm"]:
            if not self.is_plugin_allowed(
                hook.plugin_id,
                adapter_id=snapshot.adapter_id,
                scene=snapshot.scene,
                channel_id=snapshot.channel_id,
            ):
                continue
            rec = self._records.get(hook.plugin_id)
            if rec is None or rec.ctx is None:
                continue
            try:
                res = hook.handler(rec.ctx, snapshot, current)
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, str) and res.strip():
                    if res != current:
                        logger.info(
                            "[%s] [插件:%s] on_before_llm 已改写提示词 (%d -> %d 字)",
                            snapshot.session_key,
                            hook.plugin_id,
                            len(current),
                            len(res),
                        )
                    current = res
            except Exception as exc:
                logger.exception("[%s] [插件:%s] on_before_llm 异常: %s", snapshot.session_key, hook.plugin_id, exc)
        return current

    async def dispatch_after_llm(self, snapshot: ContextSnapshot, reply_text: str) -> str:
        if not self._app.config.plugins.enabled:
            return reply_text
        current = reply_text
        for hook in self._hooks["on_after_llm"]:
            if not self.is_plugin_allowed(
                hook.plugin_id,
                adapter_id=snapshot.adapter_id,
                scene=snapshot.scene,
                channel_id=snapshot.channel_id,
            ):
                continue
            rec = self._records.get(hook.plugin_id)
            if rec is None or rec.ctx is None:
                continue
            try:
                res = hook.handler(rec.ctx, snapshot, current)
                if inspect.isawaitable(res):
                    res = await res
                if isinstance(res, str):
                    if res != current:
                        logger.info(
                            "[%s] [插件:%s] on_after_llm 已处理回复文本 (%d -> %d 字)",
                            snapshot.session_key,
                            hook.plugin_id,
                            len(current),
                            len(res),
                        )
                    current = res
            except Exception as exc:
                logger.exception("[%s] [插件:%s] on_after_llm 异常: %s", snapshot.session_key, hook.plugin_id, exc)
        return current


def _normalize_reply(res: Any, *, default_stop: bool) -> PluginReply | None:
    if res is None:
        return None
    if isinstance(res, PluginReply):
        return res
    if isinstance(res, str):
        if not res.strip():
            return None
        return PluginReply(text=res, stop=True)
    if isinstance(res, dict):
        return PluginReply(
            text=str(res.get("text", "")),
            stop=bool(res.get("stop", default_stop)),
            quote=bool(res.get("quote", True)),
            at=bool(res.get("at", False)),
        )
    if res is True:
        return PluginReply(text="", stop=True)
    return None


def render_plugin_template(plugin_id: str) -> str:
    cls_name = "".join(part.capitalize() for part in re.split(r"[^a-zA-Z0-9]+", plugin_id) if part) or "Custom"
    return f'''from __future__ import annotations

from agy_chat_bot.plugins import BotPlugin, PluginContext, PluginReply, command, on_after_llm, on_before_llm, on_message


class {cls_name}Plugin(BotPlugin):
    id = "{plugin_id}"
    name = "{plugin_id}"
    version = "1.0.0"
    author = "custom"
    description = "自定义外部插件"
    default_config = {{
        "greeting": "你好，这是自定义插件回复",
    }}

    async def on_load(self) -> None:
        self.ctx.logger.info("插件 %s 已加载", self.id)

    @command("{plugin_id}", description="示例命令")
    async def cmd_demo(self, ctx: PluginContext, msg, args: str) -> str:
        base = self.config.get("greeting", "OK")
        return f"{{base}}: {{args}}" if args else str(base)

    # 可选钩子示例：
    # @on_message(priority=50)
    # async def handle_msg(self, ctx: PluginContext, msg) -> PluginReply | str | None:
    #     return None
    #
    # @on_before_llm(priority=50)
    # async def before_llm(self, ctx: PluginContext, snapshot, prompt: str) -> str:
    #     return prompt
    #
    # @on_after_llm(priority=50)
    # async def after_llm(self, ctx: PluginContext, snapshot, reply_text: str) -> str:
    #     return reply_text
'''
