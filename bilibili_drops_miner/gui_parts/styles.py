from __future__ import annotations


BUTTON_STYLES: dict[str, str] = {
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


CARD_STYLE = (
    "QFrame#card{background:#242832;border:1px solid #2f3440;border-radius:10px;}"
)

# 统一的禁用态：不改尺寸，只降低对比度，让「当前不可操作」一眼可辨。
_DISABLED_BUTTON_STYLE = "QPushButton:disabled{background:#2a2e37;color:#5f6673;}"
BUTTON_STYLES = {
    name: style + _DISABLED_BUTTON_STYLE for name, style in BUTTON_STYLES.items()
}

DIALOG_STYLE = (
    "QDialog{background:#1f232b;color:#e6e7eb;}"
    "QLabel{color:#e6e7eb;}"
    "QPushButton{background:#2f343e;color:#e6e7eb;border:1px solid #3a3f4b;"
    "border-radius:6px;padding:8px 18px;min-width:92px;}"
    "QPushButton:hover{background:#3a4150;border-color:#4f8cff;}"
    "QPushButton#primary{background:#4f8cff;color:#ffffff;border-color:#4f8cff;}"
    "QPushButton#primary:hover{background:#3b73e6;}"
)

