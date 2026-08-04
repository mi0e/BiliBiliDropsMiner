from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import httpx
import qrcode
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QCloseEvent, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from bilibili_drops_miner.client_parts.qr_login import (
    QrLoginApi,
    QrLoginError,
    QrLoginStatus,
)
from bilibili_drops_miner.gui_parts.styles import BUTTON_STYLES


POLL_INTERVAL_SECONDS = 2.0
MAX_LOGIN_SECONDS = 180.0


def make_qr_matrix(url: str) -> list[list[bool]]:
    """Build a QR matrix locally; no image or URL is sent to another service."""

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=1,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    return [[bool(cell) for cell in row] for row in qr.get_matrix()]


def matrix_to_pixmap(matrix: list[list[bool]], target_size: int = 280) -> QPixmap:
    if not matrix or not matrix[0] or any(len(row) != len(matrix[0]) for row in matrix):
        raise ValueError("二维码矩阵无效")
    width = len(matrix[0])
    height = len(matrix)
    scale = max(1, min(target_size // width, target_size // height))
    image = QImage(width * scale, height * scale, QImage.Format_RGB32)
    image.fill(QColor("white"))
    painter = QPainter(image)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, enabled in enumerate(row):
            if enabled:
                painter.drawRect(x * scale, y * scale, scale, scale)
    painter.end()
    return QPixmap.fromImage(image)


class QrLoginDialog(QDialog):
    _matrix_ready = Signal(int, object)
    _status_ready = Signal(int, str)
    _terminal_ready = Signal(int, str, str)

    def __init__(
        self,
        parent: QWidget | None,
        apply_cookie: Callable[[str], None],
        *,
        api_factory: Callable[[], QrLoginApi] = QrLoginApi,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        max_duration: float = MAX_LOGIN_SECONDS,
    ) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self.setWindowTitle("哔哩哔哩扫码登录")
        self.setModal(False)
        self.setMinimumWidth(380)

        self._apply_cookie = apply_cookie
        self._api_factory = api_factory
        self._poll_interval = poll_interval
        self._max_duration = max_duration
        self._session_id = 0
        self._cancel_event: threading.Event | None = None
        self._thread: threading.Thread | None = None

        self.qr_label = QLabel("正在生成二维码…")
        self.qr_label.setAlignment(Qt.AlignCenter)
        self.qr_label.setMinimumSize(280, 280)
        self.qr_label.setStyleSheet(
            "background:#ffffff;color:#5f6368;border-radius:8px;padding:8px;"
        )
        self.status_label = QLabel("正在连接哔哩哔哩…")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setWordWrap(True)

        self.retry_button = QPushButton("重新生成")
        self.retry_button.setStyleSheet(BUTTON_STYLES["blue"])
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self.start_session)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setStyleSheet(BUTTON_STYLES[""])
        self.cancel_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.retry_button)
        buttons.addWidget(self.cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(self.qr_label, alignment=Qt.AlignCenter)
        layout.addWidget(self.status_label)
        layout.addLayout(buttons)

        self._matrix_ready.connect(self._on_matrix_ready)
        self._status_ready.connect(self._on_status_ready)
        self._terminal_ready.connect(self._on_terminal_ready)

    @property
    def session_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start_session(self) -> None:
        if self.session_running:
            return
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._session_id += 1
        session_id = self._session_id
        cancel_event = threading.Event()
        self._cancel_event = cancel_event

        self.qr_label.clear()
        self.qr_label.setText("正在生成二维码…")
        self.status_label.setText("正在连接哔哩哔哩…")
        self.retry_button.setEnabled(False)

        self._thread = threading.Thread(
            target=self._run_session,
            args=(session_id, cancel_event),
            name="bilibili-qr-login",
            daemon=True,
        )
        self._thread.start()

    def cancel_session(self) -> None:
        self._session_id += 1
        if self._cancel_event is not None:
            self._cancel_event.set()

    def reject(self) -> None:
        self.cancel_session()
        super().reject()

    def closeEvent(self, event: QCloseEvent) -> None:
        self.cancel_session()
        super().closeEvent(event)

    def _run_session(self, session_id: int, cancel_event: threading.Event) -> None:
        outcome: tuple[str, str] | None = None
        api: QrLoginApi | None = None
        try:
            api = self._api_factory()
            challenge = api.generate()
            if cancel_event.is_set():
                return
            matrix = make_qr_matrix(challenge.url)
            self._safe_emit("_matrix_ready", session_id, matrix)
            self._safe_emit(
                "_status_ready",
                session_id,
                "请使用哔哩哔哩客户端扫码",
            )

            deadline = time.monotonic() + self._max_duration
            while not cancel_event.wait(self._poll_interval):
                if time.monotonic() >= deadline:
                    outcome = ("expired", "二维码已过期，请重新生成")
                    break
                result = api.poll(challenge.key)
                if cancel_event.is_set():
                    return
                if result.status is QrLoginStatus.UNSCANNED:
                    continue
                if result.status is QrLoginStatus.CONFIRMED_PENDING:
                    self._safe_emit(
                        "_status_ready",
                        session_id,
                        "已扫码，请在手机上确认登录",
                    )
                    continue
                if result.status is QrLoginStatus.EXPIRED:
                    outcome = ("expired", "二维码已过期，请重新生成")
                    break
                if result.status is QrLoginStatus.SUCCESS:
                    outcome = ("success", result.cookie)
                    break
        except QrLoginError as exc:
            outcome = ("error", str(exc))
        except httpx.HTTPError:
            outcome = ("error", "网络请求失败，请检查网络后重试")
        except Exception:
            logging.getLogger(__name__).error("扫码登录发生未预期错误")
            outcome = ("error", "扫码登录失败，请重试")
        finally:
            if api is not None:
                try:
                    api.close()
                except Exception:
                    logging.getLogger(__name__).warning("关闭扫码登录连接失败")

        if outcome is not None and not cancel_event.is_set():
            self._safe_emit(
                "_terminal_ready",
                session_id,
                outcome[0],
                outcome[1],
            )

    def _safe_emit(self, signal_name: str, *args: object) -> None:
        try:
            getattr(self, signal_name).emit(*args)
        except RuntimeError:
            # The parent window may already have destroyed the Qt object.
            pass

    def _is_current(self, session_id: int) -> bool:
        return session_id == self._session_id and self.isVisible()

    def _on_matrix_ready(self, session_id: int, matrix: object) -> None:
        if not self._is_current(session_id) or not isinstance(matrix, list):
            return
        self.qr_label.setText("")
        self.qr_label.setPixmap(matrix_to_pixmap(matrix))

    def _on_status_ready(self, session_id: int, message: str) -> None:
        if self._is_current(session_id):
            self.status_label.setText(message)

    def _on_terminal_ready(self, session_id: int, kind: str, value: str) -> None:
        if not self._is_current(session_id):
            return
        self._thread = None
        self._cancel_event = None
        if kind == "success":
            self._apply_cookie(value)
            logging.getLogger(__name__).info("扫码登录成功，Cookie 已回填")
            self.accept()
            return
        self.status_label.setText(value)
        self.retry_button.setEnabled(True)
