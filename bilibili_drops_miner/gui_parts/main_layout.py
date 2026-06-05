from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from bilibili_drops_miner.gui_parts.styles import CARD_STYLE, BUTTON_STYLES


@dataclass(slots=True)
class MainWindowCallbacks:
    auto_fetch_cookie: Callable[..., None]
    auto_fetch_room_id: Callable[..., None]
    auto_fetch_task_ids: Callable[..., None]
    start: Callable[..., None]
    stop: Callable[..., None]
    load_config: Callable[..., None]
    save_config: Callable[..., None]
    clear_logs: Callable[..., None]
    claim_rewards: Callable[..., None]
    refresh_tasks: Callable[..., None]
    toggle_log: Callable[..., None]


@dataclass(slots=True)
class MainWindowWidgets:
    cookie_edit: QLineEdit
    rooms_edit: QLineEdit
    task_ids_edit: QLineEdit
    notify_urls_edit: QLineEdit
    threads_edit: QLineEdit
    reconnect_edit: QLineEdit
    task_interval_edit: QLineEdit
    verbose_check: QCheckBox
    disable_task_notify_check: QCheckBox
    progress_bar: QProgressBar
    task_text: QPlainTextEdit
    log_text: QPlainTextEdit
    log_card: QFrame
    log_toggle_btn: QPushButton
    claim_rewards_btn: QPushButton


def build_main_window_layout(
    window: QMainWindow,
    callbacks: MainWindowCallbacks,
) -> MainWindowWidgets:
    central = QWidget(window)
    window.setCentralWidget(central)
    root_layout = QVBoxLayout(central)
    root_layout.setContentsMargins(18, 18, 18, 18)
    root_layout.setSpacing(12)

    # ---- Config card ----
    config_card = QFrame()
    config_card.setObjectName("card")
    config_card.setStyleSheet(CARD_STYLE)
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

    cookie_edit = _make_line_edit("必填: SESSDATA=xxx; bili_jct=xxx; DedeUserID=xxx")
    rooms_edit = _make_line_edit("必填: 直播间号，多个用逗号分隔")
    rooms_edit.setText("23612045")
    task_ids_edit = _make_line_edit("可留空: F12 从 totalv2 请求中提取 task_ids")
    notify_urls_edit = _make_line_edit("可留空: 通知 URL，如 gotify://host/token")

    config_layout.addLayout(
        _build_labeled_row(
            "Cookie",
            cookie_edit,
            ("自动获取", "purple", callbacks.auto_fetch_cookie),
        )
    )
    config_layout.addLayout(
        _build_labeled_row(
            "房间号",
            rooms_edit,
            ("自动获取", "blue", callbacks.auto_fetch_room_id),
        )
    )
    config_layout.addLayout(
        _build_labeled_row(
            "任务 ID",
            task_ids_edit,
            ("自动获取", "blue", callbacks.auto_fetch_task_ids),
        )
    )
    config_layout.addLayout(_build_labeled_row("通知 URL", notify_urls_edit))

    threads_edit = _make_small_edit("1")
    reconnect_edit = _make_small_edit("8")
    task_interval_edit = _make_small_edit("30")
    verbose_check = QCheckBox("详细日志")
    disable_task_notify_check = QCheckBox("禁用任务完成通知")

    num_row = QHBoxLayout()
    num_row.setSpacing(12)
    for text, widget in (
        ("线程数", threads_edit),
        ("重连延迟(s)", reconnect_edit),
        ("任务查询间隔(s)", task_interval_edit),
    ):
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#9aa0a6;")
        num_row.addWidget(lbl)
        num_row.addWidget(widget)
        num_row.addSpacing(6)
    num_row.addSpacing(6)
    num_row.addWidget(verbose_check)
    num_row.addWidget(disable_task_notify_check)
    num_row.addStretch(1)
    config_layout.addLayout(num_row)

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addWidget(_make_button("启动", "green", callbacks.start))
    btn_row.addWidget(_make_button("停止", "red", callbacks.stop))
    btn_row.addWidget(_make_button("加载配置", "", callbacks.load_config))
    btn_row.addWidget(_make_button("保存配置", "", callbacks.save_config))
    btn_row.addWidget(_make_button("清空日志", "gray", callbacks.clear_logs))
    btn_row.addStretch(1)
    config_layout.addLayout(btn_row)

    progress_bar = QProgressBar()
    progress_bar.setTextVisible(False)
    progress_bar.setMinimumHeight(6)
    progress_bar.setMaximumHeight(10)
    progress_bar.setRange(0, 1)  # stopped state
    progress_bar.setValue(0)
    progress_bar.setVisible(False)
    config_layout.addWidget(progress_bar)

    root_layout.addWidget(config_card)

    # ---- Task progress card ----
    task_card = QFrame()
    task_card.setObjectName("card")
    task_card.setStyleSheet(CARD_STYLE)
    task_layout = QVBoxLayout(task_card)
    task_layout.setContentsMargins(18, 12, 18, 14)
    task_layout.setSpacing(8)

    task_header = QHBoxLayout()
    task_title = QLabel("任务进度")
    task_title_font = QFont()
    task_title_font.setPointSize(11)
    task_title_font.setBold(True)
    task_title.setFont(task_title_font)
    task_title.setStyleSheet("color:#f5f6f8;")
    task_header.addWidget(task_title)
    task_header.addStretch(1)
    claim_rewards_btn = _make_button("领取奖励", "blue", callbacks.claim_rewards)
    task_header.addWidget(claim_rewards_btn)
    task_header.addWidget(_make_button("手动刷新", "", callbacks.refresh_tasks))
    task_layout.addLayout(task_header)

    task_text = QPlainTextEdit()
    task_text.setReadOnly(True)
    task_text.setFont(QFont("Consolas", 10))
    task_text.setLineWrapMode(QPlainTextEdit.NoWrap)
    task_text.setMinimumHeight(160)
    task_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    task_text.setPlainText("点击“手动刷新”查看任务进度")
    task_layout.addWidget(task_text)

    root_layout.addWidget(task_card, 1)

    # ---- Log card (collapsible, default collapsed) ----
    log_card = QFrame()
    log_card.setObjectName("card")
    log_card.setStyleSheet(CARD_STYLE)
    log_layout = QVBoxLayout(log_card)
    log_layout.setContentsMargins(18, 8, 18, 14)
    log_layout.setSpacing(6)

    log_toggle_btn = QPushButton("▶ 运行日志")
    log_title_font = QFont()
    log_title_font.setPointSize(11)
    log_title_font.setBold(True)
    log_toggle_btn.setFont(log_title_font)
    log_toggle_btn.setFlat(True)
    log_toggle_btn.setCursor(Qt.PointingHandCursor)
    log_toggle_btn.setStyleSheet(
        "QPushButton{text-align:left;padding:6px 4px;border:0;background:transparent;color:#e6e7eb;}"
        "QPushButton:hover{color:#4f8cff;}"
    )
    log_toggle_btn.clicked.connect(callbacks.toggle_log)
    log_layout.addWidget(log_toggle_btn)

    log_text = QPlainTextEdit()
    log_text.setReadOnly(True)
    log_text.setFont(QFont("Consolas", 10))
    log_text.setMaximumBlockCount(5000)
    log_text.setMinimumHeight(160)
    log_text.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    log_text.setVisible(False)
    log_layout.addWidget(log_text)

    root_layout.addWidget(log_card)

    return MainWindowWidgets(
        cookie_edit=cookie_edit,
        rooms_edit=rooms_edit,
        task_ids_edit=task_ids_edit,
        notify_urls_edit=notify_urls_edit,
        threads_edit=threads_edit,
        reconnect_edit=reconnect_edit,
        task_interval_edit=task_interval_edit,
        verbose_check=verbose_check,
        disable_task_notify_check=disable_task_notify_check,
        progress_bar=progress_bar,
        task_text=task_text,
        log_text=log_text,
        log_card=log_card,
        log_toggle_btn=log_toggle_btn,
        claim_rewards_btn=claim_rewards_btn,
    )


def _make_line_edit(placeholder: str) -> QLineEdit:
    widget = QLineEdit()
    widget.setPlaceholderText(placeholder)
    return widget


def _make_small_edit(default: str) -> QLineEdit:
    widget = QLineEdit()
    widget.setText(default)
    widget.setMinimumWidth(70)
    widget.setMaximumWidth(120)
    widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
    return widget


def _make_button(text: str, color: str, slot: Callable[..., None]) -> QPushButton:
    button = QPushButton(text)
    button.setStyleSheet(BUTTON_STYLES.get(color, BUTTON_STYLES[""]))
    button.setCursor(Qt.PointingHandCursor)
    button.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    button.clicked.connect(slot)
    return button


def _build_labeled_row(
    label: str,
    editor: QLineEdit,
    extra_button: tuple[str, str, Callable[..., None]] | None = None,
) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    lab = QLabel(label)
    lab.setMinimumWidth(72)
    lab.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
    lab.setStyleSheet("color:#9aa0a6;")
    row.addWidget(lab)
    editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    row.addWidget(editor, 1)
    if extra_button is not None:
        text, color, slot = extra_button
        button = _make_button(text, color, slot)
        button.setMinimumWidth(100)
        row.addWidget(button)
    return row
