from __future__ import annotations

import asyncio
import logging
from typing import Any

from bilibili_drops_miner.client import (
    BilibiliClient,
    LiveTraceSession,
    TaskProgress,
)
from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.notifier import MultiPlatformNotifier

LOGGER = logging.getLogger(__name__)


class X25KnWorker:
    def __init__(
        self,
        client: BilibiliClient,
        notifier: MultiPlatformNotifier,
        config: MinerConfig,
        uid: int,
        room_id: int,
        session_id: str = "",
        primary_session: bool = True,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.client = client
        self.notifier = notifier
        self.config = config
        self.uid = uid
        self.room_id = room_id
        self.session_id = session_id
        self.primary_session = primary_session
        self._stop_event = stop_event or asyncio.Event()

    @property
    def _ctx(self) -> str:
        if self.session_id:
            return f"room={self.room_id} session={self.session_id}"
        return f"room={self.room_id}"

    def _log_info(self, message: str, *args: Any, primary_only: bool = False) -> None:
        if primary_only and not self.primary_session:
            LOGGER.debug(message, *args)
            return
        LOGGER.info(message, *args)

    def _log_warning(
        self, message: str, *args: Any, primary_only: bool = False
    ) -> None:
        if primary_only and not self.primary_session:
            LOGGER.debug(message, *args)
            return
        LOGGER.warning(message, *args)

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(
                    "直播间 %s x25Kn 运行异常: %s",
                    self.room_id,
                    exc,
                    primary_only=True,
                )
            if not self._stop_event.is_set():
                await asyncio.sleep(self.config.reconnect_delay_seconds)
                if self._stop_event.is_set():
                    return

    async def _run_once(self) -> None:
        trace_heartbeat_task = asyncio.create_task(self._trace_heartbeat_loop())
        task_monitor_task = (
            asyncio.create_task(self._task_monitor_loop()) if self.primary_session else None
        )
        stop_wait_task = asyncio.create_task(self._stop_event.wait())

        tasks: list[asyncio.Task[Any]] = [trace_heartbeat_task, stop_wait_task]
        if task_monitor_task is not None:
            tasks.append(task_monitor_task)

        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            if stop_wait_task in done:
                return

            for task in done:
                if task is stop_wait_task:
                    continue
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    raise exc

            raise RuntimeError("x25Kn 子任务意外退出")

        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _trace_heartbeat_loop(self) -> None:
        session: LiveTraceSession | None = None
        wait_seconds = 60
        while not self._stop_event.is_set():
            if not self.config.enable_web_heartbeat:
                session = None
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=5)
                    return
                except asyncio.TimeoutError:
                    continue
            try:
                if session is None:
                    await self.client.room_entry_action(self.room_id)
                    session = await self.client.live_trace_enter(self.room_id)
                    wait_seconds = max(5, int(session.heartbeat_interval))
                    self._log_info(
                        "直播间 %s 观看时长上报已启动",
                        self.room_id,
                        primary_only=True,
                    )
                else:
                    session = await self.client.live_trace_heartbeat(session)
                    wait_seconds = max(5, int(session.heartbeat_interval))
                    LOGGER.debug(
                        "%s x25Kn heartbeat success seq=%s ets=%s interval=%s",
                        self._ctx,
                        session.seq_id,
                        session.ets,
                        wait_seconds,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = str(exc).strip() or repr(exc)
                self._log_warning(
                    "直播间 %s 观看时长上报失败[%s]: %s",
                    self.room_id,
                    type(exc).__name__,
                    detail,
                    primary_only=True,
                )
                session = None
                wait_seconds = max(5, self.config.reconnect_delay_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                return
            except asyncio.TimeoutError:
                pass

    async def _task_monitor_loop(self) -> None:
        last_snapshot: dict[str, tuple[int | float, int | float, int]] = {}
        notified_completed_ids: set[str] = set()
        while not self._stop_event.is_set():
            task_ids = self.config.task_ids
            wait_seconds = max(10, self.config.task_query_interval_seconds)
            if not task_ids:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait_seconds
                    )
                    return
                except asyncio.TimeoutError:
                    continue
            try:
                progresses = await self.client.get_task_progress(task_ids)
                if not progresses:
                    LOGGER.warning("未获取到任务进度，请检查任务 ID 是否正确")
                else:
                    for task in progresses:
                        key = task.task_id
                        current = (task.cur_value, task.limit_value, task.status)
                        previous = last_snapshot.get(key)
                        if previous != current:
                            self._log_info(
                                "任务进度: %s %s/%s",
                                task.task_name,
                                task.cur_value,
                                task.limit_value,
                                primary_only=True,
                            )
                            last_snapshot[key] = current
                        if (
                            self.config.notify_on_task_complete
                            and task.is_completed
                            and key not in notified_completed_ids
                        ):
                            notified_completed_ids.add(key)
                            self._log_info(
                                "任务完成: %s (%s/%s)",
                                task.task_name,
                                task.cur_value,
                                task.limit_value,
                                primary_only=True,
                            )
                            self._send_task_complete_notification(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.debug("查询任务进度失败: %s", exc)
                wait_seconds = max(10, self.config.reconnect_delay_seconds)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait_seconds)
                return
            except asyncio.TimeoutError:
                pass

    def _send_task_complete_notification(self, task: TaskProgress) -> None:
        if not self.notifier.enabled:
            return
        title = "Bilibili 任务完成"
        body = (
            f"直播间: {self.room_id}\n"
            f"任务: {task.task_name}\n"
            f"进度: {task.cur_value}/{task.limit_value}"
        )
        sent = self.notifier.notify(title=title, body=body)
        if sent:
            self._log_info(
                "已发送通知: %s",
                task.task_name,
                primary_only=True,
            )
