from __future__ import annotations

import getpass
import json
import sys
from pathlib import Path
from typing import Callable


def _load_raw_config(cfg_path: Path) -> dict | None:
    if not cfg_path.is_file():
        return None
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def needs_webui_credentials_setup(cfg_path: Path) -> bool:
    """Return True if WebUI is enabled in config.json but username or password is unset."""
    raw = _load_raw_config(cfg_path)
    if raw is None:
        return False
    wu = raw.get("webui")
    if not isinstance(wu, dict):
        return False
    if not bool(wu.get("enabled", True)):
        return False
    username = str(wu.get("username", "") or "").strip()
    password = str(wu.get("password", "") or "").strip()
    return (not username) or (not password)


def ensure_webui_credentials(
    cfg_path: Path,
    *,
    interactive: bool | None = None,
    input_fn: Callable[[str], str] = input,
    getpass_fn: Callable[[str], str] = getpass.getpass,
    print_fn: Callable[[str], None] = print,
) -> bool:
    """Prompt for WebUI username and password on first startup and persist to config.json.

    Returns True if startup may continue, or False if cancelled by the user.
    """
    raw = _load_raw_config(cfg_path)
    if raw is None:
        return True
    wu = raw.get("webui")
    if not isinstance(wu, dict):
        return True
    if not bool(wu.get("enabled", True)):
        return True

    cur_user = str(wu.get("username", "") or "").strip()
    cur_pass = str(wu.get("password", "") or "").strip()
    if cur_user and cur_pass:
        return True

    is_tty = bool(sys.stdin and sys.stdin.isatty()) if interactive is None else interactive
    if not is_tty:
        print_fn(
            "[提示] 检测到 WebUI 尚未设置登录账号或密码，且当前为非交互终端；"
            "请在前台运行一次 start.bat（或 scripts/linux/start.sh）完成首次设置，或直接编辑 config.json 的 webui.username / webui.password。"
        )
        return True

    print_fn("========================================")
    print_fn("  首次启动：请设置 WebUI 管理面板登录账号与密码")
    print_fn("========================================")

    try:
        username = cur_user
        while not username:
            entered = input_fn("请输入 WebUI 登录账号 (直接回车默认 admin): ").strip()
            username = entered or "admin"

        password = cur_pass
        while not password:
            p1 = getpass_fn("请输入 WebUI 登录密码 (输入时不显示字符，输完按回车): ")
            if not p1.strip():
                print_fn("[提示] 密码不能为空，请重新输入。")
                continue
            p2 = getpass_fn("请再次输入密码确认: ")
            if p1 != p2:
                print_fn("[提示] 两次输入的密码不一致，请重新输入。")
                continue
            password = p1
    except (KeyboardInterrupt, EOFError):
        print_fn("\n已取消 WebUI 账号初始化，启动中止。")
        return False

    wu["username"] = username
    wu["password"] = password
    raw["webui"] = wu

    tmp_path = cfg_path.with_name(f"{cfg_path.name}.tmp")
    tmp_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(cfg_path)
    print_fn(f"[OK] WebUI 账号与密码已保存至 {cfg_path.name}（登录账号: {username}）\n")
    return True


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cfg_path = Path(args[0]) if args else Path("config.json")
    ok = ensure_webui_credentials(cfg_path)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
