from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QPushButton

from bilibili_drops_miner.gui_parts.browser_sniffer import (
    is_sniff_finished,
    select_login_cookies,
)
from bilibili_drops_miner.gui_parts.browser_utils import (
    browser_try_order,
    find_browser_binary,
)
from bilibili_drops_miner.gui_parts.app_style import configure_qt_app
from bilibili_drops_miner.gui_parts.gui_state import GuiStateStore
from bilibili_drops_miner.gui_parts.main_window import APP_VERSION, MinerGUI


class MacBrowserSupportTests(unittest.TestCase):
    def test_user_applications_chrome_is_detected(self) -> None:
        user_chrome = os.path.expanduser(
            "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        )

        def exists(path: str) -> bool:
            return path == user_chrome

        with (
            patch("bilibili_drops_miner.gui_parts.browser_utils.sys.platform", "darwin"),
            patch(
                "bilibili_drops_miner.gui_parts.browser_utils.os.path.exists",
                side_effect=exists,
            ),
        ):
            self.assertEqual(find_browser_binary("chrome"), user_chrome)

    def test_browser_preference_is_first(self) -> None:
        with patch(
            "bilibili_drops_miner.gui_parts.browser_utils.available_browsers",
            return_value=["chrome", "edge"],
        ):
            self.assertEqual(browser_try_order("edge"), ("edge", "chrome"))

    def test_cookie_capture_requires_logged_in_identity(self) -> None:
        cookies = [
            {"name": "SESSDATA", "value": "a"},
            {"name": "DedeUserID", "value": "1"},
            {"name": "bili_jct", "value": "b"},
            {"name": "unrelated", "value": "x"},
        ]
        selected = select_login_cookies(cookies)
        self.assertIsNotNone(selected)
        self.assertEqual(
            {cookie["name"] for cookie in selected or []},
            {"SESSDATA", "DedeUserID", "bili_jct"},
        )

    def test_capture_completion_modes(self) -> None:
        pairs = ((True, False), (True, True))
        self.assertTrue(is_sniff_finished(pairs, finish_on_any=True))
        self.assertFalse(is_sniff_finished(pairs, finish_on_any=False))


class GuiParitySmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        configure_qt_app(cls.app)

    def test_all_desktop_actions_are_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = GuiStateStore(
                QSettings(
                    os.path.join(temp_dir, "gui.ini"),
                    QSettings.IniFormat,
                )
            )
            window = MinerGUI(gui_state=state)
            try:
                window.show()
                self.app.processEvents()
                self.assertEqual(
                    window.windowTitle(), f"Bilibili 直播掉宝助手 {APP_VERSION}"
                )
                labels = {
                    button.text() for button in window.findChildren(QPushButton)
                }
                self.assertTrue(
                    {
                        "自动获取",
                        "自动获取模式1",
                        "自动获取模式2",
                        "启动",
                        "停止",
                        "加载配置",
                        "保存配置",
                        "清空日志",
                        "领取奖励",
                        "手动刷新",
                        "▶ 运行日志",
                    }.issubset(labels)
                )
            finally:
                window.close()
                self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
