from __future__ import annotations

import os
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QWidget

from bilibili_drops_miner.gui_parts.account_status import (
    AccountStatus,
    AccountStatusController,
)


class AccountStatusControllerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _wait_for(self, predicate, timeout: float = 1.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.005)
        self.fail("timed out waiting for account status")

    def test_valid_invalid_and_network_error_are_distinct(self) -> None:
        parent = QWidget()
        cookie = [""]
        results: list[AccountStatus] = []

        def checker(value: str) -> tuple[int | None, str]:
            if value == "valid":
                return 42, "测试用户"
            if value == "invalid":
                return None, ""
            raise OSError("offline")

        controller = AccountStatusController(
            parent,
            get_cookie=lambda: cookie[0],
            on_status=results.append,
            checker=checker,
            debounce_ms=0,
            recheck_ms=60_000,
        )
        try:
            for value, expected in (
                ("valid", "valid"),
                ("invalid", "invalid"),
                ("network", "error"),
            ):
                cookie[0] = value
                controller.cookie_changed()
                self._wait_for(
                    lambda: results[-1].cookie == value
                    and results[-1].kind == expected
                )
            self.assertEqual(results[1].uid, 42)
            self.assertEqual(results[1].uname, "测试用户")
        finally:
            controller.close()
            parent.close()

    def test_old_result_cannot_overwrite_new_cookie(self) -> None:
        parent = QWidget()
        cookie = ["old"]
        old_started = threading.Event()
        release_old = threading.Event()
        results: list[AccountStatus] = []

        def checker(value: str) -> tuple[int | None, str]:
            if value == "old":
                old_started.set()
                release_old.wait(timeout=1)
                return 1, "旧账号"
            return 2, "新账号"

        controller = AccountStatusController(
            parent,
            get_cookie=lambda: cookie[0],
            on_status=results.append,
            checker=checker,
            debounce_ms=0,
            recheck_ms=60_000,
        )
        try:
            controller.cookie_changed()
            self._wait_for(old_started.is_set)
            cookie[0] = "new"
            controller.cookie_changed()
            self._wait_for(
                lambda: results[-1].kind == "valid" and results[-1].uid == 2
            )
            release_old.set()
            time.sleep(0.02)
            self.app.processEvents()
            self.assertEqual(results[-1].uid, 2)
        finally:
            release_old.set()
            controller.close()
            parent.close()

    def test_close_discards_inflight_result(self) -> None:
        parent = QWidget()
        cookie = ["cookie"]
        started = threading.Event()
        release = threading.Event()
        results: list[AccountStatus] = []

        def checker(_value: str) -> tuple[int | None, str]:
            started.set()
            release.wait(timeout=1)
            return 42, "late"

        controller = AccountStatusController(
            parent,
            get_cookie=lambda: cookie[0],
            on_status=results.append,
            checker=checker,
            debounce_ms=0,
        )
        controller.cookie_changed()
        self._wait_for(started.is_set)
        before_close = len(results)
        controller.close()
        release.set()
        time.sleep(0.02)
        self.app.processEvents()
        self.assertEqual(len(results), before_close)
        parent.close()

    def test_safe_emit_ignores_deleted_qobject_source(self) -> None:
        parent = QWidget()
        results: list[AccountStatus] = []
        controller = AccountStatusController(
            parent,
            get_cookie=lambda: "cookie",
            on_status=results.append,
            checker=lambda _cookie: (42, "user"),
        )
        safe_emit = controller._safe_emit_result
        controller.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

        uncaught: list[BaseException] = []
        original_hook = threading.excepthook
        threading.excepthook = lambda args: uncaught.append(args.exc_value)
        try:
            thread = threading.Thread(
                target=safe_emit,
                args=(1, "cookie", AccountStatus("valid", "cookie", 42, "user")),
            )
            thread.start()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.app.processEvents()
        finally:
            threading.excepthook = original_hook
            parent.close()

        self.assertEqual(uncaught, [])
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
