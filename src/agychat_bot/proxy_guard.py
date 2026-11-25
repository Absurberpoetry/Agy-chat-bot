from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp
from yarl import URL

logger = logging.getLogger(__name__)

_META_PROXY_TYPES = {
    "Selector",
    "URLTest",
    "Fallback",
    "LoadBalance",
    "Direct",
    "Reject",
    "RejectDrop",
    "Compatible",
    "Pass",
}

_GOOGLE_HOST_KEYWORDS = (
    "googleapis.com",
    "google.com",
    "antigravity.google",
    "gstatic.com",
    "googleusercontent.com",
)

_GEO_BLOCKED_ERROR_PATTERNS = (
    "user location is not supported",
    "location is not supported",
    "failed_precondition",
    "unsupported region",
    "region is not supported",
    "country is not supported",
)

_PROXY_OR_GEO_ERROR_PATTERNS = (
    *_GEO_BLOCKED_ERROR_PATTERNS,
    "timed out",
    "timeout",
    "超时",
    "connection refused",
    "connection reset",
    "connection aborted",
    "broken pipe",
    "unexpected eof",
    "eof",
    "tls",
    "ssl",
    "proxy",
    "network is unreachable",
    "no route to host",
    "name or service not known",
    "temporary failure in name resolution",
    "getaddrinfo",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
    "no capacity",
    "upstream connect error",
)

_AUTH_REQUIRED_ERROR_PATTERNS = (
    "authentication failed",
    "authentication timed out",
    "authentication required",
    "not logged into antigravity",
    "not authenticated",
    "please sign in",
    "unauthenticated",
)

_NON_PROXY_ERROR_PATTERNS = (
    *_AUTH_REQUIRED_ERROR_PATTERNS,
    "terms of service",
    "account suspended",
    "account disabled",
    "tos violation",
    "invalid_grant",
)

_AI_TOKEN_RE = re.compile(r"(?<![a-z0-9])ai(?![a-z0-9])", re.IGNORECASE)
_EXCLUDED_REGION_RE = re.compile(
    r"(香港|澳门|澳門|俄罗斯|俄羅斯|(?<![a-z])(?:hk|mo|ru)(?![a-z]))",
    re.IGNORECASE,
)
_TAIWAN_RE = re.compile(r"(台湾|台灣|taiwan|🇹🇼|(?<![a-z])tw(?![a-z]))", re.IGNORECASE)
_JAPAN_RE = re.compile(r"(日本|japan|🇯🇵|(?<![a-z])jp(?![a-z]))", re.IGNORECASE)
_PHOENIX_RE = re.compile(r"(凤凰城|鳳凰城|phoenix|(?<![a-z])phx(?![a-z]))", re.IGNORECASE)


def is_auth_required_error(error_text: str) -> bool:
    """Return True if the error indicates the local agy CLI is not signed in or auth timed out."""
    low = (error_text or "").strip().lower()
    if not low:
        return False
    return any(p in low for p in _AUTH_REQUIRED_ERROR_PATTERNS)


def is_geo_blocked_error(error_text: str) -> bool:
    """Return True if the error explicitly indicates regional / location unavailability."""
    low = (error_text or "").strip().lower()
    if not low:
        return False
    return any(p in low for p in _GEO_BLOCKED_ERROR_PATTERNS)


def is_allowed_proxy_node(name: str) -> bool:
    """Restrict failover candidates to Taiwan(AI), Japan(AI), and US Phoenix nodes only."""
    raw = (name or "").strip()
    if not raw or _EXCLUDED_REGION_RE.search(raw):
        return False
    if _PHOENIX_RE.search(raw):
        return True
    has_ai = bool(_AI_TOKEN_RE.search(raw))
    if has_ai and (_TAIWAN_RE.search(raw) or _JAPAN_RE.search(raw)):
        return True
    return False


def proxy_node_region_priority(name: str) -> int:
    """Region priority order: Taiwan(AI) first (0), Japan(AI) second (1), US Phoenix/other fallback (2)."""
    raw = (name or "").strip()
    if _TAIWAN_RE.search(raw):
        return 0
    if _JAPAN_RE.search(raw):
        return 1
    return 2


@dataclass(slots=True)
class ProxyControllerInfo:
    proxy_port: int
    controller_url: str
    secret: str = ""
    pid: int = 0
    process_name: str = ""


@dataclass(slots=True)
class NodeLatency:
    name: str
    node_type: str
    delay_ms: int | None
    ok: bool
    error: str = ""


@dataclass(slots=True)
class ProbeOutcome:
    ok: bool
    elapsed_ms: int = 0
    error: str = ""
    response: str = ""
    geo_blocked: bool = False


@dataclass(slots=True)
class ProxyFailoverResult:
    ok: bool
    group: str = ""
    previous_node: str = ""
    selected_node: str = ""
    selected_delay_ms: int | None = None
    probe_elapsed_ms: int = 0
    total_nodes: int = 0
    filtered_nodes: int = 0
    reachable_nodes: int = 0
    tried_nodes: list[dict[str, Any]] = field(default_factory=list)
    geo_blocked_nodes: list[str] = field(default_factory=list)
    latencies: list[NodeLatency] = field(default_factory=list)
    error: str = ""


ProbeFn = Callable[[], Awaitable[ProbeOutcome]]
NodeFilterFn = Callable[[str], bool]


def is_proxy_or_geo_error(error_text: str) -> bool:
    """Return True if an upstream Antigravity error is caused by geo-blocking, timeout, or proxy/network failure."""
    low = (error_text or "").strip().lower()
    if not low:
        return False
    if any(p in low for p in _NON_PROXY_ERROR_PATTERNS):
        return False
    return any(p in low for p in _PROXY_OR_GEO_ERROR_PATTERNS)


def extract_local_proxy_port(proxy_url: str) -> int | None:
    """Extract port if proxy_url points to localhost/127.0.0.1, else None."""
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    return parsed.port


def parse_controller_from_cmdline(cmdline: str, *, proxy_port: int = 7890, pid: int = 0, name: str = "") -> ProxyControllerInfo | None:
    """Parse Mihomo/Clash `-ext-ctl` and `-secret` flags from a process command line."""
    if not cmdline:
        return None
    m_ctl = re.search(r"(?:--?ext-ctl|--?external-controller)(?:\s+|=)[\"']?([^\s\"']+)[\"']?", cmdline)
    if not m_ctl:
        return None
    ctl = m_ctl.group(1).strip()
    if not ctl.startswith(("http://", "https://")):
        if ctl.startswith(":"):
            ctl = "127.0.0.1" + ctl
        ctl = "http://" + ctl
    # Rewrite wildcard bind hosts (0.0.0.0 / [::] / ::) to 127.0.0.1 for local HTTP client access
    parsed_ctl = urlparse(ctl)
    if (parsed_ctl.hostname or "") in ("0.0.0.0", "::", "*"):
        port_part = f":{parsed_ctl.port}" if parsed_ctl.port else ""
        ctl = f"{parsed_ctl.scheme or 'http'}://127.0.0.1{port_part}{parsed_ctl.path or ''}"
    m_sec = re.search(r"(?:--?secret)(?:\s+|=)[\"']?([^\s\"']+)[\"']?", cmdline)
    secret = m_sec.group(1).strip() if m_sec else ""
    return ProxyControllerInfo(
        proxy_port=proxy_port,
        controller_url=ctl.rstrip("/"),
        secret=secret,
        pid=pid,
        process_name=name,
    )


def discover_proxy_controller(proxy_port: int = 7890) -> ProxyControllerInfo | None:
    """Discover the Mihomo/Clash external controller URL & secret of the process listening on `proxy_port`."""
    if os.name != "nt":
        return None

    ps_script = (
        f"$ProgressPreference='SilentlyContinue';"
        f"$conns = Get-NetTCPConnection -LocalPort {int(proxy_port)} -State Listen -ErrorAction SilentlyContinue;"
        f"$pids = @($conns | Select-Object -ExpandProperty OwningProcess -Unique);"
        f"foreach ($p in $pids) {{"
        f"  $w = Get-CimInstance Win32_Process -Filter \"ProcessId=$p\" -ErrorAction SilentlyContinue;"
        f"  if ($w) {{ Write-Output (ConvertTo-Json @{{pid=$w.ProcessId;name=$w.Name;cmd=$w.CommandLine}} -Compress) }}"
        f"}}"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
        )
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            info = parse_controller_from_cmdline(
                str(obj.get("cmd") or ""),
                proxy_port=proxy_port,
                pid=int(obj.get("pid") or 0),
                name=str(obj.get("name") or ""),
            )
            if info is not None:
                return info
    except Exception as exc:
        logger.debug("探测端口 %d 代理控制器失败: %s", proxy_port, exc)
    return None


def _ctl_headers(ctl: ProxyControllerInfo) -> dict[str, str]:
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if ctl.secret:
        headers["Authorization"] = f"Bearer {ctl.secret}"
    return headers


async def fetch_selector_group(
    ctl: ProxyControllerInfo,
    *,
    preferred_group: str = "",
) -> tuple[str, str, list[tuple[str, str]]]:
    """Return `(group_name, current_node, [(node_name, node_type), ...])` for the active Selector group."""
    timeout = aiohttp.ClientTimeout(total=8.0)
    async with aiohttp.ClientSession(headers=_ctl_headers(ctl), timeout=timeout, trust_env=False) as session:
        async with session.get(f"{ctl.controller_url}/proxies") as resp:
            if resp.status != 200:
                raise RuntimeError(f"读取代理列表失败: HTTP {resp.status}")
            payload = await resp.json()

    proxies = payload.get("proxies") if isinstance(payload, dict) else None
    if not isinstance(proxies, dict):
        raise RuntimeError("代理控制器返回的 proxies 数据格式无效")

    group_name = preferred_group.strip()
    if group_name and group_name in proxies:
        grp = proxies[group_name]
    else:
        # Pick the primary non-GLOBAL Selector group with the most nodes
        selectors: list[tuple[int, str, dict[str, Any]]] = []
        for name, item in proxies.items():
            if not isinstance(item, dict) or item.get("type") != "Selector":
                continue
            if name.upper() == "GLOBAL":
                continue
            all_list = item.get("all") or []
            selectors.append((len(all_list), name, item))
        if not selectors and "GLOBAL" in proxies and isinstance(proxies["GLOBAL"], dict):
            group_name = "GLOBAL"
            grp = proxies["GLOBAL"]
        elif selectors:
            selectors.sort(key=lambda x: x[0], reverse=True)
            _, group_name, grp = selectors[0]
        else:
            raise RuntimeError("未在代理控制器中找到可切换的 Selector 策略组")

    current_node = str(grp.get("now") or "")
    real_nodes: list[tuple[str, str]] = []
    for n in grp.get("all") or []:
        n_str = str(n)
        item = proxies.get(n_str)
        ntype = str(item.get("type") or "") if isinstance(item, dict) else ""
        if ntype in _META_PROXY_TYPES or n_str.upper() in ("DIRECT", "REJECT", "PASS"):
            continue
        real_nodes.append((n_str, ntype))

    return group_name, current_node, real_nodes


async def benchmark_all_nodes(
    ctl: ProxyControllerInfo,
    nodes: list[tuple[str, str]],
    *,
    test_url: str = "https://cloudcode-pa.googleapis.com/generate_204",
    timeout_ms: int = 4500,
    concurrency: int = 16,
) -> list[NodeLatency]:
    """Benchmark all real proxy nodes concurrently via Mihomo `/proxies/{name}/delay` without switching traffic."""
    if not nodes:
        return []

    sem = asyncio.Semaphore(max(1, concurrency))
    http_timeout = aiohttp.ClientTimeout(total=max(5.0, (timeout_ms / 1000.0) + 2.5))

    async with aiohttp.ClientSession(headers=_ctl_headers(ctl), timeout=http_timeout, trust_env=False) as session:
        async def _test_one(name: str, ntype: str) -> NodeLatency:
            async with sem:
                enc = urllib.parse.quote(name, safe="")
                q = urllib.parse.urlencode({"timeout": int(timeout_ms), "url": test_url})
                url = URL(f"{ctl.controller_url}/proxies/{enc}/delay?{q}", encoded=True)
                try:
                    async with session.get(url) as resp:
                        body = await resp.json(content_type=None)
                        if resp.status == 200 and isinstance(body, dict):
                            delay = body.get("delay")
                            if isinstance(delay, (int, float)) and delay > 0:
                                return NodeLatency(name=name, node_type=ntype, delay_ms=int(delay), ok=True)
                        err = (
                            str(body.get("message") or body)
                            if isinstance(body, dict)
                            else f"HTTP {resp.status}"
                        )
                        return NodeLatency(name=name, node_type=ntype, delay_ms=None, ok=False, error=err)
                except Exception as exc:
                    return NodeLatency(name=name, node_type=ntype, delay_ms=None, ok=False, error=str(exc))

        results = await asyncio.gather(*(_test_one(n, t) for n, t in nodes))

    results.sort(key=lambda r: (0 if r.ok else 1, r.delay_ms if r.delay_ms is not None else 999999, r.name))
    return results


async def switch_proxy_node(
    ctl: ProxyControllerInfo,
    group_name: str,
    node_name: str,
    *,
    close_google_connections: bool = True,
) -> None:
    """Switch the Mihomo Selector group to `node_name` and optionally flush stale Google connections."""
    timeout = aiohttp.ClientTimeout(total=8.0)
    enc_group = urllib.parse.quote(group_name, safe="")
    async with aiohttp.ClientSession(headers=_ctl_headers(ctl), timeout=timeout, trust_env=False) as session:
        async with session.put(URL(f"{ctl.controller_url}/proxies/{enc_group}", encoded=True), json={"name": node_name}) as resp:
            if resp.status not in (200, 204):
                txt = await resp.text(errors="replace")
                raise RuntimeError(f"切换节点失败 ({node_name}): HTTP {resp.status} {txt[:200]}")

        if close_google_connections:
            try:
                async with session.get(f"{ctl.controller_url}/connections") as c_resp:
                    if c_resp.status == 200:
                        cdata = await c_resp.json(content_type=None)
                        conns = cdata.get("connections") if isinstance(cdata, dict) else None
                        if isinstance(conns, list):
                            to_close: list[str] = []
                            for conn in conns:
                                if not isinstance(conn, dict):
                                    continue
                                meta = conn.get("metadata") or {}
                                host = str(meta.get("host") or meta.get("sniffHost") or "").lower()
                                if any(k in host for k in _GOOGLE_HOST_KEYWORDS):
                                    cid = str(conn.get("id") or "")
                                    if cid:
                                        to_close.append(cid)
                            if to_close:
                                async def _close_one(conn_id: str) -> None:
                                    del_url = URL(
                                        f"{ctl.controller_url}/connections/{urllib.parse.quote(conn_id, safe='')}",
                                        encoded=True,
                                    )
                                    async with session.delete(del_url) as _dresp:
                                        await _dresp.read()

                                await asyncio.gather(
                                    *(_close_one(cid) for cid in to_close),
                                    return_exceptions=True,
                                )
            except Exception as exc:
                logger.debug("清理旧 Google 代理连接时忽略异常: %s", exc)


async def select_best_working_node(
    ctl: ProxyControllerInfo,
    *,
    probe_fn: ProbeFn,
    preferred_group: str = "",
    skip_current_if_broken: bool = True,
    node_filter: NodeFilterFn | None = is_allowed_proxy_node,
    geo_blocked_nodes: set[str] | list[str] | tuple[str, ...] | None = None,
    test_url: str = "https://cloudcode-pa.googleapis.com/generate_204",
    delay_timeout_ms: int = 4500,
    settle_seconds: float = 0.4,
    max_probes: int = 8,
) -> ProxyFailoverResult:
    """Benchmark allowed nodes on the proxy controller, prioritize nodes NOT marked geo-blocked
    sorted from lowest latency upward, verify real primary-model Antigravity connectivity,
    and lock onto the lowest-latency non-geo-blocked working node."""
    group_name, current_node, all_nodes = await fetch_selector_group(ctl, preferred_group=preferred_group)
    candidate_nodes = [(n, t) for n, t in all_nodes if (node_filter is None or node_filter(n))]

    result = ProxyFailoverResult(
        ok=False,
        group=group_name,
        previous_node=current_node,
        total_nodes=len(all_nodes),
        filtered_nodes=len(candidate_nodes),
        reachable_nodes=0,
    )
    if not candidate_nodes:
        result.error = f"策略组「{group_name}」中没有符合白名单规则（台湾带AI / 日本带AI / 美国凤凰城）的候选节点"
        return result

    latencies = await benchmark_all_nodes(
        ctl,
        candidate_nodes,
        test_url=test_url,
        timeout_ms=delay_timeout_ms,
    )
    reachable = [x for x in latencies if x.ok]
    result.reachable_nodes = len(reachable)
    result.latencies = latencies

    if not reachable:
        result.error = f"策略组「{group_name}」全部 {len(candidate_nodes)} 个白名单节点均不可达或超时"
        return result

    known_geo_blocked = {str(x).strip() for x in (geo_blocked_nodes or ()) if str(x).strip()}

    # Selection priority:
    #   Tier 0: Not known geo-blocked AND not the currently broken node
    #   Tier 1: Currently broken node (if not known geo-blocked and skip_current_if_broken=True)
    #   Tier 2: Previously geo-blocked nodes (only tried if all clean nodes failed)
    # Within each tier, strictly prefer Taiwan(AI) first (0), then Japan(AI) (1), then Phoenix (2),
    # and within the same region sort by lowest latency first.
    def _rank(item: NodeLatency) -> tuple[int, int, int, str]:
        is_geo = item.name in known_geo_blocked
        is_cur_broken = skip_current_if_broken and item.name == current_node
        if not is_geo and not is_cur_broken:
            tier = 0
        elif not is_geo and is_cur_broken:
            tier = 1
        else:
            tier = 2
        region_prio = proxy_node_region_priority(item.name)
        return (tier, region_prio, item.delay_ms if item.delay_ms is not None else 999999, item.name)

    ordered = sorted(reachable, key=_rank)
    if max_probes > 0 and len(ordered) > max_probes:
        ordered = ordered[:max_probes]
    newly_geo_blocked: list[str] = []

    for cand in ordered:
        try:
            await switch_proxy_node(ctl, group_name, cand.name, close_google_connections=True)
        except Exception as exc:
            result.tried_nodes.append(
                {"node": cand.name, "delay_ms": cand.delay_ms, "ok": False, "geo_blocked": False, "error": str(exc)}
            )
            continue

        if settle_seconds > 0:
            await asyncio.sleep(settle_seconds)

        t0 = time.monotonic()
        try:
            probe = await probe_fn()
        except Exception as exc:
            err_s = str(exc)
            probe = ProbeOutcome(
                ok=False,
                elapsed_ms=int((time.monotonic() - t0) * 1000),
                error=err_s,
                geo_blocked=is_geo_blocked_error(err_s),
            )

        geo_hit = bool(probe.geo_blocked or (not probe.ok and is_geo_blocked_error(probe.error)))
        if geo_hit and cand.name not in newly_geo_blocked:
            newly_geo_blocked.append(cand.name)

        result.tried_nodes.append(
            {
                "node": cand.name,
                "delay_ms": cand.delay_ms,
                "ok": probe.ok,
                "geo_blocked": geo_hit,
                "probe_ms": probe.elapsed_ms,
                "error": probe.error,
            }
        )
        if probe.ok and not geo_hit:
            result.ok = True
            result.selected_node = cand.name
            result.selected_delay_ms = cand.delay_ms
            result.probe_elapsed_ms = probe.elapsed_ms
            result.geo_blocked_nodes = newly_geo_blocked
            return result

    result.geo_blocked_nodes = newly_geo_blocked
    # Restore previous node if all candidates failed real Antigravity verification
    if current_node:
        try:
            await switch_proxy_node(ctl, group_name, current_node, close_google_connections=False)
        except Exception:
            pass
    result.error = f"已按「无地区限制优先 + 低延迟优先」实测全部 {len(ordered)} 个白名单可达节点，均未通过反重力主模型连通性校验"
    return result
