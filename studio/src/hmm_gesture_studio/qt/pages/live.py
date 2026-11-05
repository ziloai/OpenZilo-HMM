# SPDX-License-Identifier: MPL-2.0
"""Live recognition; the latest result is the page's visual event."""
from __future__ import annotations

from .. import theme
from ..charts import LiveImuChart
from ..widgets import DeviceNotice, Page, Switch, button, label, row, set_text


class LivePage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c = controller
        top = self.add_section()
        self.header(top, "实时试识别", "先静止，每次手势后恢复静止。拒识检查不是概率保证，请用普通运动、抖动等负样本验证误触。")
        self.notice = DeviceNotice(self, "尚未连接戒指。")
        top.add(self.notice)
        self.switch = Switch("实时试识别")
        self.switch.toggled.connect(c.set_live)
        self.pause = button("暂停显示", self._toggle_pause)
        self.clear = button("清空", c.stream.clear)
        self.pause_state = label(tone="muted")
        top.add(row(self.switch, self.pause, self.clear, self.pause_state))
        self.result = label("—", "result")
        top.add(self.result)
        self.detail = label(tone="muted", wrap=True)
        top.add(self.detail)
        self.chart = LiveImuChart(c.stream)
        self.chart.setFixedHeight(theme.CHART_HEIGHT)
        top.add(self.chart)
        self.caption = label(role="caption", wrap=True)
        top.add(self.caption)
        self.finish()

    def _toggle_pause(self):
        self.controller.stream.toggle_pause()
        self.render_state()

    def render_state(self):
        c = self.controller
        self.notice.render_state()
        self.switch.setChecked(c.live)
        self.switch.setEnabled(c.can_toggle_live or c.live)
        paused = c.stream.paused
        set_text(self.pause, "恢复显示" if paused else "暂停显示")
        set_text(self.pause_state, "显示已冻结（仍接收数据）" if paused else "实时显示")
        if c.prediction is not None:
            set_text(self.result, c.prediction[0])
            set_text(self.detail, c.prediction[1])
        elif c.live and c.recognizer is None:
            set_text(self.result, "—")
            set_text(self.detail, c.result_text)
        else:
            set_text(self.result, "—")
            set_text(self.detail, "等待手势" if c.live else "开启后，仅通过拒识检查的片段会在曲线上画紫框。")
        set_text(self.caption, f"{c.stream_text}。横轴为相对最新样本的秒数（按采样率估算，非设备时间戳）；紫框 = 已识别片段。")

    def on_tick(self):
        self.chart.redraw()
