from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class DanmuServerConf:
    room_id: int
    token: str
    host: str
    wss_port: int


@dataclass(slots=True)
class LiveRoomInfo:
    room_id: int
    ruid: int
    parent_area_id: int
    area_id: int
    live_status: int


@dataclass(slots=True)
class LiveWatchTime:
    room_id: int
    ruid: int
    watch_time: int
    rusername: str = ""


@dataclass(slots=True)
class LiveTraceSession:
    room_id: int
    ruid: int
    parent_area_id: int
    area_id: int
    seq_id: int
    ets: int
    heartbeat_interval: int
    secret_key: str
    secret_rule: list[int]


@dataclass(slots=True)
class TaskProgress:
    task_id: str
    task_name: str
    status: int
    cur_value: int | float
    limit_value: int | float

    @property
    def is_completed(self) -> bool:
        try:
            limit = float(self.limit_value)
            cur = float(self.cur_value)
            if limit > 0:
                return cur >= limit
        except (TypeError, ValueError):
            pass
        # 无可用进度指标时，仅信任明确的终态（已领取等）。
        return self.status in (3, 6)


@dataclass(slots=True)
class MissionRewardInfo:
    task_id: str
    task_name: str
    status: int
    message: str
    act_id: str
    act_name: str
    reward_name: str

    @property
    def is_claimable(self) -> bool:
        return self.status == 0

    @property
    def is_claimed(self) -> bool:
        return self.status == 6


@dataclass(slots=True)
class MissionRewardClaimResult:
    task_id: str
    task_name: str
    reward_name: str
    status: int
    message: str
    success: bool
    skipped: bool
    code: int | str | None = None
