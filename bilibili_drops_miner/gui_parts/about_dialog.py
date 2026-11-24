from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from bilibili_drops_miner.gui_parts.styles import DIALOG_STYLE


def build_about_dialog(
    parent: QWidget | None,
    *,
    app_version: str,
    update_channel: str,
    repository_url: str,
    releases_url: str,
) -> QDialog:
    dialog = QDialog(parent)
    dialog.setWindowTitle("关于")
    dialog.setModal(True)
    dialog.setMinimumWidth(430)
    dialog.setStyleSheet(DIALOG_STYLE)

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 20)
    layout.setSpacing(14)

    title = QLabel("Bilibili 直播掉宝助手")
    title_font = QFont()
    title_font.setPointSize(14)
    title_font.setBold(True)
    title.setFont(title_font)
    layout.addWidget(title)

    body = QLabel(
        f"版本 {app_version} · {update_channel}\n"
        f"{repository_url.removeprefix('https://')}"
    )
    body.setTextFormat(Qt.PlainText)
    body.setTextInteractionFlags(Qt.TextSelectableByMouse)
    body.setStyleSheet("color:#c9d1d9;line-height:1.45;")
    layout.addWidget(body)

    button_row = QHBoxLayout()
    button_row.addStretch(1)
    for text, url, primary in (
        ("问题反馈", f"{repository_url}/issues", False),
        ("发布页", releases_url, False),
        ("项目主页", repository_url, True),
    ):
        button = QPushButton(text)
        if primary:
            button.setObjectName("primary")
            button.setDefault(True)
        button.clicked.connect(lambda _checked=False, target=url: _open(dialog, target))
        button_row.addWidget(button)
    layout.addLayout(button_row)
    return dialog


def show_about_dialog(parent: QWidget, **kwargs: str) -> None:
    build_about_dialog(parent, **kwargs).exec()


def _open(dialog: QDialog, url: str) -> None:
    webbrowser.open(url)
    dialog.accept()
