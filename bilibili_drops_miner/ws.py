from __future__ import annotations

import asyncio
import json
import logging
import struct
import zlib
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

try:
    import brotli  # type: ignore
except Exception:
    brotli = None


LOGGER = logging.getLogger(__name__)
HEADER = struct.Struct(">IHHII")


def _build_packet(
    payload: bytes, operation: int, version: int = 1, sequence: int = 1
) -> bytes:
    packet_len = 16 + len(payload)
    return HEADER.pack(packet_len, 16, version, operation, sequence) + payload


def _read_packets(raw: bytes) -> list[tuple[int, int, int, bytes]]:
    packets: list[tuple[int, int, int, bytes]] = []
    offset = 0
    while offset + 16 <= len(raw):
        packet_len, header_len, version, operation, sequence = HEADER.unpack_from(
            raw, offset
        )
        if packet_len < header_len or packet_len <= 0:
            break
        body_start = offset + header_len
        body_end = offset + packet_len
        if body_end > len(raw):
            break
        packets.append((version, operation, sequence, raw[body_start:body_end]))
        offset += packet_len
    return packets


def _parse_text_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for chunk in text.split("\x00"):
        candidate = chunk.strip()
        if not candidate:
            continue
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1:
            continue
        payload = candidate[start : end + 1]
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def decode_message(message: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for version, operation, _sequence, body in _read_packets(message):
        if operation != 5:
            continue
        if version in (0, 1):
            events.extend(_parse_text_events(body.decode("utf-8", errors="ignore")))
            continue
        if version == 2:
            try:
                events.extend(decode_message(zlib.decompress(body)))
            except zlib.error:
                LOGGER.debug("zlib decompress failed", exc_info=True)
            continue
        if version == 3 and brotli is not None:
            try:
                events.extend(decode_message(brotli.decompress(body)))
            except Exception:
                LOGGER.debug("brotli decompress failed", exc_info=True)
            continue
    return events


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
                conf = await self.client.get_danmu_server(self.room_id)
                LOGGER.debug("%s danmu server fetched host=%s", self._ctx, conf.host)
                await self._run_once(conf)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log_warning(
                    "\u76f4\u64ad\u95f4 %s \u8fde\u63a5\u65ad\u5f00: %s",
                    self.room_id,
                    exc,
                    primary_only=True,
                )
            if not self._stop_event.is_set():
                await asyncio.sleep(self.config.reconnect_delay_seconds)

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
            self._log_info("\u76f4\u64ad\u95f4 %s \u5df2\u8fde\u63a5", self.room_id, primary_only=True)
            await ws.send(_build_packet(auth_payload, operation=7, version=1))

            heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws))
            trace_heartbeat_task = asyncio.create_task(self._trace_heartbeat_loop())
            task_monitor_task = (
                asyncio.create_task(self._task_monitor_loop())
                if self.primary_session
                else None
            )

            try:
                async for message in ws:
                    if self._stop_event.is_set():
                        return
                    if not isinstance(message, bytes):
                        continue
                    events = decode_message(message)
                    if not events:
                        continue
                    for event in events:
                        if self.config.debug_events:
                            LOGGER.debug("%s event cmd=%s", self._ctx, event.get("cmd"))
                        self.on_event(event)
            finally:
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
            await asyncio.sleep(self.config.heartbeat_interval_seconds)

    async def _trace_heartbeat_loop(self) -> None:
        session: LiveTraceSession | None = None
        wait_seconds = 60
        while not self._stop_event.is_set():
            if not self.config.enable_web_heartbeat:
                session = None
                await asyncio.sleep(5)
                continue
            try:
                if session is None:
                    await self.client.room_entry_action(self.room_id)
                    session = await self.client.live_trace_enter(self.room_id)
                    wait_seconds = max(5, int(session.heartbeat_interval))
                    self._log_info(
                        "\u76f4\u64ad\u95f4 %s \u89c2\u770b\u65f6\u957f\u4e0a\u62a5\u5df2\u542f\u52a8",
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
                self._log_warning(
                    "\u76f4\u64ad\u95f4 %s \u89c2\u770b\u65f6\u957f\u4e0a\u62a5\u5931\u8d25: %s", self.room_id, exc, primary_only=True
                )
                session = None
                wait_seconds = max(5, self.config.reconnect_delay_seconds)
            await asyncio.sleep(wait_seconds)

    async def _task_monitor_loop(self) -> None:
        last_snapshot: dict[str, tuple[int | float, int | float, int]] = {}
        notified_completed_ids: set[str] = set()
        while not self._stop_event.is_set():
            task_ids = self.config.task_ids
            wait_seconds = max(10, self.config.task_query_interval_seconds)
            if not task_ids:
                await asyncio.sleep(wait_seconds)
                continue
            try:
                progresses = await self.client.get_task_progress(task_ids)
                if not progresses:
                    LOGGER.warning(
                        "\u672a\u83b7\u53d6\u5230\u4efb\u52a1\u8fdb\u5ea6\uff0c\u8bf7\u68c0\u67e5\u4efb\u52a1 ID \u662f\u5426\u6b63\u786e"
                    )
                else:
                    for task in progresses:
                        key = task.task_id
                        current = (task.cur_value, task.limit_value, task.status)
                        previous = last_snapshot.get(key)
                        if previous != current:
                            self._log_info(
                                "\u4efb\u52a1\u8fdb\u5ea6: %s %s/%s",
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
                                "\u4efb\u52a1\u5b8c\u6210: %s (%s/%s)",
                                task.task_name,
                                task.cur_value,
                                task.limit_value,
                                primary_only=True,
                            )
                            self._send_task_complete_notification(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning("\u67e5\u8be2\u4efb\u52a1\u8fdb\u5ea6\u5931\u8d25: %s", exc)
                wait_seconds = max(10, self.config.reconnect_delay_seconds)
            await asyncio.sleep(wait_seconds)

    def _send_task_complete_notification(self, task: TaskProgress) -> None:
        if not self.notifier.enabled:
            return
        title = "Bilibili \u4efb\u52a1\u5b8c\u6210"
        body = (
            f"\u76f4\u64ad\u95f4: {self.room_id}\n"
            f"\u4efb\u52a1: {task.task_name}\n"
            f"\u8fdb\u5ea6: {task.cur_value}/{task.limit_value}"
        )
        sent = self.notifier.notify(title=title, body=body)
        if sent:
            self._log_info(
                "\u5df2\u53d1\u9001\u901a\u77e5: %s",
                task.task_name,
                primary_only=True,
            )

    def on_event(self, event: dict[str, Any]) -> None:
        cmd = str(event.get("cmd", ""))
        if cmd.startswith("POPULARITY_RED_POCKET_START"):
            self._log_info(
                "\u76f4\u64ad\u95f4 %s \u53d1\u73b0\u7ea2\u5305\u62bd\u5956",
                self.room_id,
                primary_only=True,
            )
        elif cmd.startswith("ANCHOR_LOT_START"):
            self._log_info(
                "\u76f4\u64ad\u95f4 %s \u53d1\u73b0\u5929\u9009\u65f6\u523b",
                self.room_id,
                primary_only=True,
            )
