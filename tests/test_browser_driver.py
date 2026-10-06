from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest

from bilibili_drops_miner.gui_parts.browser_driver import (
    BrowserPreparationCancelled, manager_status, prepare_driver, run_manager,
)
from bilibili_drops_miner.gui_parts.browser_actions import BrowserActions


class ManagerProgressTests(unittest.TestCase):
    def test_progress_arrives_before_process_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "progress-received"
            code = (
                "import json,pathlib,sys,time; "
                "print('DEBUG Downloading chromedriver from https://example.invalid',file=sys.stderr,flush=True); "
                "p=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+3\n"
                "while not p.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                "assert p.exists(), 'progress was buffered until exit'\n"
                "print(json.dumps({'driver_path':'driver.exe'}))"
            )
            statuses = []
            def status(message):
                statuses.append(message)
                marker.touch()
            result = run_manager([sys.executable, "-c", code, str(marker)], status, threading.Event(), timeout=5)
            self.assertEqual(result["driver_path"], "driver.exe")
            self.assertIn("下载", statuses[0])
            self.assertNotIn("example.invalid", statuses[0])

    def test_cancel_reaps_manager(self):
        cancel = threading.Event()
        code = "import sys,time; print('Downloading driver',file=sys.stderr,flush=True); time.sleep(20)"
        processes = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        with patch("bilibili_drops_miner.gui_parts.browser_driver.subprocess.Popen", side_effect=launch):
            with self.assertRaises(BrowserPreparationCancelled):
                run_manager([sys.executable, "-c", code], lambda _: cancel.set(), cancel, timeout=5)
        self.assertIsNotNone(processes[0].poll())

    def test_timeout_reaps_manager(self):
        processes = []
        real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process
        started = time.monotonic()
        with patch("bilibili_drops_miner.gui_parts.browser_driver.subprocess.Popen", side_effect=launch):
            with self.assertRaisesRegex(TimeoutError, "超时"):
                run_manager([sys.executable, "-c", "import time; time.sleep(20)"], lambda _: None, threading.Event(), timeout=.2)
        self.assertLess(time.monotonic() - started, 5)
        self.assertIsNotNone(processes[0].poll())

    def test_error_does_not_expose_raw_proxy_details(self):
        code = "import sys; print('https://user:secret@proxy.invalid',file=sys.stderr); sys.exit(1)"
        with self.assertRaises(RuntimeError) as caught:
            run_manager([sys.executable, "-c", code], lambda _: None, threading.Event(), timeout=5)
        self.assertIn("代理", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))

    def test_invalid_output_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "无效结果"):
            run_manager([sys.executable, "-c", "print('not JSON')"], lambda _: None, threading.Event(), timeout=5)

    def test_precancel_does_not_start_process(self):
        cancel = threading.Event()
        cancel.set()
        with patch("bilibili_drops_miner.gui_parts.browser_driver.subprocess.Popen") as launch:
            with self.assertRaises(BrowserPreparationCancelled):
                run_manager([], lambda _: None, cancel)
        launch.assert_not_called()

    def test_explicit_driver_path_bypasses_download(self):
        from selenium import webdriver
        with tempfile.TemporaryDirectory() as directory:
            driver = Path(directory) / "driver.exe"
            driver.touch()
            for browser, key, options in [
                ("chrome", "SE_CHROMEDRIVER", webdriver.ChromeOptions()),
                ("edge", "SE_EDGEDRIVER", webdriver.EdgeOptions()),
            ]:
                with self.subTest(browser=browser), patch.dict(os.environ, {key: str(driver)}):
                    with patch("bilibili_drops_miner.gui_parts.browser_driver.run_manager") as manager:
                        service = prepare_driver(browser, options, lambda _: None, threading.Event())
                    self.assertEqual(service.path, str(driver))
                    manager.assert_not_called()

    def test_stage_messages(self):
        self.assertIn("解压", manager_status("DEBUG Extracting driver") or "")
        self.assertIn("版本", manager_status("DEBUG Required driver: chromedriver 154") or "")
        self.assertIn("就绪", manager_status("INFO Driver path: local") or "")
        self.assertIsNone(manager_status("Unrecognized output"))


class BrowserProgressGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.errors = []
        self.actions = BrowserActions(
            parent=None, show_warning=lambda *_: None,
            show_error=lambda *args: self.errors.append(args),
            post_ui_task=lambda callback, *args: callback(*args),
            set_room_id=lambda _: None, set_cookie=lambda _: None, set_task_ids=lambda _: None,
        )
        self.patcher = patch("bilibili_drops_miner.gui_parts.browser_actions.start_browser_sniff")
        self.start = self.patcher.start()
        self.actions.browser_sniff(None, "test")
        self.callbacks = self.start.call_args.kwargs

    def tearDown(self):
        self.actions.close()
        self.patcher.stop()
        self.app.processEvents()

    def test_stages_elapsed_and_finish_without_cancelling(self):
        dialog = self.actions._preparation_dialog
        self.callbacks["on_preparation_status"]("正在下载浏览器驱动…")
        dialog._started -= 65
        dialog._refresh()
        self.assertEqual((dialog.minimum(), dialog.maximum()), (0, 0))
        self.assertIn("65 秒", dialog.labelText())
        self.assertIn("下载浏览器驱动", dialog.labelText())
        self.callbacks["on_browser_ready"]()
        self.assertFalse(dialog.isVisible())
        self.assertFalse(self.callbacks["cancel_event"].is_set())
        self.callbacks["on_preparation_status"]("late queued status")
        self.callbacks["on_finished"]()
        self.assertIsNone(self.actions._browser_cancel)

    def test_cancel_button_signals_worker_and_suppresses_error(self):
        dialog = self.actions._preparation_dialog
        QTest.mouseClick(dialog.findChild(QPushButton), Qt.LeftButton)
        self.assertTrue(self.callbacks["cancel_event"].is_set())
        self.callbacks["on_error"]("错误", "cancelled operation")
        self.callbacks["on_finished"]()
        self.assertFalse(self.errors)
        self.assertIsNone(self.actions._browser_cancel)

    def test_error_closes_progress_and_next_attempt_can_start(self):
        dialog = self.actions._preparation_dialog
        self.callbacks["on_error"]("错误", "网络不可用")
        self.assertFalse(dialog.isVisible())
        self.assertEqual(self.errors, [("错误", "网络不可用")])
        self.callbacks["on_finished"]()
        self.actions.browser_sniff(None, "retry")
        self.assertEqual(self.start.call_count, 2)

    def test_close_cancels_and_duplicate_click_does_not_start(self):
        self.actions.browser_sniff(None, "duplicate")
        self.assertEqual(self.start.call_count, 1)
        self.actions.close()
        self.assertTrue(self.callbacks["cancel_event"].is_set())
        self.assertIsNone(self.actions._preparation_dialog)


if __name__ == "__main__":
    unittest.main()
