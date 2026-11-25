#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."

echo "========================================"
echo "  AGY CLI BOT Linux / macOS 一键安装初始化"
echo "========================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3，请先安装 Python 3.11+（Ubuntu: sudo apt install -y python3 python3-venv python3-pip）"
  exit 1
fi

echo "[1/3] 检测到 Python: $(python3 --version)"
if [ ! -x ".venv/bin/python" ]; then
  echo "[2/3] 正在创建虚拟环境 .venv ..."
  python3 -m venv .venv
else
  echo "[2/3] 已存在 .venv 虚拟环境，跳过创建。"
fi

echo "[3/3] 正在安装依赖 (requirements.txt) ..."
./.venv/bin/python -m pip install -r requirements.txt

if [ ! -f "config.json" ]; then
  cp templates/config.example.json config.json
  echo "[配置] 已生成 config.json"
else
  echo "[配置] 检测到已有 config.json，保持原配置不变。"
fi

if [ ! -f "settings.antigravity-cli.bot.json" ]; then
  cp templates/settings.antigravity-cli.example.json settings.antigravity-cli.bot.json
  echo "[配置] 已生成 settings.antigravity-cli.bot.json"
fi

echo ""
echo "========================================"
echo "  安装完成！下一步："
echo "  1. 前台启动: bash scripts/linux/start.sh"
echo "  2. 后台常驻: bash scripts/linux/start.sh -d"
echo "  3. 停止服务: bash scripts/linux/start.sh stop"
echo "  4. 打开浏览器访问 http://127.0.0.1:8618 进行图形化配置"
echo "========================================"
