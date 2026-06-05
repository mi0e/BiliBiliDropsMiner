from __future__ import annotations

import logging
import threading
import time
from typing import Literal

from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.miner import BilibiliWatchTimeMiner

StopRequestResult = Literal[
    "not_running",
    "stopping_started",
    "force_requested",
    "already_stopping",
]
PollResult = Literal["no_thread", "running", "stopped"]


class WorkerController:
    def __init__(self, *, auto_force_stop_after_seconds: float = 2.0) -> None:
        self.worker_thread: threading.Thread | None = None
        self.miner: BilibiliWatchTimeMiner | None = None
        self.stop_signal_set = False
        self.stopping_in_progress = False
        self.stop_poll_started_at: float | None = None
        self.stop_timeout_warned = False
        self.stop_force_sent = False
        self.auto_force_stop_after_seconds = auto_force_stop_after_seconds

    @property
    def is_running(self) -> bool:
        return self.worker_thread is not None and self.worker_thread.is_alive()

    @property
    def has_thread(self) -> bool:
        return self.worker_thread is not None

    def start(self, config: MinerConfig, *, logger: logging.Logger) -> bool:
        self.stop_signal_set = False
        self._reset_stop_state()
        if self.is_running:
            return False

        self.miner = BilibiliWatchTimeMiner(config)

        def runner() -> None:
            try:
                if self.miner is not None:
                    self.miner.run()
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

        logger.info("停止成功")
        self.worker_thread = None
        self.miner = None
        self._reset_stop_state()
        return "stopped"

    def _reset_stop_state(self) -> None:
        self.stopping_in_progress = False
        self.stop_poll_started_at = None
        self.stop_timeout_warned = False
        self.stop_force_sent = False
