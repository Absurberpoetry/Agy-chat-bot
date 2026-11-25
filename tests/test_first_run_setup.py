from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agy_bot.first_run import ensure_webui_credentials, needs_webui_credentials_setup  # noqa: E402


class FirstRunWebUISetupTests(unittest.TestCase):
    def _write_cfg(self, tmp: Path, webui: dict) -> Path:
        p = tmp / "config.json"
        p.write_text(
            json.dumps({"bot_name": "AGY CLI BOT", "webui": webui}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    def test_first_run_prompts_and_persists_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._write_cfg(Path(td), {"enabled": True, "port": 8618, "username": "", "password": ""})
            self.assertTrue(needs_webui_credentials_setup(cfg_path))

            inputs = iter(["star"])
            passwords = iter(["", "pw1", "pw2", "mySecret!99", "mySecret!99"])
            logs: list[str] = []

            ok = ensure_webui_credentials(
                cfg_path,
                interactive=True,
                input_fn=lambda _: next(inputs),
                getpass_fn=lambda _: next(passwords),
                print_fn=logs.append,
            )
            self.assertTrue(ok)
            self.assertFalse(needs_webui_credentials_setup(cfg_path))

            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["webui"]["username"], "star")
            self.assertEqual(saved["webui"]["password"], "mySecret!99")
            self.assertTrue(any("密码不能为空" in line for line in logs))
            self.assertTrue(any("不一致" in line for line in logs))

    def test_default_username_on_empty_enter(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._write_cfg(Path(td), {"enabled": True, "username": "", "password": ""})
            inputs = iter([""])
            passwords = iter(["pass1234", "pass1234"])
            ok = ensure_webui_credentials(
                cfg_path,
                interactive=True,
                input_fn=lambda _: next(inputs),
                getpass_fn=lambda _: next(passwords),
                print_fn=lambda _: None,
            )
            self.assertTrue(ok)
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["webui"]["username"], "admin")
            self.assertEqual(saved["webui"]["password"], "pass1234")

    def test_already_configured_skips_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._write_cfg(Path(td), {"enabled": True, "username": "star", "password": "secret"})
            self.assertFalse(needs_webui_credentials_setup(cfg_path))
            called = False

            def _fail(_: str) -> str:
                nonlocal called
                called = True
                raise AssertionError("Should not prompt when credentials already exist")

            ok = ensure_webui_credentials(
                cfg_path,
                interactive=True,
                input_fn=_fail,
                getpass_fn=_fail,
                print_fn=lambda _: None,
            )
            self.assertTrue(ok)
            self.assertFalse(called)

    def test_disabled_webui_skips_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._write_cfg(Path(td), {"enabled": False, "username": "", "password": ""})
            self.assertFalse(needs_webui_credentials_setup(cfg_path))
            ok = ensure_webui_credentials(
                cfg_path,
                interactive=True,
                input_fn=lambda _: (_ for _ in ()).throw(AssertionError("unexpected")),
                getpass_fn=lambda _: (_ for _ in ()).throw(AssertionError("unexpected")),
                print_fn=lambda _: None,
            )
            self.assertTrue(ok)

    def test_keyboard_interrupt_cancels_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._write_cfg(Path(td), {"enabled": True, "username": "", "password": ""})

            def _abort(_: str) -> str:
                raise KeyboardInterrupt

            ok = ensure_webui_credentials(
                cfg_path,
                interactive=True,
                input_fn=_abort,
                getpass_fn=_abort,
                print_fn=lambda _: None,
            )
            self.assertFalse(ok)
            saved = json.loads(cfg_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["webui"]["username"], "")
            self.assertEqual(saved["webui"]["password"], "")

    def test_non_interactive_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cfg_path = self._write_cfg(Path(td), {"enabled": True, "username": "", "password": ""})
            logs: list[str] = []
            ok = ensure_webui_credentials(
                cfg_path,
                interactive=False,
                print_fn=logs.append,
            )
            self.assertTrue(ok)
            self.assertTrue(any("非交互终端" in line for line in logs))


if __name__ == "__main__":
    unittest.main()
