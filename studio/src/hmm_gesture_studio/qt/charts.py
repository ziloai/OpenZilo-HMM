# SPDX-License-Identifier: MPL-2.0
"""pyqtgraph charts. One y-scale per panel; identity is never color alone.

Mouse pan/zoom are off so a horizontal drag has one meaning: selecting a training
region. Live data is peak-downsampled so one-frame spikes stay visible.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget
import numpy as np
import pyqtgraph as pg

from ..plotting import AXES, RecognitionRegion
from . import theme
from .models import drag_to_region, recording_region_span
from .widgets import label, set_text

pg.setConfigOptions(antialias=True, background=theme.GALLERY_WHITE, foreground=theme.SLATE)


def _caption_font():
    font = QFont()
    font.setPixelSize(12)
    return font


def _tint(color, amount=0.55):
    """Blend toward white for the de-emphasized raw overlay."""
    source = QColor(color)
    mix = lambda channel: int(channel + (255 - channel) * amount)
    return QColor(mix(source.red()), mix(source.green()), mix(source.blue()))


def _limits(values):
    """Shared raw-value scale for a panel, with a nonzero span for flat data."""
    if not values.size:
        return -1.0, 1.0
    low, high = float(values.min()), float(values.max())
    padding = max(1.0, (high - low) * 0.1)
    return low - padding, high + padding


class _SelectionViewBox(pg.ViewBox):
    def __init__(self):
        super().__init__(enableMenu=False)
        self.drag_handler = None

    def mouseDragEvent(self, event, axis=None):
        if self.drag_handler is not None and self.drag_handler(self, event):
            event.accept()
        else:
            event.ignore()

    def wheelEvent(self, event, axis=None):
        event.ignore()


class ChartPanel(QWidget):
    """Title, legend with hover read-out, and one plot sharing a single y-scale."""

    def __init__(self, title, names, colors, *, unit_format="{:.0f}"):
        super().__init__()
        self._format = unit_format
        self._x = np.empty(0)
        self._y = np.empty((0, len(names)))
        self._region_items = []
        self.on_hover = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        header = QHBoxLayout()
        header.setSpacing(14)
        self.title = label(title, "strong")
        header.addWidget(self.title)
        header.addStretch(1)
        self._legend = []
        for name, color in zip(names, colors):
            swatch = QLabel()
            swatch.setFixedSize(12, 3)
            swatch.setStyleSheet(f"background: {color}; border-radius: 1px;")
            header.addWidget(swatch)
            text = label(name, "caption")
            header.addWidget(text)
            self._legend.append((name, text))
        layout.addLayout(header)
        self.view_box = _SelectionViewBox()
        self.plot = pg.PlotWidget(viewBox=self.view_box)
        self.plot.setMenuEnabled(False)
        self.plot.setMouseEnabled(x=False, y=False)
        self.plot.hideButtons()
        self.plot.setFrameShape(self.plot.Shape.NoFrame)
        self.plot.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        tick_font = QFont()
        tick_font.setPixelSize(11)
        for side in ("left", "bottom"):
            axis = self.plot.getAxis(side)
            axis.setPen(pg.mkPen(theme.HAIRLINE))
            axis.setTextPen(pg.mkPen(theme.SLATE))
            axis.setTickFont(tick_font)
            axis.setStyle(tickLength=0, maxTickLevel=0)
        self.plot.getAxis("left").setWidth(56)
        self.plot.showGrid(x=False, y=True, alpha=0.35)
        self.raw_curves = [self.plot.plot(pen=pg.mkPen(_tint(color), width=1, style=Qt.PenStyle.DashLine))
                           for color in colors]
        self.curves = [self.plot.plot(pen=pg.mkPen(color, width=2)) for color in colors]
        for curve in self.raw_curves + self.curves:
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
        self._crosshair = pg.InfiniteLine(angle=90, pen=pg.mkPen(theme.STEEL, width=1))
        self._crosshair.setZValue(50)
        self._crosshair.hide()
        self.plot.addItem(self._crosshair, ignoreBounds=True)
        self._empty = pg.TextItem("", color=theme.SLATE, anchor=(0.5, 0.5))
        self.plot.addItem(self._empty, ignoreBounds=True)
        layout.addWidget(self.plot, 1)
        self.plot.scene().sigMouseMoved.connect(self._mouse_moved)

    def set_data(self, x, y, *, raw=None, x_range, y_limits=None, empty_text=""):
        self._x, self._y = x, y
        for index, curve in enumerate(self.curves):
            curve.setData(x, y[:, index]) if len(x) else curve.setData([], [])
        for index, curve in enumerate(self.raw_curves):
            if raw is not None and len(x):
                curve.setData(x, raw[:, index])
            else:
                curve.setData([], [])
        scale = np.concatenate((y.ravel(), raw.ravel())) if raw is not None else y.ravel()
        low, high = y_limits if y_limits is not None else _limits(scale)
        self.view_box.setRange(xRange=x_range, yRange=(low, high), padding=0, disableAutoRange=True)
        self._empty.setText(empty_text if not len(x) else "")
        self._empty.setPos((x_range[0] + x_range[1]) / 2, (low + high) / 2)

    def set_regions(self, spans):
        """spans: [(start_s, stop_s, text, color)] in x units; items are pooled."""
        while len(self._region_items) < len(spans):
            region = pg.LinearRegionItem(movable=False)
            region.setZValue(40)
            caption = pg.TextItem("", anchor=(0, 0))
            caption.setFont(_caption_font())
            caption.setZValue(41)
            self.plot.addItem(region, ignoreBounds=True)
            self.plot.addItem(caption, ignoreBounds=True)
            self._region_items.append((region, caption))
        top = self.view_box.viewRange()[1][1]
        for index, (region, caption) in enumerate(self._region_items):
            if index >= len(spans):
                region.hide()
                caption.hide()
                continue
            start, stop, text, color = spans[index]
            tint = QColor(color)
            tint.setAlpha(26)
            pen = QPen(QColor(color), 1.5, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            region.setBrush(tint)
            region.setHoverBrush(tint)
            for line in region.lines:
                line.setPen(pen)
                line.setHoverPen(pen)
            region.blockLineSignal = True
            region.setRegion((start, stop))
            region.blockLineSignal = False
            caption.setColor(QColor(color))
            caption.setText(text)
            caption.setPos(start, top)
            region.show()
            caption.show()

    def _mouse_moved(self, position):
        inside = self.view_box.sceneBoundingRect().contains(position)
        x = self.view_box.mapSceneToView(position).x() if inside else None
        if self.on_hover is not None:
            self.on_hover(x)
        else:
            self.show_hover(x)

    def show_hover(self, x):
        if x is None or not len(self._x):
            self._crosshair.hide()
            for name, text in self._legend:
                set_text(text, name)
            return
        index = int(np.clip(np.searchsorted(self._x, x), 0, len(self._x) - 1))
        if index > 0 and abs(self._x[index - 1] - x) < abs(self._x[index] - x):
            index -= 1
        self._crosshair.setPos(self._x[index])
        self._crosshair.show()
        for column, (name, text) in enumerate(self._legend):
            set_text(text, f"{name} {self._format.format(self._y[index, column])}")


def _link_hover(panels):
    def hover(x):
        for panel in panels:
            panel.show_hover(x)
    for panel in panels:
        panel.on_hover = hover


class LiveImuChart(QWidget):
    """A view of LiveStreamModel; several views may share one model."""

    def __init__(self, model):
        super().__init__()
        self.model = model
        self._drawn = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.ELEMENT_GAP)
        self.accel = ChartPanel("加速度 · 原始 int16", AXES[:3], theme.AXIS_COLORS)
        self.gyro = ChartPanel("陀螺仪 · 原始 int16", AXES[3:], theme.AXIS_COLORS)
        layout.addWidget(self.accel, 1)
        layout.addWidget(self.gyro, 1)
        _link_hover((self.accel, self.gyro))

    def redraw(self):
        if self._drawn == (id(self.model), self.model.revision):
            return
        self._drawn = (id(self.model), self.model.revision)
        x, data, spans = self.model.snapshot()
        seconds = self.model.buffer.seconds
        for group, panel in enumerate((self.accel, self.gyro)):
            panel.set_data(x, data[:, group * 3:group * 3 + 3], x_range=(-seconds, 0.0),
                           empty_text="等待 IMU 样本")
            panel.set_regions([(start, stop, text, theme.REGION) for start, stop, text in spans])


class RecordingChart(QWidget):
    """Whole-take preview; drag proposes a region, the controller validates it."""

    def __init__(self, on_region):
        super().__init__()
        self._on_region = on_region
        self._view = None
        self._overlay = True
        self._selecting = False
        self._editable = False
        self._drag_anchor = None
        self._drag_region = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.ELEMENT_GAP)
        self.accel = ChartPanel("加速度 · 滤波后", AXES[:3], theme.AXIS_COLORS, unit_format="{:.1f}")
        self.gyro = ChartPanel("陀螺仪 · 滤波后", AXES[3:], theme.AXIS_COLORS, unit_format="{:.1f}")
        layout.addWidget(self.accel, 1)
        layout.addWidget(self.gyro, 1)
        _link_hover((self.accel, self.gyro))
        for panel in (self.accel, self.gyro):
            panel.view_box.drag_handler = self._drag
        self._draw()

    def set_view(self, view):
        self.cancel_drag()  # A switch/reload must never finish an earlier recording's drag.
        self._view = view
        self._draw()

    def set_overlay(self, enabled):
        self._overlay = bool(enabled)
        self._draw()

    def set_selection(self, *, selecting, editable):
        self._selecting, self._editable = bool(selecting), bool(editable)
        if not self._active:
            self.cancel_drag()
        cursor = Qt.CursorShape.CrossCursor if self._active else Qt.CursorShape.ArrowCursor
        for panel in (self.accel, self.gyro):
            panel.plot.setCursor(cursor)

    @property
    def _active(self):
        return self._selecting and self._editable and self._view is not None

    def cancel_drag(self):
        if self._drag_anchor is not None or self._drag_region is not None:
            self._drag_anchor = self._drag_region = None
            self._draw_regions()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cancel_drag()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.cancel_drag()  # Geometry changes invalidate the drag's coordinates.

    def hideEvent(self, event):
        super().hideEvent(event)
        self.cancel_drag()

    def _drag(self, view_box, event):
        if not self._active or event.button() != Qt.MouseButton.LeftButton:
            return False
        view = self._view
        x = view_box.mapSceneToView(event.scenePos()).x()
        if event.isStart():
            start = view_box.mapSceneToView(event.buttonDownScenePos()).x()
            self._drag_anchor = start
            self.setFocus()
        if self._drag_anchor is None:  # Cancelled by Escape, resize or a data change.
            return True
        rect = view_box.sceneBoundingRect()
        inside = rect.top() <= event.scenePos().y() <= rect.bottom()
        self._drag_region = drag_to_region(self._drag_anchor, x, len(view.raw), view.seconds) if inside else None
        if event.isFinish():
            region = self._drag_region
            self._drag_anchor = self._drag_region = None
            self._draw_regions()
            if region is not None:
                # Not optimistic: the controller may reject a short crop.
                self._on_region(region)
        else:
            self._draw_regions()
        return True

    def _draw(self):
        view = self._view
        for group, panel in enumerate((self.accel, self.gyro)):
            columns = slice(group * 3, group * 3 + 3)
            if view is None:
                panel.set_data(np.empty(0), np.empty((0, 3)), x_range=(0.0, 1.0), empty_text="暂无录制波形")
            else:
                x = np.arange(len(view.raw)) / view.rate
                panel.set_data(x, view.filtered[:, columns], raw=view.raw[:, columns] if self._overlay else None,
                               x_range=(0.0, view.seconds))
        self._draw_regions()

    def _draw_regions(self):
        view = self._view
        spans = []
        if view is not None:
            regions = view.regions
            if self._drag_region is not None:
                regions = (RecognitionRegion(*self._drag_region, "手动训练窗口（预览）"),)
            for region in regions:
                span = recording_region_span(region, view)
                if span is None:
                    continue
                frames = region.end_sample - region.start_sample
                short = ((view.training_region is not None or self._drag_region is not None)
                         and frames < view.min_samples)
                text = region.label
                if self._drag_region is not None:
                    text += f" · {frames} 帧"
                if short:
                    text += f"（无法训练，至少 {view.min_samples} 帧）"
                spans.append((*span, text, theme.CRITICAL if short else theme.REGION))
        for panel in (self.accel, self.gyro):
            panel.set_regions(spans)


class MagnitudeChart(ChartPanel):
    """One magnitude series with dashed, directly labelled reference bands."""

    def __init__(self, narrow, wide):
        super().__init__("合加速度 |a|", ["|a|"], [theme.AXIS_COLORS[0]], unit_format="{:.4f}")
        self._lines = []
        self._captions = []
        for band, color, text in ((narrow, theme.GOOD, "±3%"), (wide, theme.WARNING, "±10%")):
            for value in band:
                line = pg.InfiniteLine(value, angle=0, pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DashLine))
                self.plot.addItem(line, ignoreBounds=True)
                self._lines.append(line)
            # One label per band, on its upper line, so close bands do not collide.
            caption = pg.TextItem(f"{text} · {band[0]:g}–{band[1]:g} g", color=color, anchor=(0, 1))
            caption.setFont(_caption_font())
            self.plot.addItem(caption, ignoreBounds=True)
            self._captions.append((caption, band[1]))

    def show_bands(self, visible, x_left=-10.0):
        for line in self._lines:
            line.setVisible(visible)
        for index, (caption, value) in enumerate(self._captions):
            caption.setVisible(visible)
            # Staggered horizontally: at a wide y-range the bands nearly coincide.
            caption.setPos(x_left * (1 - 0.14 * index), value)


class DirectionDial(QWidget):
    """Latest unit direction projected onto one sensor plane."""

    def __init__(self, axis_x, axis_y):
        super().__init__()
        self.axes = axis_x, axis_y
        self.direction = None
        self.setMinimumSize(170, 170)

    def set_direction(self, direction):
        if direction != self.direction:
            self.direction = direction
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        names = "XYZ"
        width, height = self.width(), self.height()
        radius = min(width, height - 28) / 2 - 22
        cx, cy = width / 2, (height + 20) / 2
        painter.setPen(QColor(theme.INK))
        title = f"{names[self.axes[0]]}{names[self.axes[1]]} 平面"
        painter.drawText(QRectF(0, 0, width, 20), Qt.AlignmentFlag.AlignCenter, title)
        painter.setPen(QPen(QColor(theme.HAIRLINE), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
        painter.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))
        painter.drawLine(QPointF(cx, cy - radius), QPointF(cx, cy + radius))
        painter.setPen(QColor(theme.SLATE))
        font = painter.font()
        font.setPixelSize(11)
        painter.setFont(font)
        painter.drawText(QPointF(cx + radius + 4, cy + 4), f"+{names[self.axes[0]]}")
        painter.drawText(QPointF(cx + 5, cy - radius + 2), f"+{names[self.axes[1]]}")
        if self.direction is not None:
            tip = QPointF(cx + radius * self.direction[self.axes[0]], cy - radius * self.direction[self.axes[1]])
            painter.setPen(QPen(QColor(theme.INK), 2.5, c=Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(cx, cy), tip)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(theme.INK))
            painter.drawEllipse(tip, 4, 4)
        painter.end()
