from __future__ import annotations

import asyncio
import datetime as dt
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agy_chat_bot.adapters.base import OnMessageFn, PlatformAdapter
from agy_chat_bot.app import BotApplication
from agy_chat_bot.config import (
    PlatformConfig,
    ScheduledTaskConfig,
    ScheduledTriggerConfig,
    ScriptRuleConfig,
    ScriptTriggerConfig,
    config_to_dict,
    load_config_from_dict,
)
from agy_chat_bot.models import IncomingMessage
from agy_chat_bot.runner import BaseAgentRunner
from agy_chat_bot.models import JudgeDecision, RunnerResult
from agy_chat_bot.triggers.scheduler import cron_matches, validate_cron
from agy_chat_bot.triggers.script_runner import ScriptTriggerEvaluator
from agy_chat_bot.webui.server import WebUIServer


class _FakeAdapter(PlatformAdapter):
    def __init__(self, cfg: PlatformConfig) -> None:
        super().__init__(cfg)
        self.self_id = cfg.self_id or "99999"
        self.sent: list[tuple[str, str, str]] = []  # (scene, channel_id, text)

    async def start(self, on_message: OnMessageFn) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_reply(
        self,
        target: IncomingMessage,
        text: str,
        *,
        quote_trigger: bool = True,
        at_user: bool = False,
    ) -> str | None:
        self.sent.append((target.scene, target.channel_id, text))
        return f"sent_{len(self.sent)}"


class _FakeRunner(BaseAgentRunner):
    def __init__(self) -> None:
        self.prompts: list[tuple[str, str]] = []

    async def ask(self, session_key: str, prompt: str) -> RunnerResult:
        self.prompts.append((session_key, prompt))
        return RunnerResult(ok=True, response=f"AI回复:{prompt.splitlines()[-1][:40]}")

    async def judge_context(self, session_key: str, messages: list[IncomingMessage]) -> JudgeDecision:
        return JudgeDecision(should_reply=False, reason="fake")

    async def shutdown(self) -> None:
        return None

    def is_circuit_open(self) -> tuple[bool, float]:
        return False, 0.0


class ScheduledAndScriptTriggerTests(unittest.IsolatedAsyncioTestCase):
    def test_cron_parser_matches_and_validates(self) -> None:
        # 2026-11-23 is Monday (weekday=0 -> cron dow=1)
        mon_800 = dt.datetime(2026, 11, 23, 8, 0)
        sun_2230 = dt.datetime(2026, 11, 22, 22, 30)

        self.assertTrue(cron_matches("0 8 * * *", mon_800))
        self.assertFalse(cron_matches("5 8 * * *", mon_800))
        self.assertTrue(cron_matches("0 8 * * 1-5", mon_800))
        self.assertFalse(cron_matches("0 8 * * 0,6", mon_800))
        self.assertTrue(cron_matches("*/15 22 * * 0", sun_2230))
        self.assertTrue(cron_matches("30 22 * * 7", sun_2230))

        ok, err = validate_cron("*/10 9-18 * * 1-5")
        self.assertTrue(ok, err)
        ok_bad, _ = validate_cron("0 8 *")
        self.assertFalse(ok_bad)

    def test_config_roundtrip_for_scheduled_and_script(self) -> None:
        raw = {
            "bot_name": "测试机",
            "platforms": [{"id": "qq_1", "ws_url": "ws://127.0.0.1:3001", "group_allowlist": ["1001"]}],
            "triggers": {
                "scheduled": {
                    "enabled": True,
                    "tasks": [
                        {
                            "id": "morning",
                            "name": "早安",
                            "enabled": True,
                            "schedule_type": "cron",
                            "cron": "0 8 * * *",
                            "scene": "group",
                            "target_ids": ["1001"],
                            "action_mode": "text",
                            "prompt": "早安 {{date}}",
                            "include_context": False,
                        }
                    ],
                },
                "script": {
                    "enabled": True,
                    "webhook_token": "tok_secret_123",
                    "rules": [
                        {
                            "id": "r1",
                            "name": "长文",
                            "enabled": True,
                            "mode": "python_expr",
                            "expr": "length >= 10",
                            "action_mode": "llm",
                            "prompt_override": "总结一下",
                            "cooldown_seconds": 5,
                            "timeout_seconds": 4,
                        }
                    ],
                },
            },
        }
        cfg = load_config_from_dict(raw, base_dir=Path("."))
        dumped = config_to_dict(cfg)
        self.assertTrue(dumped["triggers"]["scheduled"]["enabled"])
        self.assertEqual(dumped["triggers"]["scheduled"]["tasks"][0]["cron"], "0 8 * * *")
        self.assertEqual(dumped["triggers"]["script"]["webhook_token"], "tok_secret_123")
        self.assertEqual(dumped["triggers"]["script"]["rules"][0]["expr"], "length >= 10")

    async def test_scheduled_runner_fires_cron_and_interval_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = load_config_from_dict(
                {
                    "bot_name": "小安",
                    "platforms": [
                        {"id": "qq_1", "ws_url": "ws://127.0.0.1:3001", "group_allowlist": ["1001"]}
                    ],
                    "triggers": {
                        "scheduled": {
                            "enabled": True,
                            "tasks": [
                                {
                                    "id": "t_text",
                                    "name": "报时",
                                    "enabled": True,
                                    "schedule_type": "cron",
                                    "cron": "0 8 * * *",
                                    "scene": "group",
                                    "target_ids": [],
                                    "action_mode": "text",
                                    "prompt": "早安 {{bot_name}} {{date}} {{time}}",
                                },
                                {
                                    "id": "t_int",
                                    "name": "巡检",
                                    "enabled": True,
                                    "schedule_type": "interval",
                                    "interval_seconds": 60,
                                    "scene": "group",
                                    "target_ids": ["1001"],
                                    "action_mode": "llm",
                                    "prompt": "活跃一下群气氛",
                                    "include_context": False,
                                },
                            ],
                        }
                    },
                    "agy": {"mode": "mock", "workspace_dir": "./ws", "data_dir": "./data"},
                },
                base_dir=base,
            )
            fake_runner = _FakeRunner()
            app = BotApplication(cfg, runner=fake_runner)
            fake_adapter = _FakeAdapter(cfg.platforms[0])
            app.registry.adapters["qq_1"] = fake_adapter
            app.channel_guard.activate_group("qq_1", "1001")
            app.channel_guard.activate_group("qq_1", "1001")

            # Cron matches 08:00 once per minute; interval initializes on first tick
            when = dt.datetime(2026, 11, 23, 8, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
            fired1 = await app.scheduler.check_once(now_dt=when, now_mono=100.0)
            self.assertEqual(fired1, ["t_text"])
            # Same minute should not re-fire
            fired2 = await app.scheduler.check_once(now_dt=when, now_mono=105.0)
            self.assertEqual(fired2, [])
            # After 65s, interval task fires
            fired3 = await app.scheduler.check_once(
                now_dt=when + dt.timedelta(minutes=1),
                now_mono=165.0,
            )
            self.assertEqual(fired3, ["t_int"])
            await asyncio.sleep(0.05)

            self.assertEqual(len(fake_adapter.sent), 2)
            self.assertIn("早安 小安 2026-11-23 08:00:00", fake_adapter.sent[0][2])
            self.assertTrue(fake_adapter.sent[1][2].startswith("AI回复:"))
            await app.shutdown()

    async def test_script_evaluator_expr_file_and_shell(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            py_script = base / "my_check.py"
            py_script.write_text(
                "def check(ctx):\n"
                "    if '密码' in ctx['text']:\n"
                "        return {'trigger': True, 'reply': '别在群里发密码！', 'reason': 'py_file_guard'}\n"
                "    return False\n",
                encoding="utf-8",
            )
            sh_script = base / "sh_check.py"
            sh_script.write_text(
                "import json, sys\n"
                "ctx = json.loads(sys.stdin.read())\n"
                "if '报警' in ctx['text']:\n"
                "    print(json.dumps({'trigger': True, 'prompt': '紧急排查报警', 'reason': 'shell_alert'}))\n"
                "else:\n"
                "    sys.exit(1)\n",
                encoding="utf-8",
            )

            rules = [
                ScriptRuleConfig(
                    id="r_expr",
                    name="表达式",
                    mode="python_expr",
                    expr="length >= 6 and '打卡' in text",
                    action_mode="text",
                    reply_text="打卡成功！",
                    cooldown_seconds=30.0,
                ),
                ScriptRuleConfig(
                    id="r_file",
                    name="文件脚本",
                    mode="python_file",
                    script_path="my_check.py",
                    cooldown_seconds=0.0,
                ),
                ScriptRuleConfig(
                    id="r_shell",
                    name="命令脚本",
                    mode="shell_cmd",
                    command=f'"{sys.executable}" "{sh_script}"',
                    cooldown_seconds=0.0,
                ),
            ]
            ev = ScriptTriggerEvaluator(ScriptTriggerConfig(enabled=True, rules=rules), base_dir=base)

            def mk_msg(txt: str) -> IncomingMessage:
                return IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="999",
                    scene="group",
                    channel_id="1001",
                    message_id="m1",
                    user_id="u1",
                    sender_name="小明",
                    text=txt,
                    timestamp=time.time(),
                )

            r1 = await ev.evaluate(mk_msg("今天来打卡啦"), now_mono=10.0)
            self.assertTrue(r1.triggered)
            self.assertEqual(r1.direct_reply, "打卡成功！")
            # Cooldown blocks second immediate match
            r1_cd = await ev.evaluate(mk_msg("今天来打卡啦"), now_mono=20.0)
            self.assertFalse(r1_cd.triggered)

            r2 = await ev.evaluate(mk_msg("我的密码是123"), now_mono=25.0)
            self.assertTrue(r2.triggered)
            self.assertEqual(r2.direct_reply, "别在群里发密码！")

            r3 = await ev.evaluate(mk_msg("服务器触发报警了"), now_mono=30.0)
            self.assertTrue(r3.triggered)
            self.assertEqual(r3.prompt_override, "紧急排查报警")

    async def test_handle_message_script_and_webui_fire_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            cfg = load_config_from_dict(
                {
                    "bot_name": "小安",
                    "platforms": [
                        {"id": "qq_1", "ws_url": "ws://127.0.0.1:3001", "group_allowlist": ["1001"]}
                    ],
                    "triggers": {
                        "mention": {"enabled": False},
                        "quote": {"enabled": False},
                        "keyword": {"enabled": False},
                        "dice": {"enabled": False},
                        "context_llm": {"enabled": False},
                        "scheduled": {
                            "enabled": True,
                            "tasks": [
                                {
                                    "id": "ping_job",
                                    "name": "测试任务",
                                    "schedule_type": "cron",
                                    "cron": "0 9 * * *",
                                    "scene": "group",
                                    "target_ids": ["1001"],
                                    "action_mode": "text",
                                    "prompt": "定时播报OK",
                                }
                            ],
                        },
                        "script": {
                            "enabled": True,
                            "webhook_token": "secret_tok_999",
                            "rules": [
                                {
                                    "id": "greet_expr",
                                    "name": "问好",
                                    "mode": "python_expr",
                                    "expr": "'脚本暗号' in text",
                                    "action_mode": "text",
                                    "reply_text": "暗号对接成功",
                                    "cooldown_seconds": 0,
                                }
                            ],
                        },
                    },
                    "collector": {"quiet_wait_seconds": 0, "max_wait_seconds": 0},
                    "rate_limit": {"user_cooldown_seconds": 0, "channel_min_interval_seconds": 0},
                    "agy": {"mode": "mock", "workspace_dir": "./ws", "data_dir": "./data"},
                    "webui": {"enabled": False, "username": "star", "password": "pw"},
                },
                base_dir=base,
            )
            fake_runner = _FakeRunner()
            app = BotApplication(cfg, runner=fake_runner)
            fake_adapter = _FakeAdapter(cfg.platforms[0])
            app.registry.adapters["qq_1"] = fake_adapter
            app.channel_guard.activate_group("qq_1", "1001")
            app.channel_guard.activate_group("qq_1", "1001")
            await app.ensure_plugins_loaded()

            # 1. Message triggers script rule direct reply
            await app.handle_message(
                IncomingMessage(
                    adapter_id="qq_1",
                    platform="onebot_v11",
                    self_id="99999",
                    scene="group",
                    channel_id="1001",
                    message_id="m10",
                    user_id="8888",
                    sender_name="路人",
                    text="试试脚本暗号看行不行",
                    timestamp=time.time(),
                )
            )
            self.assertEqual(len(fake_adapter.sent), 1)
            self.assertEqual(fake_adapter.sent[0][2], "暗号对接成功")

            # 2. WebUI endpoints: /api/triggers/fire (token auth), /api/triggers/script/test, /api/triggers/scheduled/{id}/run
            server = WebUIServer(app, cfg.webui)
            async with TestClient(TestServer(server.web_app)) as client:
                # Reject bad token
                bad = await client.post("/api/triggers/fire", json={"prompt": "hi"}, headers={"X-Trigger-Token": "wrong"})
                self.assertEqual(bad.status, 401)

                # Accept valid webhook token without WebUI login!
                good = await client.post(
                    "/api/triggers/fire",
                    json={"scene": "group", "channel_id": "1001", "mode": "text", "prompt": "外部脚本推送来了"},
                    headers={"X-Trigger-Token": "secret_tok_999"},
                )
                self.assertEqual(good.status, 200)
                good_data = await good.json()
                self.assertTrue(good_data["ok"])
                self.assertEqual(fake_adapter.sent[-1][2], "外部脚本推送来了")

                # Login to WebUI to test script dry-run and scheduled manual fire
                lr = await client.post("/api/auth/login", json={"username": "star", "password": "pw"})
                self.assertEqual(lr.status, 200)

                tr = await client.post(
                    "/api/triggers/script/test",
                    json={
                        "rule": {"mode": "python_expr", "expr": "'测试' in text", "action_mode": "text", "reply_text": "OK"},
                        "sample_text": "这是一条测试消息",
                    },
                )
                self.assertEqual(tr.status, 200)
                tr_json = await tr.json()
                self.assertTrue(tr_json["data"]["triggered"])
                self.assertEqual(tr_json["data"]["direct_reply"], "OK")

                sr = await client.post("/api/triggers/scheduled/ping_job/run")
                self.assertEqual(sr.status, 200)
                sr_json = await sr.json()
                self.assertTrue(sr_json["ok"])
                self.assertEqual(fake_adapter.sent[-1][2], "定时播报OK")

            await app.shutdown()


if __name__ == "__main__":
    unittest.main()
