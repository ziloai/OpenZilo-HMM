# SPDX-License-Identifier: MPL-2.0
"""Start the PySide6 interface."""
from __future__ import annotations

import os
import sys
from importlib.resources import files


def application_icon():
    """Load the packaged PNG eagerly, including when resources live in a ZIP."""
    from PySide6.QtGui import QIcon, QPixmap

    data = files("hmm_gesture_studio").joinpath("qt/assets/studio-icon.png").read_bytes()
    pixmap = QPixmap()
    if not pixmap.loadFromData(data, "PNG"):
        raise RuntimeError("Cannot load Studio application icon")
    return QIcon(pixmap)


def main() -> None:
    if (sys.platform.startswith("linux") and not os.environ.get("QT_QPA_PLATFORM")
            and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")):
        # Qt would abort the process with a much less helpful message.
        raise SystemExit("无法初始化桌面显示：请在有图形桌面的环境运行。")
    from PySide6.QtWidgets import QApplication

    from .dialogs import QtDialogs
    from .i18n import language_manager, tr
    from .theme import apply_theme
    from .window import MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName("OpenZilo Studio")
    app.setWindowIcon(application_icon())
    language_manager.initialize()
    apply_theme(app)
    try:
        window = MainWindow()
        window.show()
        window.controller.start()
    except Exception as exc:
        QtDialogs().error(tr("启动失败"), str(exc))
        raise SystemExit(tr("桌面平台启动失败：{error}", error=str(exc))) from exc
    raise SystemExit(app.exec())
