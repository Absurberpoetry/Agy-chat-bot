#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "========================================"
echo "  AGY BOT Linux / macOS 一键安装初始化"
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
  cp config.example.json config.json
  echo "[配置] 已生成 config.json"
else
  echo "[配置] 检测到已有 config.json，保持原配置不变。"
fi

if [ ! -f "settings.antigravity-cli.bot.json" ]; then
  cp settings.antigravity-cli.example.json settings.antigravity-cli.bot.json
  echo "[配置] 已生成 settings.antigravity-cli.bot.json"
fi

echo ""
echo "========================================"
echo "  安装完成！下一步："
echo "  1. 编辑 config.json（填写代理、QQ/TG 参数与 WebUI 密码）"
echo "  2. 前台启动测试: bash start.sh"
echo "  3. 后台持久运行: bash start.sh -d"
echo "  4. 停止后台服务: bash start.sh stop"
echo "========================================"
