#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agy_bot.app import BotApplication  # noqa: E402
from agy_bot.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Multi-platform forward-WebSocket bot powered by official Antigravity CLI",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=str(ROOT / "config.json"),
        help="配置文件路径 (默认: ./config.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="强制使用离线 MockAgentRunner，仅测试 NapCat WS、触发器与防抖逻辑而不消耗额度",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(
            f"找不到配置文件: {cfg_path}\n请先复制 config.example.json 为 config.json 并填写你的 NapCat WS 地址与群白名单。",
            file=sys.stderr,
        )
        return 1

    cfg = load_config(cfg_path)
    if args.dry_run:
        cfg.agy.mode = "mock"

    logging.basicConfig(
        level=getattr(logging, cfg.logging.level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    app = BotApplication(cfg)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("收到中断信号，机器人已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
