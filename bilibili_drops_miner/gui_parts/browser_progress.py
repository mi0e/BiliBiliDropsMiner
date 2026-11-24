from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QProgressDialog, QWidget


class BrowserPreparationDialog(QProgressDialog):
    """Indeterminate progress: Selenium Manager does not expose byte counts."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", "取消", 0, 0, parent)
        self.setWindowTitle("正在准备浏览器")
        self.setWindowModality(Qt.NonModal)
        self.setMinimumDuration(0)
        self.setMinimumWidth(440)
        self.setAutoClose(False)
        self.setAutoReset(False)
        self._started = time.monotonic()
        self._status = "正在检查浏览器驱动…"
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()
        self.canceled.connect(self._timer.stop)
        self._refresh()

    def set_status(self, status: str) -> None:
        self._status = status
        self._refresh()

    def _refresh(self) -> None:
        elapsed = int(time.monotonic() - self._started)
        self.setLabelText(
            f"{self._status}\n已等待 {elapsed} 秒\n\n"
            "首次使用或浏览器升级后可能需要下载驱动。\n"
            "当前无法获取下载百分比；网络较慢时可继续等待，或取消后重试。"
        )

    def finish(self) -> None:
        self._timer.stop()
        self.reset()
        self.hide()
        self.deleteLater()
