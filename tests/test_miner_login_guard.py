from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.miner import BilibiliWatchTimeMiner, SessionPlan, StopOutcome


def config() -> MinerConfig:
    return MinerConfig(cookie="cookie", room_ids=[1])


class MinerLoginGuardTest(unittest.TestCase):
    def test_active_count_excludes_staggered_threads_and_returns_to_zero(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        miner._uid = 42

        class FakeClient:
            def __init__(self, _cookie):
                pass

            def update_cookie(self, _cookie):
                pass

            async def close(self):
                pass

        async def scenario():
            started = asyncio.Event()

            class FakeWorker:
                def __init__(self, **_kwargs):
                    pass

                async def run_forever(self):
                    started.set()
                    await asyncio.Event().wait()

                async def stop(self):
                    pass

            with patch("bilibili_drops_miner.miner.BilibiliClient", FakeClient), patch(
                "bilibili_drops_miner.miner.X25KnWorker", FakeWorker
            ):
                active = asyncio.create_task(miner._thread_loop(SessionPlan(1, 1), 1))
                queued = asyncio.create_task(miner._thread_loop(SessionPlan(1, 2), 30))
                try:
                    await asyncio.wait_for(started.wait(), timeout=1)
                    self.assertEqual(miner.active_session_count, 1)
                finally:
                    miner.stop()
                    await asyncio.wait_for(asyncio.gather(active, queued), timeout=2)
                self.assertEqual(miner.active_session_count, 0)
                self.assertEqual(miner._session_tasks, set())

        asyncio.run(scenario())

    def test_worker_start_failure_does_not_increase_active_count(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        miner._uid = 42
        client = AsyncMock()
        with patch("bilibili_drops_miner.miner.BilibiliClient", return_value=client), patch(
            "bilibili_drops_miner.miner.X25KnWorker", side_effect=RuntimeError("failed")
        ):
            # update_cookie is synchronous on the real client.
            from unittest.mock import Mock
            client.update_cookie = Mock()
            with self.assertRaises(RuntimeError):
                asyncio.run(miner._thread_loop(SessionPlan(1, 1), 1))
        self.assertEqual(miner.active_session_count, 0)
        self.assertEqual(miner._clients, [])
        client.close.assert_awaited_once()

    def test_invalid_initial_cookie_does_not_start_sessions(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        miner._probe_login = AsyncMock(return_value=(None, ""))
        with patch.object(miner, "_thread_entry") as thread_entry:
            with self.assertRaisesRegex(RuntimeError, "Cookie 已失效"):
                miner.run()
        thread_entry.assert_not_called()
        self.assertTrue(miner.login_invalidated)

    def test_watchdog_stops_on_explicit_logout(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        miner._probe_login = AsyncMock(
            side_effect=[(42, "user"), (None, "")]
        )

        def thread_entry(*_args) -> None:
            miner._stop_event.wait(timeout=2)

        with patch.object(miner, "_thread_entry", side_effect=thread_entry), patch(
            "bilibili_drops_miner.miner.LOGIN_WATCHDOG_INTERVAL_SECONDS", 0.01
        ):
            miner.run()
        self.assertTrue(miner.login_invalidated)

    def test_watchdog_network_error_does_not_mark_cookie_invalid(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        calls = 0

        async def probe() -> tuple[int | None, str]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 42, "user"
            if calls == 2:
                raise OSError("temporary offline")
            miner.stop()
            return 42, "user"

        miner._probe_login = probe  # type: ignore[method-assign]

        def thread_entry(*_args) -> None:
            miner._stop_event.wait(timeout=3)

        with patch.object(miner, "_thread_entry", side_effect=thread_entry), patch(
            "bilibili_drops_miner.miner.LOGIN_WATCHDOG_INTERVAL_SECONDS", 0.01
        ):
            miner.run()
        self.assertGreaterEqual(calls, 3)
        self.assertFalse(miner.login_invalidated)

    def test_late_client_registration_uses_latest_cookie_atomically(self) -> None:
        constructed = threading.Event()
        release_constructor = threading.Event()
        registered_with_new_cookie = threading.Event()
        clients = []

        class FakeClient:
            def __init__(self, cookie: str) -> None:
                self.cookie = cookie
                clients.append(self)
                constructed.set()
                release_constructor.wait(timeout=2)

            def update_cookie(self, cookie: str) -> None:
                self.cookie = cookie
                if cookie == "new-cookie":
                    registered_with_new_cookie.set()

            async def close(self) -> None:
                return None

        class FakeWorker:
            def __init__(self, **_kwargs) -> None:
                pass

            async def run_forever(self) -> None:
                await asyncio.Event().wait()

            async def stop(self) -> None:
                return None

        miner = BilibiliWatchTimeMiner(config())
        miner._uid = 42

        def run_loop() -> None:
            asyncio.run(miner._thread_loop(SessionPlan(1, 1), 1))

        with patch("bilibili_drops_miner.miner.BilibiliClient", FakeClient), patch(
            "bilibili_drops_miner.miner.X25KnWorker", FakeWorker
        ):
            loop_thread = threading.Thread(target=run_loop)
            loop_thread.start()
            self.assertTrue(constructed.wait(timeout=1))
            miner.update_cookie("new-cookie")
            release_constructor.set()
            self.assertTrue(registered_with_new_cookie.wait(timeout=1))
            miner.stop()
            loop_thread.join(timeout=2)

        self.assertFalse(loop_thread.is_alive())
        self.assertEqual(miner.config.cookie, "new-cookie")
        self.assertEqual(clients[0].cookie, "new-cookie")
        self.assertEqual(miner._clients, [])

    def test_run_waits_until_all_session_threads_really_exit(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        miner._probe_login = AsyncMock(return_value=(42, "user"))
        session_started = threading.Event()
        release_session = threading.Event()
        outcomes: list[StopOutcome] = []

        def thread_entry(*_args) -> None:
            session_started.set()
            release_session.wait(timeout=3)

        def run_miner() -> None:
            outcomes.append(miner.run())

        run_thread = threading.Thread(target=run_miner)
        with patch.object(miner, "_thread_entry", side_effect=thread_entry), patch(
            "bilibili_drops_miner.miner.LOGIN_WATCHDOG_INTERVAL_SECONDS", 1000
        ):
            run_thread.start()
            self.assertTrue(session_started.wait(timeout=1))
            miner.stop(force=True)

            # The owner must not return and let the GUI report stopped while a
            # session is still alive after its bounded joins.
            run_thread.join(timeout=0.4)
            self.assertTrue(run_thread.is_alive())
            self.assertTrue(any(thread.is_alive() for thread in miner._threads))

            release_session.set()
            run_thread.join(timeout=2)

        self.assertFalse(run_thread.is_alive())
        self.assertEqual(outcomes, [StopOutcome.STOPPED])
        self.assertEqual(miner._threads, [])

    def test_run_reports_incomplete_stop_and_keeps_thread_ownership(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        miner._probe_login = AsyncMock(return_value=(42, "user"))
        session_started = threading.Event()
        release_session = threading.Event()
        outcomes: list[StopOutcome] = []

        def thread_entry(*_args) -> None:
            session_started.set()
            release_session.wait(timeout=5)

        def run_miner() -> None:
            outcomes.append(miner.run())

        run_thread = threading.Thread(target=run_miner)
        with patch.object(miner, "_thread_entry", side_effect=thread_entry), patch(
            "bilibili_drops_miner.miner.JOIN_BUDGET_SECONDS", 0.05
        ):
            run_thread.start()
            self.assertTrue(session_started.wait(timeout=1))
            miner.stop(force=True)
            run_thread.join(timeout=2)
            self.assertFalse(run_thread.is_alive())

            # 预算耗尽：如实报告未完成，并把未退出线程的所有权留给调用方，
            # 而不是清空 _threads 让界面误判为已停止。
            self.assertEqual(outcomes, [StopOutcome.STOP_INCOMPLETE])
            self.assertTrue(miner.has_residual_sessions)
            self.assertEqual(miner.residual_session_count, 1)
            self.assertTrue(any(thread.is_alive() for thread in miner._threads))

            release_session.set()
            deadline = time.monotonic() + 2
            while miner.poll_stop_state() is not StopOutcome.STOPPED:
                if time.monotonic() >= deadline:
                    self.fail("session thread did not exit in time")
                time.sleep(0.01)

        self.assertEqual(miner._threads, [])
        self.assertEqual(miner._clients, [])

    def test_join_budget_is_shared_by_blocking_threads(self) -> None:
        miner = BilibiliWatchTimeMiner(
            MinerConfig(cookie="cookie", room_ids=list(range(1, 9)))
        )
        miner._probe_login = AsyncMock(return_value=(42, "user"))
        all_started = threading.Event()
        release_session = threading.Event()
        lock = threading.Lock()
        started_count = 0
        outcomes: list[StopOutcome] = []

        def thread_entry(*_args) -> None:
            nonlocal started_count
            with lock:
                started_count += 1
                if started_count >= 8:
                    all_started.set()
            release_session.wait(timeout=10)

        def run_miner() -> None:
            outcomes.append(miner.run())

        run_thread = threading.Thread(target=run_miner)
        with patch.object(miner, "_thread_entry", side_effect=thread_entry), patch(
            "bilibili_drops_miner.miner.JOIN_BUDGET_SECONDS", 0.3
        ), patch("bilibili_drops_miner.miner.LOGIN_WATCHDOG_INTERVAL_SECONDS", 1000):
            run_thread.start()
            self.assertTrue(all_started.wait(timeout=2))
            miner.stop(force=True)
            began = time.monotonic()
            run_thread.join(timeout=5)
            elapsed = time.monotonic() - began

            # 旧实现：finally 里的 8 个 join(0.2) 一轮要 1.6 秒；修复后
            # 每次 join 都服从 0.3 秒总预算。
            self.assertFalse(run_thread.is_alive())
            self.assertLess(elapsed, 1.0)
            self.assertEqual(outcomes, [StopOutcome.STOP_INCOMPLETE])
            self.assertEqual(miner.residual_session_count, 8)

            release_session.set()
            deadline = time.monotonic() + 3
            while miner.poll_stop_state() is not StopOutcome.STOPPED:
                if time.monotonic() >= deadline:
                    self.fail("session threads did not exit in time")
                time.sleep(0.01)

    def test_run_refuses_restart_while_sessions_residual(self) -> None:
        miner = BilibiliWatchTimeMiner(config())
        miner._probe_login = AsyncMock(return_value=(42, "user"))
        release_session = threading.Event()
        session = threading.Thread(
            target=release_session.wait, args=(5,), daemon=True
        )
        session.start()
        miner._threads.append(session)
        try:
            with self.assertRaisesRegex(RuntimeError, "上一次停止未完成"):
                miner.run()
            self.assertTrue(miner.has_residual_sessions)
        finally:
            release_session.set()
            session.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
