# SPDX-License-Identifier: MPL-2.0
"""Training parameters, presets, evaluation, and model import/export."""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout

from ..widgets import Card, Page, StatTile, button, field, label, line_edit, row, set_field_text, set_text

_FIELDS = (("n_states", "状态数"), ("cutoff_hz", "低通 Hz"), ("window_size", "窗口"), ("window_overlap", "重叠"),
           ("sample_rate_hz", "采样率 Hz"), ("energy_threshold", "触发阈值"), ("median_kernel", "中值点数（1 关）"))


class TrainPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c = controller
        top = self.add_section()
        self.header(top, "训练模型", "使用当前数据目录中全部有效 JSON 训练。训练只使用已保存数据，不向戒指上传模型。")
        self.gestures = StatTile("手势")
        self.repetitions = StatTile("录制次数")
        self.rates = StatTile("采样率 Hz")
        self.model_state = StatTile("模型状态")
        top.add(row(self.gestures, self.repetitions, self.rates, self.model_state, spacing=64))
        self.train = button("训练全部数据", c.train, kind="primary")
        self.evaluate = button("留出评估", c.evaluate)
        self.load = button("加载模型包", c.load_bundle)
        self.export = button("导出模型包", c.export_bundle)
        top.add(row(self.train, self.evaluate, self.load, self.export))
        self.model_text = label(wrap=True, selectable=True)
        top.add(self.model_text)
        self.evaluation_text = label(tone="muted", wrap=True, selectable=True)
        top.add(self.evaluation_text)

        band = self.add_section(band=True)
        band.add(label("训练参数", "section"))
        card = Card("分段预设", tone="white")
        band.add(card)
        self.impulse = button("短促动作 / 响指", lambda: c.apply_preset("impulse"))
        self.motion = button("连续动作", lambda: c.apply_preset("motion"))
        card.add(row(self.impulse, self.motion))
        self.mode_text = label(tone="muted", wrap=True)
        card.add(self.mode_text)
        card.add(label("响指、敲击等短促动作请先应用短促预设；挥手等选择连续动作。不同类型动作不要未经评估就混用同一预设。",
                       "caption", wrap=True))
        fields = Card("模型与滤波", tone="white")
        band.add(fields)
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(12)
        self.inputs = {}
        for index, (key, caption) in enumerate(_FIELDS):
            entry = line_edit(c.params[key], on_edit=lambda value, key=key: c.set_param(key, value))
            self.inputs[key] = entry
            grid.addLayout(field(caption, entry), index // 4, index % 4)
        fields.add(grid)
        fields.add(label("数据或参数修改后，已训练模型失效，需要重新训练才能导出或试识别。低通截止频率必须小于采样率一半。",
                         "caption", wrap=True))
        self.finish()
        c.paramsReplaced.connect(self._params_replaced)

    def _params_replaced(self):
        for key, entry in self.inputs.items():
            set_field_text(entry, self.controller.params[key])

    def render_state(self):
        c = self.controller
        datasets = [dataset for _, dataset in c.records.values()]
        self.gestures.set_value(str(len(datasets)))
        self.repetitions.set_value(str(sum(len(d.repetitions) for d in datasets)))
        rates = sorted({d.sample_rate_hz for d in datasets})
        self.rates.set_value(" / ".join(f"{rate:g}" for rate in rates) or "—")
        if c.training:
            state = "进行中"
        elif c.bundle is None:
            state = "无"
        else:
            state = "已失效" if c.stale else "可用"
        self.model_state.set_value(state)
        self.train.setEnabled(not c.locked)
        self.evaluate.setEnabled(not c.locked)
        self.load.setEnabled(not c.locked)
        self.export.setEnabled(c.can_export)
        self.impulse.setEnabled(not c.locked)
        self.motion.setEnabled(not c.locked)
        for entry in self.inputs.values():
            entry.setEnabled(not c.locked)
        set_text(self.model_text, c.model_text)
        set_text(self.evaluation_text, c.evaluation_text)
        set_text(self.mode_text, c.mode_text)
