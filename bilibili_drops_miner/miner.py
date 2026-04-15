from __future__ import annotations

import asyncio
import logging
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
        # asyncio primitives — created inside _run_all on the running loop
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task[None]] = []
        # session state
        self._uid: int | None = None
        self._uname: str = ""
        self._notifier = MultiPlatformNotifier(config.notify_urls)
        # all BilibiliClient instances currently alive; mutated only from event-loop thread
        self._clients: list[BilibiliClient] = []

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # per-session coroutine (replaces _thread_entry + _thread_loop)
    # ------------------------------------------------------------------

    async def _session_coro(self, plan: SessionPlan, session_index: int) -> None:
        """Manages the full lifecycle of one room-session as an async task."""
        stop_event = self._stop_event
        assert stop_event is not None  # always set before tasks are created

        # Stagger session startups to avoid x25Kn session collision.
        # session_index 1 starts immediately; each subsequent one waits 3 s more.
        if session_index > 1:
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=(session_index - 1) * 3,
                )
                return  # stop was signalled during stagger delay
            except asyncio.TimeoutError:
                pass  # normal path — proceed to start

        if stop_event.is_set():
            return

        client = BilibiliClient(self.config.cookie)
        self._clients.append(client)

        worker: LiveRoomWorker | None = None
        worker_task: asyncio.Task[None] | None = None
        try:
            # Reuse the already-probed uid; fall back to a fresh probe only if
            # something went wrong during startup.
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
            worker_task = asyncio.create_task(
                worker.run_forever(),
                name=f"ws-{plan.room_id}-s{plan.session_no}",
            )
            LOGGER.info(
                "直播间 %s 连接 #%s 已启动",
                plan.room_id,
                plan.session_no,
            )

            # Park here until the global stop signal is set.
            await stop_event.wait()

        finally:
            # Tear down in reverse order: stop the worker (closes WS + sets its
            # own stop-event), cancel its task, then close the HTTP client.
            if worker is not None:
                await worker.stop()
            if worker_task is not None:
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            await client.close()
            if client in self._clients:
                self._clients.remove(client)

    # ------------------------------------------------------------------
    # main async entry point
    # ------------------------------------------------------------------

    async def _run_all(self) -> None:
        # Capture the running loop so that stop() / update_cookie() can reach
        # it safely from the GUI thread via call_soon_threadsafe.
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        # Login probe — one single HTTP call before any sessions start.
        uid, uname = await self._probe_login()
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

        tasks = [
            asyncio.create_task(
                self._session_coro(plan, i),
                name=f"session-{plan.room_id}-s{plan.session_no}",
            )
            for i, plan in enumerate(plans, start=1)
        ]
        self._tasks = tasks

        try:
            # Wait for all session tasks to finish. Each task parks on
            # stop_event.wait() and will only exit once stop() is called or it
            # crashes. return_exceptions=True ensures one bad session never
            # brings down the others.
            await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            # Raised when KeyboardInterrupt reaches asyncio.run().
            # Signal all tasks to stop, then wait for them.
            self._stop_event.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        finally:
            # Guarantee the stop flag is set so any task still in its stagger
            # delay can exit cleanly.
            self._stop_event.set()

            # Force-cancel anything that somehow survived.
            remaining = [t for t in tasks if not t.done()]
            if remaining:
                for task in remaining:
                    task.cancel()
                await asyncio.gather(*remaining, return_exceptions=True)

            # Summarise shutdown result.
            still_alive = [t.get_name() for t in tasks if not t.done()]
            if still_alive:
                preview = ", ".join(still_alive[:5])
                if len(still_alive) > 5:
                    preview += f" ... 共 {len(still_alive)} 个"
                LOGGER.warning("停止未完成，仍有连接未退出: %s", preview)
            else:
                LOGGER.info("所有连接已停止")

            # Release references so the object can be GC-ed cleanly.
            self._tasks.clear()
            self._clients.clear()
            self._loop = None
            self._stop_event = None

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Blocking call — runs the single event loop until stopped."""
        try:
            asyncio.run(self._run_all())
        except KeyboardInterrupt:
            LOGGER.info("收到停止信号，正在停止...")

    def stop(self) -> None:
        """Thread-safe: may be called from any thread (e.g. the GUI thread)."""
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and not loop.is_closed() and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)

    def _apply_cookie_update(self, new_cookie: str) -> None:
        """Runs inside the event-loop thread — safe to touch self._clients."""
        for client in self._clients:
            client.update_cookie(new_cookie)

    def update_cookie(self, new_cookie: str) -> None:
        """Thread-safe cookie hot-swap; propagates to all live HTTP clients."""
        self.config.cookie = new_cookie
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._apply_cookie_update, new_cookie)

    def update_notifier(self, notify_urls: list[str]) -> None:
        """Safe to call from any thread — only touches config and the notifier."""
        self.config.notify_urls = notify_urls
        self._notifier.update_urls(notify_urls)
