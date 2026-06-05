from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bilibili_drops_miner.config import MinerConfig


@dataclass(slots=True)
class GuiConfigValues:
    cookie: str
    rooms_text: str
    thread_count_text: str
    reconnect_delay_text: str
    task_ids_text: str
    task_query_interval_text: str
    notify_urls_text: str
    notify_on_task_complete: bool
    verbose: bool


def load_config_data(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    return payload


def values_from_config_data(data: dict[str, Any]) -> GuiConfigValues:
    return GuiConfigValues(
        cookie=str(data.get("cookie", "")),
        rooms_text=",".join(str(x) for x in data.get("room_ids", [])),
        thread_count_text=str(data.get("thread_count", 1)),
        reconnect_delay_text=str(data.get("reconnect_delay_seconds", 8)),
        task_ids_text=",".join(str(x) for x in data.get("task_ids", [])),
        task_query_interval_text=str(data.get("task_query_interval_seconds", 30)),
        notify_urls_text=",".join(str(x) for x in data.get("notify_urls", [])),
        notify_on_task_complete=bool(data.get("notify_on_task_complete", True)),
        verbose=bool(data.get("verbose", False)),
    )


def build_config_payload(config: MinerConfig, *, verbose: bool) -> dict[str, Any]:
    return {
        "cookie": config.cookie,
        "room_ids": config.room_ids,
        "thread_count": config.thread_count,
        "reconnect_delay_seconds": config.reconnect_delay_seconds,
        "enable_web_heartbeat": config.enable_web_heartbeat,
        "task_ids": config.task_ids,
        "task_query_interval_seconds": config.task_query_interval_seconds,
        "notify_urls": config.notify_urls,
        "notify_on_task_complete": config.notify_on_task_complete,
        "verbose": verbose,
    }


def save_config_data(path: str | Path, data: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

