from __future__ import annotations

from typing import Any

import httpx

from bilibili_drops_miner.client_parts.cookies import DEFAULT_USER_AGENT
from bilibili_drops_miner.utils import extract_bili_live_task_groups

LIVE_ROOM_URL = "https://live.bilibili.com/{room_id}"


def fetch_live_task_groups(
    room_id: int,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: httpx.Timeout | float | None = None,
) -> list[dict[str, Any]]:
    """Fetch and parse task groups from a live room's static HTML."""
    if room_id <= 0:
        raise ValueError("room_id must be greater than zero")

    url = LIVE_ROOM_URL.format(room_id=room_id)
    request_timeout = timeout or httpx.Timeout(15.0, connect=5.0)
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": "https://live.bilibili.com/",
    }
    with httpx.Client(
        headers=headers,
        follow_redirects=True,
        timeout=request_timeout,
        transport=transport,
    ) as client:
        response = client.get(url)
        response.raise_for_status()
    return extract_bili_live_task_groups(response.text)
