# SPDX-License-Identifier: MPL-2.0
"""Record repetitions, inspect waveforms and crops, and manage saved datasets."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView, QTableWidgetItem

from .. import theme
from ..charts import RecordingChart
from ..i18n import resolve, tr
from ..motion import PulseDot
from ..widgets import (
    Card, Combo, DeviceNotice, Page, Switch, button, label, line_edit, row, set_field_text, set_kind, set_text, table,
)


class CapturePage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c = controller
        self._shown = (None, False)
        top = self.add_section()
        self.header(top, tr("采集手势"), tr("每次录制一个完整动作，前后留少量静止。每次至少 12 个样本；拒识标定每类至少 3 次，建议 5–10 次以上。"))
        self.notice = DeviceNotice(self, tr("尚未连接戒指。"))
        top.add(self.notice)

        self.name = line_edit(on_edit=c.set_name, width=220, placeholder=tr("手势名称，如 响指"))
        self.record = button(tr("开始一次录制"), c.toggle_record, kind="primary")
        self.undo = button(tr("撤销最近一次"), c.undo)
        self.save = button(tr("保存 / 追加"), c.save_takes)
        self.discard = button(tr("清空未保存录制"), c.discard_takes)
        top.add(row(self.name, self.record, self.undo, self.save, self.discard))
        self.pulse = PulseDot()
        self.pulse.hide()
        self.takes = label(tone="muted")
        self.stream = label(role="caption")
        status = row(self.pulse, self.takes, spacing=8)
        status.addWidget(self.stream)
        top.add(status)

        self.title = label(role="cardTitle")
        self.choice = Combo()
        self.choice.setMinimumWidth(180)
        self.choice.activated.connect(c.set_preview_index)
        self.overlay = Switch(tr("叠加原始波形（虚线）"))
        self.overlay.setChecked(True)
        self.selecting = Switch(tr("手动框选"))
        self.reset_crop = button(tr("恢复自动裁剪"), self._restore_automatic)
        top.add(self.title)
        top.add(row(self.choice, self.overlay, self.selecting, self.reset_crop))
        self.chart = RecordingChart(c.set_preview_training_region)
        self.chart.setFixedHeight(theme.CAPTURE_CHART_HEIGHT)
        top.add(self.chart)
        self.caption = label(role="caption", wrap=True)
        top.add(self.caption)
        self.overlay.toggled.connect(self.chart.set_overlay)
        self.selecting.toggled.connect(lambda _on: self._sync_selection())

        more = self.add_section()
        more.add(label(tr("数据集"), "section"))
        card = Card()
        more.add(card)
        self.directory = label(tone="muted", selectable=True, wrap=True)
        self.choose_dir = button(tr("选择数据目录"), c.choose_directory)
        card.add(row(self.directory, self.choose_dir))
        self.table = table((tr("名称"), tr("重复数"), tr("采样率 Hz"), tr("各次样本数")), multi=True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 100)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        card.add(self.table)
        self.refresh = button(tr("刷新"), c.refresh)
        self.import_json = button(tr("导入 JSON"), c.import_json)
        self.import_csv = button(tr("导入 CSV（多选）"), c.import_csv)
        self.delete = button(tr("删除选中手势"), c.delete_datasets, kind="danger")
        card.add(row(self.refresh, self.import_json, self.import_csv, self.delete))
        card.add(label(tr("CSV：无表头，六列逗号分隔；每文件一次重复。训练使用列表中全部有效 JSON。"), "caption", wrap=True))
        self.finish()

        c.datasetsChanged.connect(self._rebuild_table)
        c.previewChanged.connect(self._preview_changed)
        self._rebuild_table()
        self._preview_changed()

    def _restore_automatic(self):
        self.selecting.setChecked(False)
        self.controller.set_preview_training_region(None)

    def _sync_chart(self):
        """While recording, the placeholder replaces any earlier take's waveform."""
        c = self.controller
        recording = c.take is not None
        shown = (c.preview_view, recording)
        if shown[0] is self._shown[0] and shown[1] == self._shown[1]:
            return
        self._shown = shown
        if recording:
            self.chart.set_view(None, tr("正在录制，停止后显示波形"))
        else:
            self.chart.set_view(c.preview_view)

    def _sync_selection(self):
        c = self.controller
        can_edit = c.preview_can_edit and c.take is None
        view = c.preview_view
        # Keep the mode through a temporary lock such as training, but not without data.
        if view is None or len(view.raw) < view.min_samples:
            self.selecting.setChecked(False)
        self.selecting.setEnabled(can_edit)
        self.reset_crop.setEnabled(can_edit and view is not None and view.training_region is not None)
        self.chart.set_selection(selecting=self.selecting.isChecked(), editable=can_edit)

    def _preview_changed(self):
        c = self.controller
        self.choice.blockSignals(True)
        self.choice.clear()
        self.choice.addItems([resolve(text) for text in c.preview_choices])
        self.choice.setCurrentIndex(c.preview_index)
        self.choice.blockSignals(False)
        self._sync_chart()
        self._sync_selection()
        if self.isVisible():
            self.render_state()

    def retranslate(self):
        # Translate item captions in place; do not emit a new preview selection.
        self.choice.blockSignals(True)
        try:
            for index, text in enumerate(self.controller.preview_choices):
                self.choice.setItemText(index, resolve(text))
        finally:
            self.choice.blockSignals(False)

    def _rebuild_table(self):
        c = self.controller
        self.table.blockSignals(True)
        self.table.setRowCount(len(c.records))
        for row_index, (record_id, (_path, dataset)) in enumerate(c.records.items()):
            values = (dataset.name, str(len(dataset.repetitions)), f"{dataset.sample_rate_hz:g}",
                      str([len(rep) for rep in dataset.repetitions]))
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, record_id)
                self.table.setItem(row_index, column, item)
        self.table.clearSelection()
        self.table.blockSignals(False)

    def _selection_changed(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        ids = [self.table.item(r, 0).data(Qt.ItemDataRole.UserRole) for r in rows]
        self.controller.select_datasets(ids)

    def render_state(self):
        c = self.controller
        self.notice.render_state()
        set_field_text(self.name, c.name)
        self.name.setEnabled(c.name_editable)
        recording = c.take is not None
        set_text(self.record, tr("停止并保留本次") if recording else tr("开始一次录制"))
        set_kind(self.record, "record" if recording else "primary")
        self.record.setEnabled(c.can_record)
        self.undo.setEnabled(c.can_undo)
        self.save.setEnabled(c.can_save)
        self.discard.setEnabled(c.can_discard)
        self.pulse.setVisible(recording)
        set_text(self.takes, c.take_text)
        set_text(self.stream, c.stream_text)
        for widget in (self.choose_dir, self.refresh, self.import_json, self.import_csv):
            widget.setEnabled(not c.locked)
        self.delete.setEnabled(c.can_delete)
        set_text(self.directory, str(c.directory))
        self.choice.setEnabled(bool(c.preview_choices) and not recording)
        self._sync_chart()
        self._sync_selection()
        set_text(self.title, tr("录制中") if recording else tr("录制波形 · {title}", title=c.preview_title))
        set_text(self.caption, "" if recording else tr(
            "{message}。在任一图内水平拖动框选，再次拖动替换，Esc 取消；原始数据不变。", message=c.preview_message))
