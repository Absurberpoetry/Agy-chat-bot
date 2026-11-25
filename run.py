#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import logging
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agy_bot.app import BotApplication  # noqa: E402
from agy_bot.config import load_config  # noqa: E402
from agy_bot.webui.log_buffer import LogRingBufferHandler  # noqa: E402

_WIN_JOB_HANDLE: int = 0
_WIN_CTRL_HANDLER_REF: object | None = None


def _disable_windows_console_quick_edit() -> bool:
    """Disable Windows Console QuickEdit Mode so clicking inside the CMD window
    never freezes stdout/stderr logging and hangs the entire bot/WebUI."""
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h_in = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if not h_in or h_in == ctypes.c_void_p(-1).value:
            return False
        mode = wintypes.DWORD(0)
        if not kernel32.GetConsoleMode(h_in, ctypes.byref(mode)):
            return False
        # Clear ENABLE_QUICK_EDIT_MODE (0x0040), set ENABLE_EXTENDED_FLAGS (0x0080)
        new_mode = (mode.value & ~0x0040) | 0x0080
        return bool(kernel32.SetConsoleMode(h_in, new_mode))
    except Exception:
        return False


def _bind_windows_kill_on_close_job() -> bool:
    """Bind current process to a Windows Job Object with KILL_ON_JOB_CLOSE.

    Ensures all spawned child processes (warm `agy.exe`, stdio MCP servers, etc.)
    are immediately terminated by the Windows kernel when `run.py` exits for any reason
    (closing console window, Ctrl+C, taskkill, or crash), preventing locked folders.
    """
    global _WIN_JOB_HANDLE
    if os.name != "nt":
        return False
    try:
        from ctypes import wintypes

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return False

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (0x2000) | JOB_OBJECT_LIMIT_BREAKAWAY_OK (0x0800)
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return False

        if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
            kernel32.CloseHandle(job)
            return False

        _WIN_JOB_HANDLE = int(job)
        return True
    except Exception:
        return False


def _cleanup_pid_file(pid_file: Path, my_pid: str) -> None:
    try:
        if pid_file.is_file() and pid_file.read_text(encoding="utf-8", errors="ignore").strip() == my_pid:
            pid_file.unlink(missing_ok=True)
    except OSError:
        pass


async def _run_with_clean_shutdown(app: BotApplication, pid_file: Path, my_pid: str) -> None:
    global _WIN_CTRL_HANDLER_REF
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    shutdown_done = threading.Event()
    closing_once = threading.Event()

    def _trigger_cancel(reason: str, *, force_exit_after: float = 0.0) -> None:
        if closing_once.is_set():
            return
        closing_once.set()
        if main_task is not None and not main_task.done():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(main_task.cancel)
        if force_exit_after > 0:
            shutdown_done.wait(timeout=force_exit_after)
            _cleanup_pid_file(pid_file, my_pid)
            os._exit(0)

    if os.name == "nt":
        try:
            from ctypes import wintypes

            handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def _win_ctrl_handler(ctrl_type: int) -> bool:
                # 0=CTRL_C, 1=CTRL_BREAK, 2=CTRL_CLOSE (X button), 5=LOGOFF, 6=SHUTDOWN
                if ctrl_type in (0, 1, 2, 5, 6):
                    # On window close / logoff / shutdown, wait briefly for graceful cleanup then exit
                    # immediately so Windows never leaves an orphan python.exe holding port 8618 or files.
                    _trigger_cancel(f"win_ctrl_{ctrl_type}", force_exit_after=2.5 if ctrl_type in (2, 5, 6) else 0.0)
                    return True
                return False

            cb = handler_type(_win_ctrl_handler)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
            kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
            if kernel32.SetConsoleCtrlHandler(cb, True):
                _WIN_CTRL_HANDLER_REF = cb
        except Exception:
            pass
        if hasattr(signal, "SIGBREAK"):
            with contextlib.suppress(Exception):
                signal.signal(signal.SIGBREAK, lambda *_: _trigger_cancel("SIGBREAK"))
    else:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, lambda s=sig: _trigger_cancel(s.name))

    try:
        await app.run()
    except asyncio.CancelledError:
        logging.getLogger(__name__).info("收到关闭信号，已完成资源清理并退出")
    finally:
        _cleanup_pid_file(pid_file, my_pid)
        shutdown_done.set()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-platform bot powered by official Antigravity CLI",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(ROOT / "config.json"),
        help="配置文件路径 (默认: ./config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="强制使用离线 MockAgentRunner，仅测试客户端连接、触发器与防抖逻辑而不消耗额度",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(
            f"找不到配置文件: {cfg_path}\n请先运行 install.bat（或复制 templates/config.example.json 为 config.json）。",
            file=sys.stderr,
        )
        return 1

    _disable_windows_console_quick_edit()
    _bind_windows_kill_on_close_job()

    cfg = load_config(cfg_path)
    if args.dry_run:
        cfg.agy.mode = "mock"

    log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, cfg.logging.level, logging.INFO),
        format=log_fmt,
    )
    log_handler = LogRingBufferHandler(max_entries=cfg.webui.log_buffer_size)
    log_handler.setFormatter(logging.Formatter(log_fmt))
    logging.getLogger().addHandler(log_handler)

    app = BotApplication(cfg, log_handler=log_handler)
    pid_file = cfg.resolve_path(cfg.agy.data_dir) / "bot.pid"
    my_pid = str(os.getpid())
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(my_pid, encoding="utf-8")
    except OSError:
        pass
    try:
        asyncio.run(_run_with_clean_shutdown(app, pid_file, my_pid))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("收到中断信号，机器人已退出")
    finally:
        _cleanup_pid_file(pid_file, my_pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
