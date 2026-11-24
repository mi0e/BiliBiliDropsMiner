from __future__ import annotations

from dataclasses import dataclass, field

# 每房间线程数上限。此前只有 WebUI 侧有这个边界（web.py 的 Settings 的
# le=128 与 index.html 的 max=128），而 MinerConfig.validate() 只检查
# 「大于 0」，GUI 的裸 QLineEdit 与 CLI 的 --threads 更是完全无上限：
# 输入 99999 会一路走到 miner 的 range(1, thread_count + 1) 去开线程。
#
# 取 128 是为了与已有的 WebUI 边界一致，不是新选的值。改动 WebUI 上下限时
# 记得同步 index.html 的 max 属性——HTML 拿不到这里的常量。
MAX_THREAD_COUNT = 128

# 房间数上限，同样来自 web.py 的 max_length=16。
MAX_ROOM_COUNT = 16


@dataclass(slots=True)
class MinerConfig:
    cookie: str
    room_ids: list[int]
    thread_count: int = 1
    reconnect_delay_seconds: int = 8
    task_ids: list[str] = field(default_factory=list)
    task_query_interval_seconds: int = 30
    notify_urls: list[str] = field(default_factory=list)
    notify_on_task_complete: bool = True

    def validate(self) -> None:
        if not self.cookie.strip():
            raise ValueError("cookie 不能为空")
        if not self.room_ids:
            raise ValueError("room_ids 不能为空")
        if len(self.room_ids) > MAX_ROOM_COUNT:
            raise ValueError(f"房间数不能超过 {MAX_ROOM_COUNT}")
        if any(room_id <= 0 for room_id in self.room_ids):
            raise ValueError("room_ids 中存在非法房间号")
        if self.thread_count <= 0:
            raise ValueError("thread_count 必须大于 0")
        if self.thread_count > MAX_THREAD_COUNT:
            raise ValueError(f"每房间线程数不能超过 {MAX_THREAD_COUNT}")
        if self.reconnect_delay_seconds <= 0:
            raise ValueError("reconnect_delay_seconds 必须大于 0")
        if self.task_query_interval_seconds <= 0:
            raise ValueError("task_query_interval_seconds 必须大于 0")
