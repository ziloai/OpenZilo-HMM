# SPDX-License-Identifier: MPL-2.0
"""Main window: sidebar, pages, and a collapsible log."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import theme
from .controller import StudioController
from .dialogs import QtDialogs
from .pages.audio import AudioPage
from .pages.capture import CapturePage
from .pages.device import DevicePage
from .pages.diagnostics import DiagnosticsPage
from .pages.live import LivePage
from .pages.tools import ToolsPage
from .pages.train import TrainPage
from .widgets import button, label, set_text, set_tone

NAV = (("capture", "采集"), ("train", "训练"), ("live", "实时"), ("diagnostics", "诊断"),
       ("audio", "录音"), ("tools", "设备工具"))
LOG_LINES = 1500


class DeviceCard(QPushButton):
    """Always-visible connection status; opens the device page."""

    def __init__(self, on_click):
        super().__init__()
        self.setObjectName("deviceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(lambda _checked=False: on_click())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = QLabel()
        self.dot.setFixedSize(8, 8)
        top.addWidget(self.dot)
        self.name = label(role="strong")
        top.addWidget(self.name, 1)
        layout.addLayout(top)
        self.detail = label(role="caption")
        layout.addWidget(self.detail)
        for child in (self.dot, self.name, self.detail):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setFixedHeight(62)

    def sizeHint(self):
        return self.layout().sizeHint()

    def render_state(self, c):
        color = theme.GOOD if c.connected and c.imu_active else theme.WARNING if c.connected or c.connecting else theme.STEEL
        self.dot.setStyleSheet(f"background: {color}; border-radius: 4px;")
        metrics = QFontMetrics(self.name.font())
        set_text(self.name, metrics.elidedText(c.device_summary, Qt.TextElideMode.ElideMiddle, 150))
        set_text(self.detail, c.battery_text if c.connected else "点击连接设备" if not c.connecting else "可点击取消")
        set_tone(self.detail, c.battery_tone if c.connected and c.battery_tone != "normal" else None)


class LogBar(QWidget):
    """One-line latest message; expands to the full log."""

    def __init__(self):
        super().__init__()
        self.setObjectName("logBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(4)
        top = QHBoxLayout()
        self.latest = label(role="caption")
        self.latest.setMinimumWidth(0)
        top.addWidget(self.latest, 1)
        self.toggle = button("显示日志", self._toggle, kind="link")
        top.addWidget(self.toggle)
        layout.addLayout(top)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(LOG_LINES)
        self.view.setFixedHeight(180)
        self.view.hide()
        layout.addWidget(self.view)

    def append(self, text):
        self.view.appendPlainText(text)
        first = text.splitlines()[0] if text else ""
        self.latest.setText(QFontMetrics(self.latest.font()).elidedText(
            first, Qt.TextElideMode.ElideRight, max(200, self.latest.width())))

    def _toggle(self):
        visible = not self.view.isVisible()
        self.view.setVisible(visible)
        self.toggle.setText("收起日志" if visible else "显示日志")


class MainWindow(QMainWindow):
    def __init__(self, *, controller=None):
        super().__init__()
        self.setWindowTitle("OpenZilo 手势与录音工作室")
        self.resize(1280, 860)
        self.setMinimumSize(1100, 720)
        self.controller = controller or StudioController(QtDialogs(self))
        if self.controller.dialogs.__class__ is QtDialogs:
            self.controller.dialogs.parent = self
        self._may_close = False
        root = QWidget()
        root.setObjectName("canvas")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.stack = QStackedWidget()
        right.addWidget(self.stack, 1)
        self.log_bar = LogBar()
        right.addWidget(self.log_bar)
        outer.addLayout(right, 1)
        self.pages = {}
        for name, page_class in (("capture", CapturePage), ("train", TrainPage), ("live", LivePage),
                                 ("diagnostics", DiagnosticsPage), ("audio", AudioPage), ("tools", ToolsPage),
                                 ("device", DevicePage)):
            page = page_class(self.controller, self.navigate)
            self.pages[name] = page
            self.stack.addWidget(page)
        c = self.controller
        c.logged.connect(self.log_bar.append)
        c.stateChanged.connect(self._render_sidebar)
        c.previewRevealed.connect(lambda: self.navigate("capture"))
        c.closeFinished.connect(self._close_now)
        self.navigate("capture")
        self._render_sidebar()

    def _build_sidebar(self):
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(208)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 20, 12, 12)
        layout.setSpacing(2)
        brand = label("OpenZilo", "brand")
        brand.setContentsMargins(12, 0, 0, 16)
        layout.addWidget(brand)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(False)  # The device page has no nav row, so none may be checked.
        self.nav_buttons = {}
        for name, text in NAV:
            item = button(text, lambda name=name: self.navigate(name), kind="nav")
            item.setCheckable(True)
            self.nav_group.addButton(item)
            self.nav_buttons[name] = item
            layout.addWidget(item)
        layout.addStretch(1)
        self.device_card = DeviceCard(lambda: self.navigate("device"))
        layout.addWidget(self.device_card)
        return sidebar

    def navigate(self, name):
        self.stack.setCurrentWidget(self.pages[name])
        for key, item in self.nav_buttons.items():
            item.setChecked(key == name)
        current = "true" if name == "device" else "false"
        if self.device_card.property("current") != current:
            self.device_card.setProperty("current", current)
            self.device_card.style().unpolish(self.device_card)
            self.device_card.style().polish(self.device_card)

    def _render_sidebar(self):
        self.device_card.render_state(self.controller)

    def closeEvent(self, event):
        if self._may_close:
            event.accept()
            return
        event.ignore()
        self.controller.request_close()

    def _close_now(self):
        self._may_close = True
        self.close()
