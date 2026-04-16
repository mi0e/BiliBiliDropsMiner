from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QFont, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from bilibili_drops_miner.client import BilibiliClient
from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.logging_utils import setup_logging
from bilibili_drops_miner.miner import BilibiliWatchTimeMiner
from bilibili_drops_miner.utils import parse_room_ids, parse_task_ids


class QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]") -> None:
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:
            self.handleError(record)


_BUTTON_STYLES: dict[str, str] = {
    "green": (
        "QPushButton{background:#22c55e;color:#ffffff;border:0;border-radius:6px;"
        "padding:8px 18px;min-height:20px;font-weight:600;}"
        "QPushButton:hover{background:#16a34a;}"
        "QPushButton:pressed{background:#15803d;}"
    ),
    "red": (
        "QPushButton{background:#ef4444;color:#ffffff;border:0;border-radius:6px;"
        "padding:8px 18px;min-height:20px;font-weight:600;}"
        "QPushButton:hover{background:#dc2626;}"
        "QPushButton:pressed{background:#b91c1c;}"
    ),
    "blue": (
        "QPushButton{background:#4f8cff;color:#ffffff;border:0;border-radius:6px;"
        "padding:8px 18px;min-height:20px;font-weight:600;}"
        "QPushButton:hover{background:#3b73e6;}"
        "QPushButton:pressed{background:#2e5fc4;}"
    ),
    "purple": (
        "QPushButton{background:#a78bfa;color:#ffffff;border:0;border-radius:6px;"
        "padding:8px 18px;min-height:20px;font-weight:600;}"
        "QPushButton:hover{background:#8b6ff0;}"
        "QPushButton:pressed{background:#7057d6;}"
    ),
    "gray": (
        "QPushButton{background:#3a3f4b;color:#e6e7eb;border:0;border-radius:6px;"
        "padding:8px 18px;min-height:20px;font-weight:600;}"
        "QPushButton:hover{background:#454b58;}"
        "QPushButton:pressed{background:#2f343e;}"
    ),
    "": (
        "QPushButton{background:#2f343e;color:#e6e7eb;border:1px solid #3a3f4b;"
        "border-radius:6px;padding:8px 18px;min-height:20px;font-weight:500;}"
        "QPushButton:hover{background:#363b47;border-color:#4a5060;}"
        "QPushButton:pressed{background:#272b34;}"
    ),
}


_CARD_STYLE = (
    "QFrame#card{background:#242832;border:1px solid #2f3440;border-radius:10px;}"
)


class MinerGUI(QMainWindow):
    # Cross-thread UI dispatcher. Any background thread may emit this signal;
    # Qt auto-queues the call onto the GUI thread (sender lives in non-Qt thread).
    ui_call = Signal(object, tuple, dict)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Bilibili 直播掉宝助手")
        self.resize(980, 580)
        self.setMinimumSize(800, 500)
        self._size_expanded = (980, 920)
        self._size_collapsed = (980, 580)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self.miner: BilibiliWatchTimeMiner | None = None
        self._stop_signal_set = False
        self._ui_alive = True

        self._last_verbose: bool | None = None
        self._task_progress_result: str = ""
        self._task_progress_pending: bool = False
        self._task_refresh_lock = threading.Lock()
        self._task_refresh_inflight: bool = False
        self._task_refresh_queued: bool = False
        self._task_refresh_trigger_pending: bool = False
        self._stopping_in_progress: bool = False
        self._stop_poll_started_at: float | None = None
        self._stop_timeout_warned: bool = False
        self._stop_force_sent: bool = False
        self._auto_force_stop_after_seconds: float = 2.0

        self.ui_call.connect(self._on_ui_call, Qt.QueuedConnection)

        self._build_layout()
        self._install_logging()

        self._log_timer = QTimer(self)
        self._log_timer.setInterval(120)
        self._log_timer.timeout.connect(self._flush_log_queue)
        self._log_timer.start()

        self._stop_poll_timer = QTimer(self)
        self._stop_poll_timer.setInterval(120)
        self._stop_poll_timer.timeout.connect(self._poll_worker_shutdown)

        self._config_sync_timer = QTimer(self)
        self._config_sync_timer.setInterval(2000)
        self._config_sync_timer.timeout.connect(self._sync_config_to_miner)

        self._task_refresh_timer = QTimer(self)
        self._task_refresh_timer.setSingleShot(True)
        self._task_refresh_timer.timeout.connect(self._schedule_task_refresh)

    # ---------- layout ----------

    def _build_layout(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(12)

        # ---- Config card ----
        config_card = QFrame()
        config_card.setObjectName("card")
        config_card.setStyleSheet(_CARD_STYLE)
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(18, 16, 18, 16)
        config_layout.setSpacing(12)

        title = QLabel("Bilibili 直播掉宝助手")
        title_font = QFont()
        title_font.setPointSize(15)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color:#f5f6f8;padding:2px 0 6px 0;")
        config_layout.addWidget(title)

        self.cookie_edit = self._make_line_edit("必填: SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx")
        self.rooms_edit = self._make_line_edit("必填: 直播间号，多个用逗号分隔")
        self.rooms_edit.setText("23612045")
        self.task_ids_edit = self._make_line_edit("可留空: F12 从 totalv2 请求中提取 task_ids")
        self.notify_urls_edit = self._make_line_edit("可留空: Apprise URL，如 gotify://host/token")

        config_layout.addLayout(
            self._build_labeled_row(
                "Cookie", self.cookie_edit, ("自动获取", "purple", self.auto_fetch_cookie)
            )
        )
        config_layout.addLayout(self._build_labeled_row("房间号", self.rooms_edit))
        config_layout.addLayout(
            self._build_labeled_row(
                "任务 ID",
                self.task_ids_edit,
                ("自动获取", "blue", self.auto_fetch_task_ids),
            )
        )
        config_layout.addLayout(self._build_labeled_row("通知 URL", self.notify_urls_edit))

        # small numeric row
        self.threads_edit = self._make_small_edit("1")
        self.reconnect_edit = self._make_small_edit("8")
        self.task_interval_edit = self._make_small_edit("30")
        self.verbose_check = QCheckBox("详细日志")
        self.disable_task_notify_check = QCheckBox("禁用任务完成通知")

        num_row = QHBoxLayout()
        num_row.setSpacing(12)
        for text, widget in (
            ("线程数", self.threads_edit),
            ("重连延迟(s)", self.reconnect_edit),
            ("任务查询间隔(s)", self.task_interval_edit),
        ):
            lbl = QLabel(text)
            lbl.setStyleSheet("color:#9aa0a6;")
            num_row.addWidget(lbl)
            num_row.addWidget(widget)
            num_row.addSpacing(6)
        num_row.addSpacing(6)
        num_row.addWidget(self.verbose_check)
        num_row.addWidget(self.disable_task_notify_check)
        num_row.addStretch(1)
        config_layout.addLayout(num_row)

        # buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addWidget(self._make_button("启动", "green", self.start))
        btn_row.addWidget(self._make_button("停止", "red", self.stop))
        btn_row.addWidget(self._make_button("加载配置", "", self.load_config))
        btn_row.addWidget(self._make_button("保存配置", "", self.save_config))
        btn_row.addWidget(self._make_button("清空日志", "gray", self.clear_logs))
        btn_row.addStretch(1)
        config_layout.addLayout(btn_row)

        # indeterminate progress bar (Qt handles animation natively)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setRange(0, 1)  # stopped state
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        config_layout.addWidget(self.progress_bar)

        root_layout.addWidget(config_card)

        # ---- Task progress card ----
        task_card = QFrame()
        task_card.setObjectName("card")
        task_card.setStyleSheet(_CARD_STYLE)
        task_layout = QVBoxLayout(task_card)
        task_layout.setContentsMargins(18, 12, 18, 14)
        task_layout.setSpacing(8)

        task_header = QHBoxLayout()
        task_title = QLabel("任务进度")
        tf = QFont()
        tf.setPointSize(11)
        tf.setBold(True)
        task_title.setFont(tf)
        task_title.setStyleSheet("color:#f5f6f8;")
        task_header.addWidget(task_title)
        task_header.addStretch(1)
        task_header.addWidget(self._make_button("手动刷新", "", self.refresh_tasks))
        task_layout.addLayout(task_header)

        self.task_text = QPlainTextEdit()
        self.task_text.setReadOnly(True)
        self.task_text.setFont(QFont("Consolas", 10))
        self.task_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.task_text.setFixedHeight(180)
        self.task_text.setPlainText("点击“手动刷新”查看任务进度")
        task_layout.addWidget(self.task_text)

        root_layout.addWidget(task_card)

        # ---- Log card (collapsible, default collapsed) ----
        self.log_card = QFrame()
        self.log_card.setObjectName("card")
        self.log_card.setStyleSheet(_CARD_STYLE)
        log_layout = QVBoxLayout(self.log_card)
        log_layout.setContentsMargins(18, 8, 18, 14)
        log_layout.setSpacing(6)

        self._log_toggle_btn = QPushButton("▶ 运行日志")
        lf = QFont()
        lf.setPointSize(11)
        lf.setBold(True)
        self._log_toggle_btn.setFont(lf)
        self._log_toggle_btn.setFlat(True)
        self._log_toggle_btn.setCursor(Qt.PointingHandCursor)
        self._log_toggle_btn.setStyleSheet(
            "QPushButton{text-align:left;padding:6px 4px;border:0;background:transparent;color:#e6e7eb;}"
            "QPushButton:hover{color:#4f8cff;}"
        )
        self._log_toggle_btn.clicked.connect(self._toggle_log)
        log_layout.addWidget(self._log_toggle_btn)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 10))
        self.log_text.setMaximumBlockCount(5000)
        self.log_text.setVisible(False)
        log_layout.addWidget(self.log_text)

        root_layout.addWidget(self.log_card)
        root_layout.addStretch(1)

        self._log_expanded = False

    def _make_line_edit(self, placeholder: str) -> QLineEdit:
        w = QLineEdit()
        w.setPlaceholderText(placeholder)
        return w

    def _make_small_edit(self, default: str) -> QLineEdit:
        w = QLineEdit()
        w.setText(default)
        w.setFixedWidth(70)
        return w

    def _make_button(self, text: str, color: str, slot) -> QPushButton:
        btn = QPushButton(text)
        btn.setStyleSheet(_BUTTON_STYLES.get(color, _BUTTON_STYLES[""]))
        btn.setCursor(Qt.PointingHandCursor)
        btn.clicked.connect(slot)
        return btn

    def _build_labeled_row(
        self,
        label: str,
        editor: QLineEdit,
        extra_button: tuple[str, str, object] | None = None,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        lab = QLabel(label)
        lab.setFixedWidth(72)
        lab.setStyleSheet("color:#9aa0a6;")
        row.addWidget(lab)
        row.addWidget(editor, 1)
        if extra_button is not None:
            text, color, slot = extra_button
            b = self._make_button(text, color, slot)
            b.setFixedWidth(100)
            row.addWidget(b)
        return row

    # ---------- logging / cross-thread ----------

    def _install_logging(self) -> None:
        queue_handler = QueueLogHandler(self.log_queue)
        setup_logging(
            verbose=self.verbose_check.isChecked(),
            no_color=True,
            extra_handlers=[queue_handler],
        )

    def _post_ui_task(self, callback, *args, **kwargs) -> None:
        """Thread-safe dispatch onto the GUI thread. Drop-in replacement for the
        original Tk-era helper; all workers keep calling this same API."""
        if not self._ui_alive:
            return
        self.ui_call.emit(callback, args, kwargs)

    def _on_ui_call(self, fn, args, kwargs) -> None:
        if not self._ui_alive:
            return
        try:
            fn(*args, **kwargs)
        except Exception:
            logging.getLogger(__name__).exception("UI 任务执行失败")

    def _flush_log_queue(self) -> None:
        if not self._ui_alive:
            return
        # Drain the log queue in a single batch; appendPlainText per line is
        # still cheap for the 120ms tick, far cheaper than canvas redraws.
        lines: list[str] = []
        while True:
            try:
                lines.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if lines:
            self.log_text.appendPlainText("\n".join(lines))
            cursor = self.log_text.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.log_text.setTextCursor(cursor)

        if self._task_progress_pending:
            self._task_progress_pending = False
            self.task_text.setPlainText(self._task_progress_result)

        if self._task_refresh_trigger_pending:
            self._task_refresh_trigger_pending = False
            self.refresh_tasks(manual=False)

    # ---------- message helpers (main thread only) ----------

    def _show_info(self, title: str, msg: str) -> None:
        QMessageBox.information(self, title, msg)

    def _show_warning(self, title: str, msg: str) -> None:
        QMessageBox.warning(self, title, msg)

    def _show_error(self, title: str, msg: str) -> None:
        QMessageBox.critical(self, title, msg)

    # ---------- config ----------

    def _build_config(self) -> MinerConfig:
        return MinerConfig(
            cookie=self.cookie_edit.text().strip(),
            room_ids=parse_room_ids(self.rooms_edit.text().strip()),
            thread_count=int(self.threads_edit.text().strip() or "1"),
            reconnect_delay_seconds=int(self.reconnect_edit.text().strip() or "8"),
            enable_web_heartbeat=True,
            task_ids=parse_task_ids(self.task_ids_edit.text().strip()),
            task_query_interval_seconds=int(
                self.task_interval_edit.text().strip() or "30"
            ),
            notify_urls=parse_task_ids(self.notify_urls_edit.text().strip()),
            notify_on_task_complete=not self.disable_task_notify_check.isChecked(),
        )

    # ---------- start / stop ----------

    def start(self) -> None:
        self._stop_signal_set = False
        self._stopping_in_progress = False
        self._stop_poll_started_at = None
        self._stop_timeout_warned = False
        self._stop_force_sent = False
        if self.worker_thread and self.worker_thread.is_alive():
            self._show_info("运行中", "助手已在运行中。")
            return
        try:
            self._install_logging()
            config = self._build_config()
            config.validate()
            self.miner = BilibiliWatchTimeMiner(config)
        except Exception as exc:
            self._show_error("配置错误", str(exc))
            return

        def runner() -> None:
            try:
                if self.miner is not None:
                    self.miner.run()
            except Exception:
                logging.getLogger(__name__).exception("GUI worker crashed")

        self.worker_thread = threading.Thread(
            target=runner, name="gui-main-worker", daemon=True
        )
        self.worker_thread.start()
        logging.getLogger(__name__).info("掉宝助手已启动")
        self._start_progress_animation()
        self._config_sync_timer.start()
        self._schedule_task_refresh()

    def stop(self) -> None:
        self._stop_signal_set = True
        logger = logging.getLogger(__name__)
        self._stop_progress_animation()
        if self.worker_thread is None or not self.worker_thread.is_alive():
            self.worker_thread = None
            self.miner = None
            self._stopping_in_progress = False
            self._stop_poll_started_at = None
            self._stop_timeout_warned = False
            self._stop_force_sent = False
            return
        if self._stopping_in_progress:
            if self.miner is not None and not self._stop_force_sent:
                self._stop_force_sent = True
                self.miner.stop(force=True)
                logger.warning("已发送强制停止请求")
            else:
                logger.info("正在停止，请稍候...")
            return
        self._stopping_in_progress = True
        self._stop_poll_started_at = time.monotonic()
        self._stop_timeout_warned = False
        self._stop_force_sent = False
        if self.miner is not None:
            self.miner.stop(force=False)
        logger.info("正在停止...")
        self._stop_poll_timer.start()

    def _poll_worker_shutdown(self) -> None:
        logger = logging.getLogger(__name__)
        if self.worker_thread is None:
            self._stop_poll_timer.stop()
            self.miner = None
            self._stopping_in_progress = False
            self._stop_poll_started_at = None
            self._stop_timeout_warned = False
            self._stop_force_sent = False
            return
        if self.worker_thread.is_alive():
            if self._stop_poll_started_at is None:
                self._stop_poll_started_at = time.monotonic()
            elapsed = time.monotonic() - self._stop_poll_started_at
            if (
                elapsed >= self._auto_force_stop_after_seconds
                and not self._stop_force_sent
                and self.miner is not None
            ):
                self._stop_force_sent = True
                self.miner.stop(force=True)
                logger.warning(
                    "停止超过 %.1f 秒，已切换为强制停止",
                    self._auto_force_stop_after_seconds,
                )
            if elapsed >= 5 and not self._stop_timeout_warned:
                logger.warning("停止超过 5 秒，后台线程仍在退出中")
                self._stop_timeout_warned = True
            return

        self._stop_poll_timer.stop()
        logger.info("停止成功")
        self.worker_thread = None
        self.miner = None
        self._stopping_in_progress = False
        self._stop_poll_started_at = None
        self._stop_timeout_warned = False
        self._stop_force_sent = False

    # ---------- progress bar (Qt-native indeterminate) ----------

    def _start_progress_animation(self) -> None:
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(True)

    def _stop_progress_animation(self) -> None:
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)

    # ---------- log / layout toggle ----------

    def _toggle_log(self) -> None:
        if self._log_expanded:
            self.log_text.setVisible(False)
            self._log_toggle_btn.setText("▶ 运行日志")
            self.resize(*self._size_collapsed)
        else:
            self.log_text.setVisible(True)
            self._log_toggle_btn.setText("▼ 运行日志")
            self.resize(*self._size_expanded)
        self._log_expanded = not self._log_expanded

    def clear_logs(self) -> None:
        self.log_text.clear()

    # ---------- task progress ----------

    def _set_task_progress_text(self, text: str) -> None:
        self._task_progress_result = text
        self._task_progress_pending = True

    def _complete_task_refresh(self, result_text: str, rerun: bool) -> None:
        self._set_task_progress_text(result_text)
        if rerun:
            self._task_refresh_trigger_pending = True

    def refresh_tasks(self, *args, manual: bool = True, **kwargs) -> None:
        # QPushButton.clicked may pass a bool (checked) — ignore positional args.
        cookie = self.cookie_edit.text().strip()
        if not cookie:
            if manual:
                self._show_warning("提示", "请先填写 Cookie")
            return
        task_ids = parse_task_ids(self.task_ids_edit.text().strip())
        if not task_ids:
            self._set_task_progress_text("无任务数据（未填写任务 ID）")
            return

        with self._task_refresh_lock:
            if self._task_refresh_inflight:
                self._task_refresh_queued = True
                if manual:
                    self._set_task_progress_text("已有刷新进行中，已排队下一次刷新...")
                return
            self._task_refresh_inflight = True

        self._set_task_progress_text("正在刷新任务进度...")

        def _do() -> None:
            result_text = ""
            try:

                async def _query():
                    client = BilibiliClient(cookie)
                    try:
                        return await client.get_task_progress(task_ids)
                    finally:
                        await client.close()

                progresses = asyncio.run(_query())
                result_text = self._format_task_progress(progresses)
            except Exception as exc:
                logging.getLogger(__name__).warning("刷新任务失败: %s", exc)
                result_text = f"刷新任务失败: {exc}"
            finally:
                rerun = False
                with self._task_refresh_lock:
                    self._task_refresh_inflight = False
                    if self._task_refresh_queued:
                        rerun = True
                        self._task_refresh_queued = False
                self._post_ui_task(self._complete_task_refresh, result_text, rerun)

        threading.Thread(target=_do, daemon=True, name="gui-task-refresh").start()

    @staticmethod
    def _find_browser(name: str) -> bool:
        if name == "edge":
            paths = [
                r"C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
                r"C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
            ]
        else:
            paths = [
                r"C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
                r"C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
                os.path.expandvars(
                    r"%LOCALAPPDATA%\\Google\\Chrome\Application\\chrome.exe"
                ),
            ]
        return any(os.path.exists(p) for p in paths)

    @staticmethod
    def _extract_room_id_from_live_url(text: str) -> int | None:
        if not text:
            return None
        for pattern in (
            r"https?://live\.bilibili\.com/blanc/(\d+)",
            r"https?://live\.bilibili\.com/(\d+)",
            r"live\.bilibili\.com/blanc/(\d+)",
            r"live\.bilibili\.com/(\d+)",
        ):
            match = re.search(pattern, text)
            if not match:
                continue
            try:
                room_id = int(match.group(1))
            except Exception:
                continue
            if room_id > 0:
                return room_id
        return None

    def _apply_auto_room_id(self, room_id: int) -> None:
        self.rooms_edit.setText(str(room_id))

    def _apply_auto_cookie(self, cookie_str: str) -> None:
        self.cookie_edit.setText(cookie_str)

    def _apply_auto_task_ids(self, task_ids_str: str) -> None:
        self.task_ids_edit.setText(task_ids_str)

    def _browser_sniff(
        self,
        url_keyword: str | None,
        hint: str,
        on_network_match=None,
        on_cookies=None,
    ) -> None:
        def _do() -> None:
            server = None
            ext_dir = None
            driver = None
            browser_type = None
            cdp_session = None
            try:
                import json
                import os
                import tempfile
                import time
                from http.server import BaseHTTPRequestHandler, HTTPServer

                from selenium import webdriver

                need_net = bool(url_keyword and on_network_match)
                need_cookie = on_cookies is not None

                net_captured: list = []
                cookie_captured: list = []

                class _Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length)
                        try:
                            data = json.loads(body)
                            if data.get("type") == "__bili_cookies__":
                                cookie_captured.append(data["cookies"])
                            else:
                                net_captured.append(data)
                        except Exception:
                            pass
                        self.send_response(204)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()

                    def do_OPTIONS(self):
                        self.send_response(204)
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.send_header("Access-Control-Allow-Methods", "POST")
                        self.send_header("Access-Control-Allow-Headers", "Content-Type")
                        self.end_headers()

                    def log_message(self, *_a):
                        pass

                server = HTTPServer(("127.0.0.1", 0), _Handler)
                port = server.server_address[1]
                threading.Thread(target=server.serve_forever, daemon=True).start()

                ext_dir = tempfile.mkdtemp(prefix="bili_sniff_")

                def _write_ext_edge() -> None:
                    manifest: dict = {
                        "manifest_version": 3,
                        "name": "BiliSniff",
                        "version": "1.0",
                        "host_permissions": ["http://127.0.0.1/*"],
                        "content_scripts": [],
                    }
                    files: dict[str, str] = {}

                    if need_net:
                        manifest["content_scripts"] += [
                            {
                                "matches": ["*://*.bilibili.com/*"],
                                "js": ["inject.js"],
                                "run_at": "document_start",
                                "world": "MAIN",
                            },
                            {
                                "matches": ["*://*.bilibili.com/*"],
                                "js": ["relay.js"],
                                "run_at": "document_start",
                            },
                        ]
                        files["inject.js"] = (
                            "(function(){\n"
                            "var origFetch=window.fetch;\n"
                            "window.fetch=async function(){\n"
                            "  var resp=await origFetch.apply(this,arguments);\n"
                            "  var url=(typeof arguments[0]==='string')?arguments[0]:arguments[0].url;\n"
                            "  if(url.indexOf('" + (url_keyword or "") + "')!==-1){\n"
                            "    try{var d=await resp.clone().json();\n"
                            "      window.postMessage({type:'__bili_sniff__',payload:{url:url,data:d,page_url:window.location.href}},'*');\n"
                            "    }catch(e){}\n"
                            "  }\n"
                            "  return resp;\n"
                            "};\n"
                            "var origOpen=XMLHttpRequest.prototype.open;\n"
                            "var origSend=XMLHttpRequest.prototype.send;\n"
                            "XMLHttpRequest.prototype.open=function(m,u){\n"
                            "  this.__url=u;return origOpen.apply(this,arguments);};\n"
                            "XMLHttpRequest.prototype.send=function(){\n"
                            "  var self=this;\n"
                            "  this.addEventListener('load',function(){\n"
                            "    if(self.__url&&self.__url.indexOf('"
                            + (url_keyword or "")
                            + "')!==-1){\n"
                            "      try{window.postMessage({type:'__bili_sniff__',\n"
                            "        payload:{url:self.__url,data:JSON.parse(self.responseText),page_url:window.location.href}},'*');\n"
                            "      }catch(e){}\n"
                            "    }\n"
                            "  });\n"
                            "  return origSend.apply(this,arguments);\n"
                            "};\n"
                            "})();"
                        )
                        files["relay.js"] = (
                            "window.addEventListener('message',function(e){\n"
                            "  if(e.data&&e.data.type==='__bili_sniff__'){\n"
                            "    fetch('http://127.0.0.1:" + str(port) + "/',{\n"
                            "      method:'POST',\n"
                            "      headers:{'Content-Type':'application/json'},\n"
                            "      body:JSON.stringify(e.data.payload)\n"
                            "    }).catch(function(){});\n"
                            "  }\n"
                            "});"
                        )

                    if need_cookie:
                        manifest["permissions"] = ["cookies"]
                        manifest["host_permissions"].append("*://*.bilibili.com/*")
                        manifest["background"] = {"service_worker": "background.js"}
                        files["background.js"] = (
                            "function checkCookies(){\n"
                            "  chrome.cookies.getAll({domain:'.bilibili.com'},function(cookies){\n"
                            "    if(!cookies.some(function(c){return c.name==='SESSDATA';}))return;\n"
                            "    fetch('http://127.0.0.1:" + str(port) + "/',{\n"
                            "      method:'POST',\n"
                            "      headers:{'Content-Type':'application/json'},\n"
                            "      body:JSON.stringify({type:'__bili_cookies__',cookies:cookies})\n"
                            "    }).catch(function(){});\n"
                            "  });\n"
                            "}\n"
                            "checkCookies();\n"
                            "setInterval(checkCookies,3000);"
                        )

                    with open(
                        os.path.join(ext_dir, "manifest.json"), "w", encoding="utf-8"
                    ) as f:
                        json.dump(manifest, f)
                    for fname, content in files.items():
                        with open(
                            os.path.join(ext_dir, fname), "w", encoding="utf-8"
                        ) as f:
                            f.write(content)

                def _write_ext_chrome() -> None:
                    port_local = server.server_address[1]
                    relay_js = (
                        "window.addEventListener('message',function(e){\n"
                        "  if(e.data && e.data.type==='__bili_sniff__'){\n"
                        "    fetch('http://127.0.0.1:" + str(port_local) + "/',{\n"
                        "      method:'POST',\n"
                        "      headers:{'Content-Type':'application/json'},\n"
                        "      body:JSON.stringify(e.data.payload)\n"
                        "    }).catch(function(){});\n"
                        "  }\n"
                        "});"
                    )

                    background_js = (
                        "var injectedTabs = {};\n"
                        "var PORT = " + str(port_local) + ";\n"
                        "function injectTab(tabId) {\n"
                        "  if (injectedTabs[tabId]) return;\n"
                        "  injectedTabs[tabId] = true;\n"
                        "  chrome.scripting.executeScript({\n"
                        "    target: {tabId: tabId},\n"
                        "    world: 'ISOLATED',\n"
                        "    func: function() {\n"
                        "      window.addEventListener('message', function(e) {\n"
                        "        if (e.data && e.data.type === '__bili_sniff__') {\n"
                        "          fetch('http://127.0.0.1:" + str(port_local) + "/', {\n"
                        "            method: 'POST',\n"
                        "            headers: {'Content-Type': 'application/json'},\n"
                        "            body: JSON.stringify(e.data.payload)\n"
                        "          }).catch(function(){});\n"
                        "        }\n"
                        "      });\n"
                        "    }\n"
                        "  });\n"
                        "  chrome.scripting.executeScript({\n"
                        "    target: {tabId: tabId},\n"
                        "    world: 'MAIN',\n"
                        "    func: function() {\n"
                        "      if (window.__bili_sniff_injected__) return;\n"
                        "      window.__bili_sniff_injected__ = true;\n"
                        "      var kw = '" + (url_keyword or "") + "';\n"
                        "      var origFetch = window.fetch;\n"
                        "      window.fetch = async function() {\n"
                        "        var resp = await origFetch.apply(this, arguments);\n"
                        "        var url = (typeof arguments[0] === 'string') ? arguments[0] : arguments[0].url;\n"
                        "        if (url.indexOf(kw) !== -1) {\n"
                        "          try {\n"
                        "            var d = await resp.clone().json();\n"
                        "            window.postMessage({type: '__bili_sniff__', payload: {url: url, data: d, page_url: window.location.href}}, '*');\n"
                        "          } catch(e) {}\n"
                        "        }\n"
                        "        return resp;\n"
                        "      };\n"
                        "      var origOpen = XMLHttpRequest.prototype.open;\n"
                        "      var origSend = XMLHttpRequest.prototype.send;\n"
                        "      XMLHttpRequest.prototype.open = function(m, u) {\n"
                        "        this.__url = u; return origOpen.apply(this, arguments);\n"
                        "      };\n"
                        "      XMLHttpRequest.prototype.send = function() {\n"
                        "        var self = this;\n"
                        "        this.addEventListener('load', function() {\n"
                        "          if (self.__url && self.__url.indexOf(kw) !== -1) {\n"
                        "            try {\n"
                        "              window.postMessage({type: '__bili_sniff__', payload: {url: self.__url, data: JSON.parse(self.responseText), page_url: window.location.href}}, '*');\n"
                        "            } catch(e) {}\n"
                        "          }\n"
                        "        });\n"
                        "        return origSend.apply(this, arguments);\n"
                        "      };\n"
                        "    }\n"
                        "  });\n"
                        "}\n"
                        "chrome.tabs.onUpdated.addListener(function(tabId, changeInfo, tab) {\n"
                        "  if (changeInfo.status === 'complete' && tab.url && tab.url.indexOf('bilibili.com') !== -1) {\n"
                        "    injectTab(tabId);\n"
                        "  }\n"
                        "});\n"
                        "chrome.tabs.query({url: '*://*.bilibili.com/*'}, function(tabs) {\n"
                        "  tabs.forEach(function(tab) { injectTab(tab.id); });\n"
                        "});\n"
                        "function sendCookies() {\n"
                        "  chrome.cookies.getAll({domain: '.bilibili.com'}, function(cookies) {\n"
                        "    if (cookies && cookies.length > 0) {\n"
                        "      fetch('http://127.0.0.1:' + PORT + '/', {\n"
                        "        method: 'POST',\n"
                        "        headers: {'Content-Type': 'application/json'},\n"
                        "        body: JSON.stringify({type: '__bili_cookies__', cookies: cookies})\n"
                        "      }).catch(function(){});\n"
                        "    }\n"
                        "  });\n"
                        "}\n"
                        "sendCookies();\n"
                        "setInterval(sendCookies, 3000);\n"
                        "chrome.cookies.onChanged.addListener(function(changeInfo) {\n"
                        "  if (changeInfo.cookie.domain.includes('bilibili.com')) {\n"
                        "    sendCookies();\n"
                        "  }\n"
                        "});\n"
                    )

                    manifest = {
                        "manifest_version": 3,
                        "name": "BiliSniff",
                        "version": "1.0",
                        "permissions": ["scripting", "tabs", "cookies"],
                        "host_permissions": [
                            "http://127.0.0.1/*",
                            "*://*.bilibili.com/*",
                        ],
                        "background": {"service_worker": "background.js"},
                        "content_scripts": [],
                    }

                    if need_net:
                        manifest["content_scripts"].append(
                            {
                                "matches": ["*://*.bilibili.com/*"],
                                "js": ["relay.js"],
                                "run_at": "document_start",
                            }
                        )

                    ext_path = os.path.join(ext_dir, "manifest.json")
                    with open(ext_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, indent=2)

                    with open(
                        os.path.join(ext_dir, "background.js"), "w", encoding="utf-8"
                    ) as f:
                        f.write(background_js)

                    if need_net:
                        with open(
                            os.path.join(ext_dir, "relay.js"), "w", encoding="utf-8"
                        ) as f:
                            f.write(relay_js)

                last_exc = None
                for _browser in ("edge", "chrome"):
                    if not MinerGUI._find_browser(_browser):
                        logging.getLogger(__name__).info("未检测到 %s，跳过", _browser)
                        continue
                    try:
                        if _browser == "edge":
                            _write_ext_edge()
                            _opts = webdriver.EdgeOptions()
                            _opts.add_argument(f"--load-extension={ext_dir}")
                            driver = webdriver.Edge(options=_opts)
                            browser_type = "edge"
                        else:
                            _write_ext_chrome()
                            _opts = webdriver.ChromeOptions()
                            _opts.enable_bidi = True
                            _opts.enable_webextensions = True
                            _opts.add_argument("--remote-allow-origins=*")
                            driver = webdriver.Chrome(options=_opts)
                            browser_type = "chrome"
                            try:
                                driver.webextension.install(path=ext_dir)
                            except Exception as e:
                                logging.getLogger(__name__).error(
                                    "安裝 extension 失败: %s", e
                                )

                        break
                    except Exception as _e:
                        last_exc = _e
                        driver = None
                        browser_type = None
                        logging.getLogger(__name__).warning(
                            "浏览器 %s 启动失败: %s", _browser, _e
                        )

                if driver is None:
                    raise RuntimeError(
                        f"未找到可用浏览器（Edge/Chrome），请确认已安装并配置好 WebDriver。\n最后错误: {last_exc}"
                    )

                driver.get("https://www.bilibili.com/")
                logging.getLogger(__name__).info(hint)

                cookie_done = False
                net_done = False
                last_cookie_count = 0
                for i in range(120):
                    if need_cookie and not cookie_done and cookie_captured:
                        current_cookies = cookie_captured[-1]
                        has_sessdata = any(
                            c.get("name") == "SESSDATA" for c in current_cookies
                        )
                        has_dedeuid = any(
                            c.get("name") == "DedeUserID" for c in current_cookies
                        )
                        if has_sessdata and has_dedeuid:
                            filtered_cookies = [
                                c
                                for c in current_cookies
                                if c.get("name")
                                in [
                                    "SESSDATA",
                                    "bili_jct",
                                    "DedeUserID",
                                    "DedeUserID__ckMd5",
                                    "buvid3",
                                    "b_nut",
                                    "sid",
                                ]
                            ]
                            on_cookies(filtered_cookies)
                            cookie_done = True
                            logging.getLogger(__name__).info("已检测到登入 Cookie")
                        else:
                            if len(cookie_captured) > last_cookie_count:
                                last_cookie_count = len(cookie_captured)

                    if need_net and not net_done and net_captured:
                        on_network_match(net_captured[0])
                        net_done = True

                    if (not need_cookie or cookie_done) and (not need_net or net_done):
                        break

                    time.sleep(1)

            except ImportError as e:
                self._post_ui_task(
                    self._show_error,
                    "依赖缺失",
                    f"缺少依赖库，请安装后重试: {e}\n\n",
                )
            except Exception as exc:
                logging.getLogger(__name__).exception("自动获取失败")
                self._post_ui_task(self._show_error, "错误", f"自动获取失败: {exc}")
            finally:
                if cdp_session:
                    try:
                        cdp_session.close()
                    except Exception:
                        pass
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
                if server:
                    try:
                        server.shutdown()
                    except Exception:
                        pass
                if ext_dir:
                    import shutil

                    shutil.rmtree(ext_dir, ignore_errors=True)

        threading.Thread(target=_do, daemon=True).start()

    def auto_fetch_task_ids(self) -> None:
        ok = QMessageBox.question(
            self,
            "自动获取任务ID",
            "点击确定后会打开浏览器，请在 2 分钟内：\n\n"
            "打开有当前任务的直播间即可自动获取任务ID和房间号，\n"
            "或手动点击页面上的「刷新任务」按钮。\n\n"
            "捕获成功后浏览器会自动关闭。",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if ok != QMessageBox.Ok:
            return

        def on_match(payload):
            payload_data = payload if isinstance(payload, dict) else {}
            request_url = str(payload_data.get("url") or "")
            page_url = str(payload_data.get("page_url") or "")
            room_id = self._extract_room_id_from_live_url(page_url)
            if room_id is None:
                room_id = self._extract_room_id_from_live_url(request_url)
            if room_id is not None:
                self._post_ui_task(self._apply_auto_room_id, room_id)
                logging.getLogger(__name__).info("房间号获取成功: %s", room_id)

            data = payload_data.get("data")
            if not isinstance(data, dict):
                raise ValueError("task response payload invalid")
            if data.get("code") != 0:
                raise ValueError("response code != 0")
            tasks = data.get("data", {}).get("list", [])
            task_ids = [t.get("task_id") for t in tasks if t.get("task_id")]
            if not task_ids:
                raise ValueError("empty task list")
            self._post_ui_task(self._apply_auto_task_ids, ",".join(task_ids))
            logging.getLogger(__name__).info("任务ID获取成功: %s", ",".join(task_ids))

        self._browser_sniff(
            "/x/task/totalv2",
            "已打开浏览器，请打开有当前任务的直播间或点击刷新任务",
            on_network_match=on_match,
        )

    def auto_fetch_cookie(self) -> None:
        ok = QMessageBox.question(
            self,
            "自动获取Cookie",
            "点击确定后会打开浏览器，请在浏览器中登录 B 站。\n\n"
            "登录成功后 Cookie 会自动获取（含 httpOnly 字段），\n"
            "浏览器会自动关闭。",
            QMessageBox.Ok | QMessageBox.Cancel,
        )
        if ok != QMessageBox.Ok:
            return

        def on_cookies(cookies: list):
            cookie_str = "; ".join(
                f"{c['name']}={c['value']}" for c in cookies if c.get("name")
            )
            if not cookie_str:
                self._post_ui_task(
                    self._show_warning,
                    "提示",
                    "未获取到 Cookie，请确认已登录 B 站",
                )
                return
            self._post_ui_task(self._apply_auto_cookie, cookie_str)
            logging.getLogger(__name__).info("Cookie 获取成功")

        self._browser_sniff(
            None,
            "已打开浏览器，正在获取 Cookie…",
            on_cookies=on_cookies,
        )

    def _schedule_task_refresh(self) -> None:
        if (
            self._stop_signal_set
            or self.worker_thread is None
            or not self.worker_thread.is_alive()
        ):
            return
        self.refresh_tasks(manual=False)
        try:
            interval = int(self.task_interval_edit.text().strip() or "30")
        except ValueError:
            interval = 30
        self._task_refresh_timer.start(max(10, interval) * 1000)

    @staticmethod
    def _format_task_progress(progresses: list) -> str:
        if not progresses:
            return "无任务数据"

        _DURATION_RE = re.compile(r"^(.+?)\d+分钟$")
        groups: dict[str, list] = {}
        for task in progresses:
            match = _DURATION_RE.match(task.task_name)
            prefix = match.group(1) if match else task.task_name
            groups.setdefault(prefix, []).append(task)
        for tasks in groups.values():
            tasks.sort(key=lambda t: float(t.limit_value))

        bar_width = 20
        lines: list[str] = []
        for prefix, tasks in groups.items():
            if len(tasks) > 1:
                cur = int(max(float(t.cur_value) for t in tasks))
                lines.append(f"{prefix} (当前: {cur} 分钟)")
                for task in tasks:
                    target = int(float(task.limit_value))
                    pct = min(
                        100,
                        int(
                            float(task.cur_value)
                            / max(1, float(task.limit_value))
                            * 100
                        ),
                    )
                    filled = int(bar_width * pct / 100)
                    bar = "█" * filled + "░" * (bar_width - filled)
                    if task.is_completed:
                        status = " ✔ 完成"
                    else:
                        status = f" {pct:>3}%"
                    lines.append(f"  {bar} {target:>4}分{status}")
            else:
                task = tasks[0]
                target = int(float(task.limit_value))
                cur = int(float(task.cur_value))
                pct = min(100, int(cur / max(1, target) * 100))
                filled = int(bar_width * pct / 100)
                bar = "█" * filled + "░" * (bar_width - filled)
                if task.is_completed:
                    status = " ✔ 完成"
                else:
                    status = f" {pct:>3}%"
                lines.append(f"{task.task_name} ({cur}/{target})")
                lines.append(f"  {bar}{status}")
        return "\n".join(lines)

    def _sync_config_to_miner(self) -> None:
        if self.miner is None:
            if self._stop_signal_set or self.worker_thread is None:
                self._config_sync_timer.stop()
            return
        config = self.miner.config

        try:
            val = int(self.reconnect_edit.text().strip() or "8")
            if val > 0:
                config.reconnect_delay_seconds = val
        except ValueError:
            pass
        try:
            val = int(self.task_interval_edit.text().strip() or "30")
            if val > 0:
                config.task_query_interval_seconds = val
        except ValueError:
            pass

        config.notify_on_task_complete = not self.disable_task_notify_check.isChecked()

        verbose = self.verbose_check.isChecked()
        if verbose != self._last_verbose:
            self._last_verbose = verbose
            self._install_logging()

        new_task_ids = parse_task_ids(self.task_ids_edit.text().strip())
        if new_task_ids != config.task_ids:
            config.task_ids = new_task_ids

        new_cookie = self.cookie_edit.text().strip()
        if new_cookie and new_cookie != config.cookie:
            self.miner.update_cookie(new_cookie)

        new_notify_urls = parse_task_ids(self.notify_urls_edit.text().strip())
        if new_notify_urls != config.notify_urls:
            self.miner.update_notifier(new_notify_urls)

        if (
            self._stop_signal_set
            or self.worker_thread is None
            or not self.worker_thread.is_alive()
        ):
            self._config_sync_timer.stop()

    # ---------- close ----------

    def closeEvent(self, event: QCloseEvent) -> None:
        self._ui_alive = False
        try:
            self.stop()
        except Exception:
            logging.getLogger(__name__).exception("关闭时停止失败")
        # Allow brief drain of background joins before Qt tears down
        QTimer.singleShot(150, QApplication.instance().quit)
        event.accept()

    # ---------- config load/save ----------

    def load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载配置文件",
            "",
            "JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.cookie_edit.setText(str(data.get("cookie", "")))
            self.rooms_edit.setText(",".join(str(x) for x in data.get("room_ids", [])))
            self.threads_edit.setText(str(data.get("thread_count", 1)))
            self.reconnect_edit.setText(str(data.get("reconnect_delay_seconds", 8)))
            self.task_ids_edit.setText(",".join(str(x) for x in data.get("task_ids", [])))
            self.task_interval_edit.setText(
                str(data.get("task_query_interval_seconds", 30))
            )
            self.notify_urls_edit.setText(
                ",".join(str(x) for x in data.get("notify_urls", []))
            )
            self.disable_task_notify_check.setChecked(
                not bool(data.get("notify_on_task_complete", True))
            )
            self.verbose_check.setChecked(bool(data.get("verbose", False)))
            logging.getLogger(__name__).info("配置已加载: %s", path)
        except Exception as exc:
            self._show_error("加载失败", str(exc))

    def save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存配置文件",
            "config.json",
            "JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            config = self._build_config()
            data = {
                "cookie": config.cookie,
                "room_ids": config.room_ids,
                "thread_count": config.thread_count,
                "reconnect_delay_seconds": config.reconnect_delay_seconds,
                "enable_web_heartbeat": config.enable_web_heartbeat,
                "task_ids": config.task_ids,
                "task_query_interval_seconds": config.task_query_interval_seconds,
                "notify_urls": config.notify_urls,
                "notify_on_task_complete": config.notify_on_task_complete,
                "verbose": self.verbose_check.isChecked(),
            }
            Path(path).write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logging.getLogger(__name__).info("配置已保存: %s", path)
        except Exception as exc:
            self._show_error("保存失败", str(exc))


def run_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    default_font = QFont("Segoe UI", 10)
    # Fall back to Microsoft YaHei for CJK glyphs on Windows
    default_font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(default_font)
    app.setStyleSheet(
        """
        QWidget { background: #1a1d23; color: #e6e7eb; }
        QLabel { background: transparent; color: #e6e7eb; }
        QToolTip { background: #2f3440; color: #e6e7eb; border: 1px solid #3a3f4b;
                   padding: 4px 8px; border-radius: 4px; }

        QLineEdit {
            background: #2b2f3a; color: #e6e7eb;
            border: 1px solid #2f3440; border-radius: 6px;
            padding: 6px 10px; min-height: 20px;
            selection-background-color: #4f8cff;
        }
        QLineEdit:focus { border-color: #4f8cff; }
        QLineEdit:disabled { color: #6b7280; background: #23262e; }

        QPlainTextEdit {
            background: #1f222a; color: #d8dae0;
            border: 1px solid #2f3440; border-radius: 6px;
            padding: 6px; selection-background-color: #4f8cff;
        }

        QCheckBox { background: transparent; spacing: 8px; color: #e6e7eb; }
        QCheckBox::indicator {
            width: 16px; height: 16px; border: 1px solid #3a3f4b;
            border-radius: 4px; background: #2b2f3a;
        }
        QCheckBox::indicator:hover { border-color: #4f8cff; }
        QCheckBox::indicator:checked {
            background: #4f8cff; border-color: #4f8cff;
            image: none;
        }

        QProgressBar {
            background: #2b2f3a; border: 0; border-radius: 4px;
            min-height: 6px; max-height: 6px;
        }
        QProgressBar::chunk {
            background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
                                        stop:0 #4f8cff, stop:1 #7aa7ff);
            border-radius: 4px;
        }

        QScrollBar:vertical {
            background: transparent; width: 10px; margin: 2px;
        }
        QScrollBar::handle:vertical {
            background: #3a3f4b; border-radius: 4px; min-height: 24px;
        }
        QScrollBar::handle:vertical:hover { background: #4a5060; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
            background: transparent; height: 0; border: 0;
        }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
            background: transparent;
        }
        QScrollBar:horizontal {
            background: transparent; height: 10px; margin: 2px;
        }
        QScrollBar::handle:horizontal {
            background: #3a3f4b; border-radius: 4px; min-width: 24px;
        }
        QScrollBar::handle:horizontal:hover { background: #4a5060; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
            background: transparent; width: 0; border: 0;
        }

        QMenu {
            background: #242832; color: #e6e7eb;
            border: 1px solid #2f3440; border-radius: 6px; padding: 4px;
        }
        QMenu::item { padding: 6px 18px; border-radius: 4px; }
        QMenu::item:selected { background: #4f8cff; color: #ffffff; }
        """
    )
    window = MinerGUI()
    window.show()
    return app.exec()
