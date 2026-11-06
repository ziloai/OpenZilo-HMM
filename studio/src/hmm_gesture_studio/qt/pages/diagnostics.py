# SPDX-License-Identifier: MPL-2.0
"""Acceleration magnitude and direction. Display only; no effect on training."""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout

from ...acceleration import NARROW_MAGNITUDE_BAND, WIDE_MAGNITUDE_BAND
from ..charts import DirectionDial, MagnitudeChart
from ..i18n import tr
from ..widgets import Card, DeviceNotice, Page, StatTile, button, label, row, set_text


def _direction_text(vector):
    """Localized presentation of the diagnostic vector, without changing its data."""
    if vector is None:
        return tr("方向：—"), tr("幅值参考：—")
    if vector.magnitude_g is None:
        gates = tr("幅值参考：量程未知，无法对照")
    else:
        def inside(band):
            return tr("范围内") if band[0] <= vector.magnitude_g <= band[1] else tr("范围外")
        gates = tr("幅值参考：1g ±3% · 0.97–1.03 g [{narrow}，绿线]；"
                   "1g ±10% · 0.90–1.10 g [{wide}，橙线]（仅供观察）",
                   narrow=inside(NARROW_MAGNITUDE_BAND), wide=inside(WIDE_MAGNITUDE_BAND))
    if vector.unit_direction is None:
        direction = tr("方向：未定义（零向量）\n与 +X / +Y / +Z 的夹角：—")
    else:
        xyz = ", ".join(f"{v:+.4f}" for v in vector.unit_direction)
        angles = " / ".join(f"{v:.1f}°" for v in vector.axis_angles_deg)
        direction = tr("单位方向 a/|a| (X, Y, Z)：({xyz})\n与 +X / +Y / +Z 的夹角：{angles}（不是欧拉角）",
                       xyz=xyz, angles=angles)
    return direction, gates


class DiagnosticsPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c = controller
        self._drawn = None
        top = self.add_section()
        self.header(top, tr("加速度诊断"), tr("含重力的加速度计读数（比力），不是去重力后的线加速度，也不是移动方向或世界航向。"))
        self.notice = DeviceNotice(self, tr("尚未连接戒指。"))
        top.add(self.notice)
        self.pause = button(tr("暂停显示"), self._toggle_pause)
        self.clear = button(tr("清空"), c.acceleration.clear)
        self.pause_state = label(tone="muted")
        top.add(row(self.pause, self.clear, self.pause_state))
        self.magnitude = StatTile(tr("合加速度 |a|"))
        self.si = StatTile("m/s²")
        self.raw = StatTile("raw")
        self.deviation = StatTile(tr("|a| − 1 g（不是去重力加速度）"))
        top.add(row(self.magnitude, self.si, self.raw, self.deviation, spacing=64))
        self.chart = MagnitudeChart(NARROW_MAGNITUDE_BAND, WIDE_MAGNITUDE_BAND)
        self.chart.setFixedHeight(300)
        top.add(self.chart)
        self.status = label(role="caption", wrap=True)
        top.add(self.status)

        more = self.add_section()
        more.add(label(tr("方向"), "section"))
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
        card.add(label(tr("方向基于设备上报的传感器坐标，不代表前后左右、世界航向或移动方向。夹角不是姿态欧拉角。\n"
                          "参考范围仅用于观察加速度幅值，不代表设备的静止、姿态融合或校准判定。"), "caption", wrap=True))
        self.finish()

    def _toggle_pause(self):
        self.controller.acceleration.toggle_pause()
        self.render_state()

    def render_state(self):
        c = self.controller
        self.notice.render_state()
        paused = c.acceleration.paused
        set_text(self.pause, tr("恢复显示") if paused else tr("暂停显示"))
        set_text(self.pause_state, tr("已冻结：数值、方向、曲线均为历史快照（仍收数）") if paused else tr("实时显示（含重力，未滤波）"))
        set_text(self.status, c.acceleration.status)
        self._drawn = None
        self.on_tick()

    def on_tick(self):
        model = self.controller.acceleration
        if self._drawn == model.revision:
            return
        self._drawn = model.revision
        x, magnitudes, vector, scale = model.snapshot()
        unit = "g" if scale is not None else tr("raw（量程未知）")
        title = tr("合加速度 |a| / {unit}", unit=unit)
        if len(magnitudes):
            title = tr("合加速度 |a| / {unit}；窗口最小 / 最大：{minimum:.4f} / {maximum:.4f}",
                       unit=unit, minimum=magnitudes.min(), maximum=magnitudes.max())
        set_text(self.chart.title, title)
        limits = None
        if scale is not None:
            low = min(float(magnitudes.min()) if len(magnitudes) else 0.9, 0.9)
            high = max(float(magnitudes.max()) if len(magnitudes) else 1.1, 1.1)
            padding = max(0.02, (high - low) * 0.12)
            limits = (low - padding, high + padding)
        seconds = model.buffer.seconds
        self.chart.set_data(x, magnitudes.reshape(-1, 1), x_range=(-seconds, 0.0), y_limits=limits,
                            empty_text=tr("等待新的 IMU 样本"))
        self.chart.show_bands(scale is not None, -seconds)
        self.magnitude.set_value(f"{vector.magnitude_g:.4f} g" if vector and vector.magnitude_g is not None else "—")
        self.si.set_value(f"{vector.magnitude_m_s2:.4f}" if vector and vector.magnitude_g is not None else "—")
        self.raw.set_value(f"{vector.magnitude_raw:.2f}" if vector else "—")
        self.deviation.set_value(f"{vector.magnitude_g - 1:+.4f} g" if vector and vector.magnitude_g is not None else "—")
        direction, gates = _direction_text(vector)
        if vector is not None and vector.xyz_g is not None:
            components = ", ".join(f"{v:+.4f}" for v in vector.xyz_g)
            direction = tr("分量 (X, Y, Z)：({components}) g\n{direction}", components=components, direction=direction)
        set_text(self.direction, direction)
        set_text(self.gates, gates)
        for dial in self.dials:
            dial.set_direction(vector.unit_direction if vector else None)
