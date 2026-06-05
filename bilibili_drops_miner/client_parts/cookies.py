from __future__ import annotations

import urllib.parse
import uuid

from bilibili_drops_miner.utils import get_cookie_value, join_cookie, parse_cookie

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def generate_live_buvid() -> str:
    numeric = uuid.uuid4().int % (10**16)
    return f"AUTO{numeric:016d}"


def build_cookie_state(
    cookie: str,
    *,
    fallback_buvid3: str | None = None,
) -> tuple[dict[str, str], str, str]:
    cookie_map = parse_cookie(cookie)
    if "buvid3" not in cookie_map:
        cookie_map["buvid3"] = fallback_buvid3 or f"{uuid.uuid4()}infoc"

    cookie_header = join_cookie(cookie_map)
    bili_jct = get_cookie_value(cookie_header, "bili_jct")
    return cookie_map, cookie_header, bili_jct


def build_default_headers(user_agent: str, cookie_header: str) -> dict[str, str]:
    return {
        "User-Agent": user_agent,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Cookie": cookie_header,
    }


def build_live_headers(
    room_id: int,
    *,
    user_agent: str,
    cookie_header: str,
    lite: bool = False,
) -> dict[str, str]:
    referer = f"https://live.bilibili.com/{room_id}"
    if lite:
        referer = f"https://live.bilibili.com/blanc/{room_id}?liteVersion=true"
    return {
        "Referer": referer,
        "Origin": "https://live.bilibili.com",
        "User-Agent": user_agent,
        "Cookie": cookie_header,
    }


def build_mission_headers(
    task_id: str,
    *,
    user_agent: str,
    cookie_header: str,
) -> dict[str, str]:
    return {
        "Referer": (
            "https://www.bilibili.com/blackboard/era/award-exchange.html"
            f"?task_id={urllib.parse.quote(task_id, safe='')}"
        ),
        "Origin": "https://www.bilibili.com",
        "User-Agent": user_agent,
        "Cookie": cookie_header,
    }
