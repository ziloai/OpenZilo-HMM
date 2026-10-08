# SPDX-License-Identifier: MPL-2.0
"""Display-only IMU plotting. Importing this module does not import Tk.

All IMUPlot methods belong on the GUI thread. Call redraw() from the GUI poll;
no timers or worker threads are created here. Time is sample-relative, not a
measurement of device timestamps or host arrival times.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math
from numbers import Integral, Real

AXES = ("ax", "ay", "az", "gx", "gy", "gz")
COLORS = ("#c0392b", "#187b3c", "#2463c5")
MAX_SAMPLES = 5000
MAX_REGIONS = 128


@dataclass(frozen=True)
class RecognitionRegion:
    start_sample: int
    end_sample: int
    label: str


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


class RollingIMUBuffer:
    """Atomic validated batches; at most ceil(rate * seconds), capped at 5000.

    samples is an immutable oldest-to-newest snapshot. times() reports seconds
    relative to the latest sample (zero). The hard cap may shorten the window
    at high rates; no synthetic samples are inserted across pauses.
    """

    def __init__(self, *, sample_rate_hz=25.0, seconds=10.0):
        self.seconds = _positive(seconds, "seconds")
        self.set_sample_rate(sample_rate_hz)

    def set_sample_rate(self, rate):
        rate = _positive(rate, "sample_rate_hz")
        capacity = (MAX_SAMPLES if rate >= MAX_SAMPLES / self.seconds
                    else max(1, math.ceil(rate * self.seconds)))
        self.sample_rate_hz = rate
        self.capacity = capacity
        self._samples = deque(maxlen=capacity)
        self.next_sample = 0

    @property
    def samples(self):
        return tuple(self._samples)

    def __len__(self):
        return len(self._samples)

    def times(self):
        return tuple(-i / self.sample_rate_hz
                     for i in range(len(self) - 1, -1, -1))

    def clear(self):
        # Keep the stream clock: clearing a view must not shift recognition boxes.
        self._samples.clear()

    def append_samples(self, samples):
        if not isinstance(samples, (list, tuple)):
            raise ValueError("samples must be a sequence of six-axis rows")
        pending = deque(maxlen=self.capacity)
        for row in samples:
            if not isinstance(row, (list, tuple)) or len(row) != 6:
                raise ValueError("each sample must contain ax, ay, az, gx, gy, gz")
            if any(isinstance(v, bool) or not isinstance(v, Integral)
                   or not -32768 <= v <= 32767 for v in row):
                raise ValueError("sample values must be finite int16 integers")
            pending.append(tuple(int(v) for v in row))
        self._samples.extend(pending)
        self.next_sample += len(samples)


def axis_limits(samples, axes):
    """Shared raw-value scale for a group, including a nonzero zero-data span."""
    values = [row[axis] for row in samples for axis in axes]
    low, high = (min(values), max(values)) if values else (0, 0)
    padding = max(1.0, (high - low) * 0.1)
    return low - padding, high + padding


def curve_points(samples, axis, *, sample_rate_hz, seconds, bounds, limits):
    """Canvas coordinates, bounded to roughly one point per horizontal pixel.

    Pixel buckets retain alternating minima/maxima to keep narrow spikes visible;
    all axes use the original sample indices for horizontal positioning.
    """
    left, top, right, bottom = bounds
    low, high = limits
    n = len(samples)
    if not n:
        return ()
    budget = max(2, int(right - left))
    if n <= budget:
        indices = range(n)
    else:
        indices = {0, n - 1}
        buckets = max(1, (budget - 2) // 2)
        for bucket in range(buckets):
            start, stop = bucket * n // buckets, (bucket + 1) * n // buckets
            span = range(start, stop)
            indices.add(min(span, key=lambda i: samples[i][axis]))
            indices.add(max(span, key=lambda i: samples[i][axis]))
        indices = sorted(indices)
    points = []
    for i in indices:
        age = (n - 1 - i) / sample_rate_hz
        x = right - (age / seconds) * (right - left)
        y = bottom - ((samples[i][axis] - low) / (high - low)) * (bottom - top)
        points.extend((x, y))
    return tuple(points)


def region_bounds(region, *, next_sample, sample_count, sample_rate_hz, seconds, bounds):
    """Clip a half-open stream interval to the visible samples and chart edges."""
    first = next_sample - sample_count
    if not sample_count or region.end_sample <= first or region.start_sample >= next_sample:
        return None
    left, top, right, bottom = bounds
    def x(index):
        return right - (next_sample - 1 - index) / sample_rate_hz / seconds * (right - left)
    start = max(left, x(max(first, region.start_sample) - 0.5))
    stop = min(right, x(min(next_sample, region.end_sample) - 0.5))
    if stop <= start:
        return None
    return start, top, stop, bottom


class IMUPlot:
    """Tk wrapper exposing .widget; freezing affects display only, never capture."""

    def __init__(self, parent, *, sample_rate_hz=25.0, seconds=10.0):
        import tkinter as tk
        from tkinter import ttk

        self.buffer = RollingIMUBuffer(sample_rate_hz=sample_rate_hz, seconds=seconds)
        self._dirty = self._layout_dirty = True
        self._frozen = None
        self._regions = deque(maxlen=MAX_REGIONS)
        self._frozen_regions = ()
        self._frozen_end = 0
        self.widget = ttk.Frame(parent)
        controls = ttk.Frame(self.widget)
        controls.pack(fill="x")
        self._pause_button = ttk.Button(controls, text="暂停显示", command=self._toggle_pause)
        self._pause_button.pack(side="left")
        ttk.Button(controls, text="清空", command=self.clear).pack(side="left", padx=6)
        self._view_status = tk.StringVar(value="实时显示")
        ttk.Label(controls, textvariable=self._view_status).pack(side="left")
        self.canvas = tk.Canvas(self.widget, background="#ffffff", highlightthickness=0,
                                width=720, height=400)
        self.canvas.pack(fill="both", expand=True)
        self._status = tk.StringVar(value="等待 IMU 样本")
        ttk.Label(self.widget, textvariable=self._status, wraplength=700).pack(anchor="w")
        self.canvas.bind("<Configure>", self._invalidate_layout)
        self.canvas.bind("<Map>", self._invalidate_layout)

    def _invalidate_layout(self, event=None):
        self._dirty = self._layout_dirty = True

    @property
    def next_sample(self):
        return self.buffer.next_sample

    def append_samples(self, samples: list[list[int]]):
        self.buffer.append_samples(samples)
        first = self.next_sample - len(self.buffer)
        self._regions = deque((r for r in self._regions if r.end_sample > first), maxlen=MAX_REGIONS)
        if samples:
            self._dirty = True

    def add_recognition(self, start_sample, end_sample, label):
        if (type(start_sample) is not int or type(end_sample) is not int
                or not 0 <= start_sample < end_sample <= self.next_sample):
            raise ValueError("recognition interval must be within the received stream")
        if end_sample > self.next_sample - len(self.buffer):
            self._regions.append(RecognitionRegion(start_sample, end_sample, str(label)))
            self._dirty = True

    def clear(self):
        self.buffer.clear()
        self._regions.clear()
        self._frozen_regions = ()
        if self._frozen is not None:
            self._frozen = ()
            self._frozen_end = self.next_sample
        self._invalidate_layout()

    def set_sample_rate(self, rate):
        self.buffer.set_sample_rate(rate)
        self.clear()

    def set_status(self, text):
        self._status.set(text)

    def _toggle_pause(self):
        if self._frozen is None:
            self._frozen = self.buffer.samples
            self._frozen_end = self.next_sample
            self._frozen_regions = tuple(self._regions)
            self._pause_button.configure(text="恢复显示")
            self._view_status.set("显示已冻结（仍接收数据）")
        else:
            self._frozen = None
            self._pause_button.configure(text="暂停显示")
            self._view_status.set("实时显示")
        self._invalidate_layout()

    def redraw(self):
        if not self._dirty or not self.canvas.winfo_ismapped():
            return
        if self._frozen is not None and not self._layout_dirty:
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 180 or height < 160:
            return
        samples = self.buffer.samples if self._frozen is None else self._frozen
        end = self.next_sample if self._frozen is None else self._frozen_end
        regions = self._regions if self._frozen is None else self._frozen_regions
        c = self.canvas
        c.delete("all")
        for group, title in enumerate(("加速度 · 原始 int16", "陀螺仪 · 原始 int16")):
            base = group * (height - 30) / 2
            left, right = 72, width - 18
            top, bottom = base + 32, base + (height - 30) / 2 - 25
            bounds = (left, top, right, bottom)
            axes = range(group * 3, group * 3 + 3)
            limits = axis_limits(samples, axes)
            c.create_text(left, base + 12, text=title, anchor="w", fill="#333333")
            for slot, axis in enumerate(axes):
                c.create_text(right - (2 - slot) * 45, base + 12, text=AXES[axis],
                              anchor="e", fill=COLORS[slot])
            for tick in range(5):
                y = top + tick * (bottom - top) / 4
                value = limits[1] - tick * (limits[1] - limits[0]) / 4
                c.create_line(left, y, right, y, fill="#e1e5e9")
                c.create_text(left - 7, y, text=f"{value:.6g}", anchor="e", fill="#555555")
                x = left + tick * (right - left) / 4
                c.create_text(x, bottom + 13, text=f"{self.buffer.seconds * (tick / 4 - 1):.3g}",
                              fill="#555555")
            for slot, axis in enumerate(axes):
                points = curve_points(samples, axis, sample_rate_hz=self.buffer.sample_rate_hz,
                                      seconds=self.buffer.seconds, bounds=bounds, limits=limits)
                if len(points) >= 4:
                    c.create_line(*points, fill=COLORS[slot], width=1.5)
                elif points:
                    x, y = points
                    c.create_oval(x - 2, y - 2, x + 2, y + 2,
                                  fill=COLORS[slot], outline=COLORS[slot])
            for region in regions:
                box = region_bounds(region, next_sample=end, sample_count=len(samples),
                                    sample_rate_hz=self.buffer.sample_rate_hz,
                                    seconds=self.buffer.seconds, bounds=bounds)
                if box is not None:
                    c.create_rectangle(*box, outline="#8e44ad", width=2, dash=(5, 3))
                    c.create_text(box[0] + 3, top + 3, text=region.label,
                                  anchor="nw", fill="#7d3c98")
        c.create_text(width / 2, height - 12,
                      text="相对最新样本 / 秒（按采样率估算，非设备时间戳）；紫框 = 已识别片段", fill="#555555")
        self._dirty = self._layout_dirty = False


class RecordingPlot:
    """Static whole-take preview using exactly the training/inference filter.

    Filtered values stay floating point (a low-pass can overshoot int16).
    Raw recording data is never modified. Canvas geometry is pixel-bounded.
    """

    def __init__(self, parent):
        import tkinter as tk
        from tkinter import ttk

        self.widget = ttk.Frame(parent)
        self._overlay = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.widget, text="叠加原始波形（浅色虚线）；滤波后为实线",
                        variable=self._overlay, command=self._invalidate).pack(anchor="w")
        self.canvas = tk.Canvas(self.widget, background="#ffffff", highlightthickness=0,
                                width=720, height=400)
        self.canvas.pack(fill="both", expand=True)
        self._status = tk.StringVar(value="停止一次录制后显示滤波波形；也可在数据集中选中已保存手势。")
        ttk.Label(self.widget, textvariable=self._status, wraplength=900).pack(anchor="w")
        self._raw = self._filtered = ()
        self._regions = ()
        self._rate = 25.0
        self._dirty = True
        self.canvas.bind("<Configure>", self._invalidate)
        self.canvas.bind("<Map>", self._invalidate)

    def _invalidate(self, event=None):
        self._dirty = True

    def clear(self, message="暂无录制波形"):
        self._raw = self._filtered = ()
        self._regions = ()
        self._status.set(message)
        self._dirty = True

    def show_recording(self, samples, pipeline, *, segmentation=None):
        import numpy as np
        from hmm_gesture.preprocessing import make_pipeline, validate_samples
        from hmm_gesture.segmentation import MotionSegmenter, impulse_peak, prepare_recording

        data = validate_samples(samples)
        signal_filter, _ = make_pipeline(pipeline)
        filtered = signal_filter.apply(data)
        self._regions = ()
        detail = "整段参与训练"
        if segmentation is not None and segmentation.mode == "impulse":
            segments = MotionSegmenter(**asdict(segmentation)).feed_segments(data)
            self._regions = tuple(RecognitionRegion(s.start_sample, s.end_sample, "训练窗口")
                                  for s in segments[:MAX_REGIONS])
            # Inside each box show EXACTLY what the model sees after segment-local
            # filtering; outside is only a whole-take context preview.
            for segment in segments:
                filtered[segment.start_sample:segment.end_sample] = signal_filter.apply(segment.samples)
            if prepare_recording(data, segmentation) is not None:
                detail = f"只用紫框内 {len(segments[0].samples)} 帧训练，框外等待不参与"
            else:
                detail = f"无法训练：需恰好一个完整冲击及前后文，当前 {len(segments)} 个完整窗口"
            if len(segments) > MAX_REGIONS:
                detail += f"（仅显示前 {MAX_REGIONS} 框）"
        self._raw = tuple(map(tuple, data))
        self._filtered = tuple(map(tuple, filtered))
        self._rate = pipeline.sample_rate_hz
        peak = impulse_peak(data)
        attenuation = ""
        if peak > 1000:
            ratio = float(np.linalg.norm(np.diff(filtered[:, :3], axis=0), axis=1).max()) / peak
            attenuation = f"；相邻加速度变化峰值保留 {ratio:.0%}"
            if ratio < 0.3:
                attenuation += "（短促动作可能被过度平滑）"
        self._status.set(f"{len(data)} 样本 / {len(data) / self._rate:.2f} 秒；{self._rate:g} Hz；"
                         f"中值 {'关闭' if pipeline.median_kernel == 1 else pipeline.median_kernel} → "
                         f"{pipeline.filter_order} 阶低通 {pipeline.cutoff_hz:g} Hz（当前训练参数，原始数据不变）"
                         f"{attenuation}；{detail}")
        self._dirty = True

    def redraw(self):
        if not self._dirty or not self.canvas.winfo_ismapped():
            return
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if width < 180 or height < 160:
            return
        c = self.canvas
        c.delete("all")
        seconds = max(1, len(self._raw) - 1) / self._rate
        overlay = self._overlay.get()
        for group, title in enumerate(("加速度 · 滤波后", "陀螺仪 · 滤波后")):
            base = group * (height - 30) / 2
            left, right = 72, width - 18
            top, bottom = base + 32, base + (height - 30) / 2 - 25
            bounds = left, top, right, bottom
            axes = range(group * 3, group * 3 + 3)
            limits = axis_limits(self._filtered + (self._raw if overlay else ()), axes)
            c.create_text(left, base + 12, text=title, anchor="w", fill="#333333")
            for tick in range(5):
                y = top + tick * (bottom - top) / 4
                value = limits[1] - tick * (limits[1] - limits[0]) / 4
                c.create_line(left, y, right, y, fill="#e1e5e9")
                c.create_text(left - 7, y, text=f"{value:.6g}", anchor="e", fill="#555555")
                c.create_text(left + tick * (right - left) / 4, bottom + 13,
                              text=f"{seconds * tick / 4:.3g}", fill="#555555")
            for slot, axis in enumerate(axes):
                c.create_text(right - (2 - slot) * 45, base + 12, text=AXES[axis],
                              anchor="e", fill=COLORS[slot])
                series = [(self._filtered, COLORS[slot], ())]
                if overlay:
                    series.insert(0, (self._raw, ("#e5aaa4", "#9dc7ac", "#a7bfe5")[slot], (3, 3)))
                for data, color, dash in series:
                    points = curve_points(data, axis, sample_rate_hz=self._rate,
                                          seconds=seconds, bounds=bounds, limits=limits)
                    if len(points) >= 4:
                        c.create_line(*points, fill=color, width=1.5, dash=dash)
                    elif points:
                        x, y = points
                        c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color)
            for region in self._regions:
                box = region_bounds(region, next_sample=len(self._raw), sample_count=len(self._raw),
                                    sample_rate_hz=self._rate, seconds=seconds, bounds=bounds)
                if box is not None:
                    c.create_rectangle(*box, outline="#8e44ad", width=2, dash=(5, 3))
                    c.create_text(box[0] + 3, top + 3, text=region.label, anchor="nw", fill="#7d3c98")
        c.create_text(width / 2, height - 12, text="录制开始后 / 秒（完整录制；短促模式的训练窗口用紫框标出）", fill="#555555")
        self._dirty = False
