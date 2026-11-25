#!/usr/bin/env python3
"""Proxy Node Guard for AGY CLI BOT (port 7890).

Automatically benchmarks all proxy nodes on the local Clash/Mihomo core listening on port 7890
against Google Antigravity upstream (`cloudcode-pa.googleapis.com`), verifies real Antigravity
connectivity (rejecting timeouts, network errors, and `User location is not supported` geo-blocks),
and switches to the lowest-latency healthy node.

Usage:
  python scripts/proxy_node_guard.py                 # Check current node; if unhealthy, scan & switch to fastest valid node
  python scripts/proxy_node_guard.py --force         # Always scan all nodes and switch to the fastest valid node
  python scripts/proxy_node_guard.py --check-only    # Benchmark all nodes and print latency table without switching
  python scripts/proxy_node_guard.py --watch         # Watch bot.log & periodic probes; auto-failover immediately on error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agy_bot.proxy_guard import (  # noqa: E402
    ProbeOutcome,
    ProxyControllerInfo,
    benchmark_all_nodes,
    discover_proxy_controller,
    extract_local_proxy_port,
    fetch_selector_group,
    is_allowed_proxy_node,
    is_geo_blocked_error,
    is_proxy_or_geo_error,
    select_best_working_node,
)


def _load_bot_defaults(config_path: Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "proxy_port": 7890,
        "webui_url": "http://127.0.0.1:8618",
        "webui_user": "star",
        "webui_pass": "",
    }
    if not config_path.is_file():
        return defaults
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            return defaults
        agy = raw.get("agy") or {}
        if isinstance(agy, dict):
            port = extract_local_proxy_port(str(agy.get("proxy") or ""))
            if port:
                defaults["proxy_port"] = port
        wui = raw.get("webui") or {}
        if isinstance(wui, dict):
            wport = int(wui.get("port") or 8618)
            defaults["webui_url"] = f"http://127.0.0.1:{wport}"
            defaults["webui_user"] = str(wui.get("username") or "star")
            defaults["webui_pass"] = str(wui.get("password") or "")
    except Exception:
        pass
    return defaults


class WebUIAntigravityProber:
    """Probes real Antigravity upstream health through the live bot's WebUI Playground API."""

    def __init__(self, webui_url: str, username: str, password: str, *, timeout_seconds: float = 35.0) -> None:
        self.webui_url = webui_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds

    async def probe(self) -> ProbeOutcome:
        t0 = time.monotonic()
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar, trust_env=False) as session:
            if self.password:
                try:
                    async with session.post(
                        f"{self.webui_url}/api/auth/login",
                        json={"username": self.username, "password": self.password},
                    ) as lr:
                        if lr.status != 200:
                            return ProbeOutcome(
                                ok=False,
                                elapsed_ms=int((time.monotonic() - t0) * 1000),
                                error=f"WebUI 登录失败 HTTP {lr.status}",
                            )
                except Exception as exc:
                    return ProbeOutcome(
                        ok=False,
                        elapsed_ms=int((time.monotonic() - t0) * 1000),
                        error=f"无法连接本地 WebUI ({self.webui_url}): {exc}",
                    )

            # Clear probe session so agy opens a fresh connection through the newly selected proxy node
            try:
                await session.post(
                    f"{self.webui_url}/api/actions/reset-sessions",
                    json={"session_key": "proxy_guard:probe"},
                )
            except Exception:
                pass

            try:
                async with session.post(
                    f"{self.webui_url}/api/playground/chat",
                    json={
                        "message": "只回复ok两个字母",
                        "session_key": "proxy_guard:probe",
                        "allow_fallback": False,
                    },
                ) as pr:
                    raw = await pr.text(errors="replace")
                    elapsed = int((time.monotonic() - t0) * 1000)
                    try:
                        data = json.loads(raw)
                    except Exception:
                        return ProbeOutcome(ok=False, elapsed_ms=elapsed, error=f"HTTP {pr.status}: {raw[:160]}")
                    if pr.status == 200 and data.get("ok") and str(data.get("response") or "").strip():
                        return ProbeOutcome(
                            ok=True,
                            elapsed_ms=elapsed,
                            response=str(data.get("response") or "").strip()[:80],
                            geo_blocked=False,
                        )
                    err = str(data.get("error") or f"HTTP {pr.status} 空响应")
                    return ProbeOutcome(
                        ok=False,
                        elapsed_ms=elapsed,
                        error=err,
                        geo_blocked=is_geo_blocked_error(err),
                    )
            except asyncio.TimeoutError:
                return ProbeOutcome(
                    ok=False,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=f"探测超时 ({self.timeout_seconds:.0f}s)",
                )
            except Exception as exc:
                return ProbeOutcome(
                    ok=False,
                    elapsed_ms=int((time.monotonic() - t0) * 1000),
                    error=str(exc),
                )


def _resolve_controller(args: argparse.Namespace) -> ProxyControllerInfo:
    if args.controller:
        url = args.controller.strip()
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        return ProxyControllerInfo(
            proxy_port=args.port,
            controller_url=url.rstrip("/"),
            secret=args.secret or "",
        )
    ctl = discover_proxy_controller(args.port)
    if ctl is None:
        raise RuntimeError(
            f"未自动发现监听端口 {args.port} 的 Clash/Mihomo 外部控制器（请确认代理软件已运行，或手动传入 --controller / --secret）"
        )
    return ctl


async def run_once(args: argparse.Namespace, *, geo_blocked_cache: dict[str, float] | None = None) -> int:
    ctl = _resolve_controller(args)
    print(f"[代理控制器] port={ctl.proxy_port} | ctl={ctl.controller_url} | proc={ctl.process_name}({ctl.pid})")

    node_filter = None if getattr(args, "all_nodes", False) else is_allowed_proxy_node
    group_name, current_node, all_nodes = await fetch_selector_group(ctl, preferred_group=args.group)
    nodes = [(n, t) for n, t in all_nodes if (node_filter is None or node_filter(n))]
    print(
        f"[策略组] {group_name} | 当前节点: {current_node} | 白名单节点: {len(nodes)}/{len(all_nodes)}"
        + ("（台湾带AI / 日本带AI / 美国凤凰城）" if node_filter is not None else "（全部节点）")
    )

    if args.check_only:
        latencies = await benchmark_all_nodes(
            ctl,
            nodes,
            test_url=args.test_url,
            timeout_ms=args.delay_timeout_ms,
        )
        ok_cnt = sum(1 for x in latencies if x.ok)
        print(f"\n=== 白名单 {len(latencies)} 个节点延迟测速结果（可达 {ok_cnt} 个）===")
        for idx, item in enumerate(latencies, start=1):
            mark = "★" if item.name == current_node else " "
            if item.ok:
                print(f" {mark} {idx:02d}. {item.delay_ms:5d} ms | [{item.node_type:10s}] {item.name}")
            else:
                print(f" {mark} {idx:02d}.  FAIL   | [{item.node_type:10s}] {item.name} ({item.error})")
        return 0

    prober = WebUIAntigravityProber(
        args.webui_url,
        args.webui_user,
        args.webui_pass,
        timeout_seconds=args.probe_timeout,
    )

    now_mono = time.monotonic()
    active_geo_blocked: set[str] = set()
    if geo_blocked_cache is not None:
        for k in list(geo_blocked_cache.keys()):
            if geo_blocked_cache[k] <= now_mono:
                geo_blocked_cache.pop(k, None)
        active_geo_blocked = set(geo_blocked_cache.keys())

    cur_allowed = node_filter is None or node_filter(current_node)
    if not args.force:
        if not cur_allowed:
            print(f"[白名单校验] 当前节点「{current_node}」不在白名单（台湾带AI / 日本带AI / 美国凤凰城）内，准备切换...")
        else:
            print(f"[健康检测] 正在使用主模型检测当前节点「{current_node}」到反重力上游的真实连通性...")
            cur_probe = await prober.probe()
            if cur_probe.ok and not cur_probe.geo_blocked:
                print(f"[正常] 当前节点「{current_node}」可用 ({cur_probe.elapsed_ms}ms，返回: {cur_probe.response!r})，无需切换。")
                return 0
            print(f"[异常] 当前节点「{current_node}」不可用: {cur_probe.error}")
            if cur_probe.geo_blocked:
                active_geo_blocked.add(current_node)
                if geo_blocked_cache is not None:
                    geo_blocked_cache[current_node] = time.monotonic() + 1800.0
            print("[切换] 开始并发测速白名单节点，按「无地区不可用优先 → 延迟从低到高」校验反重力主模型...")
    else:
        print("[强制测速] 并发测速白名单节点，按「无地区不可用优先 → 延迟从低到高」校验反重力主模型...")

    res = await select_best_working_node(
        ctl,
        probe_fn=prober.probe,
        preferred_group=args.group,
        skip_current_if_broken=(not args.force),
        node_filter=node_filter,
        geo_blocked_nodes=active_geo_blocked,
        test_url=args.test_url,
        delay_timeout_ms=args.delay_timeout_ms,
    )

    if geo_blocked_cache is not None:
        exp = time.monotonic() + 1800.0
        for gnode in res.geo_blocked_nodes:
            geo_blocked_cache[gnode] = exp
        if res.ok and res.selected_node:
            geo_blocked_cache.pop(res.selected_node, None)

    print(f"\n[测速汇总] 总节点={res.total_nodes} | 白名单={res.filtered_nodes} | TCP/HTTP可达={res.reachable_nodes}")
    for item in res.latencies[:12]:
        d_str = f"{item.delay_ms:5d} ms" if item.ok else "  FAIL  "
        print(f"  - {d_str} | {item.name}")

    print("\n[反重力主模型逐个校验记录]")
    for t in res.tried_nodes:
        if t["ok"]:
            status = "✓ 可用"
        elif t.get("geo_blocked"):
            status = f"✕ 地区不可用 ({t.get('error', '')})"
        else:
            status = f"✕ 失败 ({t.get('error', '')})"
        print(f"  - {t['delay_ms']:4d} ms | {t['node']} -> {status}")

    if res.ok:
        print(
            f"\n[切换成功] 策略组「{res.group}」已从「{res.previous_node}」切换至无地区限制且延迟最低的节点："
            f"「{res.selected_node}」(测速 {res.selected_delay_ms}ms，主模型响应 {res.probe_elapsed_ms}ms)"
        )
        return 0

    print(f"\n[切换失败] {res.error}", file=sys.stderr)
    return 1


async def run_watch(args: argparse.Namespace) -> int:
    log_path = Path(args.log_file)
    print(
        f"[守护模式已启动] 端口={args.port} | 监控日志={log_path} | 定时巡检间隔={args.interval}s | 冷却={args.cooldown}s"
    )
    last_pos = log_path.stat().st_size if log_path.is_file() else 0
    last_failover_mono = 0.0
    next_periodic_mono = time.monotonic() + max(15.0, args.interval)
    geo_blocked_cache: dict[str, float] = {}

    while True:
        trigger_reason = ""
        # 1) Watch bot.log for real-time proxy/geo/timeout errors
        if log_path.is_file():
            try:
                cur_size = log_path.stat().st_size
                if cur_size < last_pos:
                    last_pos = 0
                if cur_size > last_pos:
                    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
                        fh.seek(last_pos)
                        chunk = fh.read()
                        last_pos = fh.tell()
                    for line in chunk.splitlines():
                        if ("ERROR" in line or "WARNING" in line) and is_proxy_or_geo_error(line):
                            trigger_reason = f"日志捕获上游异常: {line.strip()[:140]}"
                            break
            except Exception as exc:
                print(f"[警告] 读取日志异常: {exc}")

        now_mono = time.monotonic()
        if not trigger_reason and now_mono >= next_periodic_mono:
            trigger_reason = "定时巡检"
            next_periodic_mono = now_mono + max(15.0, args.interval)

        if trigger_reason:
            if trigger_reason != "定时巡检" and (now_mono - last_failover_mono) < args.cooldown:
                await asyncio.sleep(1.0)
                continue
            print(f"\n[{time.strftime('%H:%M:%S')}] 触发检测 ({trigger_reason})")
            try:
                args.force = False
                rc = await run_once(args, geo_blocked_cache=geo_blocked_cache)
                if rc == 0:
                    last_failover_mono = time.monotonic()
            except Exception as exc:
                print(f"[错误] 节点检测/切换异常: {exc}")
            next_periodic_mono = time.monotonic() + max(15.0, args.interval)

        await asyncio.sleep(1.5)


def main() -> int:
    defaults = _load_bot_defaults(ROOT / "config.json")
    parser = argparse.ArgumentParser(description="检测 7890 代理所有节点到 Antigravity 上游连通性并自动切换最低延迟可用节点")
    parser.add_argument("--port", type=int, default=defaults["proxy_port"], help="本地代理端口（默认 7890）")
    parser.add_argument("--controller", default="", help="手动指定 Clash/Mihomo 控制器地址（留空则自动发现）")
    parser.add_argument("--secret", default="", help="手动指定 Clash/Mihomo 控制器 secret（留空则自动发现）")
    parser.add_argument("--group", default="", help="指定策略组名称（留空则自动选择主 Selector 策略组）")
    parser.add_argument(
        "--test-url",
        default="https://cloudcode-pa.googleapis.com/generate_204",
        help="初筛测速 URL（默认 Google CloudCode/Antigravity 上游）",
    )
    parser.add_argument("--delay-timeout-ms", type=int, default=4500, help="单节点延迟测速超时（毫秒）")
    parser.add_argument("--probe-timeout", type=float, default=35.0, help="反重力上游真实连通性探测超时（秒）")
    parser.add_argument("--webui-url", default=defaults["webui_url"], help="机器人本地 WebUI 地址")
    parser.add_argument("--webui-user", default=defaults["webui_user"], help="WebUI 用户名")
    parser.add_argument("--webui-pass", default=defaults["webui_pass"], help="WebUI 密码")
    parser.add_argument("--check-only", action="store_true", help="仅测速展示白名单节点延迟，不切换节点")
    parser.add_argument("--all-nodes", action="store_true", help="忽略白名单过滤，检测策略组内全部节点")
    parser.add_argument("--force", action="store_true", help="忽略当前节点状态，强制全量测速并切至无地区限制且延迟最低的节点")
    parser.add_argument("--watch", action="store_true", help="持续守护模式：实时监控 bot.log + 定时巡检，报错即切")
    parser.add_argument("--log-file", default=str(ROOT / "bot.log"), help="--watch 监控的日志路径")
    parser.add_argument("--interval", type=float, default=180.0, help="--watch 定时巡检间隔（秒）")
    parser.add_argument("--cooldown", type=float, default=45.0, help="--watch 两次故障切换间的最小冷却（秒）")
    args = parser.parse_args()

    try:
        if args.watch:
            return asyncio.run(run_watch(args))
        return asyncio.run(run_once(args))
    except KeyboardInterrupt:
        print("\n已停止。")
        return 0
    except Exception as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
