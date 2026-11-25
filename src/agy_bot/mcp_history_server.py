from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

_TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "name": "get_older_messages",
        "description": "向上翻页查询当前群聊/私聊因上下文压缩而移出窗口的更早历史对话记录（包含群友消息与机器人自身的历史回复）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "当前会话标识（必须原样传入提示词中给出的 session_key，如 qq_napcat_1:group:1106615059）。",
                },
                "before_seq": {
                    "type": "integer",
                    "description": "读取序号小于此值的消息（填当前窗口起始序号即可紧接往前翻页；填 0 则自动从归档分界线往前读）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回条数（1~80，默认 30）。",
                },
            },
            "required": ["session_key"],
        },
    },
    {
        "name": "search_chat_history",
        "description": "在当前群聊/私聊的全量 SQLite 历史数据库中按关键词、发言人或角色检索早期消息（支持只搜机器人自己以前说过的话）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "当前会话标识（必须原样传入提示词中给出的 session_key）。",
                },
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（匹配消息正文或被引用内容，留空则按发言人/角色筛选）。",
                },
                "sender": {
                    "type": "string",
                    "description": "可选：按发言人 QQ 号或昵称过滤。",
                },
                "role": {
                    "type": "string",
                    "enum": ["all", "user", "assistant"],
                    "description": "all=全部消息，user=仅群友/用户发言，assistant=仅模型/机器人自身的历史回复。",
                },
                "before_seq": {
                    "type": "integer",
                    "description": "可选：仅检索序号小于 before_seq 的早期消息（0 表示检索全库）。",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回条数（1~60，默认 25）。",
                },
            },
            "required": ["session_key"],
        },
    },
    {
        "name": "get_context_around",
        "description": "根据某条历史消息序号 #seq，读取该消息前后连续若干条完整对话上下文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "当前会话标识（必须原样传入提示词中给出的 session_key）。",
                },
                "target_seq": {
                    "type": "integer",
                    "description": "目标历史消息序号（如 42）。",
                },
                "radius": {
                    "type": "integer",
                    "description": "前后各取多少条（1~30，默认 10）。",
                },
            },
            "required": ["session_key", "target_seq"],
        },
    },
]


def _open_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"历史数据库尚未生成: {db_path}")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def _format_rows(rows: list[tuple[Any, ...]]) -> str:
    if not rows:
        return "未查询到符合条件的历史消息记录。"
    lines: list[str] = []
    for r in rows:
        seq_id, role, user_id, sender_name, text, ts, reply_to_text, attachments_json = r
        try:
            t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts or 0)))
        except Exception:
            t_str = "-"
        who = f"{sender_name or '机器人'}(我/assistant)" if role == "assistant" else f"{sender_name}({user_id})"
        q_part = f' [引用: "{str(reply_to_text)[:80]}"]' if reply_to_text else ""
        att_part = ""
        try:
            atts = json.loads(attachments_json or "[]")
            if isinstance(atts, list) and atts:
                paths = [str(a.get("local_path") or a.get("file_name") or "图片") for a in atts if isinstance(a, dict)]
                if paths:
                    att_part = " " + " ".join(f"[图片:{p}]" for p in paths)
        except Exception:
            pass
        lines.append(f"[#{seq_id}][{t_str}] {who}{q_part}: {(text or '').strip()}{att_part}")
    return "\n".join(lines)


def execute_history_tool(db_path: Path, name: str, args: dict[str, Any]) -> str:
    session_key = str(args.get("session_key") or "").strip()
    if not session_key:
        raise ValueError("参数 session_key 不能为空")
    # Normalize unique_session suffix (`...:u:12345`) to channel session_key if model passed runner_key
    if ":u:" in session_key:
        session_key = session_key.split(":u:", 1)[0]

    if not db_path.is_file():
        return "当前历史数据库尚未产生任何记录。"

    conn = _open_ro(db_path)
    try:
        cols = "seq_id, role, user_id, sender_name, text, timestamp, reply_to_text, attachments_json"
        if name == "get_older_messages":
            before_seq = int(args.get("before_seq") or 0)
            limit = max(1, min(80, int(args.get("limit") or 30)))
            if before_seq <= 0:
                cur_row = conn.execute(
                    "SELECT active_min_seq FROM session_cursors WHERE session_key = ? ORDER BY active_min_seq DESC LIMIT 1",
                    (session_key,),
                ).fetchone()
                before_seq = int(cur_row[0]) if (cur_row and int(cur_row[0] or 0) > 0) else 2_000_000_000
            rows = conn.execute(
                f"SELECT {cols} FROM messages WHERE session_key = ? AND seq_id < ? ORDER BY seq_id DESC LIMIT ?",
                (session_key, before_seq, limit),
            ).fetchall()
            rows.reverse()
            total_older = int(
                conn.execute(
                    "SELECT COUNT(1) FROM messages WHERE session_key = ? AND seq_id < ?",
                    (session_key, rows[0][0] if rows else before_seq),
                ).fetchone()[0]
            )
            header = f"[会话 {session_key} 历史记录 (< #{before_seq})，返回 {len(rows)} 条，更早还有 {total_older} 条]"
            return header + "\n" + _format_rows(rows)

        if name == "search_chat_history":
            keyword = str(args.get("keyword") or "").strip()
            sender = str(args.get("sender") or "").strip()
            role = str(args.get("role") or "all").strip().lower()
            before_seq = int(args.get("before_seq") or 0)
            limit = max(1, min(60, int(args.get("limit") or 25)))

            clauses = ["session_key = ?"]
            params: list[Any] = [session_key]
            if before_seq > 0:
                clauses.append("seq_id < ?")
                params.append(before_seq)
            if role in ("user", "assistant"):
                clauses.append("role = ?")
                params.append(role)
            if sender:
                clauses.append("(user_id = ? OR sender_name LIKE ?)")
                params.extend([sender, f"%{sender}%"])
            if keyword:
                clauses.append("(text LIKE ? OR COALESCE(reply_to_text, '') LIKE ?)")
                params.extend([f"%{keyword}%", f"%{keyword}%"])

            sql = f"SELECT {cols} FROM messages WHERE {' AND '.join(clauses)} ORDER BY seq_id DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(sql, params).fetchall()
            rows.reverse()
            header = f"[会话 {session_key} 检索结果：共命中 {len(rows)} 条（按时间正序）]"
            return header + "\n" + _format_rows(rows)

        if name == "get_context_around":
            target_seq = int(args.get("target_seq") or 0)
            radius = max(1, min(30, int(args.get("radius") or 10)))
            low_seq = max(1, target_seq - radius)
            high_seq = target_seq + radius
            rows = conn.execute(
                f"SELECT {cols} FROM messages WHERE session_key = ? AND seq_id BETWEEN ? AND ? ORDER BY seq_id ASC",
                (session_key, low_seq, high_seq),
            ).fetchall()
            header = f"[会话 {session_key} 序号 #{target_seq} 前后上下文 (#{low_seq}~#{high_seq})，共 {len(rows)} 条]"
            return header + "\n" + _format_rows(rows)

        raise ValueError(f"未知的历史检索工具: {name}")
    finally:
        conn.close()


def _write_rpc(resp: dict[str, Any], *, content_length_framing: bool) -> None:
    body = json.dumps(resp, ensure_ascii=False)
    if content_length_framing:
        raw = body.encode("utf-8")
        hdr = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(hdr + raw)
        sys.stdout.buffer.flush()
    else:
        sys.stdout.buffer.write((body + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def run_stdio_server(db_path: Path) -> None:
    use_cl = False
    while True:
        raw_line = sys.stdin.buffer.readline()
        if not raw_line:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line.lower().startswith("content-length:"):
            use_cl = True
            length = int(line.split(":", 1)[1].strip())
            while True:
                h = sys.stdin.buffer.readline()
                if not h or h in (b"\r\n", b"\n"):
                    break
            body_bytes = sys.stdin.buffer.read(length)
            if not body_bytes:
                break
            line = body_bytes.decode("utf-8", errors="replace").strip()

        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(req, dict):
            continue

        method = str(req.get("method") or "")
        req_id = req.get("id")
        # Notifications (no id) require no response
        if req_id is None:
            continue

        if method == "initialize":
            _write_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "agy-cli-bot-chat-history", "version": "1.0.0"},
                    },
                },
                content_length_framing=use_cl,
            )
        elif method == "ping":
            _write_rpc({"jsonrpc": "2.0", "id": req_id, "result": {}}, content_length_framing=use_cl)
        elif method == "tools/list":
            _write_rpc(
                {"jsonrpc": "2.0", "id": req_id, "result": {"tools": _TOOLS_SPEC}},
                content_length_framing=use_cl,
            )
        elif method == "tools/call":
            params = req.get("params") if isinstance(req.get("params"), dict) else {}
            tname = str(params.get("name") or "")
            targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            try:
                out_text = execute_history_tool(db_path, tname, targs)
                _write_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": out_text}],
                            "isError": False,
                        },
                    },
                    content_length_framing=use_cl,
                )
            except Exception as exc:
                _write_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"查询历史记录失败: {exc}"}],
                            "isError": True,
                        },
                    },
                    content_length_framing=use_cl,
                )
        else:
            _write_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                },
                content_length_framing=use_cl,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Built-in read-only SQLite chat history MCP server")
    parser.add_argument("--db", required=True, help="Path to chat_history.db")
    args = parser.parse_args()
    run_stdio_server(Path(args.db))


if __name__ == "__main__":
    main()
