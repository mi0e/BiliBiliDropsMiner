"""pytest 的共享前置设置。

这里只做一件事：让无显示环境（CI 的 ubuntu runner、本地 SSH 会话）也能跑
GUI 相关测试。此前 7 个测试文件各自在模块顶部写了同一行
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")，必须放在 import PySide6
之前才生效，于是正确性依赖导入顺序——漏写的文件在有显示的机器上照常通过，
只有 headless 环境才会失败，排查成本很高。

conftest.py 由 pytest 在收集阶段、导入任何测试模块之前加载，所以放在这里
对全部测试文件都生效，包括没有自己写那行的那些。
"""

from __future__ import annotations

import os

# 必须在 PySide6 被导入之前设置：QApplication 创建时读这个变量，之后再改无效。
# 无条件 setdefault，不做平台判断——Windows 上有显示环境，offscreen 同样能正常
# 跑（QSettings / QMessageBox 都不弹窗），而按平台分支会在「判断条件写错」时
# 静默失效，那正是这次要消除的那类问题。显式指定平台的调用者不受影响。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")