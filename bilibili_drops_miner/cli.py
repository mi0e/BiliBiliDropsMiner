from __future__ import annotations

import argparse
import sys

from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.logging_utils import setup_logging
from bilibili_drops_miner.miner import BilibiliWatchTimeMiner
from bilibili_drops_miner.utils import parse_room_ids, parse_task_ids


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bilibili Watch-Time Miner")
    parser.add_argument("--cookie", default="", help="Bilibili cookie string")
    parser.add_argument("--rooms", default="", help="Room ids, comma/newline separated")
    parser.add_argument("--threads", type=int, default=1, help="Sessions per room")
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=30,
        help="Danmu websocket heartbeat interval",
    )
    parser.add_argument(
        "--reconnect-delay", type=int, default=8, help="Reconnect delay in seconds"
    )
    parser.add_argument(
        "--disable-web-heartbeat",
        action="store_true",
        help="Disable x25Kn business heartbeat",
    )
    parser.add_argument(
        "--x25kn-only",
        action="store_true",
        help="Run x25Kn heartbeat/task monitor without websocket connection",
    )
    parser.add_argument(
        "--task-ids", default="", help="Task ids for progress monitoring"
    )
    parser.add_argument(
        "--task-interval", type=int, default=30, help="Task query interval in seconds"
    )
    parser.add_argument(
        "--notify-urls",
        default="",
        help="Apprise URLs, comma/newline separated (WeCom, Gotify, ServerChan, etc.)",
    )
    parser.add_argument(
        "--disable-task-notify",
        action="store_true",
        help="Disable notification when task reaches target",
    )
    parser.add_argument("--no-color", action="store_true", help="Disable color logs")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logs"
    )
    return parser


def _resolve_cookie(args: argparse.Namespace) -> str:
    cookie = args.cookie.strip()
    if cookie:
        return cookie
    print("Please input Bilibili cookie:")
    return input("> ").strip()


def _resolve_rooms(args: argparse.Namespace) -> list[int]:
    rooms_raw = args.rooms.strip()
    if not rooms_raw:
        print("Please input room ids (comma separated):")
        rooms_raw = input("> ").strip()
    return parse_room_ids(rooms_raw)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    setup_logging(verbose=args.verbose, no_color=args.no_color)
    try:
        cookie = _resolve_cookie(args)
        room_ids = _resolve_rooms(args)
        task_ids = parse_task_ids(args.task_ids)
        notify_urls = parse_task_ids(args.notify_urls)
        config = MinerConfig(
            cookie=cookie,
            room_ids=room_ids,
            thread_count=args.threads,
            heartbeat_interval_seconds=args.heartbeat_interval,
            reconnect_delay_seconds=args.reconnect_delay,
            enable_web_heartbeat=not args.disable_web_heartbeat,
            x25kn_only_mode=args.x25kn_only,
            task_ids=task_ids,
            task_query_interval_seconds=args.task_interval,
            notify_urls=notify_urls,
            notify_on_task_complete=not args.disable_task_notify,
        )
        config.validate()
        miner = BilibiliWatchTimeMiner(config)
        miner.run()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"Run failed: {exc}", file=sys.stderr)
        return 1
