from __future__ import annotations

import asyncio
import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from bilibili_drops_miner.client import BilibiliClient, TaskProgress
from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.notifier import MultiPlatformNotifier
from bilibili_drops_miner.x25kn_worker import X25KnWorker

LOGGER = logging.getLogger(__name__)
LOGIN_WATCHDOG_INTERVAL_SECONDS = 60.0
# 停机时等待所有会话线程退出的总预算。预算耗尽后不丢弃未退出线程的所有权：
# run() 返回 STOP_INCOMPLETE 并保留 _threads/_clients，由调用方（GUI 的
# WorkerController / WebUI 的 _run）继续轮询 poll_stop_state()，直到连接真正
# 释放才报告「已停止」并允许重新启动。会话线程是 daemon，进程退出时仍会被回收。
JOIN_BUDGET_SECONDS = 30.0


class StopOutcome(enum.Enum):
    """run() 的结束状态。

    STOP_INCOMPLETE 表示仍有会话线程存活，调用方不得报告「已停止」，
    也不得放行新的启动——旧连接仍在占用资源。
    """

    STOPPED = "stopped"
    STOP_INCOMPLETE = "stop_incomplete"


@dataclass(slots=True)
class SessionPlan:
    room_id: int
    session_no: int


class BilibiliWatchTimeMiner:
    def __init__(self, config: MinerConfig) -> None:
        self.config = config
        self.on_task_progress: Callable[[list[TaskProgress], list[str]], None] | None = None
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._uid: int | None = None
        self._uname: str = ""
        self._notifier = MultiPlatformNotifier(config.notify_urls)
        self._clients: list[BilibiliClient] = []
        self._clients_lock = threading.Lock()
        self._session_tasks: set[asyncio.Task[None]] = set()
        self._force_stop_requested = False
        self._login_invalidated = threading.Event()

    @property
    def uid(self) -> int | None:
        return self._uid

    @property
    def login_invalidated(self) -> bool:
        return self._login_invalidated.is_set()

    @property
    def active_session_count(self) -> int:
        """Count running session workers, excluding threads waiting for their stagger."""
        with self._clients_lock:
            return sum(not task.done() for task in self._session_tasks)

    @property
    def has_residual_sessions(self) -> bool:
        """上一次 run() 结束后仍未退出的会话线程。"""
        return any(thread.is_alive() for thread in self._threads)

    @property
    def residual_session_count(self) -> int:
        return sum(1 for thread in self._threads if thread.is_alive())

    def poll_stop_state(self) -> StopOutcome:
        """回收已退出的会话线程；只有全部退出才清空引用并报告 STOPPED。

        调用方在 owner 线程结束后反复调用本方法，直到返回 STOPPED 才允许
        报告「已停止」与重新启动。JOIN_BUDGET_SECONDS 耗尽时 run() 会保留
        未退出线程的引用，正是靠这里继续跟踪并最终释放。
        """
        if self.has_residual_sessions:
            return StopOutcome.STOP_INCOMPLETE
        self._threads.clear()
        with self._clients_lock:
            self._clients.clear()
        return StopOutcome.STOPPED

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
        # 1s stagger is the bench-verified floor under 128 threads: 0.5s triggers server throttle, 0.75s exhausts local proxy.
        # 错峰必须按全局序号 thread_index 计算：该下限约束的是整个进程的建连速率。
        # 按房间内序号计算会让每个房间的第 N 个连接在同一秒启动，多房间时全局
        # 速率按房间数倍增，超出实测下限。
        if thread_index > 1 and await asyncio.to_thread(
            self._stop_event.wait, (thread_index - 1) * 1
        ):
            return
        if self._stop_event.is_set():
            return

        client = BilibiliClient(self.config.cookie)
        with self._clients_lock:
            # The Cookie may have changed while this client was being built.
            # Registration and updates share this lock, so calibrate against
            # the latest committed Cookie before publishing the client.
            client.update_cookie(self.config.cookie)
            self._clients.append(client)

        worker: X25KnWorker | None = None
        task: asyncio.Task[None] | None = None
        try:
            if self._uid is None:
                uid, _ = await client.get_self_info()
                self._uid = uid or 0
            runtime_uid = self._uid or 0

            worker = X25KnWorker(
                client=client,
                notifier=self._notifier,
                config=self.config,
                uid=runtime_uid,
                room_id=plan.room_id,
                session_id=f"s{plan.session_no}",
                primary_session=plan.session_no == 1,
                on_task_progress=self.on_task_progress,
            )
            task = asyncio.create_task(
                worker.run_forever(),
                name=f"x25kn-{plan.room_id}-s{plan.session_no}",
            )
            with self._clients_lock:
                self._session_tasks.add(task)
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

            with self._clients_lock:
                if task is not None:
                    self._session_tasks.discard(task)
                if client in self._clients:
                    self._clients.remove(client)

            try:
                await client.close()
            except Exception:
                LOGGER.debug(
                    "关闭 HTTP client 失败 room=%s session=%s",
                    plan.room_id,
                    plan.session_no,
                    exc_info=True,
                )

    def _thread_entry(self, plan: SessionPlan, thread_index: int) -> None:
        try:
            asyncio.run(self._thread_loop(plan, thread_index))
        except Exception as exc:
            LOGGER.exception("直播间连接异常退出: %s", exc)
            self._stop_event.set()

    def run(self) -> StopOutcome:
        # 上一次停止未完成时不能清空 _threads 重新开始：那些线程仍在占用连接，
        # 清掉引用就等于丢弃所有权，之后再没有任何地方能跟踪它们的释放。
        if self.has_residual_sessions:
            raise RuntimeError(
                f"上一次停止未完成，仍有 {self.residual_session_count} 个连接线程未退出"
            )

        self._stop_event.clear()
        self._force_stop_requested = False
        self._login_invalidated.clear()

        uid, uname = asyncio.run(self._probe_login())
        self._uid = uid
        self._uname = uname
        if uid is None:
            self._login_invalidated.set()
            raise RuntimeError("Cookie 已失效，无法启动")
        LOGGER.info("登录成功: %s (UID: %s)", uname, uid)

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

        for thread_index, plan in enumerate(plans, start=1):
            thread = threading.Thread(
                target=self._thread_entry,
                args=(plan, thread_index),
                name=f"room-{plan.room_id}-s{plan.session_no}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

        next_login_check = time.monotonic() + LOGIN_WATCHDOG_INTERVAL_SECONDS
        try:
            while not self._stop_event.is_set():
                if not any(thread.is_alive() for thread in self._threads):
                    break
                if time.monotonic() >= next_login_check:
                    try:
                        checked_uid, _ = asyncio.run(self._probe_login())
                    except Exception as exc:
                        LOGGER.warning("登录状态复检失败，将稍后重试: %s", exc)
                    else:
                        if checked_uid is None:
                            LOGGER.error("Cookie 已失效，正在自动停止")
                            self._login_invalidated.set()
                            self.stop()
                            break
                        if checked_uid != self._uid:
                            LOGGER.error("Cookie 所属账号已变化，正在自动停止")
                            self._login_invalidated.set()
                            self.stop()
                            break
                    next_login_check = (
                        time.monotonic() + LOGIN_WATCHDOG_INTERVAL_SECONDS
                    )
                for thread in self._threads:
                    # 每个存活线程单独 join(0.5)，2048 个线程会把停止请求拖延
                    # 到 1024 秒之后才进入 finally。停止一旦被请求就立刻离开
                    # 这一轮，把剩余时间交给带总预算的停机循环。
                    if self._stop_event.is_set():
                        break
                    thread.join(timeout=0.5)
        except KeyboardInterrupt:
            LOGGER.info("收到停止信号，正在停止...")
            self.stop()
        finally:
            self.stop(force=self._force_stop_requested)
            join_timeout = 1.2 if self._force_stop_requested else 3.0
            next_warning_at = time.monotonic() + join_timeout
            # 总预算：这个循环在没有全部线程退出时会一直转，只在每轮末尾打一条
            # 警告。run() 返回后调用方（GUI 的 worker 线程 / WebUI 的
            # join(timeout=25)）仍在等，于是表现为「关了没反应」且无法定位。
            #
            # 预算必须在每次 join 前重算：原先只在整轮 for 之后检查，每个存活
            # 线程单独 join(0.2)，16 房间 × 128 连接全阻塞时一轮就要 409.6 秒，
            # 30 秒预算形同虚设。
            deadline = time.monotonic() + JOIN_BUDGET_SECONDS
            while True:
                alive_threads = [
                    thread for thread in self._threads if thread.is_alive()
                ]
                if not alive_threads:
                    break

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                # Keep the miner's owner thread alive until every session has
                # released its client and event loop. The GUI polls this owner
                # thread, so these short joins never block the GUI thread.
                for thread in alive_threads:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    thread.join(timeout=min(0.2, remaining))

                if time.monotonic() >= next_warning_at:
                    alive_names = [
                        thread.name for thread in alive_threads if thread.is_alive()
                    ]
                    if alive_names:
                        preview = ", ".join(alive_names[:5])
                        if len(alive_names) > 5:
                            preview += f" ... 共 {len(alive_names)} 个"
                        LOGGER.warning("停止未完成，仍有线程未退出: %s", preview)
                    next_warning_at = time.monotonic() + join_timeout

            # 预算耗尽时不清空 _threads / _clients：那些线程仍持有连接，
            # 清掉引用就等于丢弃所有权，调用方将无法判断「是否真的停止」，
            # 界面会误报已停止并放行新的启动。交由 poll_stop_state() 继续跟踪，
            # 直到全部退出才回收引用。
            outcome = self.poll_stop_state()
            if outcome is StopOutcome.STOP_INCOMPLETE:
                stuck = [thread.name for thread in self._threads if thread.is_alive()]
                LOGGER.error(
                    "等待连接线程退出超过 %.0f 秒，保留 %s 个未退出会话的所有权交由调用方继续跟踪: %s",
                    JOIN_BUDGET_SECONDS,
                    len(stuck),
                    ", ".join(stuck[:5]),
                )
            else:
                LOGGER.info("所有连接已停止")

        # 放在 finally 之外：finally 里 return 会吞掉 try 中传播的异常。
        return outcome

    def stop(self, *, force: bool = False) -> None:
        # Keep GUI compatibility: force flag is accepted and can tighten join budget.
        if force:
            self._force_stop_requested = True
        self._stop_event.set()

    def update_cookie(self, new_cookie: str) -> None:
        with self._clients_lock:
            self.config.cookie = new_cookie
            for client in self._clients:
                client.update_cookie(new_cookie)

    def update_notifier(self, notify_urls: list[str]) -> None:
        self.config.notify_urls = notify_urls
        self._notifier.update_urls(notify_urls)
