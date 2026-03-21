from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass

from bilibili_drops_miner.client import BilibiliClient
from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.notifier import MultiPlatformNotifier
from bilibili_drops_miner.ws import LiveRoomWorker

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionPlan:
    room_id: int
    session_no: int


class BilibiliWatchTimeMiner:
    def __init__(self, config: MinerConfig) -> None:
        self.config = config
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._uid: int | None = None
        self._uname: str = ""
        self._notifier = MultiPlatformNotifier(config.notify_urls)
        self._clients: list[BilibiliClient] = []

    def _build_session_plans(self) -> list[SessionPlan]:
        plans: list[SessionPlan] = []
        for room_id in self.config.room_ids:
            for session_no in range(1, self.config.thread_count + 1):
                plans.append(SessionPlan(room_id=room_id, session_no=session_no))
        return plans

    async def _probe_login(self) -> tuple[int | None, str]:
        client = BilibiliClient(self.config.cookie)
        try:
            return await client.get_self_info()
        finally:
            await client.close()

    async def _thread_loop(self, plan: SessionPlan, thread_index: int) -> None:
        # Stagger thread startups by 3s to avoid x25Kn session collision
        if thread_index > 1:
            await asyncio.sleep((thread_index - 1) * 3)
        client = BilibiliClient(self.config.cookie)
        self._clients.append(client)
        worker: LiveRoomWorker | None = None
        task: asyncio.Task[None] | None = None
        try:
            if self._uid is None:
                uid, _ = await client.get_self_info()
                self._uid = uid or 0
            runtime_uid = self._uid or 0
            worker = LiveRoomWorker(
                client=client,
                notifier=self._notifier,
                config=self.config,
                uid=runtime_uid,
                room_id=plan.room_id,
                session_id=f"s{plan.session_no}",
                primary_session=plan.session_no == 1,
            )
            task = asyncio.create_task(worker.run_forever())
            LOGGER.info(
                "\u76f4\u64ad\u95f4 %s \u8fde\u63a5 #%s \u5df2\u542f\u52a8",
                plan.room_id,
                plan.session_no,
            )
            while not self._stop_event.is_set():
                await asyncio.sleep(1)
        finally:
            if worker is not None:
                await worker.stop()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.close()

    def _thread_entry(self, plan: SessionPlan, thread_index: int) -> None:
        try:
            asyncio.run(self._thread_loop(plan, thread_index))
        except Exception as exc:
            LOGGER.exception("\u76f4\u64ad\u95f4\u8fde\u63a5\u5f02\u5e38\u9000\u51fa: %s", exc)
            self._stop_event.set()

    def run(self) -> None:
        uid, uname = asyncio.run(self._probe_login())
        self._uid = uid
        self._uname = uname
        if uid:
            LOGGER.info("\u767b\u5f55\u6210\u529f: %s (UID: %s)", uname, uid)
        else:
            LOGGER.warning("Cookie \u672a\u767b\u5f55\uff0c\u5c06\u4ee5\u6e38\u5ba2\u6a21\u5f0f\u8fd0\u884c")

        plans = self._build_session_plans()
        LOGGER.info(
            "\u5f00\u59cb\u8fd0\u884c: \u623f\u95f4 %s\uff0c\u6bcf\u623f\u95f4 %s \u4e2a\u8fde\u63a5",
            self.config.room_ids,
            self.config.thread_count,
        )
        if self.config.task_ids:
            LOGGER.info(
                "\u4efb\u52a1\u8ffd\u8e2a\u5df2\u5f00\u542f\uff0c\u6bcf %s \u79d2\u67e5\u8be2\u4e00\u6b21",
                self.config.task_query_interval_seconds,
            )
        else:
            LOGGER.info("\u4efb\u52a1\u8ffd\u8e2a\u672a\u5f00\u542f\uff08\u672a\u8bbe\u7f6e\u4efb\u52a1 ID\uff09")
        if self._notifier.enabled:
            LOGGER.info("\u901a\u77e5\u63a8\u9001\u5df2\u5f00\u542f\uff08%s \u4e2a\u5730\u5740\uff09", len(self.config.notify_urls))
        elif self.config.notify_urls:
            LOGGER.warning("\u901a\u77e5\u5730\u5740\u5df2\u914d\u7f6e\u4f46\u63a8\u9001\u670d\u52a1\u4e0d\u53ef\u7528")

        for thread_index, plan in enumerate(plans, start=1):
            thread = threading.Thread(
                target=self._thread_entry,
                args=(plan, thread_index),
                name=f"room-{plan.room_id}-s{plan.session_no}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        try:
            while not self._stop_event.is_set():
                if not any(thread.is_alive() for thread in self._threads):
                    break
                for thread in self._threads:
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            LOGGER.info("\u6536\u5230\u505c\u6b62\u4fe1\u53f7\uff0c\u6b63\u5728\u505c\u6b62...")
            self.stop()
        finally:
            self.stop()
            for thread in self._threads:
                thread.join(timeout=3)

    def stop(self) -> None:
        self._stop_event.set()

    def update_cookie(self, new_cookie: str) -> None:
        self.config.cookie = new_cookie
        for client in self._clients:
            client.update_cookie(new_cookie)

    def update_notifier(self, notify_urls: list[str]) -> None:
        self.config.notify_urls = notify_urls
        self._notifier.update_urls(notify_urls)
