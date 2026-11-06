# SPDX-License-Identifier: MPL-2.0
"""Live recognition; the latest result is the page's visual event."""
from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation
from PySide6.QtWidgets import QGraphicsOpacityEffect

from .. import theme
from ..charts import LiveImuChart
from ..i18n import tr
from ..widgets import DeviceNotice, Page, Switch, button, label, row, set_text


class LivePage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c = controller
        top = self.add_section()
        self.header(top, tr("实时试识别"), tr("先静止，每次手势后恢复静止。拒识检查不是概率保证，请用普通运动、抖动等负样本验证误触。"))
        self.notice = DeviceNotice(self, tr("尚未连接戒指。"))
        top.add(self.notice)
        self.switch = Switch(tr("实时试识别"))
        self.switch.toggled.connect(c.set_live)
        self.pause = button(tr("暂停显示"), self._toggle_pause)
        self.clear = button(tr("清空"), c.stream.clear)
        self.pause_state = label(tone="muted")
        top.add(row(self.switch, self.pause, self.clear, self.pause_state))
        self.result = label("—", "result")
        self._result_fade = QGraphicsOpacityEffect(self.result)
        self._result_fade.setOpacity(1.0)
        self.result.setGraphicsEffect(self._result_fade)
        self._result_motion = QPropertyAnimation(self._result_fade, b"opacity", self)
        self._result_motion.setDuration(theme.MOTION)
        self._result_motion.setStartValue(0.1)
        self._result_motion.setEndValue(1.0)
        self._result_motion.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._shown_serial = controller.prediction_serial
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
        set_text(self.pause, tr("恢复显示") if paused else tr("暂停显示"))
        set_text(self.pause_state, tr("显示已冻结（仍接收数据）") if paused else tr("实时显示"))
        if c.prediction_serial != self._shown_serial:
            # Replays for a repeated gesture too, whose text alone would not change.
            self._shown_serial = c.prediction_serial
            self._result_motion.start()
        if c.prediction is not None:
            set_text(self.result, c.prediction[0])
            set_text(self.detail, c.prediction[1])
        elif c.live and c.recognizer is None:
            set_text(self.result, "—")
            set_text(self.detail, c.result_text)
        else:
            set_text(self.result, "—")
            set_text(self.detail, tr("等待手势") if c.live else tr("开启后，仅通过拒识检查的片段会在曲线上画紫框。"))
        set_text(self.caption, tr("{stream}。横轴为相对最新样本的秒数（按采样率估算，非设备时间戳）；紫框 = 已识别片段。",
                                  stream=c.stream_text))

    def on_tick(self):
        self.chart.redraw()
