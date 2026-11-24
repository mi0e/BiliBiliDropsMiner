from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from bilibili_drops_miner.gui_parts.account_status import AccountStatus
from bilibili_drops_miner.gui_parts.config_io import GuiConfigValues
from bilibili_drops_miner.gui_parts.gui_state import GuiStateStore
from bilibili_drops_miner.gui_parts.main_window import MinerGUI


class GuiAccountStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, temp_dir: str) -> MinerGUI:
        state = GuiStateStore(
            QSettings(str(Path(temp_dir) / "gui.ini"), QSettings.IniFormat)
        )
        window = MinerGUI(gui_state=state)
        # Tests below drive deterministic status results directly and never use network.
        window.account_status_controller.close()
        return window

    def test_status_label_and_start_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(temp_dir)
            try:
                window.cookie_edit.setText("cookie")
                window.rooms_edit.setText("1")
                window.worker_controller.start = Mock(return_value=True)
                window._show_warning = Mock()

                window._on_account_status(AccountStatus("invalid", "cookie"))
                self.assertEqual(window.account_status_label.text(), "Cookie 已失效")
                self.assertIn("#ff5d68", window.account_status_label.styleSheet())
                window.start()
                window.worker_controller.start.assert_not_called()
                window._show_warning.assert_called_once()

                window._show_warning.reset_mock()
                window._on_account_status(
                    AccountStatus("valid", "cookie", 42, "测试用户")
                )
                self.assertEqual(window.account_status_label.text(), "账号：测试用户")
                window.start()
                window.worker_controller.start.assert_called_once()
            finally:
                window.close()

    def test_cookie_set_text_paths_enter_checking_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(temp_dir)
            # Re-enable only the synchronous textChanged path; close before
            # the debounce can start a real checker.
            window.account_status_controller._alive = True
            try:
                window._apply_auto_cookie("browser-or-qr-cookie")
                self.assertEqual(window._account_status.kind, "checking")
                self.assertEqual(
                    window._account_status.cookie, "browser-or-qr-cookie"
                )

                window._apply_config_values(
                    GuiConfigValues(
                        cookie="config-cookie",
                        rooms_text="1",
                        thread_count_text="1",
                        reconnect_delay_text="8",
                        task_ids_text="",
                        task_query_interval_text="30",
                        notify_urls_text="",
                        notify_on_task_complete=True,
                        verbose=False,
                    )
                )
                self.assertEqual(window._account_status.kind, "checking")
                self.assertEqual(window._account_status.cookie, "config-cookie")
            finally:
                window.close()

    def test_hot_update_same_uid_and_stop_on_change_or_invalid_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(temp_dir)
            try:
                miner = SimpleNamespace(
                    config=SimpleNamespace(cookie="old"),
                    update_cookie=Mock(),
                )
                window.worker_controller = SimpleNamespace(
                    is_running=True,
                    miner=miner,
                )
                window._running_account_uid = 42
                window.stop = Mock()

                window._on_account_status(
                    AccountStatus("valid", "new", 42, "同一账号")
                )
                miner.update_cookie.assert_called_once_with("new")
                window.stop.assert_not_called()

                window._on_account_status(
                    AccountStatus("error", "new")
                )
                window.stop.assert_not_called()

                window._on_account_status(
                    AccountStatus("valid", "other", 99, "另一账号")
                )
                window._on_account_status(AccountStatus("invalid", "other"))
                window._on_account_status(AccountStatus("error", "other"))
                self.assertEqual(window.account_status_label.text(), "Cookie 已失效")
                self.assertEqual(window.stop.call_count, 1)

                window._account_auto_stop_requested = False
                window._on_account_status(AccountStatus("empty", ""))
                self.assertEqual(window.account_status_label.text(), "Cookie 已失效")
                self.assertEqual(window.stop.call_count, 2)
            finally:
                window.worker_controller = SimpleNamespace(
                    is_running=False,
                    request_stop=Mock(return_value="not_running"),
                )
                window.close()

    def test_finished_invalid_worker_is_reaped_and_ui_is_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(temp_dir)
            window.account_status_controller._alive = True
            worker = SimpleNamespace(
                has_thread=True,
                is_running=False,
                miner=SimpleNamespace(login_invalidated=True),
                poll_shutdown=Mock(return_value="stopped"),
            )
            window.worker_controller = worker
            window._start_progress_animation()
            window._config_sync_timer.start()
            try:
                window._sync_config_to_miner()
                worker.poll_shutdown.assert_called_once()
                self.assertFalse(window.progress_bar.isVisible())
                self.assertFalse(window._config_sync_timer.isActive())
                self.assertEqual(window.account_status_label.text(), "Cookie 已失效")
            finally:
                window.worker_controller = SimpleNamespace(
                    is_running=False,
                    request_stop=Mock(return_value="not_running"),
                )
                window.close()

    def test_login_invalidated_is_reported_before_residual_sessions_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(temp_dir)
            window.account_status_controller.mark_current_invalid = Mock()
            window._show_warning = Mock()
            miner = SimpleNamespace(login_invalidated=True)
            worker = SimpleNamespace(
                has_thread=True,
                is_running=False,
                miner=miner,
                poll_shutdown=Mock(
                    side_effect=["stopped_incomplete", "stopped_incomplete", "stopped"]
                ),
                stop_incomplete_message=Mock(return_value="请重启程序"),
            )
            window.worker_controller = worker
            window._start_progress_animation()
            window._config_sync_timer.start()
            try:
                # owner 退出即可确定登录失效，不等残留会话线程释放。
                window._sync_config_to_miner()
                window.account_status_controller.mark_current_invalid.assert_called_once()
                self.assertIs(worker.miner, miner)
                window._poll_worker_shutdown()
                window._poll_worker_shutdown()
                # 线程最终释放时不能再标一次：用户期间可能已换了新 Cookie。
                window.account_status_controller.mark_current_invalid.assert_called_once()
                window._show_warning.assert_called_once()
            finally:
                window._stop_poll_timer.stop()
                window.worker_controller = SimpleNamespace(
                    is_running=False,
                    request_stop=Mock(return_value="not_running"),
                )
                window.close()

    def test_stop_incomplete_finalizes_ui_and_tells_user_to_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = self._window(temp_dir)
            window._show_warning = Mock()
            window._show_info = Mock()
            worker = SimpleNamespace(
                has_thread=True,
                is_running=False,
                is_stop_incomplete=True,
                miner=SimpleNamespace(login_invalidated=False),
                poll_shutdown=Mock(return_value="stopped_incomplete"),
                request_stop=Mock(return_value="stop_incomplete"),
                start=Mock(return_value=False),
                stop_incomplete_message=Mock(return_value="请重启程序"),
            )
            window.worker_controller = worker
            window._start_progress_animation()
            window._config_sync_timer.start()
            try:
                window._sync_config_to_miner()
                worker.poll_shutdown.assert_called_once()
                # 挂机已结束：进度条与计时器立即收尾，只保留停止轮询跟踪残留。
                self.assertFalse(window.progress_bar.isVisible())
                self.assertEqual(window.progress_bar.maximum(), 1)
                self.assertFalse(window._config_sync_timer.isActive())
                self.assertTrue(window._stop_poll_timer.isActive())
                window._show_warning.assert_called_once_with("停止未完成", "请重启程序")

                # 重复轮询不再弹窗。
                window._poll_worker_shutdown()
                window._show_warning.assert_called_once()

                # 点启动：给出「停止未完成」而不是「助手已在运行中」。
                window._show_warning.reset_mock()
                window.start()
                window._show_warning.assert_called_once_with("停止未完成", "请重启程序")
                window._show_info.assert_not_called()
                worker.start.assert_not_called()

                # 再点停止：同样直接说明需要重启。
                window._show_warning.reset_mock()
                window.stop()
                window._show_warning.assert_called_once_with("停止未完成", "请重启程序")
            finally:
                window._stop_poll_timer.stop()
                window.worker_controller = SimpleNamespace(
                    is_running=False,
                    request_stop=Mock(return_value="not_running"),
                )
                window.close()


if __name__ == "__main__":
    unittest.main()
