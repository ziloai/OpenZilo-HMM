# SPDX-License-Identifier: MPL-2.0
"""Acceleration magnitude and direction. Display only; no effect on training."""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout

from ...acceleration import NARROW_MAGNITUDE_BAND, WIDE_MAGNITUDE_BAND
from ...acceleration_view import reading_text
from ..charts import DirectionDial, MagnitudeChart
from ..widgets import Card, DeviceNotice, Page, StatTile, button, label, row, set_text


class DiagnosticsPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c = controller
        self._drawn = None
        top = self.add_section()
        self.header(top, "加速度诊断", "含重力的加速度计读数（比力），不是去重力后的线加速度，也不是移动方向或世界航向。")
        self.notice = DeviceNotice(self, "尚未连接戒指。")
        top.add(self.notice)
        self.pause = button("暂停显示", self._toggle_pause)
        self.clear = button("清空", c.acceleration.clear)
        self.pause_state = label(tone="muted")
        top.add(row(self.pause, self.clear, self.pause_state))
        self.magnitude = StatTile("合加速度 |a|")
        self.si = StatTile("m/s²")
        self.raw = StatTile("raw")
        self.deviation = StatTile("|a| − 1 g（不是去重力加速度）")
        top.add(row(self.magnitude, self.si, self.raw, self.deviation, spacing=64))
        self.chart = MagnitudeChart(NARROW_MAGNITUDE_BAND, WIDE_MAGNITUDE_BAND)
        self.chart.setFixedHeight(300)
        top.add(self.chart)
        self.status = label(role="caption", wrap=True)
        top.add(self.status)

        more = self.add_section()
        more.add(label("方向", "section"))
        card = Card()
        more.add(card)
        dials = QHBoxLayout()
        self.dials = [DirectionDial(0, 1), DirectionDial(0, 2), DirectionDial(1, 2)]
        for dial in self.dials:
            dials.addWidget(dial)
        card.add(dials)
        self.direction = label(wrap=True, selectable=True)
        card.add(self.direction)
        self.gates = label(tone="muted", wrap=True)
        card.add(self.gates)
        card.add(label("方向基于设备上报的传感器坐标，不代表前后左右、世界航向或移动方向。夹角不是姿态欧拉角。\n"
                       "参考范围仅用于观察加速度幅值，不代表设备的静止、姿态融合或校准判定。", "caption", wrap=True))
        self.finish()

    def _toggle_pause(self):
        self.controller.acceleration.toggle_pause()
        self.render_state()

    def render_state(self):
        c = self.controller
        self.notice.render_state()
        paused = c.acceleration.paused
        set_text(self.pause, "恢复显示" if paused else "暂停显示")
        set_text(self.pause_state, "已冻结：数值、方向、曲线均为历史快照（仍收数）" if paused else "实时显示（含重力，未滤波）")
        set_text(self.status, c.acceleration.status)
        self._drawn = None
        self.on_tick()

    def on_tick(self):
        model = self.controller.acceleration
        if self._drawn == model.revision:
            return
        self._drawn = model.revision
        x, magnitudes, vector, scale = model.snapshot()
        unit = "g" if scale is not None else "raw（量程未知）"
        title = f"合加速度 |a| / {unit}"
        if len(magnitudes):
            title += f"；窗口最小 / 最大：{magnitudes.min():.4f} / {magnitudes.max():.4f}"
        set_text(self.chart.title, title)
        limits = None
        if scale is not None:
            low = min(float(magnitudes.min()) if len(magnitudes) else 0.9, 0.9)
            high = max(float(magnitudes.max()) if len(magnitudes) else 1.1, 1.1)
            padding = max(0.02, (high - low) * 0.12)
            limits = (low - padding, high + padding)
        seconds = model.buffer.seconds
        self.chart.set_data(x, magnitudes.reshape(-1, 1), x_range=(-seconds, 0.0), y_limits=limits,
                            empty_text="等待新的 IMU 样本")
        self.chart.show_bands(scale is not None, -seconds)
        self.magnitude.set_value(f"{vector.magnitude_g:.4f} g" if vector and vector.magnitude_g is not None else "—")
        self.si.set_value(f"{vector.magnitude_m_s2:.4f}" if vector and vector.magnitude_g is not None else "—")
        self.raw.set_value(f"{vector.magnitude_raw:.2f}" if vector else "—")
        self.deviation.set_value(f"{vector.magnitude_g - 1:+.4f} g" if vector and vector.magnitude_g is not None else "—")
        _, direction, gates = reading_text(vector)
        if vector is not None and vector.xyz_g is not None:
            components = ", ".join(f"{v:+.4f}" for v in vector.xyz_g)
            direction = f"分量 (X, Y, Z)：({components}) g\n{direction}"
        set_text(self.direction, direction)
        set_text(self.gates, gates)
        for dial in self.dials:
            dial.set_direction(vector.unit_direction if vector else None)
