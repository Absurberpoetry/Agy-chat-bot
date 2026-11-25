from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable
from zoneinfo import ZoneInfo

from ..config import ScheduledTaskConfig, ScheduledTriggerConfig

logger = logging.getLogger(__name__)

OnScheduledFireFn = Callable[[ScheduledTaskConfig, dt.datetime], Awaitable[int]]


def resolve_timezone(tz_name: str) -> dt.tzinfo:
    name = (tz_name or "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        upper = name.upper()
        if upper in ("ASIA/SHANGHAI", "ASIA/CHONGQING", "ASIA/BEIJING", "PRC", "CST", "UTC+8", "GMT+8"):
            return dt.timezone(dt.timedelta(hours=8))
        if upper in ("UTC", "GMT", "Z"):
            return dt.timezone.utc
        return dt.datetime.now().astimezone().tzinfo or dt.timezone(dt.timedelta(hours=8))


def _parse_cron_field(expr: str, min_val: int, max_val: int, *, is_dow: bool = False) -> set[int]:
    """Parse a single 5-field cron component (*, */n, a, a-b, a-b/n, comma-separated)."""
    result: set[int] = set()
    raw = (expr or "").strip()
    if not raw:
        raise ValueError("Cron 字段不能为空")
    max_allowed = 7 if is_dow else max_val
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"无效步长: {part}")
        else:
            base = part

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            start, end = int(s_str), int(e_str)
            if not (min_val <= start <= max_allowed) or not (min_val <= end <= max_allowed):
                raise ValueError(f"Cron 范围越界: {part}")
            if is_dow and start == 7 and end < 7:
                start = 0
            if start > end:
                raise ValueError(f"无效范围: {part}")
        else:
            val = int(base)
            if not (min_val <= val <= max_allowed):
                raise ValueError(f"Cron 数值越界: {part}")
            if is_dow and val == 7:
                val = 0
            start = end = val

        for v in range(start, end + 1, step):
            if is_dow and v == 7:
                v = 0
            if min_val <= v <= max_val:
                result.add(v)
    if not result:
        raise ValueError(f"Cron 字段未匹配任何值: {expr}")
    return result


def cron_matches(cron_expr: str, when: dt.datetime) -> bool:
    """Return True if a 5-field cron expression (min hour dom month dow) matches `when`.

    Standard cron dow: 0 or 7 = Sunday, 1 = Monday, ..., 6 = Saturday.
    """
    parts = (cron_expr or "").strip().split()
    if len(parts) != 5:
        raise ValueError(f"Cron 表达式必须为 5 段 (分 时 日 月 周)，当前为: {cron_expr!r}")

    minutes = _parse_cron_field(parts[0], 0, 59)
    hours = _parse_cron_field(parts[1], 0, 23)
    doms = _parse_cron_field(parts[2], 1, 31)
    months = _parse_cron_field(parts[3], 1, 12)
    dows = _parse_cron_field(parts[4], 0, 6, is_dow=True)

    # Convert Python weekday() (Mon=0..Sun=6) to standard cron dow (Sun=0, Mon=1..Sat=6)
    cron_dow = (when.weekday() + 1) % 7

    if when.minute not in minutes or when.hour not in hours or when.month not in months:
        return False

    dom_star = parts[2].strip() == "*"
    dow_star = parts[4].strip() == "*"
    if dom_star and dow_star:
        return True
    if dom_star:
        return cron_dow in dows
    if dow_star:
        return when.day in doms
    # Standard cron semantics when both dom and dow are restricted: either matches
    return (when.day in doms) or (cron_dow in dows)


def validate_cron(cron_expr: str) -> tuple[bool, str]:
    try:
        cron_matches(cron_expr, dt.datetime(2026, 1, 1, 0, 0))
        return True, ""
    except Exception as exc:
        return False, str(exc)


class ScheduledTriggerRunner:
    """Background asyncio scheduler for Cron and fixed-interval proactive triggers."""

    def __init__(
        self,
        config: ScheduledTriggerConfig,
        *,
        timezone: str = "Asia/Shanghai",
        on_fire: OnScheduledFireFn,
        tick_seconds: float = 5.0,
    ) -> None:
        self._config = config
        self._tz = resolve_timezone(timezone)
        self._on_fire = on_fire
        self._tick_seconds = max(0.5, float(tick_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._last_cron_minute: dict[str, tuple[int, int, int, int, int]] = {}
        self._last_interval_mono: dict[str, float] = {}
        self._inflight: set[asyncio.Task[None]] = set()
        self._started_mono = time.monotonic()

    def update_config(self, config: ScheduledTriggerConfig, *, timezone: str = "Asia/Shanghai") -> None:
        self._config = config
        self._tz = resolve_timezone(timezone)
        valid_ids = {t.id for t in config.tasks}
        self._last_cron_minute = {k: v for k, v in self._last_cron_minute.items() if k in valid_ids}
        self._last_interval_mono = {k: v for k, v in self._last_interval_mono.items() if k in valid_ids}

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._started_mono = time.monotonic()
        self._task = asyncio.create_task(self._loop(), name="scheduled-triggers")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for t in list(self._inflight):
            t.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
            self._inflight.clear()

    def find_task(self, task_id: str) -> ScheduledTaskConfig | None:
        for t in self._config.tasks:
            if t.id == task_id:
                return t
        return None

    async def fire_now(self, task: ScheduledTaskConfig) -> int:
        now_dt = dt.datetime.now(self._tz)
        return await self._on_fire(task, now_dt)

    async def check_once(
        self,
        *,
        now_dt: dt.datetime | None = None,
        now_mono: float | None = None,
    ) -> list[str]:
        """Evaluate all enabled scheduled tasks once; returns list of fired task IDs."""
        if not self._config.enabled or not self._config.tasks:
            return []

        cur_dt = now_dt if now_dt is not None else dt.datetime.now(self._tz)
        cur_mono = time.monotonic() if now_mono is None else now_mono
        minute_key = (cur_dt.year, cur_dt.month, cur_dt.day, cur_dt.hour, cur_dt.minute)
        fired: list[str] = []

        for task in list(self._config.tasks):
            if not task.enabled or not task.prompt.strip():
                continue
            should_fire = False
            if task.schedule_type == "interval":
                last = self._last_interval_mono.get(task.id)
                if last is None:
                    # Wait one full interval after startup / task registration unless manually fired
                    self._last_interval_mono[task.id] = cur_mono
                elif (cur_mono - last) >= max(10.0, task.interval_seconds):
                    self._last_interval_mono[task.id] = cur_mono
                    should_fire = True
            else:
                if self._last_cron_minute.get(task.id) != minute_key:
                    try:
                        if cron_matches(task.cron, cur_dt):
                            self._last_cron_minute[task.id] = minute_key
                            should_fire = True
                    except Exception as exc:
                        logger.warning("定时任务 %s Cron 表达式无效 (%s): %s", task.id, task.cron, exc)

            if should_fire:
                fired.append(task.id)
                self._spawn_fire(task, cur_dt)

        return fired

    def _spawn_fire(self, task: ScheduledTaskConfig, when: dt.datetime) -> None:
        async def _run() -> None:
            try:
                sent = await self._on_fire(task, when)
                logger.info("定时触发 [%s/%s] 执行完成，覆盖 %d 个目标", task.id, task.name, sent)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("定时触发 [%s/%s] 执行失败", task.id, task.name)

        t = asyncio.create_task(_run(), name=f"sched-{task.id}")
        self._inflight.add(t)
        t.add_done_callback(self._inflight.discard)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("定时触发器轮询异常")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._tick_seconds)
            except asyncio.TimeoutError:
                pass
