from __future__ import annotations

from typing import Any


def coerce_task_number(value: Any) -> int | float:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    if not text:
        return 0
    try:
        number = float(text)
    except ValueError:
        return 0
    if number.is_integer():
        return int(number)
    return number


def extract_task_indicator_values(
    indicators: Any,
) -> tuple[int | float, int | float]:
    if isinstance(indicators, dict):
        candidates = [indicators]
    elif isinstance(indicators, list):
        candidates = [item for item in indicators if isinstance(item, dict)]
    else:
        return 0, 0

    watch_types = {"watch_time", "watch", "live_watch", "duration", "live_time"}
    best_cur: int | float = 0
    best_limit: int | float = 0
    watch_cur: int | float = 0
    watch_limit: int | float = 0

    for indicator in candidates:
        indicator_type = str(
            indicator.get("type")
            or indicator.get("indicator_type")
            or indicator.get("indicator_id")
            or ""
        ).lower()
        cur_value = coerce_task_number(
            indicator.get("cur_value")
            or indicator.get("cur")
            or indicator.get("current")
            or indicator.get("progress")
        )
        limit_value = coerce_task_number(
            indicator.get("limit")
            or indicator.get("target")
            or indicator.get("total")
            or indicator.get("max")
            or indicator.get("max_value")
        )
        if indicator_type in watch_types:
            if limit_value >= watch_limit:
                watch_cur = cur_value
                watch_limit = limit_value
        elif limit_value >= best_limit:
            best_cur = cur_value
            best_limit = limit_value

    if watch_limit > 0:
        return watch_cur, watch_limit
    return best_cur, best_limit
