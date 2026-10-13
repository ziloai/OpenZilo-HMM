# SPDX-License-Identifier: MPL-2.0
"""Live acceleration diagnostics; display-only, with no new BLE consumer.

Like IMUPlot, this view is owned by the Tk thread and redrawn by Studio.poll.
Every received sample is retained in a bounded window, not just batch tails.
"""
from __future__ import annotations

import math

from .acceleration import (
    NARROW_MAGNITUDE_BAND, WIDE_MAGNITUDE_BAND, acceleration_scale, acceleration_vector,
)
from .plotting import RollingIMUBuffer, curve_points


def reading_text(vector):
    """Keep diagnostic wording testable without Tk or hardware."""
    if vector is None:
        return ("合加速度 |a|：—；等待新的 IMU 样本", "方向：—", "幅值参考：—")
    magnitude = f"合加速度 |a|：{vector.magnitude_raw:.2f} raw"
    if vector.magnitude_g is None:
        magnitude += "；量程未知，无法换算 g / m/s²"
        gates = "幅值参考：量程未知，无法对照"
    else:
        magnitude += f" = {vector.magnitude_g:.4f} g = {vector.magnitude_m_s2:.4f} m/s²"
        xyz = ", ".join(f"{v:+.4f}" for v in vector.xyz_g)
        magnitude += f"\n分量 (X, Y, Z)：({xyz}) g；|a| − 1 g = {vector.magnitude_g - 1:+.4f} g（不是去重力加速度）"
        def inside(band):
            return "范围内" if band[0] <= vector.magnitude_g <= band[1] else "范围外"
        gates = (f"幅值参考：1g ±3% · 0.97–1.03 g [{inside(NARROW_MAGNITUDE_BAND)}，绿线]；"
                 f"1g ±10% · 0.90–1.10 g [{inside(WIDE_MAGNITUDE_BAND)}，橙线]（仅供观察）")
    if vector.unit_direction is None:
        direction = "方向：未定义（零向量）\n与 +X / +Y / +Z 的夹角：—"
    else:
        xyz = ", ".join(f"{v:+.4f}" for v in vector.unit_direction)
        angles = " / ".join(f"{v:.1f}°" for v in vector.axis_angles_deg)
        direction = f"单位方向 a/|a| (X, Y, Z)：({xyz})\n与 +X / +Y / +Z 的夹角：{angles}（不是欧拉角）"
    return magnitude, direction, gates


class AccelerationView:
    """Magnitude history and three projections of the latest unit direction."""

    def __init__(self, parent):
        import tkinter as tk
        from tkinter import ttk

        self.buffer = RollingIMUBuffer()
        self._active = False
        self._range_g = None
        self._frozen = None
        self._dirty = self._layout_dirty = True
        self.widget = ttk.Frame(parent)
        self.widget.columnconfigure(0, weight=1)
        self.widget.rowconfigure(4, weight=1)
        controls = ttk.Frame(self.widget)
        controls.grid(row=0, column=0, sticky="ew")
        self._pause_button = ttk.Button(controls, text="暂停显示", command=self._toggle_pause)
        self._pause_button.pack(side="left")
        ttk.Button(controls, text="清空", command=self.clear).pack(side="left", padx=6)
        self._view_status = tk.StringVar(value="实时显示（含重力，未滤波）")
        ttk.Label(controls, textvariable=self._view_status).pack(side="left")
        self._magnitude = tk.StringVar()
        self._direction = tk.StringVar()
        self._gates = tk.StringVar()
        for row, variable in enumerate((self._magnitude, self._direction, self._gates), start=1):
            ttk.Label(self.widget, textvariable=variable, wraplength=920).grid(row=row, column=0, sticky="w", pady=2)
        self.canvas = tk.Canvas(self.widget, background="#ffffff", highlightthickness=0,
                                width=720, height=330)
        self.canvas.grid(row=4, column=0, sticky="nsew")
        self._status = tk.StringVar(value="等待 IMU；量程未知")
        ttk.Label(self.widget, textvariable=self._status, wraplength=920).grid(row=5, column=0, sticky="w")
        ttk.Label(self.widget, text="方向基于设备上报的传感器坐标，不代表前后左右、世界航向或移动方向。\n"
                  "参考范围仅用于观察加速度幅值，不代表设备的静止、姿态融合或校准判定。",
                  wraplength=920).grid(row=6, column=0, sticky="w", pady=2)
        self.canvas.bind("<Configure>", self._invalidate_layout)
        self.canvas.bind("<Map>", self._invalidate_layout)
        self._set_reading(None)

    def _set_reading(self, vector):
        for variable, text in zip((self._magnitude, self._direction, self._gates), reading_text(vector)):
            variable.set(text)

    def _invalidate_layout(self, event=None):
        self._dirty = self._layout_dirty = True

    def set_stream_state(self, payload):
        active = payload["state"] == "active"
        if active:
            rate = float(payload["sample_rate_hz"])
            scale = acceleration_scale(payload.get("accel_range_g"))
            new_range = None if scale is None else scale * 32768.0
            if not self._active or self.buffer.sample_rate_hz != rate or self._range_g != new_range:
                self.buffer.set_sample_rate(rate)
                self.clear()
            self._range_g = new_range
            units = f"量程 ±{new_range:g} g" if new_range is not None else "量程未知，仅显示 raw"
            self._status.set(f"IMU 正常上报；{rate:g} Hz；{units}；时间轴按采样率估算，非设备时间戳")
        else:
            self._range_g = None
            self.clear()
            self._status.set(payload.get("message", "无新 IMU 数据；旧值已清除"))
        self._active = active
        self._invalidate_layout()

    def append_samples(self, samples):
        if not self._active:
            return
        self.buffer.append_samples(samples)
        if samples:
            self._dirty = True

    def clear(self):
        self.buffer.clear()
        if self._frozen is not None:
            self._frozen = ()
        self._set_reading(None)
        self._invalidate_layout()

    def _toggle_pause(self):
        if self._frozen is None:
            self._frozen = self.buffer.samples
            self._pause_button.configure(text="恢复显示")
            self._view_status.set("已冻结：数值、方向、曲线均为历史快照（仍收数）")
        else:
            self._frozen = None
            self._pause_button.configure(text="暂停显示")
            self._view_status.set("实时显示（含重力，未滤波）")
        self._invalidate_layout()

    def redraw(self):
        if not self._dirty or not self.canvas.winfo_ismapped():
            return
        if self._frozen is not None and not self._layout_dirty:
            return
        samples = self.buffer.samples if self._frozen is None else self._frozen
        vector = acceleration_vector(samples[-1][:3], accel_range_g=self._range_g) if samples else None
        self._set_reading(vector)
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        # Clear old arrows even if a resized view is too small to draw anew.
        self.canvas.delete("all")
        if width < 360 or height < 180:
            return
        projection_height = min(150, height * 0.45)
        self._draw_direction(vector, width, projection_height)
        self._draw_history(samples, width, height, projection_height)
        self._dirty = self._layout_dirty = False

    def _draw_direction(self, vector, width, height):
        c = self.canvas
        radius = min(48, (height - 42) / 2)
        cy = height / 2 + 7
        for panel, (axis_x, axis_y, label) in enumerate(((0, 1, "XY"), (0, 2, "XZ"), (1, 2, "YZ"))):
            cx = width * (panel + 0.5) / 3
            c.create_text(cx, 12, text=f"{label} 平面 · 单位方向投影", fill="#555555")
            c.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline="#d0d5da")
            c.create_line(cx - radius, cy, cx + radius, cy, fill="#aab0b6")
            c.create_line(cx, cy + radius, cx, cy - radius, fill="#aab0b6")
            c.create_text(cx + radius + 16, cy, text=f"+{'XYZ'[axis_x]}", fill="#555555")
            c.create_text(cx + 10, cy - radius, text=f"+{'XYZ'[axis_y]}", anchor="w", fill="#555555")
            if vector is not None and vector.unit_direction is not None:
                dx, dy = vector.unit_direction[axis_x], vector.unit_direction[axis_y]
                x, y = cx + radius * dx, cy - radius * dy
                c.create_line(cx, cy, x, y, arrow="last", width=2.5,
                              fill="#2463c5", tags="direction")
                c.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#2463c5", outline="")

    def _draw_history(self, samples, width, height, top):
        c = self.canvas
        scale = acceleration_scale(self._range_g)
        values = [(math.hypot(*row[:3]) * (scale if scale is not None else 1.0),) for row in samples]
        unit = "g" if scale is not None else "raw（量程未知）"
        text = f"合加速度 |a| / {unit}"
        if values:
            low, high = min(row[0] for row in values), max(row[0] for row in values)
            text += f"；窗口最小 / 最大：{low:.4f} / {high:.4f}"
        else:
            low = high = 0.0
        c.create_text(68, top + 12, text=text, anchor="w", fill="#333333")
        if scale is not None:
            low, high = min(low, 0.9), max(high, 1.1)
        padding = max(0.02 if scale is not None else 1.0, (high - low) * 0.12)
        limits = low - padding, high + padding
        left, right, bottom = 68, width - 20, height - 30
        top += 30
        bounds = left, top, right, bottom
        def y(value):
            return bottom - (value - limits[0]) / (limits[1] - limits[0]) * (bottom - top)
        for tick in range(5):
            value = limits[0] + tick * (limits[1] - limits[0]) / 4
            c.create_line(left, y(value), right, y(value), fill="#edf0f2")
            c.create_text(left - 7, y(value), text=f"{value:.4g}", anchor="e", fill="#555555")
            x = left + tick * (right - left) / 4
            c.create_text(x, bottom + 14, text=f"{self.buffer.seconds * (tick / 4 - 1):.3g}s", fill="#555555")
        if scale is not None:
            for band, color in ((NARROW_MAGNITUDE_BAND, "#187b3c"), (WIDE_MAGNITUDE_BAND, "#c47b17")):
                for value in band:
                    c.create_line(left, y(value), right, y(value), fill=color, dash=(4, 3), tags="threshold")
        points = curve_points(values, 0, sample_rate_hz=self.buffer.sample_rate_hz,
                              seconds=self.buffer.seconds, bounds=bounds, limits=limits)
        if len(points) >= 4:
            c.create_line(*points, fill="#2463c5", width=1.5, tags="magnitude")
        elif points:
            x, yy = points
            c.create_oval(x - 2, yy - 2, x + 2, yy + 2, fill="#2463c5", outline="")
