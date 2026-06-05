from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def show_update_available_dialog(
    parent: QWidget,
    *,
    app_version: str,
    latest_version: str,
    release_url: str,
) -> bool:
    dialog = QDialog(parent)
    dialog.setWindowTitle("发现新版本")
    dialog.setModal(True)
    dialog.setMinimumWidth(430)
    dialog.setStyleSheet(
        "QDialog{background:#1f232b;color:#e6e7eb;}"
        "QLabel{color:#e6e7eb;}"
        "QPushButton{background:#2f343e;color:#e6e7eb;border:1px solid #3a3f4b;"
        "border-radius:6px;padding:8px 18px;min-width:92px;}"
        "QPushButton:hover{background:#3a4150;border-color:#4f8cff;}"
        "QPushButton#primary{background:#4f8cff;color:#ffffff;border-color:#4f8cff;}"
        "QPushButton#primary:hover{background:#3b73e6;}"
    )

    layout = QVBoxLayout(dialog)
    layout.setContentsMargins(24, 22, 24, 20)
    layout.setSpacing(14)

    title = QLabel(f"发现新版本 {latest_version}")
    title_font = QFont()
    title_font.setPointSize(14)
    title_font.setBold(True)
    title.setFont(title_font)
    layout.addWidget(title)

    body = QLabel(
        f"当前版本：{app_version}\n"
        "发布仓库：github.com/mi0e/BiliBiliDropsMiner\n"
        "可前往发布页下载最新版本。"
    )
    body.setWordWrap(True)
    body.setStyleSheet("color:#c9d1d9;line-height:1.45;")
    layout.addWidget(body)

    button_row = QHBoxLayout()
    button_row.addStretch(1)
    later_btn = QPushButton("稍后")
    open_btn = QPushButton("打开发布页")
    open_btn.setObjectName("primary")
    later_btn.clicked.connect(dialog.reject)
    open_btn.clicked.connect(dialog.accept)
    button_row.addWidget(later_btn)
    button_row.addWidget(open_btn)
    layout.addLayout(button_row)

    return dialog.exec() == QDialog.Accepted
