# SPDX-License-Identifier: MPL-2.0
"""Restrained motion. Continuous animations run only while their widget is visible."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QWidget

from . import theme
from .widgets import label, set_text


class _Looping(QWidget):
    def __init__(self, duration):
        super().__init__()
        self._value = 0.0
        self._loop = QVariantAnimation(self)
        self._loop.setStartValue(0.0)
        self._loop.setEndValue(1.0)
        self._loop.setDuration(duration)
        self._loop.setLoopCount(-1)
        self._loop.valueChanged.connect(self._advance)

    def _advance(self, value):
        self._value = float(value)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._loop.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._loop.stop()

    @property
    def animating(self):
        return self._loop.state() == QVariantAnimation.State.Running


class Spinner(_Looping):
    def __init__(self, size=16):
        super().__init__(900)
        self.setFixedSize(QSize(size, size))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(2, 2, -2, -2)
        painter.setPen(QPen(QColor(theme.CONTROL_GRAY), 2))
        painter.drawEllipse(area)
        painter.setPen(QPen(QColor(theme.SLATE), 2, c=Qt.PenCapStyle.RoundCap))
        painter.drawArc(area, int(-self._value * 360 * 16), 90 * 16)
        painter.end()


class PulseDot(_Looping):
    """Marks live capture; the accompanying text states it, so color is not alone."""

    def __init__(self, color=theme.CRITICAL, size=10):
        super().__init__(1000)
        self._color = QColor(color)
        self._loop.setEasingCurve(QEasingCurve.Type.InOutSine)
        self.setFixedSize(QSize(size, size))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        # Triangle wave: fade out and back within each loop.
        painter.setOpacity(0.25 + 0.75 * abs(1 - 2 * self._value))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(self.rect())
        painter.end()


class Busy(QWidget):
    """A spinner with a short description, hidden when nothing is running."""

    def __init__(self, text=""):
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.spinner = Spinner()
        layout.addWidget(self.spinner)
        self.text = label(text, tone="muted")
        layout.addWidget(self.text)
        self.text.setVisible(bool(text))
        self.hide()

    def set_busy(self, busy, text=None):
        if text is not None:
            set_text(self.text, text)
        self.setVisible(bool(busy))


class CrossFade(QWidget):
    """Fade a snapshot of the previous page over the live new one. Unlike an opacity
    effect on the page, this never re-renders the charts through an offscreen buffer.
    One long-lived instance is reused; a new transition simply restarts it."""

    def __init__(self, parent):
        super().__init__(parent)
        self._pixmap = None
        self._opacity = 0.0
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.setDuration(theme.MOTION)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.valueChanged.connect(self._set_opacity)
        self._fade.finished.connect(self._finished)

    def play(self, pixmap):
        self._fade.stop()
        self._pixmap = pixmap
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self._fade.start()

    def _finished(self):
        self.hide()
        self._pixmap = None  # Release the snapshot between transitions.

    def _set_opacity(self, value):
        self._opacity = float(value)
        self.update()

    def paintEvent(self, event):
        if self._pixmap is None:
            return
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        painter.drawPixmap(self.rect(), self._pixmap)
        painter.end()
