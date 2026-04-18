from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class MinerConfig:
    cookie: str
    room_ids: list[int]
    thread_count: int = 1
    reconnect_delay_seconds: int = 8
    enable_web_heartbeat: bool = True
    task_ids: list[str] = field(default_factory=list)
    task_query_interval_seconds: int = 30
    notify_urls: list[str] = field(default_factory=list)
    notify_on_task_complete: bool = True

    def validate(self) -> None:
        if not self.cookie.strip():
            raise ValueError("cookie 不能为空")
        if not self.room_ids:
            raise ValueError("room_ids 不能为空")
        if any(room_id <= 0 for room_id in self.room_ids):
            raise ValueError("room_ids 中存在非法房间号")
        if self.thread_count <= 0:
            raise ValueError("thread_count 必须大于 0")
        if self.reconnect_delay_seconds <= 0:
            raise ValueError("reconnect_delay_seconds 必须大于 0")
        if self.task_query_interval_seconds <= 0:
            raise ValueError("task_query_interval_seconds 必须大于 0")
