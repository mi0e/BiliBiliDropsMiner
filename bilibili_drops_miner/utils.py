from __future__ import annotations

import re

COOKIE_PATTERN = re.compile(r"\s*([^=;\s]+)\s*=\s*([^;]*)")


def parse_room_ids(raw: str) -> list[int]:
    room_ids: list[int] = []
    for token in raw.replace("\n", ",").split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        if not cleaned.isdigit():
            raise ValueError(f"房间号格式错误: {cleaned}")
        room_id = int(cleaned)
        if room_id <= 0:
            raise ValueError(f"房间号必须大于 0: {cleaned}")
        room_ids.append(room_id)
    return room_ids


def parse_task_ids(raw: str) -> list[str]:
    task_ids: list[str] = []
    for token in raw.replace("\n", ",").split(","):
        cleaned = token.strip()
        if not cleaned:
            continue
        task_ids.append(cleaned)
    return task_ids


def parse_cookie(cookie_text: str) -> dict[str, str]:
    cookie_map: dict[str, str] = {}
    for key, value in COOKIE_PATTERN.findall(cookie_text):
        cookie_map[key] = value
    return cookie_map


def get_cookie_value(cookie_text: str, key: str) -> str:
    return parse_cookie(cookie_text).get(key, "")


def join_cookie(cookie_map: dict[str, str]) -> str:
    return "; ".join(f"{key}={value}" for key, value in cookie_map.items())
