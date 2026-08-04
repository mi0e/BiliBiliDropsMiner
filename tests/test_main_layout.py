from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton

from bilibili_drops_miner.gui_parts.main_layout import (
    MainWindowCallbacks,
    MainWindowWidgets,
    build_main_window_layout,
)


def _noop(*_args, **_kwargs) -> None:
    pass


class MainLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_build_main_window_layout_returns_expected_widgets(self) -> None:
        window = QMainWindow()
        cookie_actions: list[str] = []
        callbacks = MainWindowCallbacks(
            qr_login_cookie=lambda: cookie_actions.append("qr"),
            auto_fetch_cookie=lambda: cookie_actions.append("browser"),
            auto_fetch_room_id=_noop,
            auto_fetch_task_ids=_noop,
            start=_noop,
            stop=_noop,
            load_config=_noop,
            save_config=_noop,
            clear_logs=_noop,
            claim_rewards=_noop,
            refresh_tasks=_noop,
            toggle_log=_noop,
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
            "可留空: F12 从 totalv2 请求中提取 task_ids",
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
        self.assertFalse(widgets.progress_bar.isVisible())
        self.assertEqual(widgets.progress_bar.minimum(), 0)
        self.assertEqual(widgets.progress_bar.maximum(), 1)
        self.assertEqual(widgets.task_text.toPlainText(), "点击“手动刷新”查看任务进度")
        self.assertEqual(widgets.claim_rewards_btn.text(), "领取奖励")
        self.assertEqual(widgets.account_status_label.text(), "账号：未填写 Cookie")
        self.assertEqual(widgets.account_status_label.textFormat(), Qt.PlainText)
        self.assertEqual(widgets.log_toggle_btn.text(), "▶ 运行日志")
        self.assertFalse(widgets.log_text.isVisible())

        cookie_buttons = {
            button.text(): button
            for button in window.centralWidget().findChildren(QPushButton)
            if button.text() in {"扫码登录", "自动获取"}
        }
        self.assertIn("扫码登录", cookie_buttons)
        cookie_buttons["扫码登录"].click()
        # There are three “自动获取” buttons. The purple cookie button is the
        # one whose click invokes the cookie callback.
        for button in window.centralWidget().findChildren(QPushButton):
            if button.text() == "自动获取" and "a78bfa" in button.styleSheet():
                button.click()
                break
        self.assertEqual(cookie_actions, ["qr", "browser"])

        window.close()


if __name__ == "__main__":
    unittest.main()
