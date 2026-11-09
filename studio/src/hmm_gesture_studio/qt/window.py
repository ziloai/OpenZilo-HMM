# SPDX-License-Identifier: MPL-2.0
"""Main window: sidebar, pages, and a collapsible log."""
from __future__ import annotations

import sys

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QRect, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QPushButton, QStackedWidget, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from . import theme
from .controller import StudioController
from .dialogs import QtDialogs
from .i18n import bind_text, language_manager, resolve, tr
from .motion import CrossFade
from .pages.about import AboutPage
from .pages.audio import AudioPage
from .pages.capture import CapturePage
from .pages.device import DevicePage
from .pages.diagnostics import DiagnosticsPage
from .pages.live import LivePage
from .pages.plugins import PluginsPage
from .pages.tools import ToolsPage
from .pages.train import TrainPage
from .pages.settings import SettingsPage
from .widgets import button, label, set_text, set_tone

NAV = (("capture", "采集"), ("train", "训练"), ("live", "实时"), ("diagnostics", "诊断"),
       ("audio", "录音"), ("tools", "设备工具"), ("plugins", "插件"))
LOG_LINES = 1500
LOG_HEIGHT = 240
SIDEBAR_TOP = 20
SIDEBAR_TOP_MERGED = 6


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
        set_text(self.detail, c.battery_text if c.connected else tr("点击连接设备") if not c.connecting else tr("可点击取消"))
        set_tone(self.detail, c.battery_tone if c.connected and c.battery_tone != "normal" else None)


_LEVEL_LABELS = {"warning": tr("警告："), "error": tr("错误：")}
_LEVEL_COLORS = {"warning": theme.WARNING, "error": theme.CRITICAL}
_LOG_ROLE = Qt.ItemDataRole.UserRole


class _LogRowDelegate(QStyledItemDelegate):
    """One row per message: time column, then the wrapped text, with a hairline."""

    TIME_WIDTH = 72
    PADDING = 10

    def _text_width(self, width):
        return max(80, width - self.TIME_WIDTH - 2 * self.PADDING)

    def sizeHint(self, option, index):
        _time, _level, text = index.data(_LOG_ROLE)
        width = option.widget.viewport().width() if option.widget else option.rect.width()
        bounds = option.fontMetrics.boundingRect(
            QRect(0, 0, self._text_width(width), 100000), Qt.TextFlag.TextWordWrap, text)
        return QSize(width, bounds.height() + 14)

    def paint(self, painter, option, index):
        when, level, text = index.data(_LOG_ROLE)
        painter.save()
        rect = option.rect
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(rect, QColor(theme.CONTROL_GRAY))
        painter.setPen(QColor(theme.SLATE))
        top = rect.top() + 7
        painter.drawText(QRect(rect.left() + self.PADDING, top, self.TIME_WIDTH, rect.height() - 14),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, when)
        painter.setPen(QColor(_LEVEL_COLORS.get(level, theme.INK)))
        painter.drawText(QRect(rect.left() + self.PADDING + self.TIME_WIDTH, top,
                               self._text_width(rect.width()), rect.height() - 14),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap, text)
        painter.setPen(QColor(theme.CONTROL_GRAY))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()


class LogList(QListWidget):
    """Newest last. Follows new rows unless the reader has scrolled up; Ctrl+C copies."""

    def __init__(self):
        super().__init__()
        self.setObjectName("logList")
        self.setItemDelegate(_LogRowDelegate(self))
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setResizeMode(QListWidget.ResizeMode.Adjust)
        font = self.font()
        font.setPixelSize(12)
        self.setFont(font)
        language_manager.changed.connect(self.retranslate)

    def retranslate(self):
        for index in range(self.count()):
            item = self.item(index)
            when, level, text = item._source_entry
            item.setData(_LOG_ROLE, (when, level, resolve(_LEVEL_LABELS.get(level, "")) + resolve(text)))
        self.doItemsLayout()
        self.viewport().update()

    def add_entry(self, when, level, text):
        bar = self.verticalScrollBar()
        following = bar.value() >= bar.maximum()
        item = QListWidgetItem()
        shown = resolve(_LEVEL_LABELS.get(level, "")) + resolve(text)
        item._source_entry = (when, level, text)
        item.setData(_LOG_ROLE, (when, level, shown))
        self.addItem(item)
        while self.count() > LOG_LINES:
            self.takeItem(0)
        if following:
            self.scrollToBottom()

    def entries(self):
        return [self.item(i).data(_LOG_ROLE) for i in range(self.count())]

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.StandardKey.Copy):
            rows = sorted(self.selectedIndexes(), key=lambda index: index.row())
            lines = ["  ".join((index.data(_LOG_ROLE)[0], index.data(_LOG_ROLE)[2])) for index in rows]
            QApplication.clipboard().setText("\n".join(lines))
        else:
            super().keyPressEvent(event)


class LogBar(QWidget):
    """One-line latest message; expands into the full list."""

    def __init__(self):
        super().__init__()
        self.setObjectName("logBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._expanded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        layout.setSpacing(4)
        top = QHBoxLayout()
        self.latest = label(role="caption")
        self.latest.setMinimumWidth(0)
        top.addWidget(self.latest, 1)
        self.toggle = button(tr("显示日志"), self._toggle, kind="link")
        top.addWidget(self.toggle)
        layout.addLayout(top)
        self.view = LogList()
        self.view.setFixedHeight(0)
        self.view.hide()
        layout.addWidget(self.view)
        self._motion = QVariantAnimation(self)
        self._motion.setDuration(theme.MOTION)
        self._motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._motion.valueChanged.connect(lambda height: self.view.setFixedHeight(int(height)))
        self._motion.finished.connect(self._motion_finished)
        self._latest_entry = None
        language_manager.changed.connect(self._render_latest)

    def append(self, when, level, text):
        self.view.add_entry(when, level, text)
        self._latest_entry = (when, level, text)
        self._render_latest()

    def _render_latest(self):
        if self._latest_entry is None:
            return
        when, level, text = self._latest_entry
        lines = resolve(text).splitlines()
        first = f"{when}  {resolve(_LEVEL_LABELS.get(level, ''))}{lines[0] if lines else ''}"
        self.latest.setText(QFontMetrics(self.latest.font()).elidedText(
            first, Qt.TextElideMode.ElideRight, max(200, self.latest.width())))
        set_tone(self.latest, {"warning": "warning", "error": "critical"}.get(level))

    def _toggle(self):
        self._expanded = not self._expanded
        set_text(self.toggle, tr("收起日志") if self._expanded else tr("显示日志"))
        self._motion.stop()
        self._motion.setStartValue(self.view.height() if self.view.isVisible() else 0)
        self._motion.setEndValue(LOG_HEIGHT if self._expanded else 0)
        self.view.show()
        self._motion.start()

    def _motion_finished(self):
        if self._expanded:
            self.view.scrollToBottom()  # The list only now has its final height.
        else:
            self.view.hide()


class TitleBarArea(QWidget):
    """Blank strip under a merged title bar, which still moves the window.

    If the platform handles the title bar itself, these events never arrive.
    """

    def __init__(self):
        super().__init__()
        self.hide()

    def mousePressEvent(self, event):
        handle = self.window().windowHandle()
        if event.button() == Qt.MouseButton.LeftButton and handle is not None:
            handle.startSystemMove()
        event.accept()

    def mouseDoubleClickEvent(self, event):
        window = self.window()
        window.showNormal() if window.isMaximized() else window.showMaximized()
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self, *, controller=None, merge_title_bar=None):
        super().__init__()
        self._merge_title_bar = sys.platform == "darwin" if merge_title_bar is None else merge_title_bar
        self._safe_area_window = None
        # A merged title bar has nothing to label; the app name still identifies the window.
        bind_text(self, "" if self._merge_title_bar else tr("OpenZilo 手势与录音工作室"), "setWindowTitle")
        if self._merge_title_bar:
            self.setWindowFlag(Qt.WindowType.ExpandedClientAreaHint)
            self.setWindowFlag(Qt.WindowType.NoTitleBarBackgroundHint)
        self.resize(1280, 860)
        self.setMinimumSize(1100, 720)
        self.controller = controller or StudioController(QtDialogs(self))
        if self.controller.dialogs.__class__ is QtDialogs:
            self.controller.dialogs.parent = self
        self._may_close = False
        self._current_page = None
        root = QWidget()
        root.setObjectName("canvas")
        self.setCentralWidget(root)
        # Layout below the title bar is explicit (set_title_bar_inset), so backgrounds
        # reach the top edge and Qt's automatic insets cannot add a second gap.
        self._no_automatic_inset = [self, root]
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        self.content_title_area = TitleBarArea()
        right.addWidget(self.content_title_area)
        self.stack = QStackedWidget()
        self._fade = CrossFade(self.stack)
        right.addWidget(self.stack, 1)
        self.log_bar = LogBar()
        right.addWidget(self.log_bar)
        outer.addLayout(right, 1)
        for widget in (*self._no_automatic_inset, self.sidebar, self.sidebar_title_area, self.content_title_area):
            widget.setAttribute(Qt.WidgetAttribute.WA_ContentsMarginsRespectsSafeArea, False)
        self.pages = {}
        for name, page_class in (("capture", CapturePage), ("train", TrainPage), ("live", LivePage),
                                 ("diagnostics", DiagnosticsPage), ("audio", AudioPage), ("tools", ToolsPage),
                                 ("plugins", PluginsPage), ("device", DevicePage), ("about", AboutPage), ("settings", SettingsPage)):
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
        language_manager.changed.connect(self._render_sidebar)

    def _build_sidebar(self):
        sidebar = QWidget()
        self.sidebar = sidebar
        sidebar.setObjectName("sidebar")
        sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sidebar.setFixedWidth(208)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, SIDEBAR_TOP, 12, 12)
        layout.setSpacing(2)
        self._sidebar_layout = layout
        self.sidebar_title_area = TitleBarArea()
        layout.addWidget(self.sidebar_title_area)
        self.nav_indicator = QFrame(sidebar)
        self.nav_indicator.setObjectName("navIndicator")
        self.nav_indicator.lower()
        self._nav_motion = QPropertyAnimation(self.nav_indicator, b"geometry", self)
        self._nav_motion.setDuration(theme.MOTION)
        self._nav_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        brand = label("OpenZilo", "brand")
        brand.setContentsMargins(12, 0, 0, 16)
        layout.addWidget(brand)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(False)  # The device page has no nav row, so none may be checked.
        self.nav_buttons = {}
        for name, text in NAV:
            item = button(tr(text), lambda name=name: self.navigate(name), kind="nav")
            item.setCheckable(True)
            self.nav_group.addButton(item)
            self.nav_buttons[name] = item
            layout.addWidget(item)
        layout.addStretch(1)
        settings = button(tr("设置"), lambda: self.navigate("settings"), kind="nav")
        settings.setCheckable(True)
        self.nav_group.addButton(settings)
        self.nav_buttons["settings"] = settings
        layout.addWidget(settings)
        about = button(tr("关于"), lambda: self.navigate("about"), kind="nav")
        about.setCheckable(True)
        self.nav_group.addButton(about)
        self.nav_buttons["about"] = about
        layout.addWidget(about)
        layout.addSpacing(10)
        self.device_card = DeviceCard(lambda: self.navigate("device"))
        layout.addWidget(self.device_card)
        return sidebar

    def _place_nav_indicator(self, *, animate):
        """Slide the highlight behind the current row; the device page has none."""
        current = self.nav_buttons.get(self._current_page)
        self._nav_motion.stop()
        if current is None:
            self.nav_indicator.hide()
            return
        target = current.geometry()
        if animate and self.nav_indicator.isVisible() and self.isVisible():
            self._nav_motion.setStartValue(self.nav_indicator.geometry())
            self._nav_motion.setEndValue(target)
            self._nav_motion.start()
        else:
            self.nav_indicator.setGeometry(target)
            self.nav_indicator.show()

    def set_title_bar_inset(self, inset):
        """Reserve the title bar's height; zero when it is not merged, as in full screen."""
        for area in (self.sidebar_title_area, self.content_title_area):
            area.setFixedHeight(max(0, inset))
            area.setVisible(inset > 0)
        self._sidebar_layout.setContentsMargins(12, SIDEBAR_TOP_MERGED if inset > 0 else SIDEBAR_TOP, 12, 12)
        self._sidebar_layout.activate()
        self.layout().activate()
        self._place_nav_indicator(animate=False)

    def _watch_safe_area(self):
        handle = self.windowHandle()
        if not self._merge_title_bar or handle is None or handle is self._safe_area_window:
            return
        self._safe_area_window = handle
        handle.safeAreaMarginsChanged.connect(lambda margins: self.set_title_bar_inset(margins.top()))
        self.set_title_bar_inset(handle.safeAreaMargins().top())

    def showEvent(self, event):
        super().showEvent(event)
        self._watch_safe_area()
        self.layout().activate()
        self._place_nav_indicator(animate=False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_nav_indicator(animate=False)

    def fade_content(self):
        """Fade from what is shown now; also for a page that swaps its own views."""
        if self.isVisible():
            self._fade.play(self.stack.grab())

    def navigate(self, name):
        target = self.pages[name]
        if self.stack.currentWidget() is not target:
            self.fade_content()
        self.stack.setCurrentWidget(target)
        self._fade.raise_()
        self._current_page = name
        for key, item in self.nav_buttons.items():
            item.setChecked(key == name)
        self._place_nav_indicator(animate=True)
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
