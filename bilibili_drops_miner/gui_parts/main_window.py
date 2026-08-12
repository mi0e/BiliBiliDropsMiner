from __future__ import annotations

import logging
import queue
import sys
import threading
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMainWindow,
    QMessageBox,
)

from bilibili_drops_miner.config import MinerConfig
from bilibili_drops_miner.gui_parts.app_style import configure_qt_app
from bilibili_drops_miner.gui_parts.account_status import (
    AccountStatus,
    AccountStatusController,
)
from bilibili_drops_miner.gui_parts.browser_actions import BrowserActions
from bilibili_drops_miner.gui_parts.config_io import (
    GuiConfigValues,
    build_config_payload,
    load_config_data,
    save_config_data,
    values_from_config_data,
)
from bilibili_drops_miner.gui_parts.gui_state import GuiStateStore
from bilibili_drops_miner.gui_parts.log_handler import QueueLogHandler
from bilibili_drops_miner.gui_parts.main_layout import (
    MainWindowCallbacks,
    build_main_window_layout,
)
from bilibili_drops_miner.gui_parts.qr_login_dialog import QrLoginDialog
from bilibili_drops_miner.gui_parts.task_controller import TaskController
from bilibili_drops_miner.gui_parts.update_checker import (
    check_latest_release,
    normalize_version,
    should_check_update,
)
from bilibili_drops_miner.gui_parts.update_dialog import show_update_available_dialog
from bilibili_drops_miner.gui_parts.worker_controller import WorkerController
from bilibili_drops_miner.logging_utils import setup_logging
from bilibili_drops_miner.utils import parse_room_ids, parse_task_ids

try:
    from bilibili_drops_miner._version import (
        APP_VERSION,
        LATEST_RELEASE_API,
        RELEASES_URL,
        UPDATE_CHANNEL,
    )
except Exception:
    APP_VERSION = "0.0.0+dev"
    UPDATE_CHANNEL = "dev"
    LATEST_RELEASE_API = (
        "https://api.github.com/repos/mi0e/BiliBiliDropsMiner/releases/latest"
    )
    RELEASES_URL = "https://github.com/mi0e/BiliBiliDropsMiner/releases/latest"


def _normalize_version(value: str) -> str:
    return normalize_version(value)


class MinerGUI(QMainWindow):
    # Cross-thread UI dispatcher. Any background thread may emit this signal;
    # Qt auto-queues the call onto the GUI thread (sender lives in non-Qt thread).
    ui_call = Signal(object, tuple, dict)

    def __init__(self, gui_state: GuiStateStore | None = None) -> None:
        super().__init__()
        self._gui_state = gui_state if gui_state is not None else GuiStateStore()
        self.setWindowTitle(f"Bilibili 直播掉宝助手 {APP_VERSION}")
        self.resize(1040, 760)
        self.setMinimumSize(860, 620)
        self._size_expanded = (1040, 920)
        self._size_collapsed = (1040, 760)

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.worker_controller = WorkerController(auto_force_stop_after_seconds=2.0)
        self._ui_alive = True

        self._last_verbose: bool | None = None
        self._task_progress_result: str = ""
        self._live_watch_time_result: str = ""
        self._task_progress_pending: bool = False
        self._task_refresh_trigger_pending: bool = False
        self._qr_login_dialog: QrLoginDialog | None = None
        self._account_status = AccountStatus("empty", "")
        self._running_account_uid: int | None = None
        self._account_auto_stop_requested = False
        self.ui_call.connect(self._on_ui_call, Qt.QueuedConnection)

        self._build_layout()
        self.account_status_controller = AccountStatusController(
            self,
            get_cookie=lambda: self.cookie_edit.text(),
            on_status=self._on_account_status,
        )
        self.cookie_edit.textChanged.connect(
            self.account_status_controller.cookie_changed
        )
        self.account_status_controller.cookie_changed()
        self.browser_actions = BrowserActions(
            parent=self,
            show_warning=self._show_warning,
            show_error=self._show_error,
            post_ui_task=self._post_ui_task,
            set_room_id=self._apply_auto_room_id,
            set_cookie=self._apply_auto_cookie,
            set_task_ids=self._apply_auto_task_ids,
            logger=logging.getLogger(__name__),
        )
        self.task_controller = TaskController(
            get_cookie=lambda: self.cookie_edit.text().strip(),
            get_room_ids=lambda: parse_room_ids(self.rooms_edit.text().strip()),
            get_task_ids=lambda: parse_task_ids(self.task_ids_edit.text().strip()),
            show_warning=self._show_warning,
            set_task_progress_text=self._set_task_progress_text,
            set_live_watch_time_text=self._set_live_watch_time_text,
            complete_task_refresh=self._complete_task_refresh,
            post_ui_task=self._post_ui_task,
            get_auto_claim_enabled=lambda: self.auto_claim_rewards_check.isChecked(),
        )
        self._install_logging()
        self._restore_window_geometry()
        self._restore_last_config()

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

        self._live_watch_time_timer = QTimer(self)
        self._live_watch_time_timer.setSingleShot(True)
        self._live_watch_time_timer.timeout.connect(
            self._schedule_live_watch_time_refresh
        )

        QTimer.singleShot(3000, self._check_update_silent)

    # ---------- layout ----------

    def _build_layout(self) -> None:
        widgets = build_main_window_layout(
            self,
            MainWindowCallbacks(
                qr_login_cookie=self.qr_login_cookie,
                auto_fetch_cookie=self.auto_fetch_cookie,
                auto_fetch_room_id=self.auto_fetch_room_id,
                auto_fetch_task_ids=self.auto_fetch_task_ids,
                start=self.start,
                stop=self.stop,
                load_config=self.load_config,
                save_config=self.save_config,
                clear_logs=self.clear_logs,
                claim_rewards=self.claim_rewards,
                refresh_tasks=self.refresh_tasks,
                toggle_log=self._toggle_log,
            ),
        )
        self.cookie_edit = widgets.cookie_edit
        self.rooms_edit = widgets.rooms_edit
        self.task_ids_edit = widgets.task_ids_edit
        self.notify_urls_edit = widgets.notify_urls_edit
        self.threads_edit = widgets.threads_edit
        self.reconnect_edit = widgets.reconnect_edit
        self.task_interval_edit = widgets.task_interval_edit
        self.verbose_check = widgets.verbose_check
        self.disable_task_notify_check = widgets.disable_task_notify_check
        self.progress_bar = widgets.progress_bar
        self.task_text = widgets.task_text
        self.log_text = widgets.log_text
        self.log_card = widgets.log_card
        self._log_toggle_btn = widgets.log_toggle_btn
        self.claim_rewards_btn = widgets.claim_rewards_btn
        self.auto_claim_rewards_check = widgets.auto_claim_rewards_check
        self.account_status_label = widgets.account_status_label
        self._log_expanded = False

    # ---------- local GUI state ----------

    def _restore_window_geometry(self) -> None:
        geometry = self._gui_state.window_geometry()
        if not geometry.isEmpty() and self.restoreGeometry(geometry):
            return
        self._center_on_primary_screen()

    def _center_on_primary_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = available.left() + max(0, (available.width() - self.width()) // 2)
        y = available.top() + max(0, (available.height() - self.height()) // 2)
        self.move(x, y)

    def _restore_last_config(self) -> None:
        path = self._gui_state.last_config_path()
        if path is None:
            return
        if not path.is_file():
            logging.getLogger(__name__).warning(
                "上次使用的配置文件不存在，已取消自动加载: %s",
                path,
            )
            self._gui_state.clear_last_config_path()
            self._gui_state.sync()
            return
        self._load_config_path(
            path,
            show_error=False,
            remember=False,
            automatic=True,
        )

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
            scroll_bar = self.log_text.verticalScrollBar()
            scroll_value = scroll_bar.value()
            self.log_text.appendPlainText("\n".join(lines))
            scroll_bar.setValue(min(scroll_value, scroll_bar.maximum()))

        if self._task_progress_pending:
            self._task_progress_pending = False
            self.task_text.setPlainText(self._build_task_progress_text())

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

    # ---------- update check ----------

    def _check_update_silent(self) -> None:
        if not should_check_update(APP_VERSION, UPDATE_CHANNEL):
            return

        def _do() -> None:
            result = check_latest_release(
                app_version=APP_VERSION,
                update_channel=UPDATE_CHANNEL,
                latest_release_api=LATEST_RELEASE_API,
                releases_url=RELEASES_URL,
            )
            if result is None:
                return
            self._post_ui_task(
                self._show_update_available,
                result.latest_version,
                result.release_url,
            )

        threading.Thread(target=_do, daemon=True, name="gui-update-check").start()

    def _show_update_available(self, latest_version: str, release_url: str) -> None:
        if show_update_available_dialog(
            self,
            app_version=APP_VERSION,
            latest_version=latest_version,
            release_url=release_url,
        ):
            webbrowser.open(release_url)

    # ---------- config ----------

    def _build_config(self) -> MinerConfig:
        return MinerConfig(
            cookie=self.cookie_edit.text().strip(),
            room_ids=parse_room_ids(self.rooms_edit.text().strip()),
            thread_count=int(self.threads_edit.text().strip() or "1"),
            reconnect_delay_seconds=int(self.reconnect_edit.text().strip() or "8"),
            task_ids=parse_task_ids(self.task_ids_edit.text().strip()),
            task_query_interval_seconds=int(
                self.task_interval_edit.text().strip() or "30"
            ),
            notify_urls=parse_task_ids(self.notify_urls_edit.text().strip()),
            notify_on_task_complete=not self.disable_task_notify_check.isChecked(),
        )

    # ---------- start / stop ----------

    def start(self) -> None:
        logger = logging.getLogger(__name__)
        if self.worker_controller.is_running:
            self._show_info("运行中", "助手已在运行中。")
            return
        try:
            self._install_logging()
            config = self._build_config()
            config.validate()
        except Exception as exc:
            self._show_error("配置错误", str(exc))
            return
        account = self._account_status
        if account.cookie != config.cookie or account.kind != "valid" or account.uid is None:
            messages = {
                "empty": "请先填写 Cookie，并等待账号校验通过。",
                "checking": "Cookie 正在校验，请稍候再启动。",
                "invalid": "Cookie 已失效，请重新登录后再启动。",
                "error": "Cookie 校验失败，请检查网络后重试。",
            }
            message = messages.get(account.kind, "当前 Cookie 尚未校验通过。")
            self._show_warning("无法启动", message)
            return
        if not self.worker_controller.start(config, logger=logger):
            self._show_info("运行中", "助手已在运行中。")
            return
        logger.info("掉宝助手已启动")
        self._running_account_uid = account.uid
        self._account_auto_stop_requested = False
        self.task_controller.reset_live_watch_time()
        self._set_live_watch_time_text("本次预估观看时长: 0秒")
        self._start_progress_animation()
        self._config_sync_timer.start()
        self._schedule_live_watch_time_refresh()
        self._schedule_task_refresh()

    def stop(self) -> None:
        logger = logging.getLogger(__name__)
        self._stop_progress_animation()
        self._task_refresh_timer.stop()
        self._live_watch_time_timer.stop()
        self.task_controller.stop_live_watch_time()
        result = self.worker_controller.request_stop(logger=logger)
        if result == "stopping_started":
            self._stop_poll_timer.start()
        elif result == "not_running":
            self._stop_poll_timer.stop()

    def _poll_worker_shutdown(self) -> None:
        logger = logging.getLogger(__name__)
        result = self.worker_controller.poll_shutdown(logger=logger)
        if result in {"no_thread", "stopped"}:
            self._stop_poll_timer.stop()
            self._finalize_runtime_ui()

    def _finalize_runtime_ui(self) -> None:
        self._stop_progress_animation()
        self._config_sync_timer.stop()
        self._task_refresh_timer.stop()
        self._live_watch_time_timer.stop()
        self.task_controller.stop_live_watch_time()
        self._running_account_uid = None

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
            self.resize(self.width(), self._size_collapsed[1])
        else:
            self.log_text.setVisible(True)
            self._log_toggle_btn.setText("▼ 运行日志")
            self.resize(self.width(), self._size_expanded[1])
        self._log_expanded = not self._log_expanded

    def clear_logs(self) -> None:
        self.log_text.clear()

    # ---------- task progress ----------

    def _build_task_progress_text(self) -> str:
        lines: list[str] = []
        if self._live_watch_time_result:
            lines.append(self._live_watch_time_result)
        if self._task_progress_result:
            lines.append(self._task_progress_result)
        return "\n".join(lines) or "点击“手动刷新”查看任务进度"

    def _set_task_progress_text(self, text: str) -> None:
        self._task_progress_result = text
        self._task_progress_pending = True

    def _set_live_watch_time_text(self, text: str) -> None:
        self._live_watch_time_result = text
        self._task_progress_pending = True

    def _complete_task_refresh(self, result_text: str, rerun: bool) -> None:
        self._set_task_progress_text(result_text)
        if rerun:
            self._task_refresh_trigger_pending = True

    def refresh_tasks(self, *args, manual: bool = True, **kwargs) -> None:
        # QPushButton.clicked may pass a bool (checked) — ignore positional args.
        self.task_controller.refresh(manual=manual)

    def claim_rewards(self, *args, **kwargs) -> None:
        # QPushButton.clicked may pass a bool (checked) — ignore positional args.
        self.task_controller.claim_rewards()

    @staticmethod
    def _find_browser(name: str) -> bool:
        return BrowserActions.find_browser(name)

    @staticmethod
    def _detect_default_browser() -> str | None:
        return BrowserActions.detect_default_browser()

    @staticmethod
    def _available_browsers() -> list[str]:
        return BrowserActions.available_browsers()

    @staticmethod
    def _browser_label(browser: str) -> str:
        return BrowserActions.browser_label(browser)

    def _pick_browser(self) -> str | None:
        return self.browser_actions.pick_browser()

    @staticmethod
    def _browser_try_order(preferred: str | None) -> tuple[str, ...]:
        return BrowserActions.browser_try_order(preferred)

    @staticmethod
    def _extract_room_id_from_live_url(text: str) -> int | None:
        return BrowserActions.extract_room_id_from_live_url(text)

    def _apply_auto_room_id(self, room_id: int) -> None:
        self.rooms_edit.setText(str(room_id))

    def _apply_auto_cookie(self, cookie_str: str) -> None:
        self.cookie_edit.setText(cookie_str)

    # ---------- account status ----------

    def _on_account_status(self, status: AccountStatus) -> None:
        if status.kind == "empty" and self.worker_controller.is_running:
            # Clearing a Cookie during a run is an invalidating action. Keep
            # the persistent red warning after the automatic stop completes.
            status = AccountStatus("invalid", "")
        previous = self._account_status
        if (
            status.kind == "error"
            and previous.kind == "invalid"
            and previous.cookie == status.cookie
        ):
            # A temporary network failure cannot revoke an earlier definitive
            # logged-out result or its persistent red warning.
            return
        if (
            status.kind == "error"
            and previous.cookie == status.cookie
            and previous.uid is not None
        ):
            status = AccountStatus(
                "error", status.cookie, previous.uid, previous.uname
            )
        self._account_status = status

        if status.kind == "valid":
            identity = status.uname or f"UID {status.uid}"
            text, color = f"账号：{identity}", "#62d995"
        elif status.kind == "invalid":
            text, color = "Cookie 已失效", "#ff5d68"
        elif status.kind == "checking":
            text, color = "账号：检测中…", "#f0b35b"
        elif status.kind == "error":
            if status.uid is not None:
                identity = status.uname or f"UID {status.uid}"
                text = f"账号：{identity}（检查失败）"
            else:
                text = "账号：检查失败（网络异常）"
            color = "#f0b35b"
        else:
            text, color = "账号：未填写 Cookie", "#9aa0a6"
        self.account_status_label.setText(text)
        self.account_status_label.setStyleSheet(f"color:{color};")

        if not self.worker_controller.is_running:
            return
        if status.kind in {"empty", "invalid"}:
            self._auto_stop_for_account("Cookie 已失效，运行已自动停止。")
            return
        if status.kind != "valid" or status.uid is None:
            return
        if self._running_account_uid != status.uid:
            self._auto_stop_for_account(
                "检测到 Cookie 所属账号已变化，运行已自动停止，请重新启动。"
            )
            return
        miner = self.worker_controller.miner
        if miner is not None and status.cookie != miner.config.cookie:
            miner.update_cookie(status.cookie)

    def _auto_stop_for_account(self, message: str) -> None:
        if self._account_auto_stop_requested:
            return
        self._account_auto_stop_requested = True
        logging.getLogger(__name__).warning(message)
        self.stop()

    def _apply_auto_task_ids(self, task_ids_str: str) -> None:
        self.task_ids_edit.setText(task_ids_str)

    def _apply_selected_task_group(
        self,
        room_id: int | None,
        task_groups: list[dict[str, object]],
    ) -> None:
        self.browser_actions.apply_selected_task_group(room_id, task_groups)

    def _browser_sniff(
        self,
        url_keyword: str | None,
        hint: str,
        on_network_match=None,
        on_cookies=None,
        on_page_url=None,
        on_page_html=None,
        browser_preference: str | None = None,
        finish_on_any: bool = False,
    ) -> None:
        self.browser_actions.browser_sniff(
            url_keyword,
            hint,
            on_network_match=on_network_match,
            on_cookies=on_cookies,
            on_page_url=on_page_url,
            on_page_html=on_page_html,
            browser_preference=browser_preference,
            finish_on_any=finish_on_any,
        )

    def auto_fetch_room_id(self) -> None:
        self.browser_actions.auto_fetch_room_id()

    def auto_fetch_task_ids(self) -> None:
        self.browser_actions.auto_fetch_task_ids()

    def auto_fetch_cookie(self) -> None:
        self.browser_actions.auto_fetch_cookie()

    def qr_login_cookie(self) -> None:
        dialog = self._qr_login_dialog
        if dialog is not None and dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()
            return

        dialog = QrLoginDialog(self, self._apply_auto_cookie)
        self._qr_login_dialog = dialog

        def clear_dialog(_result: int, current: QrLoginDialog = dialog) -> None:
            if self._qr_login_dialog is current:
                self._qr_login_dialog = None

        dialog.finished.connect(clear_dialog)
        dialog.show()
        dialog.start_session()

    def _schedule_task_refresh(self) -> None:
        if (
            self.worker_controller.stop_signal_set
            or not self.worker_controller.is_running
        ):
            return
        self.refresh_tasks(manual=False)
        try:
            interval = int(self.task_interval_edit.text().strip() or "30")
        except ValueError:
            interval = 30
        self._task_refresh_timer.start(max(10, interval) * 1000)

    def _schedule_live_watch_time_refresh(self) -> None:
        if (
            self.worker_controller.stop_signal_set
            or not self.worker_controller.is_running
        ):
            return
        self.task_controller.refresh_live_watch_time()
        self._live_watch_time_timer.start(3000)

    def _sync_config_to_miner(self) -> None:
        worker = self.worker_controller
        miner = worker.miner
        if worker.has_thread and not worker.is_running:
            login_invalidated = bool(miner and miner.login_invalidated)
            worker.poll_shutdown(logger=logging.getLogger(__name__))
            self._finalize_runtime_ui()
            if login_invalidated:
                self.account_status_controller.mark_current_invalid()
            return
        if miner is None:
            if worker.stop_signal_set or not worker.has_thread:
                self._config_sync_timer.stop()
            return
        config = miner.config

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

        new_notify_urls = parse_task_ids(self.notify_urls_edit.text().strip())
        if new_notify_urls != config.notify_urls:
            miner.update_notifier(new_notify_urls)

        if worker.stop_signal_set or not worker.is_running:
            self._config_sync_timer.stop()

    # ---------- close ----------

    def closeEvent(self, event: QCloseEvent) -> None:
        try:
            self._gui_state.set_window_geometry(self.saveGeometry())
            self._gui_state.sync()
        except Exception:
            logging.getLogger(__name__).exception("保存 GUI 状态失败")
        dialog = self._qr_login_dialog
        if dialog is not None:
            dialog.cancel_session()
            dialog.close()
            self._qr_login_dialog = None
        self.account_status_controller.close()
        self._ui_alive = False
        try:
            self.stop()
        except Exception:
            logging.getLogger(__name__).exception("关闭时停止失败")
        # Allow brief drain of background joins before Qt tears down
        QTimer.singleShot(150, QApplication.instance().quit)
        event.accept()

    # ---------- config load/save ----------

    def _apply_config_values(self, values: GuiConfigValues) -> None:
        self.cookie_edit.setText(values.cookie)
        self.rooms_edit.setText(values.rooms_text)
        self.threads_edit.setText(values.thread_count_text)
        self.reconnect_edit.setText(values.reconnect_delay_text)
        self.task_ids_edit.setText(values.task_ids_text)
        self.task_interval_edit.setText(values.task_query_interval_text)
        self.notify_urls_edit.setText(values.notify_urls_text)
        self.disable_task_notify_check.setChecked(not values.notify_on_task_complete)
        self.auto_claim_rewards_check.setChecked(values.auto_claim_rewards)
        self.verbose_check.setChecked(values.verbose)

    def _load_config_path(
        self,
        path: str | Path,
        *,
        show_error: bool,
        remember: bool,
        automatic: bool,
    ) -> bool:
        try:
            values = values_from_config_data(load_config_data(path))
            self._apply_config_values(values)
            if remember:
                self._gui_state.set_last_config_path(path)
                self._gui_state.sync()
            message = "已自动加载上次配置" if automatic else "配置已加载"
            logging.getLogger(__name__).info("%s: %s", message, path)
            return True
        except Exception as exc:
            if show_error:
                self._show_error("加载失败", str(exc))
            else:
                logging.getLogger(__name__).warning(
                    "自动加载上次配置失败: %s (%s)",
                    path,
                    exc,
                )
            return False

    def _config_dialog_path(self, *, save: bool) -> str:
        path = self._gui_state.last_config_path()
        if path is not None and path.is_file():
            return str(path)
        return "config.json" if save else ""

    def load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "加载配置文件",
            self._config_dialog_path(save=False),
            "JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if not path:
            return
        self._load_config_path(
            path,
            show_error=True,
            remember=True,
            automatic=False,
        )

    def save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存配置文件",
            self._config_dialog_path(save=True),
            "JSON 文件 (*.json);;所有文件 (*.*)",
        )
        if not path:
            return
        try:
            config = self._build_config()
            save_config_data(
                path,
                build_config_payload(
                    config,
                    verbose=self.verbose_check.isChecked(),
                    auto_claim_rewards=self.auto_claim_rewards_check.isChecked(),
                ),
            )
            self._gui_state.set_last_config_path(path)
            self._gui_state.sync()
            logging.getLogger(__name__).info("配置已保存: %s", path)
        except Exception as exc:
            self._show_error("保存失败", str(exc))


def run_gui() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    configure_qt_app(app)
    window = MinerGUI()
    window.show()
    return app.exec()
