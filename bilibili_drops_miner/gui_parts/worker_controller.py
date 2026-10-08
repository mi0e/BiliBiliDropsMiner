from __future__ import annotations

import logging
import threading
import time
from typing import Literal

from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.miner import BilibiliWatchTimeMiner, StopOutcome

StopRequestResult = Literal[
    "not_running",
    "stopping_started",
    "force_requested",
    "already_stopping",
]
PollResult = Literal["no_thread", "running", "stopped", "stopped_incomplete"]

# 「停止未完成」期间重复日志的间隔：首次记 warning，之后按这个周期记 info，
# 既不刷屏又能让用户看到仍在等待释放。
STOP_INCOMPLETE_LOG_INTERVAL_SECONDS = 5.0


class WorkerController:
    def __init__(self, *, auto_force_stop_after_seconds: float = 2.0) -> None:
        self.worker_thread: threading.Thread | None = None
        self.miner: BilibiliWatchTimeMiner | None = None
        self.stop_signal_set = False
        self.stopping_in_progress = False
        self.stop_poll_started_at: float | None = None
        self.stop_timeout_warned = False
        self.stop_force_sent = False
        self.stop_outcome: StopOutcome | None = None
        self.stop_incomplete_warned = False
        self.stop_incomplete_logged_at: float | None = None
        self.auto_force_stop_after_seconds = auto_force_stop_after_seconds

    @property
    def is_running(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    @property
    def has_thread(self) -> bool:
        return self.worker_thread is not None

    def start(self, config: MinerConfig, *, logger: logging.Logger) -> bool:
        # A completed worker remains owned until the GUI poll finalizes it.
        # Do not replace its miner reference in that small window.
        if self.has_thread:
            return False

        self.stop_signal_set = False
        self._reset_stop_state()

        self.miner = BilibiliWatchTimeMiner(config)

        def runner() -> None:
            try:
                if self.miner is not None:
                    # 返回值只用于诊断；「是否真的停止」一律以
                    # poll_shutdown() 里的 miner.poll_stop_state() 为准。
                    self.stop_outcome = self.miner.run()
            except Exception:
                logger.exception("GUI worker crashed")

        self.worker_thread = threading.Thread(
            target=runner, name="gui-main-worker", daemon=True
        )
        self.worker_thread.start()
        return True

    def request_stop(self, *, logger: logging.Logger) -> StopRequestResult:
        self.stop_signal_set = True
        if not self.is_running:
            # owner 线程已退出，但 miner 可能仍保留未释放的会话线程。此时不能
            # 清空引用：那会让「停止未完成」失去跟踪对象，界面也会误判为已停止。
            if self._has_residual_sessions():
                self.stopping_in_progress = True
                if self.stop_poll_started_at is None:
                    self.stop_poll_started_at = time.monotonic()
                logger.warning(
                    "停止未完成，仍有 %s 个连接未释放，继续等待",
                    self.miner.residual_session_count if self.miner else 0,
                )
                return "stopping_started"
            self.worker_thread = None
            self.miner = None
            self._reset_stop_state()
            return "not_running"

        if self.stopping_in_progress:
            if self.miner is not None and not self.stop_force_sent:
                self.stop_force_sent = True
                self.miner.stop(force=True)
                logger.warning("已发送强制停止请求")
                return "force_requested"
            logger.info("正在停止，请稍候...")
            return "already_stopping"

        self.stopping_in_progress = True
        self.stop_poll_started_at = time.monotonic()
        self.stop_timeout_warned = False
        self.stop_force_sent = False
        if self.miner is not None:
            self.miner.stop(force=False)
        logger.info("正在停止...")
        return "stopping_started"

    def poll_shutdown(self, *, logger: logging.Logger) -> PollResult:
        if self.worker_thread is None:
            self.miner = None
            self._reset_stop_state()
            return "no_thread"

        if self.worker_thread.is_alive():
            if self.stop_poll_started_at is None:
                self.stop_poll_started_at = time.monotonic()
            elapsed = time.monotonic() - self.stop_poll_started_at
            if (
                elapsed >= self.auto_force_stop_after_seconds
                and not self.stop_force_sent
                and self.miner is not None
            ):
                self.stop_force_sent = True
                self.miner.stop(force=True)
                logger.warning(
                    "停止超过 %.1f 秒，已切换为强制停止",
                    self.auto_force_stop_after_seconds,
                )
            if elapsed >= 5 and not self.stop_timeout_warned:
                logger.warning("停止超过 5 秒，后台线程仍在退出中")
                self.stop_timeout_warned = True
            return "running"

        # owner 线程已退出，但 run() 可能因超出 JOIN_BUDGET_SECONDS 而保留了
        # 未退出的会话线程。此时不能报告 stopped：GUI 会清空 miner 引用并放行
        # 新的启动，而旧连接仍在占用资源。保留所有权并继续轮询，直到
        # poll_stop_state() 确认全部释放。
        if self.miner is not None and (
            self.miner.poll_stop_state() is StopOutcome.STOP_INCOMPLETE
        ):
            self._log_stop_incomplete(logger)
            return "stopped_incomplete"

        logger.info("停止成功")
        self.worker_thread = None
        self.miner = None
        self._reset_stop_state()
        return "stopped"

    def _has_residual_sessions(self) -> bool:
        return self.miner is not None and self.miner.has_residual_sessions

    def _log_stop_incomplete(self, logger: logging.Logger) -> None:
        remaining = self.miner.residual_session_count if self.miner else 0
        now = time.monotonic()
        if not self.stop_incomplete_warned:
            self.stop_incomplete_warned = True
            self.stop_incomplete_logged_at = now
            logger.warning(
                "停止未完成，仍有 %s 个连接未释放；释放完成前不会报告已停止，"
                "也无法重新启动",
                remaining,
            )
            return
        if (
            self.stop_incomplete_logged_at is None
            or now - self.stop_incomplete_logged_at >= STOP_INCOMPLETE_LOG_INTERVAL_SECONDS
        ):
            self.stop_incomplete_logged_at = now
            logger.info("仍在等待 %s 个连接释放", remaining)

    def _reset_stop_state(self) -> None:
        self.stopping_in_progress = False
        self.stop_poll_started_at = None
        self.stop_timeout_warned = False
        self.stop_force_sent = False
        self.stop_outcome = None
        self.stop_incomplete_warned = False
        self.stop_incomplete_logged_at = None
