from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientSession, ClientTimeout, WSServerHandshakeError, WSMsgType, web

from ..adapters.telegram import normalize_telegram_api_base, probe_telegram_bot
from ..config import (
    ScheduledTaskConfig,
    ScriptRuleConfig,
    WebUIConfig,
    config_to_dict,
    load_config_from_dict,
    save_config,
)
from ..extensions import open_in_file_manager
from ..models import IncomingMessage
from ..reply_formatter import split_and_clean_reply
from .cli_pty import AgyCliManager, AgyPtySession, extract_auth_urls
from .log_buffer import LogRingBufferHandler

if TYPE_CHECKING:
    from ..app import BotApplication

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_COOKIE_NAME = "agy_bot_session"
_SESSION_TTL_SECONDS = 7 * 86400  # 7 days (well above 2-day minimum)
_SESSION_SLIDING_THRESHOLD = 6 * 86400  # Refresh expiry when <6 days left
_LOGIN_RATE_WINDOW_SECONDS = 300.0
_LOGIN_RATE_MAX_FAILURES = 10
_MAX_LOG_DOWNLOAD_BYTES = 5 * 1024 * 1024


def _safe_compare_digest(a: str, b: str) -> bool:
    """Constant-time string comparison safe for non-ASCII UTF-8 input."""
    return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))


async def _read_json_dict(request: web.Request) -> dict[str, Any]:
    """Parse JSON request body and require a JSON object."""
    try:
        data = await request.json()
    except Exception as exc:
        raise ValueError("请求体不是有效的 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


class WebUIServer:
    """Built-in aiohttp admin WebUI and JSON/SSE API server for Antigravity QQ Bot."""

    def __init__(
        self,
        bot_app: BotApplication,
        config: WebUIConfig,
        log_handler: LogRingBufferHandler | None = None,
    ) -> None:
        self._bot = bot_app
        self._cfg = config
        self._log_handler = log_handler
        data_dir = getattr(bot_app, "_data_dir", None) or bot_app.config.resolve_path(bot_app.config.agy.data_dir)
        self._sessions_file = Path(data_dir) / "webui_sessions.json"
        self._sessions: dict[str, float] = {}
        self._login_failures: dict[str, list[float]] = {}
        self._sse_tasks: set[asyncio.Task[Any]] = set()
        self._cli_ws_clients: set[web.WebSocketResponse] = set()
        self._cli_manager = AgyCliManager(bot_app)
        self._load_sessions()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._web_app = self._build_web_app()

    def _load_sessions(self) -> None:
        if not self._sessions_file.is_file():
            return
        try:
            raw = json.loads(self._sessions_file.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                now = time.time()
                self._sessions = {
                    str(tok): float(exp)
                    for tok, exp in raw.items()
                    if isinstance(exp, (int, float)) and float(exp) > now
                }
        except Exception as exc:
            logger.warning("读取 WebUI 会话缓存失败: %s", exc)
            self._sessions = {}

    def _save_sessions(self) -> None:
        try:
            now = time.time()
            live = {tok: exp for tok, exp in self._sessions.items() if exp > now}
            self._sessions = live
            self._sessions_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._sessions_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(live, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self._sessions_file)
        except Exception as exc:
            logger.warning("持久化 WebUI 会话失败: %s", exc)

    @property
    def web_app(self) -> web.Application:
        return self._web_app

    def update_config(self, config: WebUIConfig) -> None:
        creds_changed = (
            config.username != self._cfg.username
            or config.password != self._cfg.password
        )
        self._cfg = config
        if creds_changed:
            self._sessions.clear()
            self._save_sessions()
            logger.info("WebUI 登录凭据已变更，已自动注销所有历史登录会话")

    def _auth_required(self) -> bool:
        return bool((self._cfg.password or "").strip())

    def _extract_token(self, request: web.Request) -> str:
        token = request.cookies.get(_COOKIE_NAME, "")
        if not token:
            auth_hdr = request.headers.get("Authorization", "")
            if auth_hdr.startswith("Bearer "):
                token = auth_hdr[7:].strip()
        return token

    def _is_authenticated(self, request: web.Request) -> bool:
        if not self._auth_required():
            return True
        token = self._extract_token(request)
        if not token:
            return False
        exp = self._sessions.get(token)
        if exp is None:
            return False
        now = time.time()
        if exp < now:
            self._sessions.pop(token, None)
            self._save_sessions()
            return False
        # Sliding renewal: keep active sessions fresh for 7 full days
        if exp - now < _SESSION_SLIDING_THRESHOLD:
            self._sessions[token] = now + _SESSION_TTL_SECONDS
            self._save_sessions()
        return True

    def _require_auth(self, request: web.Request) -> None:
        if not self._is_authenticated(request):
            raise web.HTTPUnauthorized(
                text=json.dumps({"ok": False, "error": "未登录或登录已过期"}, ensure_ascii=False),
                content_type="application/json",
            )

    def _build_web_app(self) -> web.Application:
        app = web.Application(client_max_size=4 * 1024 * 1024)
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/api/auth/me", self._handle_auth_me)
        app.router.add_post("/api/auth/login", self._handle_auth_login)
        app.router.add_post("/api/auth/logout", self._handle_auth_logout)

        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/config", self._handle_get_config)
        app.router.add_put("/api/config", self._handle_put_config)
        app.router.add_post("/api/platforms/test", self._handle_test_platform)
        app.router.add_get("/api/models", self._handle_list_models)
        app.router.add_get("/api/agents", self._handle_list_agents)

        app.router.add_get("/api/logs", self._handle_get_logs)
        app.router.add_delete("/api/logs", self._handle_clear_logs)
        app.router.add_get("/api/logs/stream", self._handle_logs_stream)
        app.router.add_get("/api/logs/download", self._handle_logs_download)

        app.router.add_get("/api/personas", self._handle_get_personas)
        app.router.add_post("/api/personas", self._handle_upsert_persona)
        app.router.add_get("/api/personas/global-dialogs", self._handle_get_global_dialogs)
        app.router.add_put("/api/personas/global-dialogs", self._handle_put_global_dialogs)
        app.router.add_delete("/api/personas/{persona_id}", self._handle_delete_persona)
        app.router.add_post("/api/personas/activate", self._handle_activate_persona)

        app.router.add_get("/api/plugins", self._handle_list_plugins)
        app.router.add_post("/api/plugins/reload", self._handle_reload_plugins)
        app.router.add_post("/api/plugins/create", self._handle_create_plugin)
        app.router.add_post("/api/plugins/import", self._handle_import_plugin)
        app.router.add_post("/api/plugins/open-location", self._handle_open_plugin_location)
        app.router.add_post("/api/plugins/{plugin_id}/open-location", self._handle_open_plugin_location)
        app.router.add_post("/api/plugins/{plugin_id}/toggle", self._handle_toggle_plugin)
        app.router.add_put("/api/plugins/{plugin_id}/config", self._handle_update_plugin_config)
        app.router.add_put("/api/plugins/{plugin_id}/scope", self._handle_update_plugin_scope)
        app.router.add_get("/api/plugins/{plugin_id}/source", self._handle_get_plugin_source)
        app.router.add_put("/api/plugins/{plugin_id}/source", self._handle_put_plugin_source)
        app.router.add_delete("/api/plugins/{plugin_id}", self._handle_delete_plugin)

        app.router.add_get("/api/skills", self._handle_list_skills)
        app.router.add_post("/api/skills", self._handle_upsert_skill)
        app.router.add_post("/api/skills/import", self._handle_import_skill)
        app.router.add_post("/api/skills/open-location", self._handle_open_skill_location)
        app.router.add_post("/api/skills/{skill_id}/open-location", self._handle_open_skill_location)
        app.router.add_post("/api/skills/{skill_id}/toggle", self._handle_toggle_skill)
        app.router.add_delete("/api/skills/{skill_id}", self._handle_delete_skill)

        app.router.add_get("/api/mcp", self._handle_list_mcp)
        app.router.add_post("/api/mcp", self._handle_upsert_mcp)
        app.router.add_post("/api/mcp/import", self._handle_import_mcp)
        app.router.add_post("/api/mcp/test", self._handle_test_mcp)
        app.router.add_post("/api/mcp/open-location", self._handle_open_mcp_location)
        app.router.add_post("/api/mcp/{server_id}/toggle", self._handle_toggle_mcp)
        app.router.add_post("/api/mcp/{server_id}/test", self._handle_test_mcp)
        app.router.add_delete("/api/mcp/{server_id}", self._handle_delete_mcp)

        app.router.add_get("/api/security-settings", self._handle_get_security_settings)
        app.router.add_put("/api/security-settings", self._handle_put_security_settings)

        app.router.add_get("/api/cli/info", self._handle_cli_info)
        app.router.add_get("/api/cli/quota", self._handle_cli_quota)
        app.router.add_get("/api/cli/settings", self._handle_cli_get_settings)
        app.router.add_put("/api/cli/settings", self._handle_cli_put_settings)
        app.router.add_post("/api/cli/quick-cmd", self._handle_cli_quick_cmd)
        app.router.add_get("/api/cli/ws", self._handle_cli_ws)

        app.router.add_post("/api/actions/reset-sessions", self._handle_reset_sessions)
        app.router.add_post("/api/actions/clear-circuit", self._handle_clear_circuit)
        app.router.add_post("/api/playground/chat", self._handle_playground_chat)

        app.router.add_get("/api/chat-data/sessions", self._handle_list_chat_sessions)
        app.router.add_get("/api/chat-data/messages", self._handle_get_chat_messages)
        app.router.add_delete("/api/chat-data/sessions", self._handle_delete_chat_sessions)

        app.router.add_get("/api/usage/summary", self._handle_usage_summary)
        app.router.add_get("/api/usage/dashboard", self._handle_usage_dashboard)

        app.router.add_post("/api/triggers/scheduled/{task_id}/run", self._handle_run_scheduled_task)
        app.router.add_post("/api/triggers/script/test", self._handle_test_script_rule)
        app.router.add_post("/api/triggers/fire", self._handle_fire_external_trigger)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self._web_app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._cfg.host, self._cfg.port)
        await self._site.start()
        logger.info("Web 管理端已启动: http://%s:%d", self._cfg.host, self._cfg.port)

    async def stop(self) -> None:
        self._cli_manager.shutdown()
        for ws in list(self._cli_ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._cli_ws_clients.clear()
        for task in list(self._sse_tasks):
            if not task.done():
                task.cancel()
        if self._sse_tasks:
            await asyncio.gather(*list(self._sse_tasks), return_exceptions=True)
            self._sse_tasks.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None

    async def _handle_index(self, request: web.Request) -> web.StreamResponse:
        index_file = _STATIC_DIR / "index.html"
        if not index_file.exists():
            return web.Response(text="WebUI index.html missing", status=404)
        return web.Response(
            text=index_file.read_text(encoding="utf-8"),
            content_type="text/html",
            charset="utf-8",
        )

    async def _handle_auth_me(self, request: web.Request) -> web.Response:
        authed = self._is_authenticated(request)
        resp = web.json_response(
            {
                "ok": True,
                "auth_required": self._auth_required(),
                "authenticated": authed,
                "username": self._cfg.username if authed else "",
                "session_ttl_days": round(_SESSION_TTL_SECONDS / 86400, 1),
            }
        )
        # Keep browser cookie Max-Age refreshed on active visits
        if authed and self._auth_required():
            tok = request.cookies.get(_COOKIE_NAME, "")
            if tok and tok in self._sessions:
                resp.set_cookie(
                    _COOKIE_NAME,
                    tok,
                    path="/",
                    httponly=True,
                    samesite="Lax",
                    max_age=_SESSION_TTL_SECONDS,
                )
        return resp

    def _check_login_rate_limit(self, client_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - _LOGIN_RATE_WINDOW_SECONDS
        bucket = [ts for ts in self._login_failures.get(client_ip, []) if ts >= cutoff]
        if bucket:
            self._login_failures[client_ip] = bucket
        else:
            self._login_failures.pop(client_ip, None)
        return len(bucket) < _LOGIN_RATE_MAX_FAILURES

    def _record_login_failure(self, client_ip: str) -> None:
        now = time.monotonic()
        cutoff = now - _LOGIN_RATE_WINDOW_SECONDS
        bucket = [ts for ts in self._login_failures.get(client_ip, []) if ts >= cutoff]
        bucket.append(now)
        self._login_failures[client_ip] = bucket

    async def _handle_auth_login(self, request: web.Request) -> web.Response:
        client_ip = str(request.remote or "unknown")
        if not self._check_login_rate_limit(client_ip):
            return web.json_response({"ok": False, "error": "登录尝试过于频繁，请稍后再试"}, status=429)
        try:
            body = await _read_json_dict(request)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        user_ok = _safe_compare_digest(username, self._cfg.username)
        pass_ok = _safe_compare_digest(password, self._cfg.password)
        if not (user_ok and pass_ok):
            self._record_login_failure(client_ip)
            return web.json_response({"ok": False, "error": "用户名或密码错误"}, status=401)
        self._login_failures.pop(client_ip, None)

        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + _SESSION_TTL_SECONDS
        self._save_sessions()
        resp = web.json_response({"ok": True, "username": self._cfg.username})
        resp.set_cookie(
            _COOKIE_NAME,
            token,
            path="/",
            httponly=True,
            samesite="Lax",
            max_age=_SESSION_TTL_SECONDS,
        )
        return resp

    async def _handle_auth_logout(self, request: web.Request) -> web.Response:
        token = self._extract_token(request)
        if token and token in self._sessions:
            self._sessions.pop(token, None)
            self._save_sessions()
        resp = web.json_response({"ok": True})
        resp.del_cookie(_COOKIE_NAME, path="/")
        return resp

    async def _handle_status(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        return web.json_response({"ok": True, "data": self._bot.get_runtime_status()})

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        return web.json_response({"ok": True, "data": config_to_dict(self._bot.config)})

    async def _handle_put_config(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            raw = await request.json()
            if not isinstance(raw, dict):
                raise ValueError("配置必须是 JSON 对象")
            new_cfg = load_config_from_dict(
                raw,
                base_dir=self._bot.config.base_dir,
                config_path=self._bot.config.config_path,
            )
            reloaded = await self._bot.reload_config(new_cfg)
            if new_cfg.config_path is not None:
                save_config(new_cfg)
            self.update_config(new_cfg.webui)
            return web.json_response(
                {
                    "ok": True,
                    "message": "配置已保存并热重载生效",
                    "details": reloaded,
                    "data": config_to_dict(self._bot.config),
                }
            )
        except Exception as exc:
            logger.exception("WebUI 更新配置失败")
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_test_platform(self, request: web.Request) -> web.Response:
        """Actively probe a chat client's forward WebSocket endpoint & OneBot v11 handshake."""
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        pid = str(body.get("id") or "").strip()
        ptype = str(body.get("type") or "onebot_v11").strip().lower()
        ws_url = str(body.get("ws_url") or "").strip()
        token = str(body.get("access_token") or "").strip()
        try:
            timeout_sec = max(2.0, min(15.0, float(body.get("timeout", 6.0) or 6.0)))
        except (TypeError, ValueError):
            timeout_sec = 6.0

        logs: list[str] = []

        def _log(msg: str) -> None:
            logs.append(f"[{time.strftime('%H:%M:%S')}] {msg}")

        # Check existing runtime adapter state if registered
        runtime_connected = False
        runtime_self_id = ""
        if pid:
            live_adapter = self._bot.registry.get(pid)
            if live_adapter is not None:
                runtime_connected = (
                    getattr(live_adapter, "_ws", None) is not None
                    or bool(getattr(live_adapter, "connected", False))
                )
                runtime_self_id = str(getattr(live_adapter, "self_id", "") or "")
                state_str = f"已在线 (self_id={runtime_self_id or '未知'})" if runtime_connected else "未连接/重连中"
                _log(f"当前运行中适配器 [{pid}] 状态: {state_str}")

        if ptype in ("telegram", "tg"):
            extra = body.get("extra") if isinstance(body.get("extra"), dict) else {}
            explicit_proxy = str(extra.get("proxy") or body.get("proxy") or "").strip()
            use_agy_proxy = extra.get("use_agy_proxy", True)
            eff_proxy = explicit_proxy if explicit_proxy else (self._bot.config.agy.proxy if use_agy_proxy else "")
            api_base = normalize_telegram_api_base(ws_url)
            _log(f"开始探测 Telegram Bot API: {api_base}/bot***/getMe (代理: {eff_proxy or '直连'}, 超时: {timeout_sec:.0f}s)")
            probe = await probe_telegram_bot(
                token,
                api_base=api_base,
                proxy=eff_proxy,
                timeout_seconds=timeout_sec,
            )
            if probe.get("ok"):
                uname = probe.get("username") or ""
                fname = probe.get("first_name") or ""
                uid = probe.get("user_id") or ""
                priv_off = probe.get("can_read_all_group_messages", False)
                _log(f"✓ Telegram getMe 成功 ({probe.get('latency_ms', 0)} ms): {fname} (@{uname}, id={uid})")
                _log(
                    "✓ 群聊隐私模式已关闭（可读取群聊上下文）"
                    if priv_off
                    else "提示: 群聊隐私模式(Group Privacy)当前为开启，私聊与显式 @机器人 均可用；若需读取群内普通闲聊上下文，请在 @BotFather 执行 /setprivacy -> Disable"
                )
                return web.json_response(
                    {
                        "ok": True,
                        "connected": True,
                        "latency_ms": probe.get("latency_ms", 0),
                        "runtime_connected": runtime_connected,
                        "runtime_self_id": runtime_self_id,
                        "login_info": {
                            "user_id": uid,
                            "nickname": f"{fname} (@{uname})".strip() if uname else fname,
                            "username": uname,
                        },
                        "logs": logs,
                        "error": "",
                    }
                )
            err = str(probe.get("error") or "Telegram 连接测试失败")
            _log(f"✗ {err}")
            return web.json_response(
                {
                    "ok": True,
                    "connected": False,
                    "latency_ms": probe.get("latency_ms", 0),
                    "runtime_connected": runtime_connected,
                    "runtime_self_id": runtime_self_id,
                    "logs": logs,
                    "error": err,
                }
            )

        if not ws_url:
            _log("错误: 正向 WebSocket 地址为空")
            return web.json_response(
                {
                    "ok": True,
                    "connected": False,
                    "latency_ms": 0,
                    "runtime_connected": runtime_connected,
                    "runtime_self_id": runtime_self_id,
                    "logs": logs,
                    "error": "请先填写正向 WebSocket 地址（如 ws://127.0.0.1:3001）",
                }
            )

        if not ws_url.lower().startswith(("ws://", "wss://")):
            _log(f"错误: 无效的协议前缀 -> {ws_url}")
            return web.json_response(
                {
                    "ok": True,
                    "connected": False,
                    "latency_ms": 0,
                    "runtime_connected": runtime_connected,
                    "runtime_self_id": runtime_self_id,
                    "logs": logs,
                    "error": "WebSocket 地址必须以 ws:// 或 wss:// 开头",
                }
            )

        _log(f"开始探测连接: {ws_url} (协议: {ptype}, 超时: {timeout_sec:.0f}s)")
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            _log("已附带 Authorization Bearer Token 鉴权头")

        t0 = time.monotonic()
        login_info: dict[str, Any] = {}
        try:
            async with ClientSession(timeout=ClientTimeout(total=timeout_sec), trust_env=False) as session:
                async with session.ws_connect(ws_url, headers=headers, heartbeat=None) as ws:
                    latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
                    _log(f"WebSocket 握手成功 (耗时 {latency_ms} ms)")

                    if ptype == "onebot_v11":
                        echo = f"agy_probe_{secrets.token_hex(4)}"
                        _log("发送 OneBot v11 探测指令: get_login_info ...")
                        await ws.send_json({"action": "get_login_info", "params": {}, "echo": echo})
                        deadline = time.monotonic() + min(3.5, timeout_sec)
                        got_reply = False
                        while time.monotonic() < deadline:
                            rem = max(0.1, deadline - time.monotonic())
                            try:
                                msg = await asyncio.wait_for(ws.receive(), timeout=rem)
                            except asyncio.TimeoutError:
                                break
                            if msg.type == WSMsgType.TEXT:
                                try:
                                    payload = json.loads(msg.data)
                                except json.JSONDecodeError:
                                    continue
                                if not isinstance(payload, dict):
                                    continue
                                if payload.get("echo") == echo:
                                    got_reply = True
                                    retcode = payload.get("retcode")
                                    status_str = str(payload.get("status") or "")
                                    data_obj = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                                    if retcode == 0 or status_str == "ok":
                                        uid = str(data_obj.get("user_id") or "")
                                        nick = str(data_obj.get("nickname") or "")
                                        login_info = {"user_id": uid, "nickname": nick}
                                        _log(f"OneBot v11 响应正常: 账号 ID={uid or '未知'}, 昵称={nick or '未知'}")
                                    else:
                                        _log(f"OneBot v11 返回异常状态: status={status_str}, retcode={retcode}, msg={payload.get('message') or payload.get('wording') or ''}")
                                    break
                                if payload.get("post_type") == "meta_event":
                                    sid = str(payload.get("self_id") or "")
                                    if sid and not login_info:
                                        login_info = {"user_id": sid, "nickname": ""}
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                                close_code = ws.close_code
                                err_txt = f"服务端主动关闭了连接 (close_code={close_code})，请检查 Access Token 是否匹配"
                                _log(f"连接被关闭: {err_txt}")
                                return web.json_response(
                                    {
                                        "ok": True,
                                        "connected": False,
                                        "latency_ms": latency_ms,
                                        "runtime_connected": runtime_connected,
                                        "runtime_self_id": runtime_self_id,
                                        "logs": logs,
                                        "error": err_txt,
                                    }
                                )
                        if not got_reply:
                            _log("WebSocket 已连通，但未在 3.5s 内收到 get_login_info 响应（客户端可能仅推送事件或未登录账号）")
                    else:
                        _log("通用 WebSocket 通道握手连通正常")

                    _log("探测完成：连接正常！")
                    return web.json_response(
                        {
                            "ok": True,
                            "connected": True,
                            "latency_ms": latency_ms,
                            "runtime_connected": runtime_connected,
                            "runtime_self_id": runtime_self_id,
                            "login_info": login_info,
                            "logs": logs,
                            "error": "",
                        }
                    )
        except WSServerHandshakeError as exc:
            latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
            hint = "（鉴权失败，请检查 Access Token 是否与客户端一致）" if exc.status in (401, 403) else ""
            err_msg = f"WebSocket 握手被拒绝: HTTP {exc.status} {exc.message}{hint}"
            _log(f"连接失败: {err_msg}")
            return web.json_response(
                {
                    "ok": True,
                    "connected": False,
                    "latency_ms": latency_ms,
                    "runtime_connected": runtime_connected,
                    "runtime_self_id": runtime_self_id,
                    "logs": logs,
                    "error": err_msg,
                }
            )
        except asyncio.TimeoutError:
            latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
            err_msg = f"连接超时 ({timeout_sec:.0f}s)，请检查地址端口是否正确、客户端是否已启动或被防火墙拦截"
            _log(f"连接失败: {err_msg}")
            return web.json_response(
                {
                    "ok": True,
                    "connected": False,
                    "latency_ms": latency_ms,
                    "runtime_connected": runtime_connected,
                    "runtime_self_id": runtime_self_id,
                    "logs": logs,
                    "error": err_msg,
                }
            )
        except (ClientError, OSError) as exc:
            latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
            err_msg = f"无法连接到目标地址: {exc}"
            _log(f"连接失败: {err_msg}")
            return web.json_response(
                {
                    "ok": True,
                    "connected": False,
                    "latency_ms": latency_ms,
                    "runtime_connected": runtime_connected,
                    "runtime_self_id": runtime_self_id,
                    "logs": logs,
                    "error": err_msg,
                }
            )
        except Exception as exc:
            latency_ms = round((time.monotonic() - t0) * 1000.0, 1)
            err_msg = f"测试异常: {exc}"
            _log(err_msg)
            return web.json_response(
                {
                    "ok": True,
                    "connected": False,
                    "latency_ms": latency_ms,
                    "runtime_connected": runtime_connected,
                    "runtime_self_id": runtime_self_id,
                    "logs": logs,
                    "error": err_msg,
                }
            )

    async def _handle_list_models(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        models = await self._bot.runner.list_available_models()
        return web.json_response({"ok": True, "data": models})

    async def _handle_list_agents(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        agents = await self._bot.runner.list_available_agents()
        return web.json_response({"ok": True, "data": agents})

    async def _handle_get_logs(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            limit = max(1, min(1000, int(request.query.get("limit", "300"))))
        except ValueError:
            limit = 300
        level = request.query.get("level", "")
        search = request.query.get("search", "")
        tag = request.query.get("tag", "")
        try:
            since_seq = max(0, int(request.query.get("since_seq", "0")))
        except ValueError:
            since_seq = 0
        entries = (
            self._log_handler.get_entries(
                limit=limit,
                level=level,
                search=search,
                since_seq=since_seq,
                tag=tag,
            )
            if self._log_handler is not None
            else []
        )
        return web.json_response(
            {
                "ok": True,
                "boot_id": self._log_handler.boot_id if self._log_handler is not None else "",
                "latest_seq": self._log_handler.latest_seq if self._log_handler is not None else 0,
                "data": entries,
            }
        )

    async def _handle_clear_logs(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        if self._log_handler is not None:
            self._log_handler.clear()
        return web.json_response({"ok": True})

    async def _handle_logs_stream(self, request: web.Request) -> web.StreamResponse:
        self._require_auth(request)
        try:
            since_seq = max(0, int(request.query.get("since_seq", "0")))
        except ValueError:
            since_seq = 0
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        if self._log_handler is None:
            await resp.write_eof()
            return resp

        queue = self._log_handler.subscribe()
        cur_task = asyncio.current_task()
        if cur_task is not None:
            self._sse_tasks.add(cur_task)
        try:
            boot_payload = json.dumps(
                {"boot_id": self._log_handler.boot_id, "latest_seq": self._log_handler.latest_seq},
                ensure_ascii=False,
            )
            await resp.write(f"retry: 3000\nevent: boot\ndata: {boot_payload}\n\n".encode("utf-8"))

            # Send recent snapshot first (up to 300 entries, or incremental since_seq)
            last_sent_seq = since_seq
            for entry in self._log_handler.get_entries(limit=300, since_seq=since_seq):
                last_sent_seq = max(last_sent_seq, int(entry.get("seq") or 0))
                chunk = f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                await resp.write(chunk.encode("utf-8"))

            while True:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=8.0)
                    seq = int(entry.get("seq") or 0)
                    if seq and seq <= last_sent_seq:
                        continue
                    last_sent_seq = max(last_sent_seq, seq)
                    chunk = f"data: {json.dumps(entry, ensure_ascii=False)}\n\n"
                    await resp.write(chunk.encode("utf-8"))
                except asyncio.TimeoutError:
                    ping_payload = json.dumps(
                        {"boot_id": self._log_handler.boot_id, "latest_seq": self._log_handler.latest_seq},
                        ensure_ascii=False,
                    )
                    await resp.write(f"event: ping\ndata: {ping_payload}\n\n".encode("utf-8"))
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if cur_task is not None:
                self._sse_tasks.discard(cur_task)
            self._log_handler.unsubscribe(queue)
        return resp

    async def _handle_logs_download(self, request: web.Request) -> web.StreamResponse:
        self._require_auth(request)
        bot_log = self._bot.config.base_dir / "bot.log"
        text = ""
        if bot_log.exists():
            try:
                size = bot_log.stat().st_size
                with bot_log.open("rb") as fh:
                    if size > _MAX_LOG_DOWNLOAD_BYTES:
                        fh.seek(size - _MAX_LOG_DOWNLOAD_BYTES)
                    text = fh.read().decode("utf-8", errors="replace")
            except OSError as exc:
                logger.warning("读取 bot.log 失败，回退至内存日志缓冲: %s", exc)
        if not text and self._log_handler is not None:
            text = "\n".join(e["formatted"] for e in self._log_handler.get_entries(limit=1000))
        return web.Response(
            text=text,
            content_type="text/plain",
            charset="utf-8",
            headers={"Content-Disposition": 'attachment; filename="agy-bot.log"'},
        )

    async def _handle_get_personas(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        items = [p.to_dict() for p in self._bot.persona_store.list_personas()]
        return web.json_response(
            {
                "ok": True,
                "active_persona_id": self._bot.config.active_persona_id,
                "subagent_persona_id": self._bot.config.agy.subagent_persona_id,
                "global_dialogs": self._bot.persona_store.get_global_dialogs(),
                "data": items,
            }
        )

    async def _handle_upsert_persona(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            pid = str(body.get("persona_id", "")).strip()
            prompt = str(body.get("system_prompt", ""))
            desc = str(body.get("description", ""))
            preset_dialogs = body.get("preset_dialogs", None)
            item = self._bot.persona_store.upsert(pid, prompt, desc, preset_dialogs=preset_dialogs)
            if self._bot.config.active_persona_id == pid:
                await self._bot.apply_active_persona(pid)
            elif self._bot.config.agy.subagent_persona_id == pid:
                self._bot.sync_subagent_workspace_instructions()
            return web.json_response({"ok": True, "data": item.to_dict()})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_get_global_dialogs(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        return web.json_response({"ok": True, "data": self._bot.persona_store.get_global_dialogs()})

    async def _handle_put_global_dialogs(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await request.json()
            dialogs = body.get("dialogs", []) if isinstance(body, dict) else body
            saved = self._bot.persona_store.set_global_dialogs(dialogs)
            await self._bot.apply_active_persona(self._bot.config.active_persona_id)
            return web.json_response(
                {
                    "ok": True,
                    "data": saved,
                    "message": f"已保存 {len(saved)} 轮全局预设对话并同步至 GEMINI.md",
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_delete_persona(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            pid = request.match_info.get("persona_id", "").strip()
            deleted = self._bot.persona_store.delete(pid)
            if deleted:
                if self._bot.config.agy.subagent_persona_id == pid:
                    self._bot.config.agy.subagent_persona_id = ""
                    if self._bot.config.config_path is not None:
                        save_config(self._bot.config)
                    self._bot.sync_subagent_workspace_instructions()
                if self._bot.config.active_persona_id == pid:
                    await self._bot.apply_active_persona("")
            return web.json_response({"ok": True, "deleted": deleted})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_activate_persona(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            pid = str(body.get("persona_id", "")).strip()
            await self._bot.apply_active_persona(pid)
            return web.json_response(
                {
                    "ok": True,
                    "active_persona_id": self._bot.config.active_persona_id,
                    "message": f"已切换人设为: {pid or '(默认助手)'}，并重置暖会话立即生效",
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    def _resolve_security_settings_path(self) -> Path:
        raw = self._bot.config.agy.settings_file.strip()
        if raw:
            candidate = self._bot.config.resolve_path(raw).resolve()
        else:
            candidate = self._bot.config.resolve_path("./settings.antigravity-cli.bot.json").resolve()
        allowed_roots = (
            self._bot.config.base_dir.resolve(),
            self._bot.config.resolve_path(self._bot.config.agy.workspace_dir).resolve(),
            self._bot.config.resolve_path(self._bot.config.agy.data_dir).resolve(),
        )
        if candidate.suffix.lower() != ".json":
            raise ValueError(f"安全权限配置文件必须为 .json 文件: {candidate.name}")
        if not any(candidate.is_relative_to(root) for root in allowed_roots):
            raise ValueError(f"安全权限配置文件路径超出允许目录范围: {candidate}")
        return candidate

    async def _handle_get_security_settings(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            path = self._resolve_security_settings_path()
            data: dict[str, Any] = {}
            if path.exists():
                parsed = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(parsed, dict):
                    raise ValueError("安全权限文件内容不是有效的 JSON 对象")
                data = parsed
            return web.json_response({"ok": True, "path": str(path), "data": data})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_put_security_settings(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            path = self._resolve_security_settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
            await self._bot.reset_warm_sessions()
            return web.json_response({"ok": True, "path": str(path), "message": "CLI 隔离安全权限已保存并重置暖会话"})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_list_chat_sessions(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        sessions = self._bot.context_store.list_sessions_summary()
        runner_status = self._bot.runner.get_status()
        warm_keys = {
            str(w.get("session_key") or "")
            for w in (runner_status.get("warm_sessions") or [])
            if isinstance(w, dict)
        }
        for item in sessions:
            sk = item["session_key"]
            sub_prefix = f"{sk}:u:"
            item["warm_active"] = (sk in warm_keys) or any(k.startswith(sub_prefix) for k in warm_keys)

        db_stats = self._bot.context_store.get_db_stats()
        total_msgs = sum(int(s.get("total_messages") or 0) for s in sessions)
        return web.json_response(
            {
                "ok": True,
                "stats": {
                    **db_stats,
                    "total_sessions": len(sessions),
                    "total_messages": total_msgs,
                    "group_sessions": sum(1 for s in sessions if s.get("scene") == "group"),
                    "private_sessions": sum(1 for s in sessions if s.get("scene") == "private"),
                },
                "data": sessions,
            }
        )

    async def _handle_get_chat_messages(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        session_key = (request.query.get("session_key") or "").strip()
        if not session_key:
            return web.json_response({"ok": False, "error": "缺少 session_key 参数"}, status=400)
        try:
            limit = max(1, min(200, int(request.query.get("limit", "40"))))
        except ValueError:
            limit = 40
        msgs = self._bot.context_store.get_session_messages_preview(session_key, limit=limit)
        return web.json_response({"ok": True, "session_key": session_key, "data": msgs})

    async def _handle_delete_chat_sessions(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        session_key = (request.query.get("session_key") or "").strip()
        delete_all = (request.query.get("all") or "").strip().lower() in ("1", "true", "yes")
        if not session_key and not delete_all:
            try:
                body = await _read_json_dict(request)
            except Exception:
                body = {}
            session_key = str(body.get("session_key") or "").strip()
            delete_all = bool(body.get("all", False))

        if not session_key and not delete_all:
            return web.json_response(
                {"ok": False, "error": "请指定要删除的 session_key，或显式传入 all=1 清空全部"},
                status=400,
            )

        res = await self._bot.purge_chat_data(None if delete_all else session_key)
        target_label = "全部群聊与私聊" if delete_all else session_key
        return web.json_response(
            {
                "ok": True,
                **res,
                "message": (
                    f"已彻底删除 {target_label} 的对话数据"
                    f"（清理数据库与上下文 {res['deleted_messages']} 条消息，重置 CLI 会话 {res['reset_cli_sessions']} 个）"
                ),
            }
        )

    async def _handle_usage_summary(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        data = self._bot.usage_store.get_workbench_summary()
        return web.json_response({"ok": True, "data": data})

    async def _handle_usage_dashboard(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        scope = (request.query.get("scope") or "all").strip()
        date_str = (request.query.get("date") or "").strip()
        month_str = (request.query.get("month") or "").strip()
        cal_month = (request.query.get("calendar_month") or "").strip()
        session_key = (request.query.get("session_key") or "").strip()
        try:
            limit = max(1, min(500, int(request.query.get("limit", "200"))))
        except ValueError:
            limit = 200
        data = self._bot.usage_store.get_dashboard(
            scope=scope,
            date=date_str,
            month=month_str,
            calendar_month=cal_month,
            session_key=session_key,
            limit=limit,
        )
        return web.json_response({"ok": True, "data": data})

    async def _handle_reset_sessions(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        session_key = None
        if request.can_read_body:
            try:
                body = await request.json()
                session_key = body.get("session_key") or None
            except Exception:
                pass
        count = await self._bot.runner.reset_sessions(session_key)
        self._bot.context_store.clear(session_key)
        logger.info("WebUI 手动重置会话记忆 (count=%d, target=%s)", count, session_key or "ALL")
        return web.json_response({"ok": True, "cleared": count})

    async def _handle_clear_circuit(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        self._bot.runner.clear_circuit_breaker()
        logger.info("WebUI 手动重置 Antigravity 熔断器")
        return web.json_response({"ok": True})

    async def _handle_playground_chat(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        try:
            message = str(body.get("message", "")).strip()
            session_key = str(body.get("session_key", "webui:playground")).strip() or "webui:playground"
            allow_fallback = bool(body.get("allow_fallback", True))
            if not message:
                return web.json_response({"ok": False, "error": "消息不能为空"}, status=400)
            if bool(body.get("use_subagent", False)):
                raw_imgs = body.get("images") or []
                img_list = [str(x).strip() for x in raw_imgs if str(x).strip()] if isinstance(raw_imgs, list) else []
                preview = await self._bot.run_subagent_preview(
                    task_desc=message,
                    mcp_id=str(body.get("mcp") or "").strip(),
                    tool_name=str(body.get("tool") or "").strip(),
                    images=img_list,
                    model_override=str(body.get("model") or "").strip() or None,
                )
                return web.json_response(preview)
            t0 = time.monotonic()
            result = await self._bot._ask_runner(session_key, message, allow_fallback=allow_fallback)
            elapsed = round(time.monotonic() - t0, 2)
            usage_rec = None
            if result.ok:
                usage_rec = self._bot.record_token_usage(
                    session_key=session_key,
                    result=result,
                    scene="playground",
                    channel_id="playground",
                    display_name="工作台测试",
                    trigger_type="playground",
                    context_messages=max(1, int(result.num_turns or 1) * 2),
                )
            segments = split_and_clean_reply(result.response, self._bot.config.reply) if result.ok else []
            return web.json_response(
                {
                    "ok": result.ok,
                    "response": result.response,
                    "segments": segments,
                    "error": result.error,
                    "conversation_id": result.conversation_id,
                    "num_turns": result.num_turns,
                    "elapsed_seconds": elapsed,
                    "usage": usage_rec,
                }
            )
        except Exception as exc:
            logger.exception("Playground 对话失败")
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    def _persist_config_if_backed(self) -> None:
        if self._bot.config.config_path is not None:
            save_config(self._bot.config)

    async def _handle_list_plugins(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        await self._bot.ensure_plugins_loaded()
        return web.json_response(
            {
                "ok": True,
                "enabled": self._bot.config.plugins.enabled,
                "plugins_dir": str(self._bot.plugin_manager.plugins_dir),
                "commands": self._bot.plugin_manager.list_registered_commands(),
                "data": self._bot.plugin_manager.list_plugins(),
            }
        )

    async def _handle_reload_plugins(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        items = await self._bot.plugin_manager.load_all()
        return web.json_response(
            {
                "ok": True,
                "message": f"插件热重载完成，当前发现 {len(items)} 个插件",
                "commands": self._bot.plugin_manager.list_registered_commands(),
                "data": items,
            }
        )

    async def _handle_create_plugin(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            pid = str(body.get("plugin_id", "")).strip()
            code = body.get("code")
            item = await self._bot.plugin_manager.create_plugin(pid, code=code)
            return web.json_response({"ok": True, "message": f"已创建并加载插件: {item['id']}", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_toggle_plugin(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            pid = request.match_info.get("plugin_id", "").strip()
            body = await _read_json_dict(request)
            enabled = bool(body.get("enabled", True))
            item = await self._bot.plugin_manager.set_enabled(pid, enabled)
            self._persist_config_if_backed()
            return web.json_response(
                {
                    "ok": True,
                    "message": f"插件 {pid} 已{'启用' if enabled else '停用'}",
                    "data": item,
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_update_plugin_config(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            pid = request.match_info.get("plugin_id", "").strip()
            body = await _read_json_dict(request)
            item = await self._bot.plugin_manager.update_plugin_config(pid, body)
            self._persist_config_if_backed()
            return web.json_response({"ok": True, "message": f"插件 {pid} 配置已保存并生效", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_update_plugin_scope(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            pid = request.match_info.get("plugin_id", "").strip()
            body = await _read_json_dict(request)
            item = await self._bot.plugin_manager.update_plugin_scope(pid, body)
            self._persist_config_if_backed()
            return web.json_response({"ok": True, "message": f"插件 {pid} 生效范围已保存", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_import_plugin(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            filename = str(body.get("filename", "") or "").strip()
            code = str(body.get("code", "") or "")
            overwrite = bool(body.get("overwrite", False))
            item = await self._bot.plugin_manager.import_plugin(filename, code, overwrite=overwrite)
            self._persist_config_if_backed()
            return web.json_response({"ok": True, "message": f"已导入并加载插件: {item['id']}", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_get_plugin_source(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            pid = request.match_info.get("plugin_id", "").strip()
            path, code = self._bot.plugin_manager.read_source(pid)
            return web.json_response({"ok": True, "path": str(path), "code": code})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)

    async def _handle_put_plugin_source(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            pid = request.match_info.get("plugin_id", "").strip()
            body = await _read_json_dict(request)
            code = str(body.get("code", ""))
            item = await self._bot.plugin_manager.write_source(pid, code)
            return web.json_response({"ok": True, "message": f"源码已保存并热重载: {item['id']}", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_delete_plugin(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            pid = request.match_info.get("plugin_id", "").strip()
            deleted = await self._bot.plugin_manager.delete_plugin(pid)
            self._persist_config_if_backed()
            return web.json_response({"ok": True, "deleted": deleted})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_open_plugin_location(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        pid = request.match_info.get("plugin_id", "").strip()
        plugins_root = self._bot.plugin_manager.plugins_dir.resolve()
        try:
            if pid:
                target, _ = self._bot.plugin_manager.read_source(pid)
                target = target.resolve()
                if not target.is_relative_to(plugins_root):
                    return web.json_response({"ok": False, "error": "路径超出插件目录范围"}, status=403)
                opened, detail = open_in_file_manager(target, select_file=True)
            else:
                target = plugins_root
                opened, detail = open_in_file_manager(target, select_file=False)
            return web.json_response(
                {
                    "ok": True,
                    "opened": opened,
                    "path": str(target),
                    "message": f"已在宿主机打开所在位置: {target}" if opened else f"插件路径: {target}（{detail}）",
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)

    # ---- Skills API ----

    async def _handle_list_skills(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        items = self._bot.skills_manager.list_skills()
        return web.json_response(
            {
                "ok": True,
                "skills_dir": str(self._bot.skills_manager.active_dir),
                "data": items,
            }
        )

    async def _handle_upsert_skill(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            sid = str(body.get("id") or body.get("skill_id") or body.get("name") or "").strip()
            item = self._bot.skills_manager.upsert_skill(
                sid,
                name=str(body.get("name") or sid).strip(),
                description=str(body.get("description") or ""),
                body=str(body.get("body") or ""),
                raw_content=body.get("raw_content") if isinstance(body.get("raw_content"), str) else None,
                enabled=body.get("enabled") if "enabled" in body else None,
            )
            await self._bot.reset_warm_sessions()
            return web.json_response({"ok": True, "message": f"Skill「{item['name']}」已保存并同步至 agy 工作区", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_import_skill(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            filename = str(body.get("filename") or "SKILL.md").strip()
            skill_id = str(body.get("skill_id") or "").strip()
            overwrite = bool(body.get("overwrite", False))
            if isinstance(body.get("content_base64"), str) and body["content_base64"].strip():
                raw_bytes = base64.b64decode(body["content_base64"].strip(), validate=True)
            else:
                raw_bytes = str(body.get("content") or "").encode("utf-8")
            item = self._bot.skills_manager.import_skill(
                filename,
                raw_bytes,
                skill_id=skill_id,
                overwrite=overwrite,
            )
            await self._bot.reset_warm_sessions()
            return web.json_response({"ok": True, "message": f"已导入 Skill「{item['name']}」并重置暖会话", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_toggle_skill(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            sid = request.match_info.get("skill_id", "").strip()
            body = await _read_json_dict(request)
            enabled = bool(body.get("enabled", True))
            item = self._bot.skills_manager.set_enabled(sid, enabled)
            await self._bot.reset_warm_sessions()
            return web.json_response(
                {
                    "ok": True,
                    "message": f"Skill「{sid}」已{'启用' if enabled else '停用'}并重置暖会话",
                    "data": item,
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_delete_skill(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            sid = request.match_info.get("skill_id", "").strip()
            deleted = self._bot.skills_manager.delete_skill(sid)
            if deleted:
                await self._bot.reset_warm_sessions()
            return web.json_response({"ok": True, "deleted": deleted, "message": f"已删除 Skill「{sid}」" if deleted else "未找到工作区 Skill"})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_open_skill_location(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        sid = request.match_info.get("skill_id", "").strip()
        try:
            target, select_file = self._bot.skills_manager.resolve_location(sid)
            opened, detail = open_in_file_manager(target, select_file=select_file)
            return web.json_response(
                {
                    "ok": True,
                    "opened": opened,
                    "path": str(target),
                    "message": f"已在宿主机打开所在位置: {target}" if opened else f"Skill 目录: {target}（{detail}）",
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=404)

    # ---- MCP Servers API ----

    async def _handle_list_mcp(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        items = self._bot.mcp_manager.list_servers()
        return web.json_response(
            {
                "ok": True,
                "mcp_config_path": str(self._bot.mcp_manager.mcp_config_path),
                "data": items,
            }
        )

    async def _handle_upsert_mcp(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            sid = str(body.get("id") or body.get("server_id") or "").strip()
            item = self._bot.mcp_manager.upsert_server(sid, body)
            self._bot.sync_workspace_instructions()
            await self._bot.reset_warm_sessions()
            return web.json_response({"ok": True, "message": f"MCP 服务器「{item['id']}」已保存并同步至 mcp_config.json 与 GEMINI.md", "data": item})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_import_mcp(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await request.json()
            payload = body.get("config", body) if isinstance(body, dict) else body
            if isinstance(payload, str):
                payload = json.loads(payload)
            overwrite = bool(body.get("overwrite", True)) if isinstance(body, dict) else True
            items = self._bot.mcp_manager.import_json(payload, overwrite=overwrite)
            self._bot.sync_workspace_instructions()
            await self._bot.reset_warm_sessions()
            return web.json_response(
                {
                    "ok": True,
                    "message": f"已导入 {len(items)} 个 MCP 服务器并同步至 mcp_config.json 与 GEMINI.md",
                    "data": items,
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_toggle_mcp(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            sid = request.match_info.get("server_id", "").strip()
            body = await _read_json_dict(request)
            enabled = bool(body.get("enabled", True))
            item = self._bot.mcp_manager.set_enabled(sid, enabled)
            self._bot.sync_workspace_instructions()
            await self._bot.reset_warm_sessions()
            return web.json_response(
                {
                    "ok": True,
                    "message": f"MCP 服务器「{sid}」已{'启用' if enabled else '停用'}，已同步 GEMINI.md 并重置暖会话",
                    "data": item,
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_delete_mcp(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            sid = request.match_info.get("server_id", "").strip()
            deleted = self._bot.mcp_manager.delete_server(sid)
            if deleted:
                self._bot.sync_workspace_instructions()
                await self._bot.reset_warm_sessions()
            return web.json_response({"ok": True, "deleted": deleted, "message": f"已删除 MCP 服务器「{sid}」" if deleted else "未找到工作区 MCP 服务器"})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_test_mcp(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            sid = request.match_info.get("server_id", "").strip()
            override: dict[str, Any] | None = None
            if request.can_read_body:
                try:
                    body = await request.json()
                    if isinstance(body, dict) and (body.get("command") or body.get("url") or body.get("serverUrl")):
                        override = body
                        if not sid:
                            sid = str(body.get("id") or body.get("server_id") or "test_mcp").strip()
                except Exception:
                    pass
            res = await self._bot.mcp_manager.test_server(
                sid,
                config_override=override,
                proxy=self._bot.config.agy.proxy,
            )
            return web.json_response(res, status=200 if res.get("ok") else 400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_open_mcp_location(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        target = self._bot.mcp_manager.mcp_config_path
        if not target.exists():
            self._bot.mcp_manager._sync_workspace_mcp_config()
        opened, detail = open_in_file_manager(target, select_file=True)
        return web.json_response(
            {
                "ok": True,
                "opened": opened,
                "path": str(target),
                "message": f"已在宿主机打开配置文件所在位置: {target}" if opened else f"MCP 配置文件路径: {target}（{detail}）",
            }
        )

    async def _handle_run_scheduled_task(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        task_id = request.match_info.get("task_id", "").strip()
        task = self._bot.scheduler.find_task(task_id)
        if task is None:
            try:
                body = await request.json()
            except Exception:
                body = {}
            if isinstance(body, dict) and body.get("prompt"):
                try:
                    interval_val = max(10.0, float(body.get("interval_seconds", 3600.0) or 3600.0))
                except (TypeError, ValueError):
                    return web.json_response({"ok": False, "error": "interval_seconds 必须为数字"}, status=400)
                raw_targets = body.get("target_ids") or []
                if isinstance(raw_targets, str):
                    target_ids = [s.strip() for s in raw_targets.split(",") if s.strip()]
                elif isinstance(raw_targets, list):
                    target_ids = [str(x).strip() for x in raw_targets if str(x).strip()]
                else:
                    target_ids = []
                task = ScheduledTaskConfig(
                    id=task_id or str(body.get("id", "manual_task")),
                    name=str(body.get("name", "") or task_id),
                    enabled=True,
                    schedule_type=str(body.get("schedule_type", "cron")),
                    cron=str(body.get("cron", "0 8 * * *")),
                    interval_seconds=interval_val,
                    adapter_id=str(body.get("adapter_id", "") or "").strip(),
                    scene=str(body.get("scene", "group") or "group").strip(),
                    target_ids=target_ids,
                    action_mode=str(body.get("action_mode", "llm") or "llm").strip(),
                    prompt=str(body.get("prompt", "") or ""),
                    include_context=bool(body.get("include_context", True)),
                    at_qq=str(body.get("at_qq", "") or "").strip(),
                )
        if task is None:
            return web.json_response({"ok": False, "error": f"未找到定时任务: {task_id}（请先保存或传入任务内容）"}, status=404)
        try:
            sent_targets = await self._bot.scheduler.fire_now(task)
            return web.json_response(
                {
                    "ok": True,
                    "sent_targets": sent_targets,
                    "message": f"定时任务「{task.name or task.id}」已执行，发送至 {sent_targets} 个目标",
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    async def _handle_test_script_rule(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
        except ValueError as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

        rule_raw = body.get("rule") if isinstance(body.get("rule"), dict) else body
        try:
            timeout_val = max(1.0, float(rule_raw.get("timeout_seconds", 5.0) or 5.0))
            recent_count_val = int(body.get("sample_recent_count", 5) or 0)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "数字参数格式无效"}, status=400)
        rule = ScriptRuleConfig(
            id=str(rule_raw.get("id", "test_rule") or "test_rule"),
            name=str(rule_raw.get("name", "测试规则") or "测试规则"),
            enabled=True,
            mode=str(rule_raw.get("mode", "python_expr") or "python_expr").strip(),
            expr=str(rule_raw.get("expr", "") or ""),
            script_path=str(rule_raw.get("script_path", "") or "").strip(),
            command=str(rule_raw.get("command", "") or "").strip(),
            action_mode=str(rule_raw.get("action_mode", "llm") or "llm").strip(),
            prompt_override=str(rule_raw.get("prompt_override", "") or ""),
            reply_text=str(rule_raw.get("reply_text", "") or ""),
            cooldown_seconds=0.0,
            timeout_seconds=timeout_val,
        )
        sample_text = str(body.get("sample_text", "") or "")
        sample_user = str(body.get("sample_user_id", "10001") or "10001")
        sample_channel = str(body.get("sample_channel_id", "100000") or "100000")
        sample_scene = "private" if str(body.get("sample_scene", "group")) == "private" else "group"
        sample_msg = IncomingMessage(
            adapter_id="webui_test",
            platform="onebot_v11",
            self_id="0",
            scene=sample_scene,  # type: ignore[arg-type]
            channel_id=sample_channel,
            message_id="test_msg",
            user_id=sample_user,
            sender_name="测试群友",
            text=sample_text,
            timestamp=time.time(),
        )
        try:
            res = await self._bot.script_triggers.evaluate_rule(
                rule,
                sample_msg,
                is_admin=bool(body.get("sample_is_admin", False)),
                recent_count=recent_count_val,
            )
            return web.json_response(
                {
                    "ok": True,
                    "data": {
                        "triggered": res.triggered,
                        "reason": res.reason,
                        "direct_reply": res.direct_reply,
                        "prompt_override": res.prompt_override,
                    },
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": f"脚本执行出错: {exc}"}, status=400)

    async def _handle_fire_external_trigger(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return web.json_response({"ok": False, "error": "请求体必须为 JSON 对象"}, status=400)

        authed = self._is_authenticated(request)
        if not authed:
            cfg_token = (self._bot.config.triggers.script.webhook_token or "").strip()
            if not self._bot.config.triggers.script.enabled or not cfg_token:
                return web.json_response(
                    {"ok": False, "error": "外部脚本触发未启用或未设置 webhook_token"},
                    status=401,
                )
            req_token = request.headers.get("X-Trigger-Token", "").strip()
            if not req_token:
                auth_hdr = request.headers.get("Authorization", "")
                if auth_hdr.startswith("Bearer "):
                    req_token = auth_hdr[7:].strip()
            if not req_token:
                req_token = str(body.get("token", "") or "").strip()
            if not req_token or not _safe_compare_digest(req_token, cfg_token):
                return web.json_response({"ok": False, "error": "触发 Token 无效"}, status=401)

        prompt = str(body.get("prompt", body.get("text", "")) or "").strip()
        if not prompt:
            return web.json_response({"ok": False, "error": "prompt / text 不能为空"}, status=400)

        res = await self._bot.trigger_proactive(
            adapter_id=str(body.get("adapter_id", "") or "").strip(),
            scene=str(body.get("scene", "group") or "group").strip(),
            channel_id=str(body.get("channel_id", "") or "").strip(),
            mode=str(body.get("mode", "llm") or "llm").strip(),
            prompt=prompt,
            include_context=bool(body.get("include_context", True)),
            trigger_type="script",
            reason=str(body.get("reason", "external_script") or "external_script").strip(),
            at_qq=str(body.get("at_qq", "") or "").strip(),
        )
        status_code = 200 if res.get("ok") else 400
        return web.json_response(res, status=status_code)

    # ---- Agy CLI Console, Login & Settings API ----

    async def _handle_cli_info(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        refresh = request.query.get("refresh", "").strip().lower() in ("1", "true", "yes")
        try:
            info = await self._cli_manager.get_cli_info(refresh_version=refresh)
            return web.json_response({"ok": True, "data": info})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_cli_quota(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        refresh = request.query.get("refresh", "").strip().lower() in ("1", "true", "yes")
        try:
            data = await self._cli_manager.fetch_account_quota(force_refresh=refresh)
            return web.json_response({"ok": True, "data": data})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_cli_get_settings(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            res = self._cli_manager.read_global_settings()
            return web.json_response({"ok": True, **res})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_cli_put_settings(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            data = body.get("data") if ("data" in body and isinstance(body.get("data"), dict)) else body
            reset_sessions = bool(body.get("reset_sessions", True)) if "data" in body else True
            saved = self._cli_manager.write_global_settings(data)
            if reset_sessions:
                await self._bot.reset_warm_sessions()
            return web.json_response(
                {
                    "ok": True,
                    **saved,
                    "message": "全局 agy CLI 设置已保存" + ("并重置机器人暖会话" if reset_sessions else ""),
                }
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_cli_quick_cmd(self, request: web.Request) -> web.Response:
        self._require_auth(request)
        try:
            body = await _read_json_dict(request)
            action = str(body.get("action") or "").strip()
            use_proxy = bool(body.get("use_proxy", True))
            res = await self._cli_manager.run_quick_command(action, use_proxy=use_proxy)
            return web.json_response(res, status=200 if res.get("ok") else 400)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)

    async def _handle_cli_ws(self, request: web.Request) -> web.StreamResponse:
        self._require_auth(request)
        ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=1024 * 1024)
        await ws.prepare(request)
        self._cli_ws_clients.add(ws)

        def _parse_bool(val: str | None, default: bool) -> bool:
            if val is None or not str(val).strip():
                return default
            return str(val).strip().lower() in ("1", "true", "yes", "on")

        def _parse_int(val: str | None, default: int) -> int:
            try:
                return int(val or default)
            except (TypeError, ValueError):
                return default

        cols = _parse_int(request.query.get("cols"), 120)
        rows = _parse_int(request.query.get("rows"), 32)
        remote_login = _parse_bool(request.query.get("remote_login"), False)
        use_proxy = _parse_bool(request.query.get("use_proxy"), True)
        cwd_mode = (request.query.get("cwd") or "workspace").strip()

        out_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=500)
        session_holder: dict[str, AgyPtySession | None] = {"session": None}
        url_tail_buf = ""
        seen_auth_urls: set[str] = set()

        async def _send_ws(payload: dict[str, Any]) -> None:
            if not ws.closed:
                try:
                    await ws.send_json(payload)
                except Exception:
                    pass

        def _enqueue(payload: dict[str, Any]) -> None:
            try:
                out_queue.put_nowait(payload)
            except asyncio.QueueFull:
                try:
                    out_queue.get_nowait()
                    out_queue.put_nowait(payload)
                except Exception:
                    pass

        def _stop_current_session() -> None:
            old = session_holder.get("session")
            if old is not None:
                session_holder["session"] = None
                self._cli_manager.close_session(old.session_id)

        def _start_session(
            *,
            s_cols: int,
            s_rows: int,
            s_remote: bool,
            s_proxy: bool,
            s_cwd: str,
            extra_args: list[str] | None = None,
        ) -> None:
            nonlocal url_tail_buf
            _stop_current_session()
            url_tail_buf = ""
            seen_auth_urls.clear()

            def _on_out(text: str) -> None:
                nonlocal url_tail_buf
                _enqueue({"type": "output", "data": text})
                url_tail_buf = (url_tail_buf + text)[-24576:]
                for u in extract_auth_urls(url_tail_buf):
                    if u not in seen_auth_urls:
                        seen_auth_urls.add(u)
                        _enqueue({"type": "auth_url", "url": u})

            def _on_exit(code: int | None) -> None:
                _enqueue({"type": "exit", "code": code})

            try:
                sess = self._cli_manager.spawn_terminal(
                    cols=s_cols,
                    rows=s_rows,
                    use_proxy=s_proxy,
                    remote_login=s_remote,
                    cwd_mode=s_cwd,
                    extra_args=extra_args,
                    on_output=_on_out,
                    on_exit=_on_exit,
                )
                session_holder["session"] = sess
                _enqueue(
                    {
                        "type": "status",
                        "running": True,
                        "session_id": sess.session_id,
                        "pid": sess.pid,
                        "cols": sess.cols,
                        "rows": sess.rows,
                        "cmd": " ".join(sess.cmd),
                        "cwd": str(sess.cwd),
                        "remote_login": s_remote,
                        "use_proxy": s_proxy,
                    }
                )
            except Exception as exc:
                _enqueue({"type": "error", "error": str(exc)})

        async def _sender_loop() -> None:
            while True:
                item = await out_queue.get()
                if item is None:
                    break
                await _send_ws(item)

        sender_task = asyncio.create_task(_sender_loop(), name="cli-ws-sender")
        try:
            _start_session(
                s_cols=cols,
                s_rows=rows,
                s_remote=remote_login,
                s_proxy=use_proxy,
                s_cwd=cwd_mode,
            )

            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        pkt = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(pkt, dict):
                        continue
                    ptype = str(pkt.get("type") or "").strip().lower()
                    cur = session_holder.get("session")
                    if ptype == "input":
                        data = str(pkt.get("data") or "")
                        if cur is not None and data:
                            cur.write(data)
                    elif ptype == "resize":
                        c = _parse_int(pkt.get("cols"), cols)
                        r = _parse_int(pkt.get("rows"), rows)
                        cols, rows = c, r
                        if cur is not None:
                            cur.resize(c, r)
                    elif ptype == "restart":
                        cols = _parse_int(pkt.get("cols"), cols)
                        rows = _parse_int(pkt.get("rows"), rows)
                        remote_login = bool(pkt.get("remote_login", remote_login))
                        use_proxy = bool(pkt.get("use_proxy", use_proxy))
                        cwd_mode = str(pkt.get("cwd") or cwd_mode).strip()
                        raw_extra = pkt.get("extra_args")
                        extra = [str(x) for x in raw_extra] if isinstance(raw_extra, list) else None
                        _start_session(
                            s_cols=cols,
                            s_rows=rows,
                            s_remote=remote_login,
                            s_proxy=use_proxy,
                            s_cwd=cwd_mode,
                            extra_args=extra,
                        )
                    elif ptype == "stop":
                        _stop_current_session()
                        _enqueue({"type": "status", "running": False, "pid": 0})
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            _stop_current_session()
            try:
                out_queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            sender_task.cancel()
            try:
                await sender_task
            except BaseException:
                pass
            self._cli_ws_clients.discard(ws)
        return ws
