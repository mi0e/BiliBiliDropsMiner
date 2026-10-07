from __future__ import annotations

import logging
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.gui_parts.worker_controller import WorkerController
from bilibili_drops_miner.miner import StopOutcome


class FakeMiner:
    def __init__(self, config: object) -> None:
        self.config = config
        self.started = threading.Event()
        self.release = threading.Event()
        self.stop_calls: list[bool] = []
        # 未退出会话数：>0 时 poll_stop_state() 报告 STOP_INCOMPLETE，
        # 用来复现「超出 JOIN_BUDGET_SECONDS 后 run() 保留所有权」的场景。
        self.residual_sessions = 0
        self.run_outcome = StopOutcome.STOPPED

    def run(self) -> StopOutcome:
        self.started.set()
        self.release.wait(timeout=2)
        return self.run_outcome

    def stop(self, *, force: bool = False) -> None:
        self.stop_calls.append(force)

    @property
    def has_residual_sessions(self) -> bool:
        return self.residual_sessions > 0

    @property
    def residual_session_count(self) -> int:
        return self.residual_sessions

    def poll_stop_state(self) -> StopOutcome:
        if self.residual_sessions:
            return StopOutcome.STOP_INCOMPLETE
        return StopOutcome.STOPPED


class FakeThread:
    def __init__(self, alive: bool) -> None:
        self.alive = alive

    def is_alive(self) -> bool:
        return self.alive


class WorkerControllerTest(unittest.TestCase):
    def test_start_duplicate_stop_and_force_stop_flow(self) -> None:
        logger = logging.getLogger("test.worker_controller")
        controller = WorkerController(auto_force_stop_after_seconds=0.01)
        miners: list[FakeMiner] = []

        def create_miner(config: object) -> FakeMiner:
            miner = FakeMiner(config)
            miners.append(miner)
            return miner

        with patch(
            "bilibili_drops_miner.gui_parts.worker_controller.BilibiliWatchTimeMiner",
            side_effect=create_miner,
        ):
            self.assertTrue(controller.start(object(), logger=logger))
            self.assertTrue(miners[0].started.wait(timeout=1))
            self.assertTrue(controller.is_running)
            self.assertFalse(controller.start(object(), logger=logger))
            self.assertFalse(controller.stop_signal_set)

            self.assertEqual(
                controller.request_stop(logger=logger),
                "stopping_started",
            )
            self.assertEqual(miners[0].stop_calls, [False])

            self.assertEqual(
                controller.request_stop(logger=logger),
                "force_requested",
            )
            self.assertEqual(miners[0].stop_calls, [False, True])

            self.assertEqual(
                controller.request_stop(logger=logger),
                "already_stopping",
            )
            self.assertEqual(miners[0].stop_calls, [False, True])

            miners[0].release.set()
            for _ in range(20):
                if controller.poll_shutdown(logger=logger) == "stopped":
                    break
                time.sleep(0.02)
            else:
                self.fail("worker thread did not stop")

            self.assertFalse(controller.has_thread)
            self.assertFalse(controller.is_running)
            self.assertIsNone(controller.miner)
            self.assertFalse(controller.stopping_in_progress)

    def test_request_stop_when_not_running_resets_worker_state(self) -> None:
        controller = WorkerController()

        self.assertEqual(
            controller.request_stop(logger=logging.getLogger("test.worker_controller")),
            "not_running",
        )

        self.assertFalse(controller.has_thread)
        self.assertIsNone(controller.miner)
        self.assertFalse(controller.stopping_in_progress)

    def test_start_waits_for_completed_worker_to_be_polled(self) -> None:
        controller = WorkerController()
        controller.worker_thread = FakeThread(alive=False)  # type: ignore[assignment]
        controller.miner = FakeMiner(object())  # type: ignore[assignment]

        with patch(
            "bilibili_drops_miner.gui_parts.worker_controller.BilibiliWatchTimeMiner"
        ) as miner_type:
            self.assertFalse(
                controller.start(
                    object(), logger=logging.getLogger("test.worker_controller")
                )
            )
        miner_type.assert_not_called()
        self.assertIsNotNone(controller.miner)

    def test_poll_shutdown_auto_force_and_success_reset(self) -> None:
        controller = WorkerController(auto_force_stop_after_seconds=0.01)
        miner = FakeMiner(object())
        controller.miner = miner  # type: ignore[assignment]
        controller.worker_thread = FakeThread(alive=True)  # type: ignore[assignment]
        controller.stop_poll_started_at = time.monotonic() - 1

        self.assertEqual(
            controller.poll_shutdown(logger=logging.getLogger("test.worker_controller")),
            "running",
        )
        self.assertEqual(miner.stop_calls, [True])

        self.assertEqual(
            controller.poll_shutdown(logger=logging.getLogger("test.worker_controller")),
            "running",
        )
        self.assertEqual(miner.stop_calls, [True])

        controller.worker_thread = FakeThread(alive=False)  # type: ignore[assignment]
        self.assertEqual(
            controller.poll_shutdown(logger=logging.getLogger("test.worker_controller")),
            "stopped",
        )
        self.assertFalse(controller.has_thread)
        self.assertIsNone(controller.miner)
        self.assertFalse(controller.stop_force_sent)

    def test_poll_shutdown_reports_incomplete_and_keeps_miner(self) -> None:
        logger = logging.getLogger("test.worker_controller")
        controller = WorkerController()
        miner = FakeMiner(object())
        miner.residual_sessions = 2
        controller.miner = miner  # type: ignore[assignment]
        controller.worker_thread = FakeThread(alive=False)  # type: ignore[assignment]
        controller.stopping_in_progress = True

        # owner 线程已退出，但会话线程仍未释放：不能报告 stopped，否则 GUI 会
        # 清空 miner 引用并放行新的启动，而旧连接仍在占用资源。
        self.assertEqual(controller.poll_shutdown(logger=logger), "stopped_incomplete")
        self.assertTrue(controller.has_thread)
        self.assertIs(controller.miner, miner)
        self.assertTrue(controller.stop_incomplete_warned)

        # 重复轮询不会重复告警，但仍保持未完成状态。
        self.assertEqual(controller.poll_shutdown(logger=logger), "stopped_incomplete")
        self.assertIs(controller.miner, miner)

        miner.residual_sessions = 0
        self.assertEqual(controller.poll_shutdown(logger=logger), "stopped")
        self.assertFalse(controller.has_thread)
        self.assertIsNone(controller.miner)
        self.assertFalse(controller.stop_incomplete_warned)

    def test_request_stop_keeps_ownership_when_owner_exited_with_residual(self) -> None:
        logger = logging.getLogger("test.worker_controller")
        controller = WorkerController()
        miner = FakeMiner(object())
        miner.residual_sessions = 1
        controller.miner = miner  # type: ignore[assignment]
        controller.worker_thread = FakeThread(alive=False)  # type: ignore[assignment]

        # owner 已退出但有残留：不能走 not_running 的清空路径，必须继续跟踪。
        self.assertEqual(controller.request_stop(logger=logger), "stopping_started")
        self.assertTrue(controller.stopping_in_progress)
        self.assertTrue(controller.has_thread)
        self.assertIs(controller.miner, miner)
        self.assertTrue(controller.stop_signal_set)

    def test_real_miner_keeps_ownership_until_sessions_release(self) -> None:
        # 上游的最小复现：owner 返回时会话线程仍存活，修复前 poll_shutdown()
        # 会返回 stopped、controller.miner 被清空，界面误报停止成功并放行重启。
        logger = logging.getLogger("test.worker_controller")
        controller = WorkerController()
        session_started = threading.Event()
        release_session = threading.Event()

        def thread_entry(*_args) -> None:
            session_started.set()
            release_session.wait(timeout=5)

        config = MinerConfig(cookie="cookie", room_ids=[1])
        with patch(
            "bilibili_drops_miner.miner.BilibiliWatchTimeMiner._probe_login",
            new=AsyncMock(return_value=(42, "user")),
        ), patch(
            "bilibili_drops_miner.miner.BilibiliWatchTimeMiner._thread_entry",
            side_effect=thread_entry,
        ), patch("bilibili_drops_miner.miner.JOIN_BUDGET_SECONDS", 0.05):
            try:
                self.assertTrue(controller.start(config, logger=logger))
                self.assertTrue(session_started.wait(timeout=2))
                self.assertEqual(controller.request_stop(logger=logger), "stopping_started")

                result = ""
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    result = controller.poll_shutdown(logger=logger)
                    if result == "stopped_incomplete":
                        break
                    time.sleep(0.01)

                self.assertEqual(result, "stopped_incomplete")
                self.assertIsNotNone(controller.miner)
                self.assertTrue(controller.has_thread)
                # 旧连接未释放前不允许重新启动。
                self.assertFalse(controller.start(config, logger=logger))

                release_session.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if controller.poll_shutdown(logger=logger) == "stopped":
                        break
                    time.sleep(0.01)

                self.assertFalse(controller.has_thread)
                self.assertIsNone(controller.miner)
            finally:
                release_session.set()


if __name__ == "__main__":
    unittest.main()
