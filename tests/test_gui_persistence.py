from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog

from bilibili_drops_miner.gui_parts.app_style import configure_qt_app
from bilibili_drops_miner.gui_parts.config_io import save_config_data
from bilibili_drops_miner.gui_parts.gui_state import GuiStateStore
from bilibili_drops_miner.gui_parts.main_window import MinerGUI


class GuiPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        configure_qt_app(cls.app)

    @staticmethod
    def _state_for(directory: str) -> GuiStateStore:
        settings = QSettings(
            str(Path(directory) / "gui.ini"),
            QSettings.IniFormat,
        )
        return GuiStateStore(settings)

    @staticmethod
    def _config_payload() -> dict[str, object]:
        return {
            "cookie": "SESSDATA=test; bili_jct=test",
            "room_ids": [123, 456],
            "thread_count": 3,
            "reconnect_delay_seconds": 11,
            "enable_web_heartbeat": True,
            "task_ids": ["task-a", "task-b"],
            "task_query_interval_seconds": 42,
            "notify_urls": ["gotify://example.invalid/token"],
            "notify_on_task_complete": False,
            "verbose": True,
        }

    def test_first_launch_geometry_intersects_primary_screen(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            window = MinerGUI(gui_state=self._state_for(temp_dir))
            try:
                screen = QApplication.primaryScreen()
                self.assertIsNotNone(screen)
                self.assertTrue(
                    window.frameGeometry().intersects(screen.availableGeometry())
                )
            finally:
                window.close()

    def test_window_geometry_is_saved_and_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state_for(temp_dir)
            first = MinerGUI(gui_state=state)
            first.resize(860, 700)
            first.move(20, 30)
            first.show()
            self.app.processEvents()
            expected_size = first.size()
            first.close()

            self.assertFalse(state.window_geometry().isEmpty())

            second = MinerGUI(gui_state=state)
            try:
                self.assertEqual(second.size(), expected_size)
            finally:
                second.close()

    def test_startup_auto_loads_last_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            save_config_data(config_path, self._config_payload())
            state = self._state_for(temp_dir)
            state.set_last_config_path(config_path)
            state.sync()

            window = MinerGUI(gui_state=state)
            try:
                self.assertEqual(
                    window.cookie_edit.text(),
                    "SESSDATA=test; bili_jct=test",
                )
                self.assertEqual(window.rooms_edit.text(), "123,456")
                self.assertEqual(window.threads_edit.text(), "3")
                self.assertEqual(window.reconnect_edit.text(), "11")
                self.assertEqual(window.task_ids_edit.text(), "task-a,task-b")
                self.assertEqual(window.task_interval_edit.text(), "42")
                self.assertEqual(
                    window.notify_urls_edit.text(),
                    "gotify://example.invalid/token",
                )
                self.assertTrue(window.disable_task_notify_check.isChecked())
                self.assertTrue(window.verbose_check.isChecked())
            finally:
                window.close()

    def test_missing_last_config_path_is_forgotten(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._state_for(temp_dir)
            state.set_last_config_path(Path(temp_dir) / "missing.json")
            state.sync()

            window = MinerGUI(gui_state=state)
            try:
                self.assertIsNone(state.last_config_path())
                self.assertEqual(window.cookie_edit.text(), "")
                self.assertEqual(window.rooms_edit.text(), "23612045")
            finally:
                window.close()

    def test_invalid_last_config_does_not_open_modal_or_forget_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "invalid.json"
            config_path.write_text("not-json", encoding="utf-8")
            state = self._state_for(temp_dir)
            state.set_last_config_path(config_path)
            state.sync()

            window = MinerGUI(gui_state=state)
            try:
                self.assertEqual(state.last_config_path(), config_path.resolve())
                self.assertEqual(window.cookie_edit.text(), "")
            finally:
                window.close()

    def test_manual_load_remembers_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "loaded.json"
            save_config_data(config_path, self._config_payload())
            state = self._state_for(temp_dir)
            window = MinerGUI(gui_state=state)
            try:
                with patch.object(
                    QFileDialog,
                    "getOpenFileName",
                    return_value=(str(config_path), "JSON 文件 (*.json)"),
                ):
                    window.load_config()

                self.assertEqual(state.last_config_path(), config_path.resolve())
                self.assertEqual(window.rooms_edit.text(), "123,456")
            finally:
                window.close()

    def test_manual_save_remembers_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "saved.json"
            state = self._state_for(temp_dir)
            window = MinerGUI(gui_state=state)
            window.cookie_edit.setText("SESSDATA=test; bili_jct=test")
            try:
                with patch.object(
                    QFileDialog,
                    "getSaveFileName",
                    return_value=(str(config_path), "JSON 文件 (*.json)"),
                ):
                    window.save_config()

                self.assertEqual(state.last_config_path(), config_path.resolve())
                payload = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["cookie"], "SESSDATA=test; bili_jct=test")
            finally:
                window.close()

    def test_qr_login_is_wired_to_cookie_field_and_window_close_cancels(self) -> None:
        instances = []

        class FakeQrLoginDialog(QDialog):
            def __init__(self, parent, apply_cookie) -> None:
                super().__init__(parent)
                self.apply_cookie = apply_cookie
                self.started = False
                self.cancelled = False
                instances.append(self)

            def start_session(self) -> None:
                self.started = True

            def cancel_session(self) -> None:
                self.cancelled = True

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "bilibili_drops_miner.gui_parts.main_window.QrLoginDialog",
            FakeQrLoginDialog,
        ):
            window = MinerGUI(gui_state=self._state_for(temp_dir))
            window.qr_login_cookie()
            dialog = instances[0]

            self.assertTrue(dialog.started)
            self.assertIs(dialog.parent(), window)
            dialog.apply_cookie("SESSDATA=session; bili_jct=csrf; DedeUserID=42")
            self.assertEqual(
                window.cookie_edit.text(),
                "SESSDATA=session; bili_jct=csrf; DedeUserID=42",
            )

            # A visible session is reused instead of starting a second one.
            window.qr_login_cookie()
            self.assertEqual(len(instances), 1)

            window.close()
            self.assertTrue(dialog.cancelled)


if __name__ == "__main__":
    unittest.main()
