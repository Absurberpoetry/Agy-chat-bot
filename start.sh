#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  stop)
    if [ -f "bot.pid" ]; then
      PID="$(cat bot.pid || true)"
      if [ -n "${PID}" ] && kill -0 "${PID}" 2>/dev/null; then
        kill "${PID}" || true
        echo "已停止 AGY BOT (PID ${PID})"
      else
        echo "AGY BOT 当前未运行。"
      fi
      rm -f bot.pid
    else
      echo "未找到 bot.pid，尝试按进程名检查..."
      pkill -f "$(pwd)/run.py" 2>/dev/null && echo "已停止 AGY BOT" || echo "未发现运行中的 AGY BOT 进程"
    fi
    ;;
  -d|--daemon)
    if [ ! -x ".venv/bin/python" ]; then
      echo "[错误] 未找到 .venv，请先运行 bash install.sh"
      exit 1
    fi
    if [ ! -f "config.json" ]; then
      echo "[错误] 未找到 config.json，请先运行 bash install.sh 并填写配置"
      exit 1
    fi
    nohup ./.venv/bin/python -u run.py >> bot.log 2>&1 &
    echo $! > bot.pid
    echo "AGY BOT 已在后台启动 (PID $!)，统一日志: $(pwd)/bot.log"
    echo "WebUI 默认地址: http://127.0.0.1:8618"
    ;;
  *)
    if [ ! -x ".venv/bin/python" ]; then
      echo "[错误] 未找到 .venv，请先运行 bash install.sh"
      exit 1
    fi
    if [ ! -f "config.json" ]; then
      echo "[错误] 未找到 config.json，请先运行 bash install.sh 并填写配置"
      exit 1
    fi
    exec ./.venv/bin/python -u run.py "$@"
    ;;
esac
