# SPDX-License-Identifier: MPL-2.0
"""Small, consistent building blocks. Their look comes from theme.STYLESHEET."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QRectF, QSize, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QTableWidget, QVBoxLayout, QWidget,
)

from . import theme
from .i18n import bind_text, language_manager, tr


def _repolish(widget):
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


def label(text="", role=None, *, tone=None, wrap=False, selectable=False):
    item = QLabel()
    bind_text(item, text)
    if role:
        item.setProperty("role", role)
    if tone:
        item.setProperty("tone", tone)
    item.setWordWrap(wrap)
    if selectable:
        item.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return item


def set_tone(item, tone):
    if item.property("tone") != tone:
        item.setProperty("tone", tone)
        _repolish(item)


def button(text, on_click=None, *, kind=None):
    item = QPushButton()
    bind_text(item, text)
    item.setCursor(Qt.CursorShape.PointingHandCursor)
    if kind:
        item.setProperty("kind", kind)
    if on_click is not None:
        item.clicked.connect(lambda _checked=False: on_click())
    return item


def set_kind(item, kind):
    if item.property("kind") != kind:
        item.setProperty("kind", kind)
        _repolish(item)


def set_text(item, text):
    """Avoid relayout when a periodically re-rendered value has not changed."""
    bind_text(item, text)


def row(*items, spacing=theme.ELEMENT_GAP, stretch=True):
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for item in items:
        if isinstance(item, QWidget):
            layout.addWidget(item)
        else:
            layout.addLayout(item)
    if stretch:
        layout.addStretch(1)
    return layout


def field(caption, content):
    """A caption above a widget or layout, such as an input and its action button."""
    layout = QVBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    layout.addWidget(label(caption, "caption"))
    if isinstance(content, QWidget):
        layout.addWidget(content)
    else:
        layout.addLayout(content)
    return layout


def line_edit(text="", *, on_edit=None, width=None, placeholder=""):
    item = QLineEdit(text)
    bind_text(item, placeholder, "setPlaceholderText")
    if width:
        item.setFixedWidth(width)
    if on_edit is not None:
        item.textEdited.connect(on_edit)
    return item


def set_field_text(item, text):
    """Programmatic update that keeps the caret when the user is mid-edit."""
    if item.text() != text:
        item.setText(text)


def hairline():
    item = QFrame()
    item.setObjectName("hairline")
    return item


class Combo(QComboBox):
    """Pill combo; Qt stylesheets need an image for the arrow, so paint a chevron."""

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(theme.INK if self.isEnabled() else theme.STEEL), 1.5))
        x, y = self.width() - 18, self.height() / 2
        painter.drawLine(int(x - 4), int(y - 2), int(x), int(y + 2))
        painter.drawLine(int(x), int(y + 2), int(x + 4), int(y - 2))
        painter.end()


class Switch(QWidget):
    """Labelled on/off control; a compact blue moment, the only colored toggle."""

    toggled = Signal(bool)

    def __init__(self, text=""):
        super().__init__()
        self._checked = False
        self._text = ""
        bind_text(self, text)
        self._position = 0.0
        self._slide = QVariantAnimation(self)
        self._slide.setDuration(theme.MOTION_FAST)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.valueChanged.connect(self._set_position)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def setText(self, text):
        self._text = text
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def text(self):
        return self._text

    def sizeHint(self):
        return QSize(36 + (8 + self.fontMetrics().horizontalAdvance(self._text) if self._text else 0), 24)

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        """Reflect state without emitting; only user input emits toggled."""
        if self._checked != bool(checked):
            self._checked = bool(checked)
            target = 1.0 if self._checked else 0.0
            self._slide.stop()
            if self.isVisible():
                self._slide.setStartValue(self._position)
                self._slide.setEndValue(target)
                self._slide.start()
            else:
                self._set_position(target)

    def _set_position(self, value):
        self._position = float(value)
        self.update()

    def _activate(self):
        if self.isEnabled():
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)

    def mouseReleaseEvent(self, event):
        if self.rect().contains(event.position().toPoint()):
            self._activate()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._activate()
        else:
            super().keyPressEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()
        off, on = QColor(theme.CONTROL_GRAY), QColor(theme.PRICING_BLUE)
        amount = self._position if enabled else 0.0
        track = QColor(*(int(a + (b - a) * amount) for a, b in
                         zip(off.getRgb()[:3], on.getRgb()[:3])))
        painter.setPen(QPen(on, 2) if self.hasFocus() else Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QRectF(1, 3, 34, 18), 9, 9)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.GALLERY_WHITE))
        painter.drawEllipse(QRectF(3 + 16 * self._position, 5, 14, 14))
        if self._text:
            painter.setPen(QColor(theme.INK if enabled else theme.STEEL))
            painter.drawText(QRectF(44, 0, self.width() - 44, 24),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text)
        painter.end()


class Card(QFrame):
    """28px shadowless card, separated from the white canvas by a hairline."""

    def __init__(self, title=None, *, spacing=theme.ELEMENT_GAP):
        super().__init__()
        self.setObjectName("card")
        self.body = QVBoxLayout(self)
        p = theme.CARD_PADDING
        self.body.setContentsMargins(p, p - 4, p, p - 4)
        self.body.setSpacing(spacing)
        if title:
            self.body.addWidget(label(title, "cardTitle"))

    def add(self, item):
        if isinstance(item, QWidget):
            self.body.addWidget(item)
        else:
            self.body.addLayout(item)
        return item


class StatTile(QWidget):
    """A value that matters more than a chart: big number, quiet caption."""

    def __init__(self, caption, value="—"):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.setMinimumWidth(92)  # Keep neighbors still as live values change width.
        self.value = label(value, "stat")
        layout.addWidget(self.value)
        self.caption = label(caption, "caption")
        layout.addWidget(self.caption)

    def set_value(self, text):
        set_text(self.value, text)


class ReadOnlyText(QPlainTextEdit):
    def __init__(self, text="", *, height=120):
        super().__init__()
        bind_text(self, text, "setPlainText")
        self.setReadOnly(True)
        self.setFixedHeight(height)

    def set_value(self, text):
        bind_text(self, text, "setPlainText")


def table(columns, *, multi=False, height=220):
    item = QTableWidget(0, len(columns))
    bind_text(item, columns, "setHorizontalHeaderLabels")
    item.verticalHeader().hide()
    item.verticalHeader().setDefaultSectionSize(38)
    item.setShowGrid(False)
    item.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    item.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    item.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    item.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection if multi
                          else QAbstractItemView.SelectionMode.SingleSelection)
    header = item.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setHighlightSections(False)
    header.setStretchLastSection(True)
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    item.setMinimumHeight(height)
    item.setMaximumHeight(height)
    return item


class Section(QWidget):
    """A chapter of a page, on the white canvas."""

    def __init__(self, *, top=theme.SECTION_GAP, bottom=theme.SECTION_GAP):
        super().__init__()
        self.setObjectName("plain")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(theme.PAGE_MARGIN, top, theme.PAGE_MARGIN, bottom)
        inner = QWidget()
        inner.setMaximumWidth(theme.CONTENT_MAX_WIDTH)
        self.body = QVBoxLayout(inner)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(theme.ELEMENT_GAP + 8)
        outer.addStretch(1)
        outer.addWidget(inner, 100)
        outer.addStretch(1)

    def add(self, item):
        if isinstance(item, QWidget):
            self.body.addWidget(item)
        else:
            self.body.addLayout(item)
        return item


class Page(QScrollArea):
    """A sidebar destination. Renders only while visible, and again when shown."""

    def __init__(self, controller, navigate=None):
        super().__init__()
        self.controller = controller
        self.navigate = navigate or (lambda _name: None)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("canvas")
        self.setWidget(content)
        self._sections = QVBoxLayout(content)
        self._sections.setContentsMargins(0, 0, 0, 0)
        self._sections.setSpacing(0)
        controller.stateChanged.connect(self._render_if_visible)
        controller.tick.connect(self._tick_if_visible)
        language_manager.changed.connect(self._language_changed)

    def add_section(self):
        # Only the first section needs top space; later ones follow the previous bottom.
        section = Section(top=0 if self._sections.count() else theme.SECTION_GAP)
        self._sections.addWidget(section)
        return section

    def finish(self):
        self._sections.addStretch(1)

    def header(self, section, title, subtitle=None):
        section.add(label(title, "title"))
        if subtitle:
            section.add(label(subtitle, tone="muted", wrap=True))

    def _render_if_visible(self):
        if self.isVisible():
            self.render_state()

    def _tick_if_visible(self):
        if self.isVisible():
            self.on_tick()

    def showEvent(self, event):
        super().showEvent(event)
        self.render_state()

    def _language_changed(self):
        self.retranslate()
        self.render_state()

    def retranslate(self):
        """Refresh non-bound presentation without changing controller state."""

    def render_state(self):
        """Apply controller state to widgets."""

    def on_tick(self):
        """Periodic chart redraw while visible."""


class DeviceNotice(QWidget):
    """Shown on device-dependent pages until a ring is connected."""

    def __init__(self, page, text):
        super().__init__()
        self._page = page
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(label(text, tone="muted"))
        layout.addWidget(button(tr("前往连接 ›"), lambda: page.navigate("device"), kind="link"))
        layout.addStretch(1)

    def render_state(self):
        self.setVisible(not self._page.controller.connected)
