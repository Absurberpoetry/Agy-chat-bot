from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

logger = logging.getLogger(__name__)

_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,63}$")
_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)", re.DOTALL)
_MAX_SKILL_ZIP_FILES = 200
_MAX_SKILL_ZIP_TOTAL_BYTES = 20 * 1024 * 1024
_MAX_SKILL_ZIP_FILE_BYTES = 5 * 1024 * 1024
_MAX_MCP_STDIO_CONTENT_LENGTH = 8 * 1024 * 1024


def open_in_file_manager(target: Path, *, select_file: bool = False) -> tuple[bool, str]:
    """Open a local file or directory in the OS file manager (non-blocking)."""
    resolved = target.resolve()
    if not resolved.exists():
        resolved.parent.mkdir(parents=True, exist_ok=True)
        if not select_file:
            resolved.mkdir(parents=True, exist_ok=True)
    try:
        if os.name == "nt":
            if select_file and resolved.is_file():
                subprocess.Popen(["explorer", f"/select,{resolved}"])
            else:
                folder = resolved if resolved.is_dir() else resolved.parent
                subprocess.Popen(["explorer", str(folder)])
            return True, str(resolved)
        if sys.platform == "darwin":
            if select_file and resolved.is_file():
                subprocess.Popen(["open", "-R", str(resolved)])
            else:
                folder = resolved if resolved.is_dir() else resolved.parent
                subprocess.Popen(["open", str(folder)])
            return True, str(resolved)
        folder = resolved if resolved.is_dir() else resolved.parent
        subprocess.Popen(["xdg-open", str(folder)])
        return True, str(resolved)
    except Exception as exc:
        logger.warning("打开系统文件管理器失败 (%s): %s", resolved, exc)
        return False, str(exc)


def _slugify_id(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\-\.]", "-", (raw or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-.")
    return cleaned[:64]


def _validate_id(raw: str, label: str = "ID") -> str:
    val = (raw or "").strip()
    if not val or not _VALID_ID_RE.match(val) or ".." in val:
        raise ValueError(f"无效的 {label}「{val}」：仅允许字母、数字、下划线、短横线或点，且必须以字母或数字开头")
    return val


def parse_skill_md(raw_text: str, fallback_name: str = "") -> tuple[str, str, str]:
    """Parse SKILL.md YAML frontmatter into (name, description, markdown_body)."""
    text = (raw_text or "").replace("\r\n", "\n")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return fallback_name, "", text.strip()

    fm_block = m.group(1)
    body = text[m.end() :].lstrip("\n")
    name = fallback_name
    description = ""

    lines = fm_block.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line and not line.startswith((" ", "\t")):
            key, val = line.split(":", 1)
            k = key.strip().lower()
            v = val.strip()
            if v in (">", ">-", "|", "|-"):
                block_lines: list[str] = []
                i += 1
                while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                    block_lines.append(lines[i].strip())
                    i += 1
                joined = (" ".join(x for x in block_lines if x) if v.startswith(">") else "\n".join(block_lines)).strip()
                if k == "name" and joined:
                    name = joined
                elif k == "description":
                    description = joined
                continue
            v = v.strip()
            if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
                v = v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            elif len(v) >= 2 and v[0] == "'" and v[-1] == "'":
                v = v[1:-1].replace("''", "'")
            if k == "name" and v:
                name = v
            elif k == "description":
                description = v
        i += 1

    return name or fallback_name, description, body


def render_skill_md(name: str, description: str, body: str) -> str:
    """Render clean SKILL.md with YAML frontmatter."""
    clean_name = re.sub(r"[\r\n]+", " ", (name or "").strip()).strip()
    clean_desc = (description or "").strip()
    clean_body = (body or "").replace("\r\n", "\n").strip()

    if any(ch in clean_name for ch in (":", "#", '"', "'")):
        safe_name = clean_name.replace("\\", "\\\\").replace('"', '\\"')
        name_yaml = f'"{safe_name}"'
    else:
        name_yaml = clean_name

    if "\n" in clean_desc or ":" in clean_desc or len(clean_desc) > 80:
        indented = "\n".join(f"  {ln}" for ln in clean_desc.splitlines())
        desc_yaml = f">-\n{indented}"
    else:
        safe_desc = clean_desc.replace("\\", "\\\\").replace('"', '\\"')
        desc_yaml = f'"{safe_desc}"' if clean_desc else '""'

    fm = f"---\nname: {name_yaml}\ndescription: {desc_yaml}\n---\n"
    if clean_body:
        return f"{fm}\n{clean_body}\n"
    return fm


class SkillsManager:
    """Manages Antigravity native workspace skills (`bot_workspace/.agents/skills/`) and discovers global skills."""

    def __init__(self, workspace_dir: Path) -> None:
        self.workspace_dir = workspace_dir
        self.agents_dir = workspace_dir / ".agents"
        self.active_dir = self.agents_dir / "skills"
        self.disabled_dir = self.agents_dir / "skills_disabled"
        self.active_dir.mkdir(parents=True, exist_ok=True)

    def _global_skill_roots(self) -> list[Path]:
        home = Path.home()
        return [
            home / ".gemini" / "config" / "skills",
            home / ".gemini" / "antigravity-cli" / "skills",
        ]

    def _read_skill_dir(self, skill_dir: Path, *, enabled: bool, scope: str) -> dict[str, Any] | None:
        md_file = skill_dir / "SKILL.md"
        if not md_file.is_file():
            return None
        try:
            raw = md_file.read_text(encoding="utf-8-sig")
        except Exception as exc:
            logger.warning("读取 Skill 失败 %s: %s", md_file, exc)
            return None
        sid = skill_dir.name
        name, desc, body = parse_skill_md(raw, fallback_name=sid)
        extra_dirs = [
            d for d in ("scripts", "references", "examples", "resources")
            if (skill_dir / d).is_dir() and any((skill_dir / d).iterdir())
        ]
        return {
            "id": sid,
            "name": name or sid,
            "description": desc,
            "body": body,
            "raw_content": raw,
            "enabled": enabled,
            "scope": scope,
            "readonly": scope != "workspace",
            "path": str(md_file),
            "dir_path": str(skill_dir),
            "extra_dirs": extra_dirs,
            "updated_at": md_file.stat().st_mtime,
        }

    def list_skills(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen_ws: set[str] = set()

        if self.active_dir.is_dir():
            for entry in sorted(self.active_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith("."):
                    rec = self._read_skill_dir(entry, enabled=True, scope="workspace")
                    if rec is not None:
                        items.append(rec)
                        seen_ws.add(entry.name)

        if self.disabled_dir.is_dir():
            for entry in sorted(self.disabled_dir.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith(".") and entry.name not in seen_ws:
                    rec = self._read_skill_dir(entry, enabled=False, scope="workspace")
                    if rec is not None:
                        items.append(rec)
                        seen_ws.add(entry.name)

        seen_global: set[str] = set()
        for groot in self._global_skill_roots():
            if not groot.is_dir():
                continue
            for entry in sorted(groot.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith(".") and entry.name not in seen_global:
                    rec = self._read_skill_dir(entry, enabled=True, scope="global")
                    if rec is not None:
                        items.append(rec)
                        seen_global.add(entry.name)

        return items

    def _find_workspace_skill_dir(self, skill_id: str) -> tuple[Path | None, bool]:
        sid = _validate_id(skill_id, "Skill ID")
        active = self.active_dir / sid
        if active.is_dir() and (active / "SKILL.md").is_file():
            return active, True
        disabled = self.disabled_dir / sid
        if disabled.is_dir() and (disabled / "SKILL.md").is_file():
            return disabled, False
        return None, False

    def upsert_skill(
        self,
        skill_id: str,
        *,
        name: str = "",
        description: str = "",
        body: str = "",
        raw_content: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        sid = _validate_id(skill_id, "Skill ID")
        existing_dir, was_enabled = self._find_workspace_skill_dir(sid)
        target_enabled = was_enabled if (existing_dir is not None and enabled is None) else (True if enabled is None else bool(enabled))
        target_root = self.active_dir if target_enabled else self.disabled_dir
        target_root.mkdir(parents=True, exist_ok=True)
        target_dir = target_root / sid

        if existing_dir is not None and existing_dir.resolve() != target_dir.resolve():
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.move(str(existing_dir), str(target_dir))
        else:
            target_dir.mkdir(parents=True, exist_ok=True)

        if raw_content is not None and raw_content.strip():
            content = raw_content.replace("\r\n", "\n")
            if not content.endswith("\n"):
                content += "\n"
        else:
            content = render_skill_md(name or sid, description, body)

        md_file = target_dir / "SKILL.md"
        tmp = target_dir / ".SKILL.md.tmp"
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, md_file)

        rec = self._read_skill_dir(target_dir, enabled=target_enabled, scope="workspace")
        if rec is None:
            raise RuntimeError(f"写入 Skill 失败: {sid}")
        return rec

    def set_enabled(self, skill_id: str, enabled: bool) -> dict[str, Any]:
        sid = _validate_id(skill_id, "Skill ID")
        existing_dir, was_enabled = self._find_workspace_skill_dir(sid)
        if existing_dir is None:
            raise FileNotFoundError(f"未找到工作区 Skill: {sid}（全局 Skill 仅支持只读查看）")
        if was_enabled == bool(enabled):
            rec = self._read_skill_dir(existing_dir, enabled=was_enabled, scope="workspace")
            assert rec is not None
            return rec

        target_root = self.active_dir if enabled else self.disabled_dir
        target_root.mkdir(parents=True, exist_ok=True)
        target_dir = target_root / sid
        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.move(str(existing_dir), str(target_dir))
        rec = self._read_skill_dir(target_dir, enabled=bool(enabled), scope="workspace")
        assert rec is not None
        return rec

    def delete_skill(self, skill_id: str) -> bool:
        sid = _validate_id(skill_id, "Skill ID")
        existing_dir, _ = self._find_workspace_skill_dir(sid)
        if existing_dir is None:
            return False
        shutil.rmtree(existing_dir)
        return True

    def import_skill(
        self,
        filename: str,
        raw_bytes: bytes,
        *,
        skill_id: str = "",
        overwrite: bool = False,
    ) -> dict[str, Any]:
        if not raw_bytes:
            raise ValueError("导入文件内容为空")
        lower_name = (filename or "").strip().lower()

        if lower_name.endswith(".zip"):
            try:
                zf_cm = zipfile.ZipFile(io.BytesIO(raw_bytes))
            except zipfile.BadZipFile as exc:
                raise ValueError(f"无效的 ZIP 压缩包: {exc}") from exc

            def _safe_zip_parts(member_name: str) -> tuple[str, ...] | None:
                norm = member_name.replace("\\", "/")
                if norm.startswith("/") or norm.startswith("//"):
                    return None
                if PureWindowsPath(member_name).drive or PureWindowsPath(member_name).is_absolute():
                    return None
                parts = tuple(p for p in PurePosixPath(norm).parts if p not in ("", "."))
                if not parts:
                    return None
                if any(p in ("..", "/", "\\") or ":" in p or "\x00" in p for p in parts):
                    return None
                return parts

            with zf_cm as zf:
                valid_entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
                total_uncompressed = 0
                for info in zf.infolist():
                    if info.is_dir() or info.filename.startswith("__MACOSX/"):
                        continue
                    # Reject symlinks embedded in ZIP archives
                    if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                        continue
                    parts = _safe_zip_parts(info.filename)
                    if parts is None:
                        logger.warning("导入 Skill ZIP 时跳过不安全路径: %s", info.filename)
                        continue
                    if info.file_size > _MAX_SKILL_ZIP_FILE_BYTES:
                        raise ValueError(f"ZIP 内单文件过大（>{_MAX_SKILL_ZIP_FILE_BYTES // (1024 * 1024)}MB）: {info.filename}")
                    total_uncompressed += max(0, int(info.file_size))
                    if total_uncompressed > _MAX_SKILL_ZIP_TOTAL_BYTES:
                        raise ValueError(f"ZIP 解压总大小超过限制（{_MAX_SKILL_ZIP_TOTAL_BYTES // (1024 * 1024)}MB）")
                    valid_entries.append((info, parts))
                    if len(valid_entries) > _MAX_SKILL_ZIP_FILES:
                        raise ValueError(f"ZIP 内文件数超过限制（{_MAX_SKILL_ZIP_FILES}）")

                skill_md_members = [parts for _, parts in valid_entries if parts[-1].lower() == "skill.md"]
                if not skill_md_members:
                    raise ValueError("ZIP 压缩包中未找到 SKILL.md 文件")
                skill_md_members.sort(key=len)
                prefix_parts = skill_md_members[0][:-1]
                inferred_id = (
                    skill_id.strip()
                    or (prefix_parts[-1] if prefix_parts else "")
                    or Path(filename).stem
                )
                sid = _validate_id(_slugify_id(inferred_id), "Skill ID")
                existing_dir, _ = self._find_workspace_skill_dir(sid)
                if existing_dir is not None and not overwrite:
                    raise FileExistsError(f"Skill「{sid}」已存在，请勾选覆盖或更换名称")

                # Extract into an isolated staging directory first so invalid archives never destroy existing skills
                stage_dir = self.active_dir / f".import_stage_{uuid.uuid4().hex[:10]}"
                stage_dir.mkdir(parents=True, exist_ok=True)
                stage_resolved = stage_dir.resolve()
                actual_total = 0
                try:
                    for info, parts in valid_entries:
                        rel_parts = parts
                        if prefix_parts and parts[: len(prefix_parts)] == prefix_parts:
                            rel_parts = parts[len(prefix_parts) :]
                        if not rel_parts:
                            continue
                        dest = (stage_dir / Path(*rel_parts)).resolve()
                        if not dest.is_relative_to(stage_resolved):
                            raise ValueError(f"ZIP 包含越界路径，已拒绝导入: {info.filename}")
                        with zf.open(info, "r") as src:
                            data = src.read(_MAX_SKILL_ZIP_FILE_BYTES + 1)
                        if len(data) > _MAX_SKILL_ZIP_FILE_BYTES:
                            raise ValueError(f"ZIP 内单文件实际体积过大: {info.filename}")
                        actual_total += len(data)
                        if actual_total > _MAX_SKILL_ZIP_TOTAL_BYTES:
                            raise ValueError(f"ZIP 实际解压体积超过限制（{_MAX_SKILL_ZIP_TOTAL_BYTES // (1024 * 1024)}MB）")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_bytes(data)

                    # Normalize SKILL.md filename casing if needed
                    if not (stage_dir / "SKILL.md").is_file():
                        for cand in stage_dir.iterdir():
                            if cand.is_file() and cand.name.lower() == "skill.md":
                                cand.rename(stage_dir / "SKILL.md")
                                break
                    if not (stage_dir / "SKILL.md").is_file():
                        raise ValueError("ZIP 压缩包根目录未找到有效 SKILL.md 文件")

                    target_dir = self.active_dir / sid
                    if existing_dir is not None and existing_dir.exists():
                        shutil.rmtree(existing_dir)
                    if target_dir.exists():
                        shutil.rmtree(target_dir)
                    shutil.move(str(stage_dir), str(target_dir))
                finally:
                    if stage_dir.exists():
                        shutil.rmtree(stage_dir, ignore_errors=True)

                rec = self._read_skill_dir(target_dir, enabled=True, scope="workspace")
                if rec is None:
                    raise RuntimeError("解压导入 Skill 失败")
                return rec

        # Markdown file import (.md)
        text = raw_bytes.decode("utf-8-sig", errors="replace")
        parsed_name, _, _ = parse_skill_md(text, fallback_name="")
        inferred = skill_id.strip() or parsed_name or Path(filename or "custom-skill").stem
        if inferred.lower() == "skill":
            inferred = "custom-skill"
        sid = _validate_id(_slugify_id(inferred), "Skill ID")
        existing_dir, _ = self._find_workspace_skill_dir(sid)
        if existing_dir is not None and not overwrite:
            raise FileExistsError(f"Skill「{sid}」已存在，请勾选覆盖或更换名称")
        return self.upsert_skill(sid, raw_content=text, enabled=True)

    def resolve_location(self, skill_id: str = "") -> tuple[Path, bool]:
        if skill_id.strip():
            sid = skill_id.strip()
            for item in self.list_skills():
                if item["id"] == sid:
                    return Path(item["dir_path"]), False
            raise FileNotFoundError(f"未找到 Skill: {sid}")
        self.active_dir.mkdir(parents=True, exist_ok=True)
        return self.active_dir, False


def _normalize_mcp_server_entry(server_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    sid = _validate_id(server_id, "MCP 服务器 ID")
    url = str(raw.get("url") or raw.get("serverUrl") or "").strip()
    command = str(raw.get("command") or "").strip()
    transport = str(raw.get("transport") or "").strip().lower()
    if transport not in ("stdio", "http"):
        transport = "http" if (url and not command) else "stdio"

    if transport == "http":
        if not url:
            raise ValueError(f"HTTP MCP 服务器「{sid}」必须填写 url")
    else:
        if not command:
            raise ValueError(f"Stdio MCP 服务器「{sid}」必须填写 command")

    args_raw = raw.get("args") or []
    args = [str(a) for a in args_raw] if isinstance(args_raw, list) else []

    env_raw = raw.get("env") or {}
    env = {str(k).strip(): str(v) for k, v in env_raw.items() if str(k).strip()} if isinstance(env_raw, dict) else {}

    headers_raw = raw.get("headers") or {}
    headers = (
        {str(k).strip(): str(v) for k, v in headers_raw.items() if str(k).strip()}
        if isinstance(headers_raw, dict)
        else {}
    )

    raw_tool_sub = raw.get("tool_subagent")
    tool_subagent: dict[str, bool] = {}
    if isinstance(raw_tool_sub, dict):
        for tk, tv in raw_tool_sub.items():
            tname = str(tk or "").strip()
            if tname:
                tool_subagent[tname] = bool(tv)

    raw_disc = raw.get("discovered_tools")
    discovered_tools: list[dict[str, Any]] = []
    if isinstance(raw_disc, list):
        seen_t: set[str] = set()
        for item in raw_disc:
            if isinstance(item, str):
                tname = item.strip()
                if tname and tname not in seen_t:
                    seen_t.add(tname)
                    discovered_tools.append({"name": tname, "description": "", "params": []})
            elif isinstance(item, dict):
                tname = str(item.get("name") or "").strip()
                if tname and tname not in seen_t:
                    seen_t.add(tname)
                    params = item.get("params")
                    discovered_tools.append(
                        {
                            "name": tname,
                            "description": str(item.get("description") or "").strip()[:240],
                            "params": [str(p) for p in params] if isinstance(params, list) else [],
                        }
                    )

    return {
        "id": sid,
        "enabled": bool(raw.get("enabled", True)),
        "transport": transport,
        "description": str(raw.get("description") or "").strip(),
        "usage_prompt": str(raw.get("usage_prompt") or raw.get("prompt") or "").replace("\r\n", "\n").strip(),
        "async_subagent": bool(raw.get("async_subagent", False)),
        "tool_subagent": tool_subagent,
        "discovered_tools": discovered_tools,
        "command": command if transport == "stdio" else "",
        "args": args if transport == "stdio" else [],
        "env": env if transport == "stdio" else {},
        "url": url if transport == "http" else "",
        "headers": headers if transport == "http" else {},
    }


def _parse_mcp_jsonrpc_body(body_text: str) -> dict[str, Any] | None:
    """Parse either plain JSON or Streamable HTTP SSE (`data: {...}`) JSON-RPC payload."""
    text = (body_text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                parsed = json.loads(line[5:].strip())
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
    return None


def _summarize_mcp_tools(raw_tools: Any) -> list[dict[str, Any]]:
    """Extract compact tool name/description/parameter summaries from MCP tools/list result."""
    if not isinstance(raw_tools, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw_tools:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        desc = str(item.get("description") or "").strip()
        schema = item.get("inputSchema")
        params: list[str] = []
        if isinstance(schema, dict):
            props = schema.get("properties")
            req = set(str(x) for x in (schema.get("required") or []))
            if isinstance(props, dict):
                for pk, pv in props.items():
                    ptype = ""
                    extra = ""
                    if isinstance(pv, dict):
                        ptype = str(pv.get("type") or "")
                        enum_vals = pv.get("enum")
                        if isinstance(enum_vals, list) and enum_vals:
                            preview = ", ".join(str(v) for v in enum_vals[:8])
                            if len(enum_vals) > 8:
                                preview += ", ..."
                            extra = f" [{preview}]"
                    star = "*" if str(pk) in req else ""
                    params.append(f"{pk}{star}{f'({ptype})' if ptype else ''}{extra}")
        out.append({"name": name, "description": desc[:240], "params": params})
    return out


def _to_antigravity_mcp_entry(entry: dict[str, Any]) -> dict[str, Any]:
    if entry.get("transport") == "http":
        out: dict[str, Any] = {"url": entry["url"]}
        if entry.get("headers"):
            out["headers"] = dict(entry["headers"])
        return out
    out = {"command": entry["command"]}
    if entry.get("args"):
        out["args"] = list(entry["args"])
    if entry.get("env"):
        out["env"] = dict(entry["env"])
    return out


class McpManager:
    """Manages Antigravity native MCP servers (`bot_workspace/.agents/mcp_config.json`) + enable/disable registry."""

    _BUILTIN_IDS = frozenset({"chat_history"})

    def __init__(
        self,
        workspace_dir: Path,
        data_dir: Path,
        *,
        settings_file: Path | None = None,
        subagent_workspace_dir: Path | None = None,
    ) -> None:
        self.workspace_dir = workspace_dir
        self.data_dir = data_dir
        self.settings_file = settings_file
        self.subagent_workspace_dir = subagent_workspace_dir
        self.agents_dir = workspace_dir / ".agents"
        self.mcp_config_path = self.agents_dir / "mcp_config.json"
        self.registry_path = data_dir / "mcp_servers.json"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._servers: dict[str, dict[str, Any]] = {}
        self._builtin_servers: dict[str, dict[str, Any]] = {}
        self._load()
        if self._servers:
            self._sync_settings_allow()
        if self.subagent_workspace_dir is not None:
            self._sync_subagent_mcp_config()

    def configure_builtin_history_server(self, *, db_path: Path | None, enabled: bool) -> None:
        """Register or remove the built-in read-only SQLite chat_history MCP server."""
        if not enabled or db_path is None:
            if "chat_history" in self._builtin_servers:
                self._builtin_servers.pop("chat_history", None)
                self._sync_workspace_mcp_config()
            return
        server_script = Path(__file__).resolve().parent / "mcp_history_server.py"
        entry = {
            "id": "chat_history",
            "enabled": True,
            "transport": "stdio",
            "description": "内置群聊/私聊 SQLite 历史消息数据库只读检索服务（供模型回查因上下文压缩移出窗口的早期对话与自身历史回复）",
            "usage_prompt": (
                "- 当用户询问早期聊过的内容、上下文被压缩截断、或需要核对群友/你自己之前说过的话时，直接同步调用 `chat_history` 的工具：\n"
                "  1. `get_older_messages(session_key, before_seq, limit)`：紧接当前窗口起始序号往前翻页读取更早对话；\n"
                "  2. `search_chat_history(session_key, keyword, sender, role, limit)`：按关键词、发言人或 `role=\"assistant\"`（仅搜你自己的历史回复）检索；\n"
                "  3. `get_context_around(session_key, target_seq, radius)`：查看某条历史消息 `#seq` 前后的连续上下文。\n"
                "- 注意：`session_key` 必须原样传入提示词中给出的当前会话标识；该 MCP 为毫秒级本地只读查询，**主代理直接同步调用，切勿派发给后台子代理**。"
            ),
            "async_subagent": False,
            "tool_subagent": {},
            "discovered_tools": [
                {"name": "get_older_messages", "description": "往前翻阅被压缩移出窗口的早期对话", "params": ["session_key*", "before_seq", "limit"]},
                {"name": "search_chat_history", "description": "按关键词/发言人/角色检索全量历史数据库", "params": ["session_key*", "keyword", "sender", "role", "limit"]},
                {"name": "get_context_around", "description": "查看指定序号 #seq 前后的连续对话语境", "params": ["session_key*", "target_seq*", "radius"]},
            ],
            "command": sys.executable,
            "args": ["-X", "utf8", "-u", str(server_script), "--db", str(db_path.resolve())],
            "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            "url": "",
            "headers": {},
        }
        self._builtin_servers["chat_history"] = entry
        self._sync_workspace_mcp_config()

    def _global_mcp_path(self) -> Path:
        return Path.home() / ".gemini" / "config" / "mcp_config.json"

    def _load(self) -> None:
        self._servers = {}
        if self.registry_path.is_file():
            try:
                raw = json.loads(self.registry_path.read_text(encoding="utf-8-sig"))
                items = raw.get("servers", raw) if isinstance(raw, dict) else {}
                if isinstance(items, dict):
                    for sid, cfg in items.items():
                        if sid in self._BUILTIN_IDS:
                            continue
                        if isinstance(cfg, dict):
                            try:
                                self._servers[sid] = _normalize_mcp_server_entry(sid, cfg)
                            except Exception:
                                pass
            except Exception as exc:
                logger.warning("读取 MCP 注册表失败: %s", exc)

        # Merge any manually edited servers from bot_workspace/.agents/mcp_config.json
        if self.mcp_config_path.is_file():
            try:
                raw = json.loads(self.mcp_config_path.read_text(encoding="utf-8-sig"))
                mcp_map = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
                if isinstance(mcp_map, dict):
                    changed = False
                    for sid, cfg in mcp_map.items():
                        if sid in self._BUILTIN_IDS:
                            continue
                        if sid not in self._servers and isinstance(cfg, dict):
                            try:
                                self._servers[sid] = _normalize_mcp_server_entry(sid, {**cfg, "enabled": True})
                                changed = True
                            except Exception:
                                pass
                    if changed:
                        self._save_registry()
            except Exception as exc:
                logger.warning("读取工作区 mcp_config.json 失败: %s", exc)

    def _save_registry(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"servers": self._servers}
        tmp = self.registry_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.registry_path)

    def _all_managed_servers(self) -> dict[str, dict[str, Any]]:
        merged = dict(self._builtin_servers)
        merged.update(self._servers)
        return merged

    def _sync_settings_allow(self) -> None:
        """Auto-whitelist enabled MCP servers (`mcp(<id>/*)`) in bot-local settings file for headless stream-json."""
        sf = self.settings_file
        if sf is None or not sf.is_file():
            return
        try:
            data = json.loads(sf.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                return
            perms = data.get("permissions")
            if not isinstance(perms, dict):
                perms = {}
                data["permissions"] = perms
            allow_list = [str(x) for x in (perms.get("allow") or []) if str(x).strip()]
            managed = self._all_managed_servers()
            # Remove stale managed `mcp(<sid>/*)` entries for known/deleted servers, then re-add enabled ones
            enabled_rules = {
                f"mcp({sid}/*)"
                for sid, entry in managed.items()
                if entry.get("enabled", True)
            }
            known_rules = {f"mcp({sid}/*)" for sid in set(managed.keys()) | set(self._BUILTIN_IDS)}
            new_allow: list[str] = []
            seen: set[str] = set()
            for rule in allow_list:
                if rule in known_rules and rule not in enabled_rules:
                    continue
                if rule not in seen:
                    seen.add(rule)
                    new_allow.append(rule)
            for rule in sorted(enabled_rules):
                if rule not in seen:
                    seen.add(rule)
                    new_allow.append(rule)
            if new_allow != allow_list:
                perms["allow"] = new_allow
                tmp = sf.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, sf)
        except Exception as exc:
            logger.warning("同步 MCP 白名单至 %s 失败: %s", sf, exc)

    def _sync_subagent_mcp_config(self) -> None:
        """Sync enabled external (non-builtin) MCP servers to the isolated subagent workspace."""
        if self.subagent_workspace_dir is None:
            return
        sub_agents_dir = self.subagent_workspace_dir / ".agents"
        sub_agents_dir.mkdir(parents=True, exist_ok=True)
        sub_map: dict[str, Any] = {}
        for sid in sorted(self._servers.keys()):
            if sid in self._BUILTIN_IDS:
                continue
            entry = self._servers[sid]
            if entry.get("enabled", True):
                sub_map[sid] = _to_antigravity_mcp_entry(entry)
        payload = {"mcpServers": sub_map}
        target = sub_agents_dir / "mcp_config.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, target)

    def _sync_workspace_mcp_config(self) -> None:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        active_map: dict[str, Any] = {}
        managed = self._all_managed_servers()
        for sid in sorted(managed.keys()):
            entry = managed[sid]
            if entry.get("enabled", True):
                active_map[sid] = _to_antigravity_mcp_entry(entry)
        payload = {"mcpServers": active_map}
        tmp = self.mcp_config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.mcp_config_path)
        self._sync_subagent_mcp_config()
        self._sync_settings_allow()

    def _read_global_servers(self) -> list[dict[str, Any]]:
        gp = self._global_mcp_path()
        if not gp.is_file():
            return []
        try:
            raw = json.loads(gp.read_text(encoding="utf-8-sig"))
            mcp_map = raw.get("mcpServers", {}) if isinstance(raw, dict) else {}
            out: list[dict[str, Any]] = []
            if isinstance(mcp_map, dict):
                for sid in sorted(mcp_map.keys()):
                    cfg = mcp_map[sid]
                    if not isinstance(cfg, dict):
                        continue
                    try:
                        entry = _normalize_mcp_server_entry(sid, {**cfg, "enabled": True})
                        entry["scope"] = "global"
                        entry["readonly"] = True
                        out.append(entry)
                    except Exception:
                        pass
            return out
        except Exception:
            return []

    def list_servers(self) -> list[dict[str, Any]]:
        self._load()
        out: list[dict[str, Any]] = []
        for sid in sorted(self._builtin_servers.keys()):
            bitem = dict(self._builtin_servers[sid])
            bitem["scope"] = "builtin"
            bitem["readonly"] = True
            out.append(bitem)
        for sid in sorted(self._servers.keys()):
            item = dict(self._servers[sid])
            item["scope"] = "workspace"
            item["readonly"] = False
            out.append(item)
        out.extend(self._read_global_servers())
        return out

    def get_usage_guides(self) -> list[dict[str, Any]]:
        """Return enabled workspace/builtin MCP servers that define usage_prompt, async_subagent, or per-tool subagent rules."""
        self._load()
        guides: list[dict[str, Any]] = []
        managed = self._all_managed_servers()
        for sid in sorted(managed.keys()):
            entry = managed[sid]
            if not entry.get("enabled", True):
                continue
            usage_prompt = str(entry.get("usage_prompt") or "").strip()
            async_subagent = bool(entry.get("async_subagent", False))
            tool_subagent = dict(entry.get("tool_subagent") or {})
            discovered_tools = list(entry.get("discovered_tools") or [])
            if usage_prompt or async_subagent or tool_subagent:
                guides.append(
                    {
                        "id": sid,
                        "description": str(entry.get("description") or "").strip(),
                        "usage_prompt": usage_prompt,
                        "async_subagent": async_subagent,
                        "tool_subagent": tool_subagent,
                        "discovered_tools": discovered_tools,
                    }
                )
        return guides

    def upsert_server(self, server_id: str, config: dict[str, Any]) -> dict[str, Any]:
        entry = _normalize_mcp_server_entry(server_id, config)
        self._servers[entry["id"]] = entry
        self._save_registry()
        self._sync_workspace_mcp_config()
        return {**entry, "scope": "workspace", "readonly": False}

    def set_enabled(self, server_id: str, enabled: bool) -> dict[str, Any]:
        sid = _validate_id(server_id, "MCP 服务器 ID")
        if sid not in self._servers:
            raise FileNotFoundError(f"未找到工作区 MCP 服务器: {sid}")
        self._servers[sid]["enabled"] = bool(enabled)
        self._save_registry()
        self._sync_workspace_mcp_config()
        return {**self._servers[sid], "scope": "workspace", "readonly": False}

    def delete_server(self, server_id: str) -> bool:
        sid = _validate_id(server_id, "MCP 服务器 ID")
        if sid not in self._servers:
            return False
        del self._servers[sid]
        self._save_registry()
        self._sync_workspace_mcp_config()
        # Also remove deleted server's `mcp(<sid>/*)` from bot-local settings allow list
        if self.settings_file is not None and self.settings_file.is_file():
            try:
                data = json.loads(self.settings_file.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and isinstance(data.get("permissions"), dict):
                    allow = data["permissions"].get("allow")
                    target_rule = f"mcp({sid}/*)"
                    if isinstance(allow, list) and target_rule in allow:
                        data["permissions"]["allow"] = [x for x in allow if x != target_rule]
                        tmp = self.settings_file.with_suffix(".tmp")
                        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                        os.replace(tmp, self.settings_file)
            except Exception:
                pass
        return True

    def import_json(self, raw_payload: dict[str, Any], *, overwrite: bool = True) -> list[dict[str, Any]]:
        if not isinstance(raw_payload, dict):
            raise ValueError("导入内容必须是 JSON 对象")
        mcp_map = raw_payload.get("mcpServers") if isinstance(raw_payload.get("mcpServers"), dict) else raw_payload
        if not isinstance(mcp_map, dict) or not mcp_map:
            raise ValueError("未找到有效的 mcpServers 定义")
        imported: list[dict[str, Any]] = []
        for sid, cfg in mcp_map.items():
            if not isinstance(cfg, dict):
                continue
            if sid in self._servers and not overwrite:
                continue
            entry = _normalize_mcp_server_entry(str(sid), cfg)
            self._servers[entry["id"]] = entry
            imported.append({**entry, "scope": "workspace", "readonly": False})
        if not imported:
            raise ValueError("没有可导入的 MCP 服务器条目")
        self._save_registry()
        self._sync_workspace_mcp_config()
        return imported

    async def test_server(
        self,
        server_id: str = "",
        *,
        config_override: dict[str, Any] | None = None,
        proxy: str = "",
    ) -> dict[str, Any]:
        if config_override is not None:
            entry = _normalize_mcp_server_entry(server_id or "test_mcp", config_override)
        else:
            all_items = {s["id"]: s for s in self.list_servers()}
            if server_id not in all_items:
                return {"ok": False, "error": f"未找到 MCP 服务器: {server_id}"}
            entry = all_items[server_id]

        t0 = time.monotonic()
        logger.info("[MCP] 开始探测 MCP 服务器: %s (transport=%s)", entry["id"], entry["transport"])
        init_req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agychat-bot-webui", "version": "1.0.0"},
            },
        }
        inited_notif = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        tools_req = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }

        if entry["transport"] == "http":
            try:
                import aiohttp
            except ImportError:
                return {"ok": False, "error": "缺少 aiohttp 依赖"}
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                **(entry.get("headers") or {}),
            }
            timeout = aiohttp.ClientTimeout(total=15.0)
            use_proxy = proxy.strip() or None
            parsed_host = (urllib.parse.urlparse(entry["url"]).hostname or "").lower()
            if parsed_host in ("127.0.0.1", "localhost", "::1"):
                use_proxy = None
            try:
                async with aiohttp.ClientSession(timeout=timeout, trust_env=False) as session:
                    async with session.post(entry["url"], json=init_req, headers=headers, proxy=use_proxy) as resp:
                        body_text = (await resp.text(errors="replace"))[:4000]
                        ms = round((time.monotonic() - t0) * 1000, 1)
                        parsed = _parse_mcp_jsonrpc_body(body_text)
                        server_info: dict[str, Any] = {}
                        if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
                            server_info = parsed["result"].get("serverInfo") or {}
                        session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id") or ""
                        ok = 200 <= resp.status < 400
                        status_code = resp.status

                    tools_list: list[dict[str, Any]] = []
                    if ok:
                        req_headers = dict(headers)
                        if session_id:
                            req_headers["Mcp-Session-Id"] = session_id
                        try:
                            async with session.post(
                                entry["url"], json=inited_notif, headers=req_headers, proxy=use_proxy
                            ) as _:
                                pass
                        except Exception:
                            pass
                        try:
                            async with session.post(
                                entry["url"], json=tools_req, headers=req_headers, proxy=use_proxy
                            ) as tresp:
                                if 200 <= tresp.status < 400:
                                    tbody = (await tresp.text(errors="replace"))[:32000]
                                    tparsed = _parse_mcp_jsonrpc_body(tbody)
                                    if isinstance(tparsed, dict) and isinstance(tparsed.get("result"), dict):
                                        tools_list = _summarize_mcp_tools(tparsed["result"].get("tools"))
                        except Exception:
                            pass

                    if ok:
                        logger.info(
                            "[MCP] 探测 HTTP 服务器 %s 成功 (HTTP %s, %.0fms, 发现 %d 个工具)",
                            entry["id"],
                            status_code,
                            ms,
                            len(tools_list),
                        )
                    else:
                        logger.warning("[MCP] 探测 HTTP 服务器 %s 失败 (HTTP %s, %.0fms)", entry["id"], status_code, ms)

                    return {
                        "ok": ok,
                        "transport": "http",
                        "status_code": status_code,
                        "latency_ms": ms,
                        "server_info": server_info,
                        "tools": tools_list,
                        "message": (
                            f"HTTP {status_code} 连接成功 ({ms}ms)"
                            + (f" · {server_info.get('name', '')} {server_info.get('version', '')}".rstrip() if server_info else "")
                            + (f" · 发现 {len(tools_list)} 个工具" if tools_list else "")
                        )
                        if ok
                        else f"HTTP {status_code}: {body_text[:200]}",
                    }
            except Exception as exc:
                return {"ok": False, "transport": "http", "error": f"连接失败: {exc}"}

        # Stdio transport check
        cmd = entry["command"]
        resolved_cmd = shutil.which(cmd)
        if not resolved_cmd and Path(cmd).is_file():
            resolved_cmd = str(Path(cmd).resolve())
        if not resolved_cmd:
            return {"ok": False, "transport": "stdio", "error": f"找不到可执行文件: {cmd}（请确认已安装并在 PATH 中）"}

        env = os.environ.copy()
        env.update(entry.get("env") or {})
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                resolved_cmd,
                *(entry.get("args") or []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace_dir),
                env=env,
            )
            assert proc.stdin is not None and proc.stdout is not None

            async def _read_stdio_message(wait_sec: float) -> dict[str, Any] | None:
                deadline = time.monotonic() + max(0.5, wait_sec)
                for _ in range(16):
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise asyncio.TimeoutError()
                    raw_line = await asyncio.wait_for(proc.stdout.readline(), timeout=rem)
                    if not raw_line:
                        return None
                    line_text = raw_line.decode("utf-8", errors="replace").strip()
                    if not line_text:
                        continue
                    if line_text.lower().startswith("content-length:"):
                        try:
                            length = int(line_text.split(":", 1)[1].strip())
                        except ValueError:
                            return None
                        if length <= 0 or length > _MAX_MCP_STDIO_CONTENT_LENGTH:
                            raise ValueError(f"MCP Content-Length 超限或无效: {length}")
                        # Drain remaining headers until blank line
                        for _hdr_i in range(16):
                            hdr_line = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
                            if not hdr_line or hdr_line in (b"\r\n", b"\n"):
                                break
                        body_bytes = await asyncio.wait_for(proc.stdout.readexactly(length), timeout=max(2.0, min(8.0, rem)))
                        parsed_msg = _parse_mcp_jsonrpc_body(body_bytes.decode("utf-8", errors="replace").strip())
                        if parsed_msg is not None:
                            return parsed_msg
                        continue
                    parsed_msg = _parse_mcp_jsonrpc_body(line_text)
                    if parsed_msg is not None:
                        return parsed_msg
                return None

            payload = (json.dumps(init_req) + "\n").encode("utf-8")
            proc.stdin.write(payload)
            await proc.stdin.drain()

            parsed = await _read_stdio_message(8.0)
            ms = round((time.monotonic() - t0) * 1000, 1)
            server_info = {}
            if isinstance(parsed, dict) and isinstance(parsed.get("result"), dict):
                server_info = parsed["result"].get("serverInfo") or {}

            if proc.returncode is not None and proc.returncode != 0 and not server_info:
                err_out = ""
                if proc.stderr is not None:
                    err_out = (await proc.stderr.read(600)).decode("utf-8", errors="replace").strip()
                return {
                    "ok": False,
                    "transport": "stdio",
                    "resolved_command": resolved_cmd,
                    "error": f"进程异常退出 (rc={proc.returncode}): {err_out or '无错误输出'}",
                }

            tools_list = []
            if server_info and proc.returncode is None:
                try:
                    proc.stdin.write((json.dumps(inited_notif) + "\n").encode("utf-8"))
                    proc.stdin.write((json.dumps(tools_req) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                    for _ in range(4):
                        tmsg = await _read_stdio_message(4.0)
                        if isinstance(tmsg, dict) and tmsg.get("id") == 2 and isinstance(tmsg.get("result"), dict):
                            tools_list = _summarize_mcp_tools(tmsg["result"].get("tools"))
                            break
                except Exception:
                    pass

            logger.info(
                "[MCP] 探测 Stdio 服务器 %s 成功 (%.0fms, 发现 %d 个工具)",
                entry["id"],
                ms,
                len(tools_list),
            )
            return {
                "ok": True,
                "transport": "stdio",
                "resolved_command": resolved_cmd,
                "latency_ms": ms,
                "server_info": server_info,
                "tools": tools_list,
                "message": (
                    f"Stdio 握手成功 ({ms}ms)"
                    + (f" · {server_info.get('name', '')} {server_info.get('version', '')}".rstrip() if server_info else f" · {Path(resolved_cmd).name}")
                    + (f" · 发现 {len(tools_list)} 个工具" if tools_list else "")
                ),
            }
        except asyncio.TimeoutError:
            # Process stayed alive cleanly even if slow to respond on first npx/uvx cold start
            ms = round((time.monotonic() - t0) * 1000, 1)
            return {
                "ok": True,
                "transport": "stdio",
                "resolved_command": resolved_cmd,
                "latency_ms": ms,
                "message": f"可执行文件有效且进程已启动（首次启动初始化超过 8s，已停止探测）: {resolved_cmd}",
            }
        except Exception as exc:
            return {"ok": False, "transport": "stdio", "resolved_command": resolved_cmd, "error": f"启动失败: {exc}"}
        finally:
            if proc is not None and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except BaseException:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except BaseException:
                        pass
