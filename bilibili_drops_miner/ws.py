from __future__ import annotations

import asyncio
import json
import logging
import struct
from typing import Any

import websockets

from bilibili_drops_miner.client import (
    BilibiliClient,
    DanmuServerConf,
    LiveTraceSession,
    TaskProgress,
)
from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.notifier import MultiPlatformNotifier

LOGGER = logging.getLogger(__name__)
HEADER = struct.Struct(">IHHII")


def _build_packet(
    payload: bytes, operation: int, version: int = 1, sequence: int = 1
) -> bytes:
    packet_len = 16 + len(payload)
    return HEADER.pack(packet_len, 16, version, operation, sequence) + payload


class LiveRoomWorker:
    def __init__(
        self,
        client: BilibiliClient,
        notifier: MultiPlatformNotifier,
        config: MinerConfig,
        uid: int,
        room_id: int,
        session_id: str = "",
        primary_session: bool = True,
    ) -> None:
        self.client = client
        self.notifier = notifier
        self.config = config
        self.uid = uid
        self.room_id = room_id
        self.session_id = session_id
        self.primary_session = primary_session
        self._stop_event = asyncio.Event()
        self._ws: Any | None = None

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
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def run_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.config.x25kn_only_mode:
                    self._log_info(
                        "直播间 %s 已进入 x25Kn-only 模式（不连接 WS）",
                        self.room_id,
                        primary_only=True,
                    )
                    await self._run_x25kn_only_once()
                else:
                    conf = await self.client.get_danmu_server(self.room_id)
                    LOGGER.debug("%s danmu server fetched host=%s", self._ctx, conf.host)
                    await self._run_once(conf)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self.config.x25kn_only_mode:
                    self._log_warning(
                        "直播间 %s x25Kn-only 运行异常: %s",
                        self.room_id,
                        exc,
                        primary_only=True,
                    )
                else:
                    self._log_warning(
                        "直播间 %s 连接断开: %s",
                        self.room_id,
                        exc,
                        primary_only=True,
                    )
            if not self._stop_event.is_set():
                await asyncio.sleep(self.config.reconnect_delay_seconds)
                if self._stop_event.is_set():
                    return

    async def _run_x25kn_only_once(self) -> None:
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

            raise RuntimeError("x25Kn-only 子任务意外退出")

        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_once(self, conf: DanmuServerConf) -> None:
        auth_payload = json.dumps(
            {
                "uid": self.uid,
                "roomid": conf.room_id,
                "protover": 3,
                "platform": "web",
                "clientver": "1.18.6",
                "type": 2,
                "key": conf.token,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        uri = f"wss://{conf.host}:{conf.wss_port}/sub"
        async with websockets.connect(
            uri, ping_interval=None, max_size=4 * 1024 * 1024
        ) as ws:
            self._ws = ws
            self._log_info("直播间 %s 已连接", self.room_id, primary_only=True)
            await ws.send(_build_packet(auth_payload, operation=7, version=1))

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            trace_heartbeat_task = asyncio.create_task(self._trace_heartbeat_loop())
            task_monitor_task = (
                asyncio.create_task(self._task_monitor_loop())
                if self.primary_session
                else None
            )

            try:
                # 只消费服务器推送的消息以维持 TCP 接收缓冲区正常，不做任何解析。
                # 掉宝依赖 WS 心跳（operation=2）+ x25Kn HTTP 心跳，无需处理弹幕/礼物等事件。
                async for _ in ws:
                    if self._stop_event.is_set():
                        return
            except websockets.exceptions.ConnectionClosed:
                if self._stop_event.is_set():
                    return
                raise
            finally:
                self._ws = None
                heartbeat_task.cancel()
                trace_heartbeat_task.cancel()
                tasks = [heartbeat_task, trace_heartbeat_task]
                if task_monitor_task is not None:
                    task_monitor_task.cancel()
                    tasks.append(task_monitor_task)
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat_loop(self, ws: Any) -> None:
        packet = _build_packet(payload=b"", operation=2, version=1)
        while not self._stop_event.is_set():
            await ws.send(packet)
            LOGGER.debug(
                "%s sent websocket heartbeat interval=%ss",
                self._ctx,
                self.config.heartbeat_interval_seconds,
            )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.config.heartbeat_interval_seconds,
                )
                return
            except asyncio.TimeoutError:
                pass

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
