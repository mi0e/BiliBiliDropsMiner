from __future__ import annotations

from typing import Any

from bilibili_drops_miner.client_parts.models import TaskProgress
from bilibili_drops_miner.client_parts.task_parsing import (
    coerce_task_number,
    extract_task_indicator_values,
)


def normalize_task_ids(task_ids: list[str]) -> list[str]:
    return [task_id.strip() for task_id in task_ids if task_id.strip()]


def parse_task_progress_payload(payload: dict[str, Any]) -> list[TaskProgress]:
    if payload.get("code") != 0:
        raise ValueError(f"查询任务进度失败: {payload.get('message')}")

    data = payload.get("data") or {}
    task_list = data.get("list") or []
    progresses: list[TaskProgress] = []
    for item in task_list:
        task_id = str(item.get("task_id") or "")
        task_name = str(item.get("task_name") or task_id)
        status = int(item.get("task_status") or item.get("status") or 0)
        cur_value, limit_value = extract_task_indicator_values(item.get("indicators"))
        if limit_value <= 0:
            limit_value = coerce_task_number(
                item.get("limit") or item.get("target") or item.get("total")
            )
        if cur_value <= 0:
            cur_value = coerce_task_number(
                item.get("cur_value") or item.get("cur") or item.get("progress")
            )
        progresses.append(
            TaskProgress(
                task_id=task_id,
                task_name=task_name,
                status=status,
                cur_value=cur_value,
                limit_value=limit_value,
            )
        )
    return progresses

