from __future__ import annotations

import contextlib
import datetime as dt
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .models import RunnerResult

logger = logging.getLogger(__name__)


def _local_dt(ts: float) -> dt.datetime:
    try:
        val = float(ts)
        if val > 1e11:
            val /= 1000.0
        if val < 86400.0 or val > 4102444800.0:
            return dt.datetime.now()
        return dt.datetime.fromtimestamp(val)
    except (OSError, OverflowError, ValueError, TypeError):
        return dt.datetime.now()


def _parse_session_meta(session_key: str, scene: str = "", channel_id: str = "", display_name: str = "") -> tuple[str, str, str]:
    sk = (session_key or "").strip()
    sc = (scene or "").strip().lower()
    cid = (channel_id or "").strip()
    dname = (display_name or "").strip()

    if not sc:
        if sk.startswith("webui:"):
            sc = "playground"
        elif sk.startswith("subagent:"):
            sc = "subagent"
        elif ":group:" in sk:
            sc = "group"
        elif ":private:" in sk:
            sc = "private"
        else:
            sc = "other"

    if not cid:
        if ":group:" in sk:
            cid = sk.split(":group:", 1)[1].split(":u:", 1)[0]
        elif ":private:" in sk:
            cid = sk.split(":private:", 1)[1].split(":u:", 1)[0]
        elif sk.startswith("webui:"):
            cid = sk.split(":", 1)[1] or "playground"
        elif sk.startswith("subagent:"):
            cid = sk.split(":", 1)[1]
        else:
            cid = sk

    if not dname:
        if sc == "playground":
            dname = "工作台测试"
        elif sc == "subagent":
            dname = f"子代理 {cid}" if cid else "后台子代理"
        else:
            dname = cid or sk or "未知会话"

    return sc, cid, dname


def _row_to_record(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "timestamp": float(row[1]),
        "date_str": str(row[2]),
        "month_str": str(row[3]),
        "session_key": str(row[4]),
        "scene": str(row[5]),
        "channel_id": str(row[6]),
        "display_name": str(row[7]),
        "model": str(row[8]),
        "trigger_type": str(row[9]),
        "input_tokens": int(row[10]),
        "output_tokens": int(row[11]),
        "cache_read_tokens": int(row[12]),
        "cache_creation_tokens": int(row[13]),
        "thinking_tokens": int(row[14]),
        "total_tokens": int(row[15]),
        "cache_hit_rate": round(float(row[16]), 2),
        "ttft_seconds": round(float(row[17]), 3),
        "thinking_seconds": round(float(row[18]), 3),
        "duration_seconds": round(float(row[19]), 3),
        "tokens_per_second": round(float(row[20]), 1),
        "context_messages": int(row[21]),
        "context_tokens": int(row[22]),
        "turn_index": int(row[23]),
        "response_chars": int(row[24]),
    }


_RECORD_COLS = (
    "id, timestamp, date_str, month_str, session_key, scene, channel_id, display_name, "
    "model, trigger_type, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, "
    "thinking_tokens, total_tokens, cache_hit_rate, ttft_seconds, thinking_seconds, "
    "duration_seconds, tokens_per_second, context_messages, context_tokens, turn_index, response_chars"
)


class TokenUsageStore:
    """SQLite-backed per-request token usage & latency telemetry store."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @contextlib.contextmanager
    def _conn(self) -> Any:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=15.0)
        conn.execute("PRAGMA busy_timeout=5000")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS token_usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    date_str TEXT NOT NULL,
                    month_str TEXT NOT NULL,
                    session_key TEXT NOT NULL,
                    scene TEXT NOT NULL DEFAULT 'other',
                    channel_id TEXT NOT NULL DEFAULT '',
                    display_name TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT 'default',
                    trigger_type TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
                    thinking_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_hit_rate REAL NOT NULL DEFAULT 0.0,
                    ttft_seconds REAL NOT NULL DEFAULT 0.0,
                    thinking_seconds REAL NOT NULL DEFAULT 0.0,
                    duration_seconds REAL NOT NULL DEFAULT 0.0,
                    tokens_per_second REAL NOT NULL DEFAULT 0.0,
                    context_messages INTEGER NOT NULL DEFAULT 0,
                    context_tokens INTEGER NOT NULL DEFAULT 0,
                    turn_index INTEGER NOT NULL DEFAULT 1,
                    response_chars INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_usage_date_ts ON token_usage_logs(date_str, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_month_ts ON token_usage_logs(month_str, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_session_ts ON token_usage_logs(session_key, timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_usage_model_date ON token_usage_logs(model, date_str);
                """
            )
            conn.commit()

    def record(
        self,
        *,
        session_key: str,
        result: RunnerResult | None = None,
        scene: str = "",
        channel_id: str = "",
        display_name: str = "",
        model: str = "",
        trigger_type: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_creation_tokens: int | None = None,
        thinking_tokens: int | None = None,
        total_tokens: int | None = None,
        cache_hit_rate: float | None = None,
        ttft_seconds: float | None = None,
        thinking_seconds: float | None = None,
        duration_seconds: float | None = None,
        tokens_per_second: float | None = None,
        context_messages: int = 0,
        context_tokens: int = 0,
        turn_index: int | None = None,
        response_chars: int | None = None,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        ts = float(timestamp if timestamp is not None else time.time())
        ldt = _local_dt(ts)
        date_str = ldt.strftime("%Y-%m-%d")
        month_str = ldt.strftime("%Y-%m")

        norm_sk = (session_key or "unknown").strip() or "unknown"
        # Strip `:u:<uid>` suffix when grouping chat channel totals? Keep base channel session_key for group/private clarity
        base_sk = norm_sk.split(":u:", 1)[0] if (":group:" in norm_sk or ":private:" in norm_sk) else norm_sk
        sc, cid, dname = _parse_session_meta(base_sk, scene, channel_id, display_name)

        eff_model = (model or (result.model if result is not None else "") or "default").strip() or "default"
        in_tok = max(0, int(input_tokens if input_tokens is not None else (result.input_tokens if result else 0)))
        out_tok = max(0, int(output_tokens if output_tokens is not None else (result.output_tokens if result else 0)))
        cache_tok = max(0, int(cache_read_tokens if cache_read_tokens is not None else (result.cache_read_tokens if result else 0)))
        cache_create_tok = max(
            0,
            int(cache_creation_tokens if cache_creation_tokens is not None else (result.cache_creation_tokens if result else 0)),
        )
        think_tok = max(0, int(thinking_tokens if thinking_tokens is not None else (result.thinking_tokens if result else 0)))

        total_in = in_tok + cache_tok
        computed_total = total_in + cache_create_tok + out_tok
        raw_total = int(total_tokens if total_tokens is not None else (result.total_tokens if result else 0))
        tot_tok = max(computed_total, raw_total)

        if cache_hit_rate is not None:
            hit_rate = round(max(0.0, min(100.0, float(cache_hit_rate))), 2)
        elif result is not None and result.cache_hit_rate > 0:
            hit_rate = round(max(0.0, min(100.0, float(result.cache_hit_rate))), 2)
        else:
            hit_rate = round((cache_tok / total_in) * 100.0, 2) if (total_in > 0 and cache_tok > 0) else 0.0

        ttft_s = round(max(0.0, float(ttft_seconds if ttft_seconds is not None else (result.ttft_seconds if result else 0.0))), 3)
        think_s = round(max(0.0, float(thinking_seconds if thinking_seconds is not None else (result.thinking_seconds if result else 0.0))), 3)
        dur_s = round(max(0.0, float(duration_seconds if duration_seconds is not None else (result.duration_seconds if result else 0.0))), 3)

        if tokens_per_second is not None:
            tps = round(max(0.0, float(tokens_per_second)), 1)
        elif result is not None and result.tokens_per_second > 0:
            tps = round(max(0.0, float(result.tokens_per_second)), 1)
        else:
            eff_gen = max(0.05, dur_s - think_s) if dur_s > think_s + 0.05 else max(0.05, dur_s)
            tps = round(out_tok / eff_gen, 1) if (out_tok > 0 and dur_s > 0) else 0.0

        ctx_msgs = max(0, int(context_messages or 0))
        ctx_tok = max(0, int(context_tokens or 0))
        if total_in > ctx_tok:
            ctx_tok = total_in

        resp_len = int(
            response_chars
            if response_chars is not None
            else (len(result.response or "") if result is not None else 0)
        )

        with self._lock, self._conn() as conn:
            if turn_index is not None and int(turn_index) > 0:
                eff_turn = int(turn_index)
            elif result is not None and result.num_turns > 0:
                eff_turn = int(result.num_turns)
            else:
                cnt_row = conn.execute(
                    "SELECT COUNT(1) FROM token_usage_logs WHERE session_key = ?",
                    (base_sk,),
                ).fetchone()
                eff_turn = int(cnt_row[0] or 0) + 1 if cnt_row else 1

            cur = conn.execute(
                """
                INSERT INTO token_usage_logs (
                    timestamp, date_str, month_str, session_key, scene, channel_id, display_name,
                    model, trigger_type, input_tokens, output_tokens, cache_read_tokens,
                    cache_creation_tokens, thinking_tokens, total_tokens, cache_hit_rate,
                    ttft_seconds, thinking_seconds, duration_seconds, tokens_per_second,
                    context_messages, context_tokens, turn_index, response_chars
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    date_str,
                    month_str,
                    base_sk,
                    sc,
                    cid,
                    dname,
                    eff_model,
                    str(trigger_type or "").strip(),
                    in_tok,
                    out_tok,
                    cache_tok,
                    cache_create_tok,
                    think_tok,
                    tot_tok,
                    hit_rate,
                    ttft_s,
                    think_s,
                    dur_s,
                    tps,
                    ctx_msgs,
                    ctx_tok,
                    eff_turn,
                    resp_len,
                ),
            )
            conn.commit()
            row_id = int(cur.lastrowid or 0)
            row = conn.execute(
                f"SELECT {_RECORD_COLS} FROM token_usage_logs WHERE id = ?",
                (row_id,),
            ).fetchone()
            return _row_to_record(row) if row else {"id": row_id, "session_key": base_sk, "total_tokens": tot_tok}

    @staticmethod
    def _aggregate_summary(conn: sqlite3.Connection, where_sql: str, params: tuple[Any, ...]) -> dict[str, Any]:
        row = conn.execute(
            f"""
            SELECT
                COUNT(1) AS requests,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(input_tokens), 0) AS input_tokens,
                COALESCE(SUM(output_tokens), 0) AS output_tokens,
                COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens,
                COALESCE(SUM(thinking_tokens), 0) AS thinking_tokens,
                COALESCE(AVG(NULLIF(ttft_seconds, 0)), 0) AS avg_ttft,
                COALESCE(AVG(thinking_seconds), 0) AS avg_thinking,
                COALESCE(AVG(NULLIF(tokens_per_second, 0)), 0) AS avg_tps,
                COALESCE(AVG(duration_seconds), 0) AS avg_duration,
                COALESCE(MAX(context_tokens), 0) AS max_ctx_tokens,
                COALESCE(AVG(NULLIF(context_tokens, 0)), 0) AS avg_ctx_tokens,
                COALESCE(AVG(NULLIF(context_messages, 0)), 0) AS avg_ctx_msgs,
                COALESCE(MAX(turn_index), 0) AS max_turn
            FROM token_usage_logs
            {where_sql}
            """,
            params,
        ).fetchone()
        if not row:
            return {
                "requests": 0,
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
                "thinking_tokens": 0,
                "cache_hit_rate": 0.0,
                "avg_ttft_seconds": 0.0,
                "avg_thinking_seconds": 0.0,
                "avg_tokens_per_second": 0.0,
                "avg_duration_seconds": 0.0,
                "max_context_tokens": 0,
                "avg_context_tokens": 0,
                "avg_context_messages": 0.0,
                "max_turn_index": 0,
            }
        in_tok = int(row[2] or 0)
        cache_tok = int(row[4] or 0)
        tot_in = in_tok + cache_tok
        hit_rate = round((cache_tok / tot_in) * 100.0, 2) if (tot_in > 0 and cache_tok > 0) else 0.0
        return {
            "requests": int(row[0] or 0),
            "total_tokens": int(row[1] or 0),
            "input_tokens": in_tok,
            "output_tokens": int(row[3] or 0),
            "cache_read_tokens": cache_tok,
            "cache_creation_tokens": int(row[5] or 0),
            "thinking_tokens": int(row[6] or 0),
            "cache_hit_rate": hit_rate,
            "avg_ttft_seconds": round(float(row[7] or 0.0), 3),
            "avg_thinking_seconds": round(float(row[8] or 0.0), 3),
            "avg_tokens_per_second": round(float(row[9] or 0.0), 1),
            "avg_duration_seconds": round(float(row[10] or 0.0), 3),
            "max_context_tokens": int(row[11] or 0),
            "avg_context_tokens": int(round(float(row[12] or 0.0))),
            "avg_context_messages": round(float(row[13] or 0.0), 1),
            "max_turn_index": int(row[14] or 0),
        }

    def get_workbench_summary(self) -> dict[str, Any]:
        """Fast summary for the Workbench Token Usage panel (prioritizing the latest request)."""
        today_str = dt.datetime.now().strftime("%Y-%m-%d")
        with self._lock, self._conn() as conn:
            latest_row = conn.execute(
                f"SELECT {_RECORD_COLS} FROM token_usage_logs ORDER BY timestamp DESC, id DESC LIMIT 1"
            ).fetchone()
            latest = _row_to_record(latest_row) if latest_row else None

            latest_session: dict[str, Any] | None = None
            if latest is not None:
                sk = latest["session_key"]
                s_agg = self._aggregate_summary(conn, "WHERE session_key = ?", (sk,))
                latest_session = {
                    "session_key": sk,
                    "scene": latest["scene"],
                    "channel_id": latest["channel_id"],
                    "display_name": latest["display_name"],
                    "context_messages": latest["context_messages"],
                    "context_tokens": latest["context_tokens"],
                    "turn_index": latest["turn_index"],
                    **s_agg,
                }

            today_agg = self._aggregate_summary(conn, "WHERE date_str = ?", (today_str,))
            overall_agg = self._aggregate_summary(conn, "", ())

            sess_rows = conn.execute(
                """
                SELECT
                    session_key,
                    MAX(scene) AS scene,
                    MAX(channel_id) AS channel_id,
                    MAX(display_name) AS display_name,
                    COUNT(1) AS requests,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    MAX(timestamp) AS last_ts
                FROM token_usage_logs
                GROUP BY session_key
                ORDER BY last_ts DESC
                LIMIT 12
                """
            ).fetchall()
            sessions_top: list[dict[str, Any]] = []
            for r in sess_rows:
                in_t = int(r[6] or 0)
                ca_t = int(r[8] or 0)
                tot_in = in_t + ca_t
                sessions_top.append(
                    {
                        "session_key": str(r[0]),
                        "scene": str(r[1]),
                        "channel_id": str(r[2]),
                        "display_name": str(r[3]),
                        "requests": int(r[4] or 0),
                        "total_tokens": int(r[5] or 0),
                        "input_tokens": in_t,
                        "output_tokens": int(r[7] or 0),
                        "cache_read_tokens": ca_t,
                        "cache_hit_rate": round((ca_t / tot_in) * 100.0, 2) if (tot_in > 0 and ca_t > 0) else 0.0,
                        "last_timestamp": float(r[9] or 0.0),
                    }
                )

            return {
                "latest": latest,
                "latest_session": latest_session,
                "today": {"date": today_str, **today_agg},
                "overall": overall_agg,
                "sessions": sessions_top,
            }

    def get_dashboard(
        self,
        *,
        scope: str = "all",
        date: str = "",
        month: str = "",
        calendar_month: str = "",
        session_key: str = "",
        limit: int = 200,
    ) -> dict[str, Any]:
        """Query the 3-layer Token Usage modal:
        - Layer 1 Left: monthly calendar heatmap (`calendar`)
        - Layer 1 Right: per-model bar chart (`models`) filtered by scope + optional session
        - Layer 2 Middle: scope totals (`summary`) + per-session breakdown (`sessions_breakdown`)
        - Layer 3 Bottom: individual request records (`requests`)
        """
        now_dt = dt.datetime.now()
        cur_month = now_dt.strftime("%Y-%m")
        cur_date = now_dt.strftime("%Y-%m-%d")

        eff_scope = (scope or "all").strip().lower()
        if eff_scope not in ("all", "month", "day"):
            eff_scope = "all"

        eff_date = (date or "").strip()
        eff_month = (month or "").strip()
        if eff_scope == "day" and not eff_date:
            eff_date = cur_date
        if eff_scope == "month" and not eff_month:
            eff_month = eff_date[:7] if len(eff_date) >= 7 else cur_month

        eff_cal_month = (calendar_month or "").strip()
        if not eff_cal_month:
            if eff_scope == "day" and len(eff_date) >= 7:
                eff_cal_month = eff_date[:7]
            elif eff_scope == "month" and len(eff_month) >= 7:
                eff_cal_month = eff_month[:7]
            else:
                eff_cal_month = cur_month

        eff_session = (session_key or "").strip()
        eff_limit = max(1, min(500, int(limit or 200)))

        # Build WHERE clause for active scope
        clauses: list[str] = []
        params_list: list[Any] = []
        if eff_scope == "day":
            clauses.append("date_str = ?")
            params_list.append(eff_date)
        elif eff_scope == "month":
            clauses.append("month_str = ?")
            params_list.append(eff_month)
        if eff_session:
            clauses.append("session_key = ?")
            params_list.append(eff_session)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params = tuple(params_list)

        # Build WHERE clause for calendar month (respects session filter if chosen)
        cal_clauses = ["month_str = ?"]
        cal_params: list[Any] = [eff_cal_month]
        if eff_session:
            cal_clauses.append("session_key = ?")
            cal_params.append(eff_session)
        cal_where_sql = f"WHERE {' AND '.join(cal_clauses)}"

        with self._lock, self._conn() as conn:
            # 1) Calendar heatmap days for eff_cal_month
            day_rows = conn.execute(
                f"""
                SELECT
                    date_str,
                    COUNT(1) AS requests,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens
                FROM token_usage_logs
                {cal_where_sql}
                GROUP BY date_str
                ORDER BY date_str ASC
                """,
                tuple(cal_params),
            ).fetchall()
            cal_days: dict[str, dict[str, Any]] = {}
            month_max = 0
            month_total = 0
            month_requests = 0
            for r in day_rows:
                d_str = str(r[0])
                tot = int(r[2] or 0)
                req_c = int(r[1] or 0)
                month_max = max(month_max, tot)
                month_total += tot
                month_requests += req_c
                in_t = int(r[3] or 0)
                ca_t = int(r[5] or 0)
                tot_in = in_t + ca_t
                cal_days[d_str] = {
                    "date": d_str,
                    "requests": req_c,
                    "total_tokens": tot,
                    "input_tokens": in_t,
                    "output_tokens": int(r[4] or 0),
                    "cache_read_tokens": ca_t,
                    "cache_hit_rate": round((ca_t / tot_in) * 100.0, 2) if (tot_in > 0 and ca_t > 0) else 0.0,
                }

            avail_months = [
                str(r[0])
                for r in conn.execute("SELECT DISTINCT month_str FROM token_usage_logs ORDER BY month_str DESC").fetchall()
                if r[0]
            ]
            if cur_month not in avail_months:
                avail_months.insert(0, cur_month)
            if eff_cal_month not in avail_months:
                avail_months.append(eff_cal_month)
            avail_months = sorted(set(avail_months), reverse=True)

            # 2) Layer 1 Right: Per-model breakdown for active scope
            model_rows = conn.execute(
                f"""
                SELECT
                    model,
                    COUNT(1) AS requests,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    COALESCE(AVG(NULLIF(ttft_seconds, 0)), 0) AS avg_ttft,
                    COALESCE(AVG(thinking_seconds), 0) AS avg_thinking,
                    COALESCE(AVG(NULLIF(tokens_per_second, 0)), 0) AS avg_tps
                FROM token_usage_logs
                {where_sql}
                GROUP BY model
                ORDER BY total_tokens DESC, requests DESC
                """,
                params,
            ).fetchall()
            models: list[dict[str, Any]] = []
            for r in model_rows:
                in_t = int(r[3] or 0)
                ca_t = int(r[5] or 0)
                tot_in = in_t + ca_t
                models.append(
                    {
                        "model": str(r[0] or "default"),
                        "requests": int(r[1] or 0),
                        "total_tokens": int(r[2] or 0),
                        "input_tokens": in_t,
                        "output_tokens": int(r[4] or 0),
                        "cache_read_tokens": ca_t,
                        "cache_hit_rate": round((ca_t / tot_in) * 100.0, 2) if (tot_in > 0 and ca_t > 0) else 0.0,
                        "avg_ttft_seconds": round(float(r[6] or 0.0), 3),
                        "avg_thinking_seconds": round(float(r[7] or 0.0), 3),
                        "avg_tokens_per_second": round(float(r[8] or 0.0), 1),
                    }
                )

            # 3) Layer 2 Middle: Aggregate totals + per-session breakdown within active scope
            summary = self._aggregate_summary(conn, where_sql, params)

            sess_bd_rows = conn.execute(
                f"""
                SELECT
                    session_key,
                    MAX(scene) AS scene,
                    MAX(channel_id) AS channel_id,
                    MAX(display_name) AS display_name,
                    COUNT(1) AS requests,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens,
                    COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                    MAX(context_messages) AS max_ctx_msgs,
                    MAX(context_tokens) AS max_ctx_tok,
                    MAX(turn_index) AS max_turn,
                    MAX(timestamp) AS last_ts
                FROM token_usage_logs
                {where_sql}
                GROUP BY session_key
                ORDER BY total_tokens DESC, last_ts DESC
                """,
                params,
            ).fetchall()
            sessions_breakdown: list[dict[str, Any]] = []
            for r in sess_bd_rows:
                in_t = int(r[6] or 0)
                ca_t = int(r[8] or 0)
                tot_in = in_t + ca_t
                sessions_breakdown.append(
                    {
                        "session_key": str(r[0]),
                        "scene": str(r[1]),
                        "channel_id": str(r[2]),
                        "display_name": str(r[3]),
                        "requests": int(r[4] or 0),
                        "total_tokens": int(r[5] or 0),
                        "input_tokens": in_t,
                        "output_tokens": int(r[7] or 0),
                        "cache_read_tokens": ca_t,
                        "cache_hit_rate": round((ca_t / tot_in) * 100.0, 2) if (tot_in > 0 and ca_t > 0) else 0.0,
                        "context_messages": int(r[9] or 0),
                        "context_tokens": int(r[10] or 0),
                        "turns": int(r[11] or 0),
                        "last_timestamp": float(r[12] or 0.0),
                    }
                )

            # 4) Layer 3 Bottom: Individual request records for active scope
            req_rows = conn.execute(
                f"SELECT {_RECORD_COLS} FROM token_usage_logs {where_sql} ORDER BY timestamp DESC, id DESC LIMIT ?",
                (*params, eff_limit),
            ).fetchall()
            requests = [_row_to_record(r) for r in req_rows]

            # 5) All distinct sessions ever seen (for filter dropdown)
            all_sess_rows = conn.execute(
                """
                SELECT
                    session_key,
                    MAX(scene) AS scene,
                    MAX(channel_id) AS channel_id,
                    MAX(display_name) AS display_name,
                    COUNT(1) AS requests,
                    COALESCE(SUM(total_tokens), 0) AS total_tokens,
                    MAX(timestamp) AS last_ts
                FROM token_usage_logs
                GROUP BY session_key
                ORDER BY last_ts DESC
                """
            ).fetchall()
            all_sessions = [
                {
                    "session_key": str(r[0]),
                    "scene": str(r[1]),
                    "channel_id": str(r[2]),
                    "display_name": str(r[3]),
                    "requests": int(r[4] or 0),
                    "total_tokens": int(r[5] or 0),
                }
                for r in all_sess_rows
            ]

            return {
                "scope": eff_scope,
                "date": eff_date,
                "month": eff_month,
                "session_key": eff_session,
                "calendar": {
                    "month": eff_cal_month,
                    "today": cur_date,
                    "month_max_tokens": month_max,
                    "month_total_tokens": month_total,
                    "month_requests": month_requests,
                    "days": cal_days,
                    "available_months": avail_months,
                },
                "models": models,
                "summary": summary,
                "sessions_breakdown": sessions_breakdown,
                "requests": requests,
                "all_sessions": all_sessions,
            }
