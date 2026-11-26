from __future__ import annotations

import asyncio
import codecs
import contextlib
import ctypes
import ctypes.wintypes as wintypes
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..runner.agy_stream import resolve_agy_command

logger = logging.getLogger(__name__)

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|[@-Z\\-_])"
)
_OSC8_HYPERLINK_RE = re.compile(
    r"\x1b\]8;[^;\x07\x1b]*;(https?://[^\x07\x1b\s]+)(?:\x07|\x1b\\)",
    re.IGNORECASE,
)
_AUTH_URL_RE = re.compile(
    r"https?://(?:accounts\.google\.com|antigravity\.google|oauth2?\.googleapis\.com|console\.cloud\.google\.com)[^\s\"'<>)\]]+",
    re.IGNORECASE,
)
_URL_CONTINUATION_LINE_RE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")

# Allowed non-interactive quick diagnostic commands for the WebUI Agy CLI panel
QUICK_CLI_ACTIONS: dict[str, tuple[str, list[str], float]] = {
    "version": ("查看 CLI 版本", ["--version"], 15.0),
    "models": ("列出可用模型 (agy models)", ["models"], 25.0),
    "agents": ("列出可用 Agent (agy agents)", ["agents"], 20.0),
    "mcp_list": ("查看 CLI MCP 列表 (agy mcp list)", ["mcp", "list"], 20.0),
    "plugin_list": ("查看 CLI 插件列表 (agy plugin list)", ["plugin", "list"], 20.0),
    "remote_status": ("查看 Remote Control 状态", ["remote-control", "status"], 15.0),
    "update": ("检查并更新 CLI (agy update)", ["update"], 90.0),
}


def strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


_RESET_PART_RE = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _parse_reset_duration_seconds(text: str) -> int:
    """Parse durations like '156h 33m', '3h 33m', '2d 4h 10m', '45m 12s' into total seconds."""
    if not text:
        return 0
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0
    for val_s, unit in _RESET_PART_RE.findall(text):
        total += int(val_s) * mult.get(unit.lower(), 0)
    return total


def _short_quota_group_label(name: str) -> str:
    up = (name or "").strip().upper()
    if "GEMINI" in up:
        return "Gemini"
    if "CLAUDE" in up and "GPT" in up:
        return "Claude & GPT"
    if "CLAUDE" in up:
        return "Claude"
    if "GPT" in up:
        return "GPT"
    cleaned = re.sub(r"\s+MODELS$", "", (name or "").strip(), flags=re.IGNORECASE).strip()
    return cleaned.title() if cleaned else "Models"


def parse_agy_quota_output(raw_text: str) -> dict[str, Any]:
    """Parse interactive `agy` CLI `/usage` (Models & Quota) screen output into structured quota data."""
    clean = strip_ansi(raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    low_all = clean.lower()

    # Slice from the latest Models & Quota panel redraw to avoid duplicate spinner frames
    # and ignore slash-command autocomplete hints (e.g. "/login Sign in to ...")
    anchor = clean.rfind("Models & Quota")
    if anchor >= 0:
        section = clean[anchor:]
    else:
        acc_anchor = clean.rfind("Account:")
        section = clean[acc_anchor:] if acc_anchor >= 0 else clean

    account = ""
    groups: list[dict[str, Any]] = []
    cur_group: dict[str, Any] | None = None
    cur_bucket: str | None = None  # "weekly" | "five_hour"

    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("│") or line.startswith("↑/↓") or line.startswith("(1–") or line.startswith("(1-"):
            continue

        m_acc = re.match(r"^Account:\s*(.+)$", line, re.IGNORECASE)
        if m_acc:
            account = m_acc.group(1).strip()
            continue

        # Detect model group heading (e.g. "GEMINI MODELS", "CLAUDE AND GPT MODELS")
        if (
            re.fullmatch(r"[A-Z0-9][A-Z0-9 &/\-]{2,48}", line)
            and ("MODEL" in line or "GEMINI" in line or "CLAUDE" in line or "GPT" in line)
        ):
            cur_group = {
                "name": line,
                "label": _short_quota_group_label(line),
                "models": "",
                "weekly": None,
                "five_hour": None,
            }
            groups.append(cur_group)
            cur_bucket = None
            continue

        low_line = line.lower()
        if cur_group is not None and low_line.startswith("models within this group:"):
            cur_group["models"] = line.split(":", 1)[1].strip()
            continue

        if "weekly limit" in low_line:
            if cur_group is None:
                cur_group = {"name": "MODELS", "label": "模型额度", "models": "", "weekly": None, "five_hour": None}
                groups.append(cur_group)
            cur_bucket = "weekly"
            cur_group["weekly"] = {
                "remaining_pct": None,
                "status_text": "",
                "reset_seconds": 0,
                "available": True,
            }
            continue

        if "five hour" in low_line or "5-hour" in low_line or "5 hour" in low_line:
            if cur_group is None:
                cur_group = {"name": "MODELS", "label": "模型额度", "models": "", "weekly": None, "five_hour": None}
                groups.append(cur_group)
            cur_bucket = "five_hour"
            cur_group["five_hour"] = {
                "remaining_pct": None,
                "status_text": "",
                "reset_seconds": 0,
                "available": True,
            }
            continue

        if cur_group is not None and cur_bucket is not None:
            bucket_obj = cur_group.get(cur_bucket)
            if isinstance(bucket_obj, dict):
                m_pct = _PCT_RE.search(line)
                if m_pct and bucket_obj["remaining_pct"] is None:
                    bucket_obj["remaining_pct"] = round(float(m_pct.group(1)), 2)
                    continue
                m_ref = re.search(r"(?:Refreshes|Resets)\s+in\s+(.+)$", line, re.IGNORECASE)
                if m_ref:
                    dur_txt = m_ref.group(1).strip()
                    bucket_obj["status_text"] = f"Refreshes in {dur_txt}"
                    bucket_obj["reset_seconds"] = _parse_reset_duration_seconds(dur_txt)
                    continue
                if "quota available" in low_line:
                    bucket_obj["status_text"] = "Quota available"
                    bucket_obj["reset_seconds"] = 0
                    bucket_obj["available"] = True
                    continue
                if "exhausted" in low_line or "reached" in low_line or "unavailable" in low_line:
                    bucket_obj["status_text"] = line
                    bucket_obj["available"] = False
                    continue

    # Filter out incomplete groups without any percentage
    valid_groups = [
        g for g in groups
        if (isinstance(g.get("weekly"), dict) and g["weekly"].get("remaining_pct") is not None)
        or (isinstance(g.get("five_hour"), dict) and g["five_hour"].get("remaining_pct") is not None)
    ]

    if not valid_groups and not account:
        if "currently not signed in" in low_all or "please sign in" in low_all or "select login method" in low_all:
            return {
                "ok": False,
                "signed_in": False,
                "account": "",
                "groups": [],
                "error": "Agy CLI 尚未登录账号，请前往「Agy CLI」终端完成登录",
            }
        return {
            "ok": False,
            "signed_in": False,
            "account": "",
            "groups": [],
            "error": "未能从 agy /usage 解析到额度信息（请确认 CLI 已正常登录且网络代理畅通）",
        }

    return {
        "ok": bool(valid_groups),
        "signed_in": bool(account or valid_groups),
        "account": account,
        "groups": valid_groups,
        "error": "" if valid_groups else "未读取到模型额度分组",
    }


def _is_complete_auth_url(url: str) -> bool:
    low = url.lower()
    if len(url) < 24:
        return False
    if "accounts.google.com/o/oauth2/" in low:
        return (
            "client_id=" in low
            and "redirect_uri=" in low
            and "response_type=" in low
        )
    return True


def extract_auth_urls(text: str) -> list[str]:
    """Extract complete OAuth/login URLs from raw terminal output.

    Priority 1: Extract terminated OSC-8 hyperlinks before stripping ANSI
    (e.g. ``\\x1b]8;id=...;https://accounts.google.com/...\\x07``), which carry
    the pristine, unwrapped full URL.
    Priority 2: Rejoin padded/soft-wrapped plain-text URLs after stripping ANSI
    and filter out truncated Google OAuth prefixes.
    """
    if not text or "http" not in text.lower():
        return []

    found: list[str] = []
    seen: set[str] = set()

    # 1. OSC-8 terminated hyperlinks (unwrapped, immune to column wrapping)
    for m in _OSC8_HYPERLINK_RE.finditer(text):
        candidate = m.group(1).strip().rstrip(".,;:!?")
        if _AUTH_URL_RE.fullmatch(candidate) and _is_complete_auth_url(candidate):
            if candidate not in seen:
                seen.add(candidate)
                found.append(candidate)

    # 2. Plain-text fallback with support for padded terminal soft-wraps
    clean = strip_ansi(text)
    lines = [ln.strip() for ln in clean.splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _AUTH_URL_RE.search(line)
        if m:
            url = m.group(0)
            # Only append continuation lines if the URL reached the end of this visual line
            if m.end() == len(line):
                j = i + 1
                while j < len(lines):
                    nxt = lines[j]
                    if not nxt or not _URL_CONTINUATION_LINE_RE.match(nxt):
                        break
                    # Avoid gluing a separate URL onto the current one
                    if nxt.lower().startswith(("http://", "https://")):
                        break
                    url += nxt
                    j += 1
                i = j
            else:
                i += 1
            url = url.rstrip(".,;:!?")
            if _is_complete_auth_url(url) and url not in seen:
                # Skip if it is merely a shorter prefix of an already extracted OSC-8 URL
                if not any(existing.startswith(url) for existing in found):
                    seen.add(url)
                    found.append(url)
        else:
            i += 1

    return found


def get_global_agy_settings_path() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli" / "settings.json"


# ============================================================================
# Windows ConPTY (kernel32 CreatePseudoConsole) pure-stdlib implementation
# ============================================================================
if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
    _STARTF_USESTDHANDLES = 0x00000100
    _STILL_ACTIVE = 259

    class _COORD(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.c_void_p),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class _STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [
            ("StartupInfo", _STARTUPINFOW),
            ("lpAttributeList", ctypes.c_void_p),
        ]

    class _PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    _kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    _kernel32.CreatePipe.restype = wintypes.BOOL

    _kernel32.CreatePseudoConsole.argtypes = [
        _COORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _kernel32.CreatePseudoConsole.restype = ctypes.c_long  # HRESULT

    _kernel32.ResizePseudoConsole.argtypes = [ctypes.c_void_p, _COORD]
    _kernel32.ResizePseudoConsole.restype = ctypes.c_long

    _kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
    _kernel32.ClosePseudoConsole.restype = None

    _kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    _kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

    _kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

    _kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    _kernel32.DeleteProcThreadAttributeList.restype = None

    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL

    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.ReadFile.restype = wintypes.BOOL

    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL


def _build_windows_env_block(env: dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    raw = "\0".join(f"{k}={v}" for k, v in items) + "\0\0"
    return ctypes.create_unicode_buffer(raw)


class AgyPtySession:
    """Cross-platform interactive pseudo-terminal hosting an `agy` CLI process."""

    def __init__(
        self,
        cmd: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        cols: int = 120,
        rows: int = 32,
        loop: asyncio.AbstractEventLoop | None = None,
        on_output: Callable[[str], None] | None = None,
        on_exit: Callable[[int | None], None] | None = None,
    ) -> None:
        self.session_id = uuid.uuid4().hex[:10]
        self.cmd = list(cmd)
        self.cwd = cwd
        self.env = dict(env)
        self.cols = max(40, min(300, int(cols)))
        self.rows = max(12, min(120, int(rows)))
        self._loop = loop or asyncio.get_running_loop()
        self._on_output = on_output
        self._on_exit = on_exit

        self.pid: int = 0
        self.exit_code: int | None = None
        self.started_at: float = 0.0
        self._closed = False
        self._lock = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._reader_thread: threading.Thread | None = None
        self._waiter_thread: threading.Thread | None = None

        # Windows ConPTY handles
        self._hpc: ctypes.c_void_p | None = None
        self._h_in_write: wintypes.HANDLE | None = None
        self._h_out_read: wintypes.HANDLE | None = None
        self._h_process: wintypes.HANDLE | None = None
        self._h_thread: wintypes.HANDLE | None = None

        # POSIX PTY handles
        self._posix_master_fd: int | None = None
        self._posix_proc: subprocess.Popen[bytes] | None = None

    @property
    def is_alive(self) -> bool:
        if self._closed or self.exit_code is not None:
            return False
        if os.name == "nt":
            if not self._h_process:
                return False
            code = wintypes.DWORD(0)
            if _kernel32.GetExitCodeProcess(self._h_process, ctypes.byref(code)):
                return code.value == _STILL_ACTIVE
            return False
        return self._posix_proc is not None and self._posix_proc.poll() is None

    def start(self) -> None:
        self.started_at = time.time()
        if os.name == "nt":
            self._start_windows_conpty()
        else:
            self._start_posix_pty()
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            name=f"agy-pty-read-{self.session_id}",
            daemon=True,
        )
        self._reader_thread.start()
        if os.name == "nt":
            self._waiter_thread = threading.Thread(
                target=self._windows_wait_loop,
                name=f"agy-pty-wait-{self.session_id}",
                daemon=True,
            )
            self._waiter_thread.start()

    def _start_windows_conpty(self) -> None:
        if not hasattr(_kernel32, "CreatePseudoConsole"):
            raise RuntimeError("当前 Windows 版本不支持 ConPTY (CreatePseudoConsole)")

        h_in_read = wintypes.HANDLE()
        h_in_write = wintypes.HANDLE()
        h_out_read = wintypes.HANDLE()
        h_out_write = wintypes.HANDLE()

        if not _kernel32.CreatePipe(ctypes.byref(h_in_read), ctypes.byref(h_in_write), None, 0):
            raise OSError(f"CreatePipe(in) failed: {ctypes.get_last_error()}")
        if not _kernel32.CreatePipe(ctypes.byref(h_out_read), ctypes.byref(h_out_write), None, 0):
            _kernel32.CloseHandle(h_in_read)
            _kernel32.CloseHandle(h_in_write)
            raise OSError(f"CreatePipe(out) failed: {ctypes.get_last_error()}")

        hpc = ctypes.c_void_p()
        coord = _COORD(self.cols, self.rows)
        hr = _kernel32.CreatePseudoConsole(coord, h_in_read, h_out_write, 0, ctypes.byref(hpc))
        if hr != 0:
            for h in (h_in_read, h_in_write, h_out_read, h_out_write):
                _kernel32.CloseHandle(h)
            raise OSError(f"CreatePseudoConsole failed: HRESULT=0x{hr & 0xFFFFFFFF:08X}")

        # ConPTY has duplicated/attached the child ends; close our copies after CreateProcessW
        attr_size = ctypes.c_size_t(0)
        _kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(attr_size))
        attr_buf = ctypes.create_string_buffer(attr_size.value)
        attr_list = ctypes.cast(attr_buf, ctypes.c_void_p)
        if not _kernel32.InitializeProcThreadAttributeList(attr_list, 1, 0, ctypes.byref(attr_size)):
            _kernel32.ClosePseudoConsole(hpc)
            for h in (h_in_read, h_in_write, h_out_read, h_out_write):
                _kernel32.CloseHandle(h)
            raise OSError(f"InitializeProcThreadAttributeList failed: {ctypes.get_last_error()}")

        try:
            if not _kernel32.UpdateProcThreadAttribute(
                attr_list,
                0,
                _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
                hpc,
                ctypes.sizeof(ctypes.c_void_p),
                None,
                None,
            ):
                raise OSError(f"UpdateProcThreadAttribute failed: {ctypes.get_last_error()}")

            si_ex = _STARTUPINFOEXW()
            si_ex.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
            # Clear inherited std handles when parent runs under a redirected service/task so output goes to ConPTY
            si_ex.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            si_ex.lpAttributeList = attr_list
            pi = _PROCESS_INFORMATION()

            cmdline = subprocess.list2cmdline(self.cmd)
            cmdline_buf = ctypes.create_unicode_buffer(cmdline)
            env_buf = _build_windows_env_block(self.env)

            ok = _kernel32.CreateProcessW(
                None,
                cmdline_buf,
                None,
                None,
                False,
                _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
                ctypes.byref(env_buf),
                str(self.cwd),
                ctypes.byref(si_ex),
                ctypes.byref(pi),
            )
            if not ok:
                err = ctypes.get_last_error()
                raise OSError(f"启动 agy CLI 失败 (CreateProcessW err={err}): {cmdline}")

            self._hpc = hpc
            self._h_in_write = h_in_write
            self._h_out_read = h_out_read
            self._h_process = pi.hProcess
            self._h_thread = pi.hThread
            self.pid = int(pi.dwProcessId)
        except BaseException:
            _kernel32.ClosePseudoConsole(hpc)
            _kernel32.CloseHandle(h_in_write)
            _kernel32.CloseHandle(h_out_read)
            raise
        finally:
            _kernel32.DeleteProcThreadAttributeList(attr_list)
            # Always release parent's copies of child-facing pipe ends so EOF propagates cleanly
            _kernel32.CloseHandle(h_in_read)
            _kernel32.CloseHandle(h_out_write)

    def _start_posix_pty(self) -> None:
        import fcntl
        import pty
        import struct
        import termios

        master_fd, slave_fd = pty.openpty()
        winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        with contextlib.suppress(OSError):
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        try:
            proc = subprocess.Popen(
                self.cmd,
                cwd=str(self.cwd),
                env=self.env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
            )
        except BaseException:
            os.close(master_fd)
            os.close(slave_fd)
            raise
        os.close(slave_fd)
        self._posix_master_fd = master_fd
        self._posix_proc = proc
        self.pid = int(proc.pid)

    def write(self, data: str | bytes) -> None:
        raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        if not raw:
            return
        with self._lock:
            if self._closed:
                return
            if os.name == "nt":
                if not self._h_in_write:
                    return
                written = wintypes.DWORD(0)
                buf = ctypes.create_string_buffer(raw, len(raw))
                _kernel32.WriteFile(self._h_in_write, buf, len(raw), ctypes.byref(written), None)
            else:
                if self._posix_master_fd is not None:
                    with contextlib.suppress(OSError):
                        os.write(self._posix_master_fd, raw)

    def resize(self, cols: int, rows: int) -> None:
        c = max(40, min(300, int(cols)))
        r = max(12, min(120, int(rows)))
        with self._lock:
            if self._closed:
                return
            self.cols = c
            self.rows = r
            if os.name == "nt":
                if self._hpc:
                    _kernel32.ResizePseudoConsole(self._hpc, _COORD(c, r))
            else:
                if self._posix_master_fd is not None:
                    import fcntl
                    import struct
                    import termios

                    winsize = struct.pack("HHHH", r, c, 0, 0)
                    with contextlib.suppress(OSError):
                        fcntl.ioctl(self._posix_master_fd, termios.TIOCSWINSZ, winsize)

    def _emit_output(self, chunk: bytes) -> None:
        text = self._decoder.decode(chunk, final=False)
        if text and self._on_output is not None:
            with contextlib.suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._on_output, text)

    def _windows_wait_loop(self) -> None:
        """Wait for the Windows child process to exit, then close the PseudoConsole to unblock ReadFile."""
        h_proc = self._h_process
        if not h_proc:
            return
        try:
            _kernel32.WaitForSingleObject(h_proc, 0xFFFFFFFF)
            code = wintypes.DWORD(0)
            if _kernel32.GetExitCodeProcess(h_proc, ctypes.byref(code)):
                self.exit_code = None if code.value == _STILL_ACTIVE else int(code.value)
        except Exception as exc:
            logger.debug("ConPTY waiter 结束 (%s): %s", self.session_id, exc)
        finally:
            with self._lock:
                hpc = self._hpc
                self._hpc = None
            if hpc:
                with contextlib.suppress(Exception):
                    _kernel32.ClosePseudoConsole(hpc)

    def _read_loop(self) -> None:
        try:
            if os.name == "nt":
                buf = ctypes.create_string_buffer(8192)
                n_read = wintypes.DWORD(0)
                while not self._closed and self._h_out_read:
                    ok = _kernel32.ReadFile(self._h_out_read, buf, 8192, ctypes.byref(n_read), None)
                    if not ok or n_read.value == 0:
                        break
                    self._emit_output(buf.raw[: n_read.value])
                # Collect exit code if waiter hasn't set it yet
                if self.exit_code is None and self._h_process:
                    _kernel32.WaitForSingleObject(self._h_process, 1500)
                    code = wintypes.DWORD(0)
                    if _kernel32.GetExitCodeProcess(self._h_process, ctypes.byref(code)):
                        self.exit_code = None if code.value == _STILL_ACTIVE else int(code.value)
            else:
                while not self._closed and self._posix_master_fd is not None:
                    try:
                        chunk = os.read(self._posix_master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self._emit_output(chunk)
                if self._posix_proc is not None:
                    with contextlib.suppress(Exception):
                        self.exit_code = self._posix_proc.wait(timeout=1.5)
        except Exception as exc:
            logger.debug("PTY 读取循环结束 (%s): %s", self.session_id, exc)
        finally:
            tail = self._decoder.decode(b"", final=True)
            if tail and self._on_output is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._on_output, tail)
            if self._on_exit is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._on_exit, self.exit_code)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pid = self.pid
            if os.name == "nt":
                hpc = self._hpc
                h_in_w = self._h_in_write
                h_out_r = self._h_out_read
                h_proc = self._h_process
                h_thr = self._h_thread
                self._hpc = None
                self._h_in_write = None
                self._h_out_read = None
                self._h_process = None
                self._h_thread = None
            else:
                mfd = self._posix_master_fd
                proc = self._posix_proc
                self._posix_master_fd = None
                self._posix_proc = None

        if os.name == "nt":
            if h_proc:
                code = wintypes.DWORD(0)
                if _kernel32.GetExitCodeProcess(h_proc, ctypes.byref(code)) and code.value == _STILL_ACTIVE:
                    if pid > 0:
                        with contextlib.suppress(Exception):
                            subprocess.run(
                                ["taskkill", "/T", "/F", "/PID", str(pid)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=4,
                            )
                    with contextlib.suppress(Exception):
                        _kernel32.TerminateProcess(h_proc, 1)
            if hpc:
                with contextlib.suppress(Exception):
                    _kernel32.ClosePseudoConsole(hpc)
            for h in (h_in_w, h_out_r, h_thr, h_proc):
                if h:
                    with contextlib.suppress(Exception):
                        _kernel32.CloseHandle(h)
        else:
            if proc is not None and proc.poll() is None:
                with contextlib.suppress(Exception):
                    proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    with contextlib.suppress(Exception):
                        proc.kill()
            if mfd is not None:
                with contextlib.suppress(OSError):
                    os.close(mfd)


class AgyCliManager:
    """Manages WebUI interactive `agy` CLI PTY sessions, global CLI settings, and quick diagnostics."""

    def __init__(self, bot_app: Any) -> None:
        self._bot = bot_app
        self._active_sessions: dict[str, AgyPtySession] = {}
        self._version_cache: tuple[float, str] | None = None
        self._quota_cache: tuple[float, dict[str, Any]] | None = None
        self._quota_lock = asyncio.Lock()

    def _resolve_cmd_prefix(self) -> list[str]:
        return resolve_agy_command(self._bot.config.agy.executable)

    def _build_cli_env(self, *, use_proxy: bool = True, remote_login: bool = False) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        if remote_login:
            # Trigger Antigravity CLI headless/SSH OAuth flow so it prints the Google login URL
            # directly into the WebUI terminal instead of attempting to open a local server browser.
            env.setdefault("SSH_CONNECTION", "127.0.0.1 50000 127.0.0.1 22")
            env.setdefault("SSH_TTY", "/dev/pts/0")
        else:
            env.pop("SSH_CONNECTION", None)
            env.pop("SSH_CLIENT", None)
            env.pop("SSH_TTY", None)

        proxy = (self._bot.config.agy.proxy or "").strip()
        if use_proxy and proxy:
            env["HTTP_PROXY"] = proxy
            env["HTTPS_PROXY"] = proxy
            env["ALL_PROXY"] = proxy
            env["http_proxy"] = proxy
            env["https_proxy"] = proxy
            env["all_proxy"] = proxy
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
            env.setdefault("no_proxy", "localhost,127.0.0.1,::1")
        elif not use_proxy:
            for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                env.pop(k, None)
        return env

    def resolve_cwd(self, cwd_mode: str = "workspace") -> Path:
        if (cwd_mode or "").strip().lower() == "base":
            p = self._bot.config.base_dir.resolve()
        else:
            p = self._bot.config.resolve_path(self._bot.config.agy.workspace_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_global_settings_path(self) -> Path:
        return get_global_agy_settings_path()

    def read_global_settings(self) -> dict[str, Any]:
        path = self.get_global_settings_path()
        exists = path.is_file()
        data: dict[str, Any] = {}
        if exists:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("全局 settings.json 格式无效（必须为 JSON 对象）")
            data = raw
        return {
            "path": str(path),
            "exists": exists,
            "data": data,
            "workspace_dir": str(self.resolve_cwd("workspace")),
        }

    def write_global_settings(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("全局 CLI 设置必须是 JSON 对象")
        path = self.get_global_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        logger.info("WebUI 已更新全局 agy CLI 设置: %s", path)
        return {
            "path": str(path),
            "exists": True,
            "data": data,
            "workspace_dir": str(self.resolve_cwd("workspace")),
        }

    async def get_cli_info(self, *, refresh_version: bool = False) -> dict[str, Any]:
        executable = ""
        exe_error = ""
        try:
            cmd = self._resolve_cmd_prefix()
            executable = " ".join(cmd)
        except Exception as exc:
            exe_error = str(exc)

        version = ""
        now = time.monotonic()
        if executable and not exe_error:
            if not refresh_version and self._version_cache and (now - self._version_cache[0]) < 120.0:
                version = self._version_cache[1]
            else:
                res = await self.run_quick_command("version")
                if res.get("ok"):
                    version = str(res.get("output") or "").strip().splitlines()[0] if res.get("output") else ""
                    self._version_cache = (now, version)

        global_path = self.get_global_settings_path()
        ws_dir = self.resolve_cwd("workspace")
        pty_supported = (os.name != "nt") or hasattr(ctypes.WinDLL("kernel32"), "CreatePseudoConsole")
        active = [
            {"session_id": sid, "pid": s.pid, "alive": s.is_alive, "cols": s.cols, "rows": s.rows}
            for sid, s in list(self._active_sessions.items())
            if s.is_alive
        ]
        return {
            "installed": bool(executable and not exe_error),
            "executable": executable,
            "executable_error": exe_error,
            "version": version,
            "pty_supported": pty_supported,
            "proxy": self._bot.config.agy.proxy,
            "bot_model": self._bot.config.agy.model,
            "workspace_dir": str(ws_dir),
            "base_dir": str(self._bot.config.base_dir.resolve()),
            "global_settings_path": str(global_path),
            "global_settings_exists": global_path.is_file(),
            "active_terminals": active,
            "quick_actions": [
                {"id": k, "label": meta[0], "args": meta[1]}
                for k, meta in QUICK_CLI_ACTIONS.items()
            ],
        }

    async def run_quick_command(self, action: str, *, use_proxy: bool = True) -> dict[str, Any]:
        key = (action or "").strip().lower()
        if key not in QUICK_CLI_ACTIONS:
            raise ValueError(f"不支持的快捷命令: {action}")
        label, sub_args, timeout_sec = QUICK_CLI_ACTIONS[key]
        cmd = [*self._resolve_cmd_prefix(), *sub_args]
        env = self._build_cli_env(use_proxy=use_proxy, remote_login=False)
        cwd = self.resolve_cwd("workspace")
        t0 = time.monotonic()
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
            )
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            out_s = stdout_b.decode("utf-8", errors="replace").strip()
            err_s = stderr_b.decode("utf-8", errors="replace").strip()
            combined = "\n".join(x for x in (out_s, err_s) if x).strip()
            return {
                "ok": proc.returncode == 0,
                "action": key,
                "label": label,
                "returncode": proc.returncode,
                "output": strip_ansi(combined)[:24000],
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            }
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
            return {
                "ok": False,
                "action": key,
                "label": label,
                "returncode": -1,
                "output": f"命令执行超时 ({timeout_sec:.0f}s)",
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            }
        except Exception as exc:
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    proc.kill()
            return {
                "ok": False,
                "action": key,
                "label": label,
                "returncode": -1,
                "output": f"执行失败: {exc}",
                "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
            }

    def spawn_terminal(
        self,
        *,
        cols: int = 120,
        rows: int = 32,
        use_proxy: bool = True,
        remote_login: bool = False,
        cwd_mode: str = "workspace",
        extra_args: list[str] | None = None,
        on_output: Callable[[str], None] | None = None,
        on_exit: Callable[[int | None], None] | None = None,
    ) -> AgyPtySession:
        # Allowlist safe optional launch flags only
        safe_extras: list[str] = []
        allowed_value_flags = {"--mode", "--model", "--effort", "--agent"}
        allowed_bool_flags = {"--sandbox", "--continue", "--remote-control"}
        raw_extras = list(extra_args or [])
        i = 0
        while i < len(raw_extras):
            tok = str(raw_extras[i] or "").strip()
            if tok in allowed_bool_flags:
                safe_extras.append(tok)
                i += 1
            elif tok in allowed_value_flags and i + 1 < len(raw_extras):
                val = str(raw_extras[i + 1] or "").strip()
                if val and not val.startswith("-"):
                    safe_extras.extend([tok, val])
                i += 2
            else:
                i += 1

        cmd = [*self._resolve_cmd_prefix(), *safe_extras]
        env = self._build_cli_env(use_proxy=use_proxy, remote_login=remote_login)
        cwd = self.resolve_cwd(cwd_mode)

        # Cap concurrent WebUI terminals to prevent resource exhaustion
        if len(self._active_sessions) >= 3:
            oldest_sid = next(iter(self._active_sessions))
            old = self._active_sessions.pop(oldest_sid, None)
            if old is not None:
                old.close()

        def _wrapped_exit(code: int | None) -> None:
            self._active_sessions.pop(session.session_id, None)
            if on_exit is not None:
                on_exit(code)

        session = AgyPtySession(
            cmd,
            cwd=cwd,
            env=env,
            cols=cols,
            rows=rows,
            on_output=on_output,
            on_exit=_wrapped_exit,
        )
        session.start()
        self._active_sessions[session.session_id] = session
        logger.info(
            "WebUI 已启动交互式 agy CLI 终端 (sid=%s, pid=%d, remote_login=%s, proxy=%s, cwd=%s)",
            session.session_id,
            session.pid,
            remote_login,
            use_proxy,
            cwd,
        )
        return session

    async def fetch_account_quota(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """Query the logged-in agy account's remaining model quotas & reset countdowns via `/usage` PTY."""
        now_mono = time.monotonic()
        if not force_refresh and self._quota_cache is not None:
            cached_mono, cached_data = self._quota_cache
            if (now_mono - cached_mono) < 180.0 and cached_data.get("ok"):
                return {**cached_data, "cached": True}

        async with self._quota_lock:
            now_mono = time.monotonic()
            if not force_refresh and self._quota_cache is not None:
                cached_mono, cached_data = self._quota_cache
                if (now_mono - cached_mono) < 180.0 and cached_data.get("ok"):
                    return {**cached_data, "cached": True}

            try:
                cmd = [*self._resolve_cmd_prefix()]
            except Exception as exc:
                return {"ok": False, "signed_in": False, "account": "", "groups": [], "error": str(exc), "fetched_at": time.time()}

            env = self._build_cli_env(use_proxy=True, remote_login=False)
            cwd = self.resolve_cwd("workspace")
            loop = asyncio.get_running_loop()
            chunks: list[str] = []
            ready_ev = asyncio.Event()
            done_ev = asyncio.Event()
            state = {"sent_usage": False, "quota_seen_at": 0.0}

            def _on_out(text: str) -> None:
                chunks.append(text)
                clean = strip_ansi("".join(chunks))
                low = clean.lower()
                if not state["sent_usage"]:
                    if "for shortcuts" in low:
                        ready_ev.set()
                    elif "currently not signed in" in low or "select login method" in low or "please sign in" in low:
                        done_ev.set()
                else:
                    if ("limit remaining" in low or "quota available" in low or "refreshes in" in low) and "%" in clean:
                        if not state["quota_seen_at"]:
                            state["quota_seen_at"] = time.monotonic()
                        # Wait until second group or explanatory footer appears, or enough data accumulated
                        if "within each group" in low or clean.count("%") >= 4:
                            done_ev.set()
                    elif "currently not signed in" in low or "please sign in" in low or "failed to load quota" in low:
                        done_ev.set()

            def _on_exit(_code: int | None) -> None:
                done_ev.set()

            sess = AgyPtySession(
                cmd,
                cwd=cwd,
                env=env,
                cols=140,
                rows=44,
                loop=loop,
                on_output=_on_out,
                on_exit=_on_exit,
            )
            try:
                sess.start()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ready_ev.wait(), timeout=9.0)
                if not done_ev.is_set():
                    await asyncio.sleep(0.9)
                    pre_clean = strip_ansi("".join(chunks)).lower()
                    if "esc to dismiss" in pre_clean:
                        sess.write("\x1b")
                        await asyncio.sleep(0.35)
                    state["sent_usage"] = True
                    sess.write("/usage")
                    await asyncio.sleep(0.25)
                    sess.write("\r")
                    retried_enter = False
                    deadline = time.monotonic() + 16.0
                    while time.monotonic() < deadline:
                        if done_ev.is_set():
                            await asyncio.sleep(0.4)
                            break
                        if state["quota_seen_at"] and (time.monotonic() - state["quota_seen_at"]) >= 1.6:
                            break
                        # If autocomplete menu intercepted first Enter, press Enter once more after 2.2s
                        if not retried_enter and (time.monotonic() > (deadline - 13.8)):
                            cur_low = strip_ansi("".join(chunks)).lower()
                            if "models & quota" not in cur_low and "loading quota" not in cur_low:
                                retried_enter = True
                                sess.write("\r")
                        await asyncio.sleep(0.25)
            except Exception as exc:
                return {
                    "ok": False,
                    "signed_in": False,
                    "account": "",
                    "groups": [],
                    "error": f"查询额度失败: {exc}",
                    "fetched_at": time.time(),
                }
            finally:
                sess.close()

            parsed = parse_agy_quota_output("".join(chunks))
            now_ts = time.time()
            # Stamp absolute reset_at timestamps on each bucket so frontend can tick live countdowns
            for g in parsed.get("groups") or []:
                for bk in ("weekly", "five_hour"):
                    b = g.get(bk)
                    if isinstance(b, dict):
                        rsec = int(b.get("reset_seconds") or 0)
                        b["reset_at"] = round(now_ts + rsec, 1) if rsec > 0 else 0.0
            parsed["fetched_at"] = now_ts
            parsed["cached"] = False
            if parsed.get("ok"):
                self._quota_cache = (time.monotonic(), parsed)
            return parsed

    def close_session(self, session_id: str) -> None:
        sess = self._active_sessions.pop(session_id, None)
        if sess is not None:
            sess.close()

    def shutdown(self) -> None:
        for sid in list(self._active_sessions.keys()):
            self.close_session(sid)
