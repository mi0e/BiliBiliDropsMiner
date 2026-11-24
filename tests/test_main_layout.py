from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton

from bilibili_drops_miner.gui_parts.about_dialog import build_about_dialog
from bilibili_drops_miner.gui_parts.account_status import AccountStatus
from bilibili_drops_miner.gui_parts.gui_state import GuiStateStore
from bilibili_drops_miner.gui_parts.main_layout import (
    MainWindowCallbacks,
    MainWindowWidgets,
    build_main_window_layout,
)
from bilibili_drops_miner.gui_parts.main_window import MinerGUI


def _noop(*_args, **_kwargs) -> None:
    pass


class MainLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_build_main_window_layout_returns_expected_widgets(self) -> None:
        window = QMainWindow()
        cookie_actions: list[str] = []
        task_id_actions: list[str] = []
        about_calls: list[bool] = []
        callbacks = MainWindowCallbacks(
            qr_login_cookie=lambda: cookie_actions.append("qr"),
            auto_fetch_cookie=lambda: cookie_actions.append("browser"),
            auto_fetch_room_id=_noop,
            auto_fetch_task_ids_mode1=lambda: task_id_actions.append("mode1"),
            auto_fetch_task_ids_mode2=lambda: task_id_actions.append("mode2"),
            start=_noop,
            stop=_noop,
            load_config=_noop,
            save_config=_noop,
            clear_logs=_noop,
            claim_rewards=_noop,
            refresh_tasks=_noop,
            toggle_log=_noop,
            show_about=lambda: about_calls.append(True),
        )

        widgets = build_main_window_layout(window, callbacks)

        self.assertIsInstance(widgets, MainWindowWidgets)
        self.assertIsNotNone(window.centralWidget())
        self.assertEqual(
            widgets.cookie_edit.placeholderText(),
            "必填: SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx",
        )
        self.assertEqual(widgets.rooms_edit.placeholderText(), "必填: 直播间号，多个用逗号分隔")
        self.assertEqual(widgets.rooms_edit.text(), "23612045")
        self.assertEqual(
            widgets.task_ids_edit.placeholderText(),
            "可留空: 可用静态解析或浏览器嗅探自动获取",
        )
        self.assertEqual(
            widgets.notify_urls_edit.placeholderText(),
            "可留空: 通知 URL，如 gotify://host/token",
        )
        self.assertEqual(widgets.threads_edit.text(), "1")
        self.assertEqual(widgets.reconnect_edit.text(), "8")
        self.assertEqual(widgets.task_interval_edit.text(), "30")
        self.assertEqual(widgets.verbose_check.text(), "详细日志")
        self.assertEqual(widgets.disable_task_notify_check.text(), "禁用任务完成通知")
        self.assertEqual(widgets.auto_claim_rewards_check.text(), "自动领取")
        self.assertFalse(widgets.auto_claim_rewards_check.isChecked())
        self.assertIn("100%", widgets.auto_claim_rewards_check.toolTip())
        self.assertFalse(widgets.progress_bar.isVisible())
        self.assertEqual(widgets.progress_bar.minimum(), 0)
        self.assertEqual(widgets.progress_bar.maximum(), 1)
        self.assertEqual(widgets.task_text.toPlainText(), "点击“手动刷新”查看任务进度")
        self.assertEqual(widgets.claim_rewards_btn.text(), "领取奖励")
        self.assertEqual(widgets.account_status_label.text(), "账号：未填写 Cookie")
        self.assertEqual(widgets.account_status_label.textFormat(), Qt.PlainText)
        self.assertEqual(widgets.log_toggle_btn.text(), "▶ 运行日志")
        self.assertFalse(widgets.log_text.isVisible())
        # 初始空闲：只能启动。
        self.assertTrue(widgets.start_btn.isEnabled())
        self.assertFalse(widgets.stop_btn.isEnabled())
        for button in window.centralWidget().findChildren(QPushButton):
            if button.text() == "关于":
                button.click()
        self.assertEqual(about_calls, [True])

        cookie_buttons = {
            button.text(): button
            for button in window.centralWidget().findChildren(QPushButton)
            if button.text() in {"扫码登录", "自动获取"}
        }
        self.assertIn("扫码登录", cookie_buttons)
        cookie_buttons["扫码登录"].click()
        # The purple cookie button is the one whose click invokes the cookie callback.
        for button in window.centralWidget().findChildren(QPushButton):
            if button.text() == "自动获取" and "a78bfa" in button.styleSheet():
                button.click()
                break
        self.assertEqual(cookie_actions, ["qr", "browser"])

        labels = {
            button.text() for button in window.centralWidget().findChildren(QPushButton)
        }
        self.assertIn("自动获取模式1", labels)
        self.assertIn("自动获取模式2", labels)
        for button in window.centralWidget().findChildren(QPushButton):
            if button.text() in {"自动获取模式1", "自动获取模式2"}:
                button.click()
        self.assertEqual(task_id_actions, ["mode1", "mode2"])

        window.close()


class RunButtonStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_buttons_follow_worker_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GuiStateStore(
                QSettings(str(Path(temp_dir) / "gui.ini"), QSettings.IniFormat)
            )
            window = MinerGUI(gui_state=state)
            window.account_status_controller.close()
            try:
                window.cookie_edit.setText("cookie")
                window._on_account_status(AccountStatus("valid", "cookie", 42, "u"))
                thread = Mock()
                thread.is_alive.return_value = True
                controller = window.worker_controller

                def fake_start(_config, *, logger):
                    controller.worker_thread = thread
                    return True

                controller.start = fake_start

                def buttons() -> tuple[bool, bool]:
                    return window.start_btn.isEnabled(), window.stop_btn.isEnabled()

                self.assertEqual(buttons(), (True, False))
                window.start()
                self.assertEqual(buttons(), (False, True))
                window.stop()  # 停止中：仍只能停止（再点会升级为强制停止）
                self.assertEqual(buttons(), (False, True))
                thread.is_alive.return_value = False
                window._poll_worker_shutdown()
                self.assertEqual(buttons(), (True, False))
            finally:
                window.close()


class AboutDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_buttons_open_repository_links(self) -> None:
        repo = "https://github.com/example/repo"
        dialog = build_about_dialog(
            None,
            app_version="1.2.3",
            update_channel="release",
            repository_url=repo,
            releases_url=repo + "/releases/latest",
        )
        labels = [label.text() for label in dialog.findChildren(QLabel)]
        self.assertIn("版本 1.2.3 · release\ngithub.com/example/repo", labels)
        opened = []
        with patch(
            "bilibili_drops_miner.gui_parts.about_dialog.webbrowser.open",
            side_effect=opened.append,
        ):
            for button in dialog.findChildren(QPushButton):
                button.click()
        self.assertEqual(
            sorted(opened),
            sorted([repo + "/issues", repo + "/releases/latest", repo]),
        )
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
