from __future__ import annotations

import getpass
import os
import pathlib
import subprocess
import sys
import time
import xml.sax.saxutils as xml_esc


TASK_NAME = "AGYCliBot"
LEGACY_TASK_NAME = "AGYBot"


def _root_dir() -> pathlib.Path:
    return pathlib.Path.cwd().resolve()


def _pid_file(root: pathlib.Path) -> pathlib.Path:
    return root / "data" / "bot.pid"


def _stop_existing(root: pathlib.Path) -> None:
    for task_name in (TASK_NAME, LEGACY_TASK_NAME):
        subprocess.run(["schtasks", "/End", "/TN", task_name], capture_output=True)
    pf = _pid_file(root)
    if pf.is_file():
        pid_txt = pf.read_text(encoding="utf-8", errors="ignore").strip()
        if pid_txt.isdigit():
            subprocess.run(["taskkill", "/F", "/PID", pid_txt], capture_output=True)
        try:
            pf.unlink(missing_ok=True)
        except OSError:
            pass
    time.sleep(0.8)


def start_bg() -> int:
    root = _root_dir()
    py_exe = root / ".venv" / "Scripts" / "python.exe"
    if not py_exe.is_file():
        print("[错误] 未找到 .venv 虚拟环境，请先运行 install.bat")
        return 1
    if not (root / "config.json").is_file():
        print("[错误] 未找到 config.json，请先运行 install.bat 并填写配置")
        return 1

    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    runner = data_dir / "_run_bg.cmd"
    runner.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "{root}"\r\n'
        f'"{py_exe}" -X utf8 -u run.py >> bot.log 2>&1\r\n',
        encoding="utf-8",
    )

    _stop_existing(root)

    domain = os.environ.get("USERDOMAIN", "").strip()
    user = getpass.getuser()
    principal_user = f"{domain}\\{user}" if domain else user

    xml_body = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers>\n"
        "    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{xml_esc.escape(principal_user)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>true</Hidden>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        "      <Command>cmd.exe</Command>\n"
        f'      <Arguments>/c "{xml_esc.escape(str(runner))}"</Arguments>\n'
        f"      <WorkingDirectory>{xml_esc.escape(str(root))}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )
    xml_path = data_dir / "_task_bg.xml"
    xml_path.write_text(xml_body, encoding="utf-16")
    try:
        r_create = subprocess.run(
            ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", str(xml_path)],
            capture_output=True,
        )
        if r_create.returncode != 0:
            r_create = subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/F",
                    "/TN",
                    TASK_NAME,
                    "/TR",
                    f'cmd.exe /c "{runner}"',
                    "/SC",
                    "ONLOGON",
                    "/IT",
                ],
                capture_output=True,
            )
        if r_create.returncode != 0:
            err = (r_create.stderr or r_create.stdout or b"").decode("gbk", errors="replace").strip()
            print(f"[错误] 创建后台计划任务失败: {err}")
            return 1
    finally:
        try:
            xml_path.unlink(missing_ok=True)
        except OSError:
            pass

    r_run = subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True)
    if r_run.returncode != 0:
        err = (r_run.stderr or r_run.stdout or b"").decode("gbk", errors="replace").strip()
        print(f"[错误] 启动后台任务失败: {err}")
        return 1

    web_port = 8618
    try:
        import json as _json

        cfg_raw = _json.loads((root / "config.json").read_text(encoding="utf-8-sig"))
        web_port = int((cfg_raw.get("webui") or {}).get("port") or 8618)
    except Exception:
        web_port = 8618

    print("========================================")
    print(f"  AGY CLI BOT 已在后台持久启动（计划任务: {TASK_NAME}）")
    print("  - 无 72 小时超时限制，断开 SSH 或关闭窗口均不影响运行")
    print(f"  - 所有运行日志统一写入: {root / 'bot.log'}")
    print(f"  - WebUI 访问地址: http://127.0.0.1:{web_port}")
    print("  - 停止服务请运行: stop.bat")
    print("========================================")
    return 0


def stop_bg() -> int:
    _stop_existing(_root_dir())
    print(f"AGY CLI BOT 已停止（计划任务 {TASK_NAME}）。")
    return 0


def main() -> int:
    action = (sys.argv[1] if len(sys.argv) > 1 else "start").strip().lower()
    if action == "stop":
        return stop_bg()
    return start_bg()


if __name__ == "__main__":
    raise SystemExit(main())
