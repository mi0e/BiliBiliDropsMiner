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
        self._clients_lock = threading.Lock()
        self._force_stop_requested = False

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
        # Keep 3s stagger from legacy model; user-verified as the best interval.
        if thread_index > 1 and await asyncio.to_thread(
            self._stop_event.wait, (thread_index - 1) * 3
        ):
            return
        if self._stop_event.is_set():
            return

        client = BilibiliClient(self.config.cookie)
        with self._clients_lock:
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
            task = asyncio.create_task(
                worker.run_forever(),
                name=f"ws-{plan.room_id}-s{plan.session_no}",
            )
            LOGGER.info("直播间 %s 连接 #%s 已启动", plan.room_id, plan.session_no)

            while not self._stop_event.is_set():
                if await asyncio.to_thread(self._stop_event.wait, 1):
                    break
        finally:
            if worker is not None:
                try:
                    await worker.stop()
                except Exception:
                    LOGGER.debug(
                        "停止 worker 失败 room=%s session=%s",
                        plan.room_id,
                        plan.session_no,
                        exc_info=True,
                    )

            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            try:
                await client.close()
            except Exception:
                LOGGER.debug(
                    "关闭 HTTP client 失败 room=%s session=%s",
                    plan.room_id,
                    plan.session_no,
                    exc_info=True,
                )

            with self._clients_lock:
                if client in self._clients:
                    self._clients.remove(client)

    def _thread_entry(self, plan: SessionPlan, thread_index: int) -> None:
        try:
            asyncio.run(self._thread_loop(plan, thread_index))
        except Exception as exc:
            LOGGER.exception("直播间连接异常退出: %s", exc)
            self._stop_event.set()

    def run(self) -> None:
        self._stop_event.clear()
        self._force_stop_requested = False

        uid, uname = asyncio.run(self._probe_login())
        self._uid = uid
        self._uname = uname
        if uid:
            LOGGER.info("登录成功: %s (UID: %s)", uname, uid)
        else:
            LOGGER.warning("Cookie 未登录，将以游客模式运行")

        plans = self._build_session_plans()
        LOGGER.info(
            "开始运行: 房间 %s，每房间 %s 个连接",
            self.config.room_ids,
            self.config.thread_count,
        )
        if self.config.task_ids:
            LOGGER.info(
                "任务追踪已开启，每 %s 秒查询一次",
                self.config.task_query_interval_seconds,
            )
        else:
            LOGGER.info("任务追踪未开启（未设置任务 ID）")

        if self._notifier.enabled:
            LOGGER.info("通知推送已开启（%s 个地址）", len(self.config.notify_urls))
        elif self.config.notify_urls:
            LOGGER.warning("通知地址已配置但推送服务不可用")

        self._threads.clear()
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
            LOGGER.info("收到停止信号，正在停止...")
            self.stop()
        finally:
            self.stop(force=self._force_stop_requested)
            join_timeout = 1.2 if self._force_stop_requested else 3.0
            for thread in self._threads:
                thread.join(timeout=join_timeout)

            alive_threads = [thread.name for thread in self._threads if thread.is_alive()]
            if alive_threads:
                preview = ", ".join(alive_threads[:5])
                if len(alive_threads) > 5:
                    preview += f" ... 共 {len(alive_threads)} 个"
                LOGGER.warning("停止未完成，仍有线程未退出: %s", preview)
            else:
                LOGGER.info("所有连接已停止")

            self._threads.clear()
            with self._clients_lock:
                self._clients.clear()

    def stop(self, *, force: bool = False) -> None:
        # Keep GUI compatibility: force flag is accepted and can tighten join budget.
        if force:
            self._force_stop_requested = True
        self._stop_event.set()

    def update_cookie(self, new_cookie: str) -> None:
        self.config.cookie = new_cookie
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            client.update_cookie(new_cookie)

    def update_notifier(self, notify_urls: list[str]) -> None:
        self.config.notify_urls = notify_urls
        self._notifier.update_urls(notify_urls)
