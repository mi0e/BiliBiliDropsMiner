from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication


APP_STYLE_SHEET = """
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


def configure_qt_app(app: QApplication) -> None:
    app.setStyle("Fusion")
    default_font = QFont("Segoe UI", 10)
    # Fall back to Microsoft YaHei for CJK glyphs on Windows.
    default_font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(default_font)
    app.setStyleSheet(APP_STYLE_SHEET)
