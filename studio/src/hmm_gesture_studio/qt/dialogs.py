# SPDX-License-Identifier: MPL-2.0
"""Dialogs behind a small interface, so controller tests need no display.

Confirmations and errors are drawn by the app; file pickers stay native."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QEvent, Qt, QUrl, QVariantAnimation
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QTextBlockFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QTextEdit, QVBoxLayout, QWidget

from . import theme
from .i18n import resolve, tr
from .widgets import button, label

_BODY_LINE_HEIGHT = 135  # Percent; long CJK warnings are hard to read at the default.


class _Scrim(QWidget):
    """Dims the window behind a dialog. The design has no shadows, so without this
    a white card on a white page would be separated only by a hairline."""

    def __init__(self, window):
        super().__init__(window)
        self._level = 0.0
        self.setGeometry(window.rect())
        window.installEventFilter(self)
        self.show()
        self.raise_()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize:
            self.setGeometry(watched.rect())
        return False

    def set_level(self, value):
        self._level = float(value)
        self.update()

    def paintEvent(self, event):
        color = QColor(theme.SCRIM)
        color.setAlphaF(theme.SCRIM_OPACITY * self._level)
        painter = QPainter(self)
        painter.fillRect(self.rect(), color)
        painter.end()


class StudioDialog(QDialog):
    """A modal card with a title, a selectable message and right-aligned actions."""

    def __init__(self, parent, title, text, *, confirm, cancel=None, danger=False, critical=False):
        super().__init__(parent, Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("studioDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle(title)
        self.setFixedWidth(theme.DIALOG_WIDTH)
        self._anchor = parent.window() if parent is not None else None
        if self._anchor is not None:
            self._anchor.installEventFilter(self)

        card = QFrame()
        card.setObjectName("dialogCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(theme.CARD_PADDING, theme.CARD_PADDING, theme.CARD_PADDING, theme.CARD_PADDING)
        layout.setSpacing(theme.ELEMENT_GAP)

        self.title_label = label(title, "cardTitle", wrap=True)
        heading = QHBoxLayout()
        heading.setSpacing(theme.ELEMENT_GAP)
        if danger or critical:
            badge = QLabel("!")
            badge.setObjectName("dialogBadge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heading.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        heading.addWidget(self.title_label, 1)
        layout.addLayout(heading)

        self.body = self._build_body(text)
        layout.addWidget(self.body)
        self.body.setVisible(bool(text))
        layout.addSpacing(theme.ELEMENT_GAP)

        self.confirm_button = button(confirm, self.accept, kind="danger" if danger else "primary")
        self.cancel_button = button(cancel, self.reject) if cancel is not None else None
        actions = QHBoxLayout()
        actions.setSpacing(theme.ELEMENT_GAP)
        actions.addStretch(1)
        if self.cancel_button is not None:
            actions.addWidget(self.cancel_button)
        actions.addWidget(self.confirm_button)
        layout.addLayout(actions)

        # Enter must never run a dangerous action by default.
        default = self.cancel_button if danger and self.cancel_button is not None else self.confirm_button
        default.setDefault(True)
        default.setFocus()
        self.default_button = default

    def _build_body(self, text):
        """A text view rather than a label: paths and errors have no spaces to wrap
        at, and an unexpectedly long message has to scroll instead of growing."""
        body = QTextEdit()
        body.setObjectName("dialogBody")
        body.setReadOnly(True)
        body.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        body.setFrameShape(QFrame.Shape.NoFrame)
        body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        body.ensurePolished()
        document = body.document()
        document.setDocumentMargin(0)
        body.setPlainText(text)
        spacing = QTextBlockFormat()
        spacing.setLineHeight(_BODY_LINE_HEIGHT, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor = QTextCursor(document)
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.mergeBlockFormat(spacing)
        # Measure a copy: until shown, QTextEdit lays its own document out at a default width.
        measure = document.clone(body)
        measure.setTextWidth(theme.DIALOG_WIDTH - 2 * (theme.CARD_PADDING + 1))
        body.setFixedHeight(min(math.ceil(measure.size().height()) + 1, theme.DIALOG_BODY_MAX_HEIGHT))
        measure.deleteLater()
        return body

    def center_on_anchor(self):
        if self._anchor is None:
            return
        self.adjustSize()
        self.move(self._anchor.frameGeometry().center() - self.rect().center())

    def eventFilter(self, watched, event):
        if watched is self._anchor and event.type() in (QEvent.Type.Move, QEvent.Type.Resize):
            self.center_on_anchor()
        return False


class QtDialogs:
    def __init__(self, parent=None):
        self.parent = parent
        self._scrim = None

    def _run(self, dialog) -> bool:
        window = self.parent.window() if self.parent is not None else None
        # An error can arrive while a confirmation is open; dim only once.
        owns_scrim = self._scrim is None and window is not None and window.isVisible()
        if owns_scrim:
            self._scrim = _Scrim(window)
        fade = QVariantAnimation(dialog)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setDuration(theme.MOTION_FAST)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade.valueChanged.connect(dialog.setWindowOpacity)
        if owns_scrim:
            fade.valueChanged.connect(self._scrim.set_level)
        dialog.center_on_anchor()
        dialog.setWindowOpacity(0.0)
        fade.start()
        try:
            return dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            fade.stop()
            if owns_scrim:
                self._scrim.hide()
                self._scrim.deleteLater()
                self._scrim = None
            dialog.deleteLater()

    def ask(self, title, text, *, danger=False) -> bool:
        """Dangerous confirmations default to the safe answer."""
        return self._run(StudioDialog(self.parent, resolve(title), resolve(text), confirm=resolve(tr("继续")),
                                      cancel=resolve(tr("取消")), danger=danger))

    def error(self, title, text) -> None:
        self._run(StudioDialog(self.parent, resolve(title), resolve(text), confirm=resolve(tr("好")), critical=True))

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
