from __future__ import annotations

import json
import os
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
    auto_claim_rewards: bool = False


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
        auto_claim_rewards=bool(data.get("auto_claim_rewards", False)),
        verbose=bool(data.get("verbose", False)),
    )


def build_config_payload(
    config: MinerConfig,
    *,
    verbose: bool,
    auto_claim_rewards: bool,
) -> dict[str, Any]:
    return {
        "cookie": config.cookie,
        "room_ids": config.room_ids,
        "thread_count": config.thread_count,
        "reconnect_delay_seconds": config.reconnect_delay_seconds,
        "task_ids": config.task_ids,
        "task_query_interval_seconds": config.task_query_interval_seconds,
        "notify_urls": config.notify_urls,
        "notify_on_task_complete": config.notify_on_task_complete,
        "auto_claim_rewards": auto_claim_rewards,
        "verbose": verbose,
    }


def save_config_data(path: str | Path, data: dict[str, Any]) -> None:
    # 原子写入，与 WebState.save 保持一致：直接覆盖目标文件时，写入过程中断电
    # 或崩溃会留下截断的 JSON，下次启动读取配置就会失败。临时文件名带上 pid，
    # 避免双开实例写同一个 .tmp 时互相交错。
    target = Path(path)
    temporary = target.with_name(f"{target.name}.tmp{os.getpid()}")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise

