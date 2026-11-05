# SPDX-License-Identifier: MPL-2.0
"""Ring recording: remote, live PCM, save-notification reception, and history."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHeaderView, QTableWidgetItem

from ..widgets import (
    Card, Combo, DeviceNotice, Page, Switch, button, field, label, line_edit, row, set_field_text, set_text, table,
)

_LABELS = (("channels", "声道（1 / 2）"), ("bitrate", "码率（bits/s）"), ("gain_left_q8", "左增益 Q8"),
           ("gain_right_q8", "右增益 Q8"), ("duration_s", "最长（秒）"))


class AudioPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c, t = controller, controller.tools
        top = self.add_section()
        self.header(top, "戒指录音", "四种音频操作不能并行；音频期间暂停 IMU，结束后恢复。")
        self.notice = DeviceNotice(self, "尚未连接戒指。")
        top.add(self.notice)
        self.state = label(wrap=True)
        top.add(self.state)
        self.directory = label(tone="muted", selectable=True)
        self.choose_dir = button("选择录音目录", c.choose_audio_directory)
        self.open_dir = button("打开录音目录", lambda: c.open_path(c.audio_directory))
        top.add(row(self.directory, self.choose_dir, self.open_dir))

        form = Card("远程录音 / 实时 PCM", tone="mist")
        top.add(form)
        grid = QGridLayout()
        grid.setHorizontalSpacing(20)
        self.inputs = {}
        for column, (key, caption) in enumerate(_LABELS):
            entry = line_edit(t.fields[key], on_edit=lambda value, key=key: t.set_field(key, value))
            self.inputs[key] = entry
            grid.addLayout(field(caption, entry), 0, column)
        form.add(grid)
        self.read = button("读取设置 / 状态", t.read_config)
        self.record = button("开始远程录音", t.start_recording, kind="primary")
        self.finish_button = button("停止并保存", t.finish_recording)
        self.live = button("实时 PCM 接收并保存", lambda: t.start_recording(live=True))
        form.add(row(self.read, self.record, self.finish_button, self.live))
        self.playback = Switch("边接收边播放（仅实时 PCM）")
        self.playback.toggled.connect(t.set_playback)
        form.add(row(self.playback, label("开始前打开；建议使用耳机，避免啸叫。", "caption")))
        self.recording_text = label(tone="muted", wrap=True)
        form.add(self.recording_text)
        form.add(label("远程保存到戒指后下载；实时接收同时保留设备副本，完整 END 后生成 WAV。"
                       "播放失败只在日志提示，接收与保存继续。最长时间以 0.02 秒递增；增益 256 = 1 倍。", "caption", wrap=True))

        band = self.add_section(band=True)
        band.add(label("接收与下载", "section"))
        notify = Card("保存通知监听", tone="white")
        band.add(notify)
        notify.add(label("戒指录音保存后，按通知索引自动下载。待机监听不阻塞手势，传输时暂停 IMU。"
                         "保留原始音频并解码 WAV；若漏收或中断，可查询历史录音重新下载。", "caption", wrap=True))
        self.start = button("开启保存通知监听", c.start_audio)
        self.stop = button("停止接收 / 下载", c.stop_audio)
        notify.add(row(self.start, self.stop))
        history = Card("戒指历史录音", tone="white")
        band.add(history)
        self.list = button("查询戒指历史录音", c.list_audio)
        self.remote = Combo()
        self.remote.setMinimumWidth(220)
        self.remote.currentTextChanged.connect(c.set_remote_audio_selected)
        self.download = button("下载选中录音", c.download_audio)
        history.add(row(self.list, self.remote, self.download))
        files = Card("本次会话保存的文件", tone="white")
        band.add(files)
        self.table = table(("文件", "音频时长", "大小"), height=180)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 100)
        self.table.itemSelectionChanged.connect(self.render_state)
        files.add(self.table)
        self.open_file = button("打开选中文件", self._open_selected)
        files.add(row(self.open_file))
        self.finish()
        c.audioFieldsChanged.connect(self._fields_replaced)
        c.audioFilesChanged.connect(self._files_changed)
        c.remoteAudioChanged.connect(self._remote_changed)

    def _fields_replaced(self):
        for key, entry in self.inputs.items():
            set_field_text(entry, self.controller.tools.fields[key])

    def _remote_changed(self):
        c = self.controller
        self.remote.blockSignals(True)
        self.remote.clear()
        self.remote.addItems(list(c.remote_audio_files))
        self.remote.setCurrentText(c.remote_audio_selected)
        self.remote.blockSignals(False)

    def _files_changed(self):
        rows = self.controller.audio_rows
        self.table.setRowCount(len(rows))
        for index, entry in enumerate(rows):
            for column, key in enumerate(("name", "duration", "size")):
                item = QTableWidgetItem(entry[key])
                item.setData(Qt.ItemDataRole.UserRole, index)
                self.table.setItem(index, column, item)

    def _open_selected(self):
        selected = self.table.selectedIndexes()
        if selected:
            self.controller.open_path(self.controller.audio_rows[selected[0].row()]["path"])

    def render_state(self):
        c, t = self.controller, self.controller.tools
        self.notice.render_state()
        set_text(self.state, c.audio_text)
        set_text(self.directory, str(c.audio_directory))
        self.choose_dir.setEnabled(c.can_change_audio_dir)
        for entry in self.inputs.values():
            entry.setEnabled(t.available("record"))
        self.read.setEnabled(t.available("read"))
        self.record.setEnabled(t.available("record"))
        self.finish_button.setEnabled(t.available("finish"))
        self.live.setEnabled(t.available("live_audio"))
        self.playback.setEnabled(t.available("playback"))
        self.playback.setChecked(t.playback)
        set_text(self.recording_text, t.recording_text)
        self.start.setEnabled(t.available("audio"))
        self.stop.setEnabled(c.can_stop_audio)
        self.list.setEnabled(t.available("audio"))
        self.remote.setEnabled(bool(c.remote_audio_files))
        self.download.setEnabled(c.can_download_audio)
        self.open_file.setEnabled(bool(self.table.selectedIndexes()))
