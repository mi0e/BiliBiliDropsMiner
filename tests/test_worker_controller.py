from __future__ import annotations

import logging
import threading
import time
import unittest
from unittest.mock import patch

from bilibili_drops_miner.gui_parts.worker_controller import WorkerController


class FakeMiner:
    def __init__(self, config: object) -> None:
        self.config = config
        self.started = threading.Event()
        self.release = threading.Event()
        self.stop_calls: list[bool] = []

    def run(self) -> None:
        self.started.set()
        self.release.wait(timeout=2)

    def stop(self, *, force: bool = False) -> None:
        self.stop_calls.append(force)


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


if __name__ == "__main__":
    unittest.main()
