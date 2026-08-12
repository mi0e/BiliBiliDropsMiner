from __future__ import annotations

import asyncio
import threading
import time
import unittest

from bilibili_drops_miner.client_parts.models import LiveTraceSession, TaskProgress
from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.x25kn_worker import X25KnWorker


class _TaskClient:
    async def get_task_progress(self, _task_ids: list[str]) -> list[TaskProgress]:
        return [
            TaskProgress(
                task_id="done",
                task_name="观看直播",
                status=3,
                cur_value=1,
                limit_value=1,
            )
        ]


class _HeartbeatClient:
    def __init__(self) -> None:
        self.entry_calls = 0
        self.enter_calls = 0

    async def room_entry_action(self, _room_id: int) -> None:
        self.entry_calls += 1

    async def live_trace_enter(self, room_id: int) -> LiveTraceSession:
        self.enter_calls += 1
        return LiveTraceSession(
            room_id=room_id,
            ruid=1,
            parent_area_id=1,
            area_id=1,
            seq_id=1,
            ets=1,
            heartbeat_interval=60,
            secret_key="secret",
            secret_rule=[],
        )


class _BlockingNotifier:
    enabled = True

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def notify(self, *, title: str, body: str) -> bool:
        try:
            self.started.set()
            self.release.wait(timeout=1)
            return True
        finally:
            self.finished.set()


class X25KnWorkerTest(unittest.TestCase):
    def test_trace_heartbeat_always_starts(self) -> None:
        client = _HeartbeatClient()
        worker = X25KnWorker(
            client=client,  # type: ignore[arg-type]
            notifier=_BlockingNotifier(),  # type: ignore[arg-type]
            config=MinerConfig(cookie="cookie", room_ids=[1]),
            uid=42,
            room_id=1,
        )

        async def exercise() -> None:
            heartbeat = asyncio.create_task(worker._trace_heartbeat_loop())
            for _ in range(20):
                if client.enter_calls:
                    break
                await asyncio.sleep(0)
            await worker.stop()
            await asyncio.wait_for(heartbeat, timeout=0.2)

        asyncio.run(exercise())

        self.assertEqual(client.entry_calls, 1)
        self.assertEqual(client.enter_calls, 1)

    def test_blocking_notification_does_not_delay_event_loop_stop(self) -> None:
        notifier = _BlockingNotifier()
        worker = X25KnWorker(
            client=_TaskClient(),  # type: ignore[arg-type]
            notifier=notifier,  # type: ignore[arg-type]
            config=MinerConfig(
                cookie="cookie",
                room_ids=[1],
                task_ids=["done"],
                task_query_interval_seconds=10,
            ),
            uid=42,
            room_id=1,
        )

        async def exercise() -> None:
            monitor = asyncio.create_task(worker._task_monitor_loop())
            self.assertTrue(
                await asyncio.to_thread(notifier.started.wait, 1),
                "notification did not start",
            )
            await worker.stop()
            await asyncio.wait_for(monitor, timeout=0.2)

        started_at = time.monotonic()
        try:
            asyncio.run(exercise())
            elapsed = time.monotonic() - started_at
            self.assertLess(
                elapsed,
                0.5,
                "asyncio.run waited for the blocking notification thread",
            )
        finally:
            notifier.release.set()
            self.assertTrue(
                notifier.finished.wait(timeout=1),
                "notification thread did not finish after release",
            )


if __name__ == "__main__":
    unittest.main()
