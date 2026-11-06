# SPDX-License-Identifier: MPL-2.0
"""Start the PySide6 interface."""
from __future__ import annotations

import os
import sys


def main() -> None:
    if (sys.platform.startswith("linux") and not os.environ.get("QT_QPA_PLATFORM")
            and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")):
        # Qt would abort the process with a much less helpful message.
        raise SystemExit("无法初始化桌面显示：请在有图形桌面的环境运行。")
    from PySide6.QtWidgets import QApplication, QMessageBox

    from .i18n import language_manager, resolve, tr
    from .theme import apply_theme
    from .window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("OpenZilo Studio")
    language_manager.initialize()
    apply_theme(app)
    try:
        window = MainWindow()
        window.show()
        window.controller.start()
    except Exception as exc:
        QMessageBox.critical(None, resolve(tr("启动失败")), str(exc))
        raise SystemExit(tr("桌面平台启动失败：{error}", error=str(exc))) from exc
    raise SystemExit(app.exec())
