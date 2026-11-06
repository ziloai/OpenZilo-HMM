# SPDX-License-Identifier: MPL-2.0
"""Native dialogs behind a small interface, so controller tests need no display."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .i18n import resolve, tr


class QtDialogs:
    def __init__(self, parent=None):
        self.parent = parent

    def ask(self, title, text, *, danger=False) -> bool:
        """Dangerous confirmations default to the safe answer."""
        box = QMessageBox(self.parent)
        box.setWindowTitle(resolve(title))
        box.setText(resolve(title))
        box.setInformativeText(resolve(text))
        box.setIcon(QMessageBox.Icon.Warning if danger else QMessageBox.Icon.Question)
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText(resolve(tr("继续")))
        box.button(QMessageBox.StandardButton.No).setText(resolve(tr("取消")))
        box.setDefaultButton(QMessageBox.StandardButton.No if danger else QMessageBox.StandardButton.Yes)
        return box.exec() == QMessageBox.StandardButton.Yes

    def error(self, title, text) -> None:
        box = QMessageBox(self.parent)
        box.setWindowTitle(resolve(title))
        box.setText(resolve(title))
        box.setInformativeText(resolve(text))
        box.setIcon(QMessageBox.Icon.Critical)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.button(QMessageBox.StandardButton.Ok).setText(resolve(tr("好")))
        box.exec()

    def open_file(self, filters) -> str:
        return QFileDialog.getOpenFileName(self.parent, resolve(tr("选择文件")), "", resolve(filters))[0]

    def open_files(self, filters) -> list[str]:
        return QFileDialog.getOpenFileNames(self.parent, resolve(tr("选择文件")), "", resolve(filters))[0]

    def save_file(self, suggested, filters) -> str:
        path = QFileDialog.getSaveFileName(self.parent, resolve(tr("保存文件")), suggested, resolve(filters))[0]
        if path and not Path(path).suffix:
            path += ".json"
        return path

    def directory(self, start) -> str:
        return QFileDialog.getExistingDirectory(self.parent, resolve(tr("选择目录")), start)

    def open_url(self, url) -> None:
        if not QDesktopServices.openUrl(QUrl(url)):
            raise OSError(tr("无法打开：{path}", path=url))

    def open_path(self, path) -> None:
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            raise OSError(tr("无法打开：{path}", path=path))
