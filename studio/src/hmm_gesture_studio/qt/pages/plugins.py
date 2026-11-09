# SPDX-License-Identifier: MPL-2.0
"""Real plugin management; all runtime, trust and validation rules live in the controller.

Settings and action parameter forms are generated from the plugin manifest.
"""
from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLineEdit, QTableWidgetItem, QVBoxLayout, QWidget

from .. import theme
from ..i18n import resolve, tr
from ..motion import Busy
from ..plugins_controller import format_size, join_titles
from ..widgets import (
    Card, Combo, Page, ReadOnlyText, StatTile, Switch, button, field, hairline, label, line_edit, row, set_field_text, set_text,
    set_tone, table,
)

_TRUST_NOTE = tr("Python 插件以你的账户权限运行，可访问文件和网络。独立进程、虚拟环境和数据目录不是安全沙箱。请只运行你信任的代码；首次启用和代码更新后需重新确认信任。")
_CONTROL_WIDTH = 240
_RECORDING_SCOPES = (("own", tr("仅此插件发起的录音")), ("all", tr("所有完整保存的录音")))
_STORAGE_TITLES = {"data": tr("数据目录"), "cache": tr("缓存目录"),
                   "settings": tr("设置文件"), "log": tr("日志文件")}


def _clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().hide()
            # Later, not now: this can run inside a signal from the widget being replaced.
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _path_field():
    item = line_edit()
    item.setReadOnly(True)
    return item


def _set_path(item, path):
    text = "" if path is None else str(path)
    if item.text() != text:
        item.setText(text)
        item.setCursorPosition(0)  # Long paths should show their start.
        item.setToolTip(text)


def _meta(plugin):
    return f"v{plugin.version} · {resolve(plugin.author)} · {plugin.id}"


class _PluginCard(Card):
    """One row of the plugin list."""

    def __init__(self, page, plugin):
        super().__init__(spacing=6)
        self.plugin = plugin
        self.name = label(plugin.name, "cardTitle", wrap=True)
        self.status = label(role="strong")
        self.enabled = Switch(tr("启用"))
        self.enabled.toggled.connect(lambda checked: page.plugins.set_enabled(plugin.id, checked))
        self.manage = button(tr("管理 ›"), lambda: page.plugins.open_plugin(plugin.id), kind="link")
        top = QHBoxLayout()
        top.setSpacing(theme.ELEMENT_GAP)
        top.addWidget(self.name, 1)
        top.addWidget(self.status)
        top.addWidget(self.enabled)
        top.addSpacing(8)
        top.addWidget(self.manage)
        self.add(top)
        self.meta = label(_meta(plugin), "caption", selectable=True, wrap=True)
        self.add(self.meta)
        self.description = label(plugin.description, wrap=True)
        self.add(self.description)
        self.summary = label(role="caption", wrap=True)
        self.add(self.summary)
        self.update_badge = label(role="caption", tone="warning", wrap=True)
        self.add(self.update_badge)

    def render_state(self, plugins):
        plugin = self.plugin
        set_text(self.name, plugin.name)
        set_text(self.description, plugin.description)
        text, tone = plugins.status(plugin)
        set_text(self.status, text)
        set_tone(self.status, tone)
        set_text(self.meta, _meta(plugin))
        self.enabled.setChecked(plugin.enabled)
        self.enabled.setEnabled(plugins.can_toggle(plugin))
        badge = plugins.update_badge(plugin)
        self.update_badge.setVisible(badge is not None)
        if badge is not None:
            set_text(self.update_badge, badge)
        set_text(self.summary, tr("{origin} · 手势绑定：{bindings} · 数据：{size}", origin=plugins.source_text(plugin),
                                  bindings=len(plugin.bindings), size=format_size(plugin.data_size)))


class PluginsPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        self.plugins = controller.plugins
        self._mode = None
        self._cards = {}
        self._detail_for = None
        self._bindings_key = None
        self._setting_controls = []
        self._binding_controls = []
        self._action_tests = []
        self._size_labels = []
        self._permissions_shown = None
        self._build_list()
        self._build_detail()
        self.finish()

    # List.

    def _build_list(self):
        p = self.plugins
        section = self.list_section = self.add_section()
        self.header(section, tr("插件"), tr("插件可以提供设置页面并响应手势事件。Studio 为每个插件分配独立的数据目录。"))
        section.add(label(_TRUST_NOTE, "caption", tone="warning", wrap=True))
        self.service_running = Switch(tr("启动插件服务"))
        self.service_running.toggled.connect(p.set_service_running)
        self.gesture_control = Switch(tr("手势总控"))
        self.gesture_control.toggled.connect(p.set_gesture_control)
        self.autostart = Switch(tr("开机加载"))
        self.autostart.toggled.connect(p.set_autostart)
        section.add(row(self.service_running, self.gesture_control, self.autostart))
        section.add(label(tr("服务与手势控制默认关闭。开机加载仅在你明确开启后生效。"), "caption", wrap=True))
        self.list_busy = Busy(tr("正在处理插件操作…"))
        self.list_busy.text.setWordWrap(True)
        section.add(self.list_busy)
        self.registered = StatTile(tr("已注册"))
        self.enabled = StatTile(tr("已启用"))
        self.bindings = StatTile(tr("手势绑定"))
        self.total_size = StatTile(tr("数据占用"))
        self.updates = StatTile(tr("可更新"))
        section.add(row(self.registered, self.enabled, self.updates, self.bindings, self.total_size, spacing=24))
        self.add_github = button(tr("从 GitHub 添加"), p.open_github_form, kind="primary")
        self.add_local = button(tr("从本地目录添加…"), p.begin_registration)
        self.add_zip = button(tr("从 ZIP 添加…"), p.begin_zip_registration)
        self.check_all = button(tr("检查更新"), p.check_updates)
        self.check_all_busy = Busy(tr("正在检查更新…"))
        section.add(row(self.add_github, self.add_local, self.add_zip))
        section.add(row(self.check_all, self.check_all_busy))
        section.add(self._build_github_form())
        section.add(self._build_candidate())
        self.empty = label(tr("还没有注册插件。"), tone="muted")
        section.add(self.empty)
        self.list_layout = QVBoxLayout()
        self.list_layout.setSpacing(theme.ELEMENT_GAP)
        section.add(self.list_layout)

    def _build_github_form(self):
        p = self.plugins
        card = self.github_card = Card(tr("从 GitHub 添加插件"))
        self.github_input = line_edit(on_edit=p.set_github_input,
                                      placeholder=tr("owner/repo 或 https://github.com/owner/repo"))
        self.github_input.setAccessibleName(resolve(tr("仓库地址")))
        self.github_input.returnPressed.connect(p.fetch_github)
        card.add(field(tr("仓库地址"), self.github_input))
        self.github_error = label(role="caption", tone="critical", wrap=True)
        card.add(self.github_error)
        self.github_fetch = button(tr("读取插件信息"), p.fetch_github, kind="primary")
        self.github_busy = Busy(tr("正在读取仓库信息…"))
        card.add(row(self.github_fetch, button(tr("取消"), p.close_github_form), self.github_busy))
        card.hide()
        return card

    def _build_candidate(self):
        p = self.plugins
        card = self.candidate_card = Card(tr("确认注册插件"))
        self.candidate_name = label(role="strong", wrap=True)
        card.add(self.candidate_name)
        self.candidate_meta = label(role="caption", wrap=True)
        card.add(self.candidate_meta)
        self.candidate_description = label(wrap=True)
        card.add(self.candidate_description)
        self.candidate_source = _path_field()
        source_box = QVBoxLayout()
        source_box.setSpacing(4)
        self.candidate_source_caption = label(role="caption")
        source_box.addWidget(self.candidate_source_caption)
        source_box.addWidget(self.candidate_source)
        card.add(source_box)
        self.candidate_data = _path_field()
        card.add(field(tr("Studio 将分配的数据目录"), self.candidate_data))
        self.candidate_permissions = label(wrap=True)
        card.add(field(tr("请求的权限"), self.candidate_permissions))
        self.candidate_note = label(role="caption", tone="warning", wrap=True)
        card.add(self.candidate_note)
        self.candidate_confirm = button(tr("确认注册"), p.confirm_registration, kind="primary")
        card.add(row(self.candidate_confirm, button(tr("取消"), p.cancel_registration)))
        card.hide()
        return card

    def _render_list(self):
        p = self.plugins
        self.registered.set_value(str(len(p.plugins)))
        self.enabled.set_value(str(p.enabled_count))
        self.bindings.set_value(str(p.binding_count))
        self.total_size.set_value(format_size(p.total_size))
        self.updates.set_value(str(p.update_count))
        self.add_github.setEnabled(p.can_register)
        self.add_local.setEnabled(p.can_register)
        self.add_zip.setEnabled(p.can_register)
        self.service_running.setChecked(p.service_running)
        self.service_running.setEnabled(p.can_control_service)
        self.gesture_control.setChecked(p.gesture_control)
        self.gesture_control.setEnabled(p.can_control_gestures)
        self.autostart.setChecked(p.autostart)
        self.autostart.setEnabled(p.can_set_autostart)
        self.list_busy.set_busy(p.busy_text is not None, p.busy_text or "")
        self.check_all.setEnabled(p.can_check_all)
        self.check_all_busy.set_busy(bool(p.checking))
        self._render_github_form()
        self.empty.setVisible(not p.plugins)
        ids = [plugin.id for plugin in p.plugins]
        if ids != list(self._cards):
            _clear(self.list_layout)
            self._cards = {plugin.id: _PluginCard(self, plugin) for plugin in p.plugins}
            for card in self._cards.values():
                self.list_layout.addWidget(card)
        for plugin in p.plugins:
            # Discovery/reload can replace a Plugin without changing its id.
            card = self._cards[plugin.id]
            card.plugin = plugin
            card.render_state(p)
        candidate = p.candidate
        self.candidate_card.setVisible(candidate is not None)
        if candidate is not None:
            set_text(self.candidate_name, candidate.name)
            set_text(self.candidate_meta, _meta(candidate))
            set_text(self.candidate_description, candidate.description)
            _set_path(self.candidate_source, candidate.source)
            from_github = candidate.github is not None
            set_text(self.candidate_source_caption, tr("仓库地址") if from_github else tr("本地来源"))
            set_text(self.candidate_note, _TRUST_NOTE)
            _set_path(self.candidate_data, candidate.storage[0].path if candidate.storage else "")
            set_text(self.candidate_permissions, join_titles(candidate.permissions))

    def _render_github_form(self):
        p = self.plugins
        self.github_card.setVisible(p.github_form_open)
        set_field_text(self.github_input, p.github_input)
        self.github_input.setEnabled(not p.github_fetching)
        self.github_fetch.setEnabled(p.can_fetch_github)
        self.github_busy.set_busy(p.github_fetching)
        self.github_error.setVisible(p.github_error is not None)
        if p.github_error is not None:
            set_text(self.github_error, p.github_error)

    # Detail.

    def _build_detail(self):
        p = self.plugins
        head = self.detail_head = self.add_section()
        # The list above it is hidden while this shows, so it is the page's first section.
        head.layout().setContentsMargins(theme.PAGE_MARGIN, theme.SECTION_GAP, theme.PAGE_MARGIN, theme.SECTION_GAP)
        head.add(row(button(tr("‹ 全部插件"), p.close_plugin, kind="link")))
        self.detail_title = label(role="title", wrap=True)
        head.add(self.detail_title)
        self.detail_description = label(tone="muted", wrap=True)
        head.add(self.detail_description)
        head.add(label(_TRUST_NOTE, "caption", tone="warning", wrap=True))
        self.detail_busy = Busy(tr("正在处理插件操作…"))
        self.detail_busy.text.setWordWrap(True)
        head.add(self.detail_busy)
        self.detail_status = label(role="strong")
        self.detail_meta = label(role="caption", selectable=True, wrap=True)
        head.add(row(self.detail_status, self.detail_meta, stretch=False))
        self.detail_error = label(tone="critical", wrap=True)
        head.add(self.detail_error)
        self.detail_enabled = Switch(tr("启用"))
        self.detail_enabled.toggled.connect(lambda checked: p.set_enabled(p.selected_id, checked))
        self.reload = button(tr("重新加载"), p.reload_plugin)
        self.dependencies = button(tr("安装依赖…"), p.install_dependencies)
        self.remove = button(tr("移除插件"), p.remove_plugin, kind="danger")
        head.add(row(self.detail_enabled, self.reload, self.dependencies))
        head.add(row(self.remove))
        self.binding_tile = StatTile(tr("手势绑定"))
        self.action_tile = StatTile(tr("可用动作"))
        self.setting_tile = StatTile(tr("设置项"))
        self.size_tile = StatTile(tr("数据占用"))
        head.add(row(self.binding_tile, self.action_tile, self.setting_tile, self.size_tile, spacing=24))

        source = self._build_source_section()

        settings = self.settings_section = self.add_section()
        settings.add(label(tr("插件设置"), "section"))
        settings.add(label(tr("表单由插件声明。修改后点击保存；动作参数在各条手势绑定中单独配置。"), tone="muted", wrap=True))
        self.settings_card = Card()
        settings.add(self.settings_card)
        self.settings_rows = QVBoxLayout()
        self.settings_rows.setSpacing(theme.ELEMENT_GAP)
        self.settings_card.add(self.settings_rows)
        self.reset = button(tr("恢复默认"), p.reset_settings)
        self.save = button(tr("保存设置"), p.save_settings, kind="primary")
        self.settings_dirty = label(tr("有未保存的修改"), "caption", tone="warning")
        self.reset_row = QWidget()
        reset_layout = QHBoxLayout(self.reset_row)
        reset_layout.setContentsMargins(0, 0, 0, 0)
        reset_layout.addWidget(self.save)
        reset_layout.addWidget(self.reset)
        reset_layout.addStretch(1)
        self.settings_card.add(self.reset_row)
        self.settings_card.add(self.settings_dirty)

        gestures = self.gestures_section = self.add_section()
        gestures.add(label(tr("手势绑定"), "section"))
        gestures.add(label(tr("一个手势可以触发多个动作。绑定按模型内容指纹保存；其他模型的绑定保留，但不会由当前模型触发。"), tone="muted", wrap=True))
        self.model_fingerprint = _path_field()
        gestures.add(field(tr("当前模型指纹"), self.model_fingerprint))
        gestures.add(label(tr("首次成功绑定会自动保存模型；下次启动恢复最近使用的已绑定模型，无需重新导入。识别与手势控制仍需手动开启。"),
                           "caption", wrap=True))
        self.saved_model_path = _path_field()
        gestures.add(field(tr("自动保存的模型"), self.saved_model_path))
        self.bindings_card = Card()
        gestures.add(self.bindings_card)
        self.binding_grid = QGridLayout()
        self.binding_grid.setHorizontalSpacing(theme.ELEMENT_GAP)
        self.binding_grid.setVerticalSpacing(theme.ELEMENT_GAP)
        self.binding_grid.setColumnStretch(0, 1)
        self.binding_grid.setColumnStretch(2, 1)
        self.bindings_card.add(self.binding_grid)
        self.add_binding = button(tr("添加绑定"), p.add_binding)
        self.copy_bindings = button(tr("从其他模型复制…"), p.copy_bindings)
        self.bindings_card.add(row(self.add_binding, self.copy_bindings))

        actions = self.actions_section = self.add_section()
        actions.add(label(tr("手动测试动作"), "section"))
        actions.add(label(tr("无需手势绑定，使用动作参数默认值测试。测试会真实执行插件代码；绑定中的测试使用该绑定的当前参数。"), tone="muted", wrap=True))
        self.action_rows = QVBoxLayout()
        self.action_rows.setSpacing(theme.ELEMENT_GAP)
        actions.add(self.action_rows)

        recording = self.recording_section = self.add_section()
        recording.add(label(tr("录音事件"), "section"))
        recording.add(label(tr("启用后，插件可收到完整保存的录音文件，并可能上传文件。请核实用途和接收方；开启订阅或扩大范围需要单独确认。"), "caption", tone="warning", wrap=True))
        self.subscriptions = label(role="caption", wrap=True, selectable=True)
        recording.add(self.subscriptions)
        self.recording_events = Switch(tr("订阅录音完成事件"))
        self.recording_events.toggled.connect(p.set_recording_events)
        recording.add(self.recording_events)
        self.recording_scope = Combo()
        self.recording_scope.setAccessibleName(resolve(tr("录音范围")))
        for value, title in _RECORDING_SCOPES:
            self.recording_scope.addItem(resolve(title), value)
        self.recording_scope.activated.connect(lambda index: p.set_recording_scope(self.recording_scope.itemData(index)))
        recording.add(field(tr("录音范围"), self.recording_scope))
        self.process_audio = button(tr("选择已有音频并发送事件…"), p.process_audio_file)
        recording.add(row(self.process_audio))

        storage = self.storage_section = self.add_section()
        storage.add(label(tr("数据与存储"), "section"))
        storage.add(label(tr("Studio 为每个插件分配数据目录，便于管理；这不限制 Python 插件访问其他文件。"), tone="muted", wrap=True))
        storage_card = Card()
        storage.add(storage_card)
        self.storage_grid = QGridLayout()
        self.storage_grid.setHorizontalSpacing(theme.ELEMENT_GAP)
        self.storage_grid.setVerticalSpacing(theme.ELEMENT_GAP)
        self.storage_grid.setColumnStretch(1, 1)
        storage_card.add(self.storage_grid)
        self.open_directory = button(tr("打开数据目录"), p.open_data_directory)
        self.clear_data = button(tr("清除数据"), p.clear_data, kind="danger")
        storage_card.add(row(self.open_directory, self.clear_data))

        permissions = self.permissions_section = self.add_section()
        permissions.add(label(tr("权限"), "section"))
        self.permissions_card = Card(spacing=6)
        permissions.add(self.permissions_card)
        self.permission_rows = QVBoxLayout()
        self.permission_rows.setSpacing(theme.ELEMENT_GAP)
        self.permissions_card.add(self.permission_rows)

        events = self.events_section = self.add_section()
        events.add(label(tr("最近事件"), "section"))
        self.events_empty = label(tr("还没有事件。"), tone="muted")
        events.add(self.events_empty)
        self.events_table = table((tr("时间"), tr("手势名称"), tr("动作"), tr("结果")), height=190)
        self.events_table.setColumnWidth(0, 110)
        self.events_table.setColumnWidth(1, 150)
        self.events_table.setColumnWidth(2, 220)
        events.add(self.events_table)

        self._detail_sections = (head, source, settings, gestures, actions, recording, storage, permissions, events)

    def _build_source_section(self):
        p = self.plugins
        section = self.source_section = self.add_section()
        section.add(label(tr("来源与更新"), "section"))
        card = Card()
        section.add(card)

        self.local_box = QWidget()
        local = QVBoxLayout(self.local_box)
        local.setContentsMargins(0, 0, 0, 0)
        local.setSpacing(theme.ELEMENT_GAP)
        self.local_path = _path_field()
        local.addLayout(field(tr("插件代码目录"), self.local_path))
        local.addWidget(label(tr("不会自动更新或安装依赖。修改本地代码后请重新加载；GitHub 更新下载并运行所显示的固定 commit。"), "caption", wrap=True))
        card.add(self.local_box)

        self.github_box = QWidget()
        box = QVBoxLayout(self.github_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(theme.ELEMENT_GAP)
        grid = QGridLayout()
        grid.setHorizontalSpacing(theme.ELEMENT_GAP)
        grid.setVerticalSpacing(theme.ELEMENT_GAP)
        grid.setColumnStretch(1, 1)
        self.repo_path = _path_field()
        self.repo_path.setAccessibleName(resolve(tr("仓库地址")))
        self.installed = _path_field()
        self.last_checked = label(wrap=True)
        for index, (caption, widget) in enumerate(((tr("仓库地址"), self.repo_path),
                                                   (tr("当前版本"), self.installed), (tr("上次检查"), self.last_checked))):
            grid.addWidget(label(caption), index, 0)
            grid.addWidget(widget, index, 1)
        box.addLayout(grid)
        box.addWidget(hairline())
        self.update_status = label(role="strong", wrap=True)
        self.update_busy = Busy(tr("正在检查更新…"))
        box.addWidget(self.update_status)
        box.addWidget(self.update_busy, 0, Qt.AlignmentFlag.AlignLeft)
        self.notes_title = label(role="caption", wrap=True)
        box.addWidget(self.notes_title)
        self.notes = ReadOnlyText(height=64)
        box.addWidget(self.notes)
        self.new_permissions = label(tone="warning", wrap=True)
        box.addWidget(self.new_permissions)
        self.install = button(tr("立即更新"), lambda: p.install_update(p.selected_id), kind="primary")
        self.check = button(tr("检查更新"), lambda: p.check_updates(p.selected_id))
        box.addLayout(row(self.install, self.check))
        card.add(self.github_box)
        return section

    def _render_source(self, plugin):
        p = self.plugins
        github = plugin.github
        _set_path(self.local_path, plugin.directory)
        self.github_box.setVisible(github is not None)
        if github is None:
            return
        _set_path(self.repo_path, plugin.source)
        _set_path(self.installed, f"v{plugin.version} ({github.commit})")
        set_text(self.last_checked, github.last_checked)
        busy = p.update_busy_text(plugin)
        self.update_busy.set_busy(busy is not None, busy if busy is not None else "")
        self.update_status.setVisible(busy is None)
        text, tone = p.update_status(plugin)
        set_text(self.update_status, text)
        set_tone(self.update_status, tone)
        release = github.update
        for widget in (self.notes_title, self.notes, self.install):
            widget.setVisible(release is not None)
        self.new_permissions.setVisible(release is not None and bool(release.new_permissions))
        if release is not None:
            set_text(self.notes_title, tr("v{version} 更新内容 · {commit}", version=release.version, commit=release.commit))
            self.notes.set_value(release.notes)
            if release.new_permissions:
                set_text(self.new_permissions, tr("新版本新增权限：{permissions}。需要你确认后才会安装。",
                                                  permissions=join_titles(release.new_permissions)))
        self.install.setEnabled(p.can_install(plugin))
        self.check.setEnabled(p.can_check(plugin))

    def _rebuild_detail(self, plugin):
        self._build_setting_rows(plugin)
        self._build_storage_rows(plugin)
        self._build_action_rows(plugin)
        self._bindings_key = None

    def _build_setting_rows(self, plugin):
        _clear(self.settings_rows)
        self._setting_controls = []
        if not plugin.settings:
            self.settings_rows.addWidget(label(tr("此插件没有设置项。"), tone="muted"))
        for spec in plugin.settings:
            item = self._form_row(spec, lambda value, key=spec.key: self.plugins.set_setting(key, value))
            self.settings_rows.addWidget(item[1])
            self._setting_controls.append(item)

    def _form_row(self, spec, on_change, *, deferred=False):
        holder = QWidget()
        layout = QHBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(24)
        text = QVBoxLayout()
        text.setSpacing(2)
        if spec.group:
            text.addWidget(label(spec.group, "caption", wrap=True))
        title = tr("{title}（必填）", title=spec.title) if spec.required else spec.title
        text.addWidget(label(title, wrap=True))
        if spec.help:
            text.addWidget(label(spec.help, "caption", wrap=True))
        error = label(role="caption", tone="critical", wrap=True)
        error.hide()
        text.addWidget(error)
        layout.addLayout(text, 1)
        control = self._setting_control(spec, on_change, deferred=deferred)
        layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
        return spec, holder, control, error

    def _setting_control(self, spec, on_change, *, deferred=False):
        if spec.kind == "switch":
            control = Switch()
            control.setAccessibleName(resolve(spec.title))
            control.toggled.connect(on_change)
            return control
        if spec.kind == "choice":
            control = Combo()
            control.setFixedWidth(_CONTROL_WIDTH)
            control.setAccessibleName(resolve(spec.title))
            for value, text in spec.options:
                control.addItem(resolve(text), value)
            control.activated.connect(lambda index: on_change(control.itemData(index)))
            return control
        control = line_edit(on_edit=None if deferred else on_change, width=_CONTROL_WIDTH)
        if deferred:
            control.setProperty("deferredCommit", True)
            control.textEdited.connect(lambda _value: control.setModified(True))
            def commit():
                value = control.text()
                control.setModified(False)
                on_change(value)
            control.editingFinished.connect(commit)
        control.setAccessibleName(resolve(spec.title))
        if spec.kind == "secret":
            control.setEchoMode(QLineEdit.EchoMode.Password)
        return control

    def _build_storage_rows(self, plugin):
        _clear(self.storage_grid)
        self._size_labels = []
        for index, slot in enumerate(plugin.storage):
            title = _STORAGE_TITLES.get(slot.kind, slot.title)
            self.storage_grid.addWidget(label(title), index, 0)
            path = _path_field()
            _set_path(path, slot.path)
            path.setAccessibleName(resolve(title))
            self.storage_grid.addWidget(path, index, 1)
            size = label(role="caption")
            size.setMinimumWidth(64)
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.storage_grid.addWidget(size, index, 2)
            self._size_labels.append((path, size))

    def _build_permission_rows(self, plugin):
        _clear(self.permission_rows)
        for index, permission in enumerate(plugin.permissions):
            if index:
                self.permission_rows.addWidget(hairline())
            holder = QWidget()
            layout = QVBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(2)
            title = tr("需要留意 · {title}", title=permission.title) if permission.sensitive else permission.title
            layout.addWidget(label(title, "strong", tone="warning" if permission.sensitive else None, wrap=True))
            layout.addWidget(label(permission.detail, "caption", wrap=True))
            self.permission_rows.addWidget(holder)

    def _fill_events(self, plugin):
        self.events_empty.setVisible(not plugin.events)
        self.events_table.setVisible(bool(plugin.events))
        self.events_table.setRowCount(len(plugin.events))
        for index, event in enumerate(plugin.events):
            result = tr("失败：{reason}", reason=event.error) if event.error else tr("成功")
            values = (event.when, event.gesture, resolve(event.action), resolve(result))
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 3:
                    item.setForeground(QColor(theme.CRITICAL if event.error else theme.GOOD))
                self.events_table.setItem(index, column, item)

    def _build_action_rows(self, plugin):
        _clear(self.action_rows)
        self._action_tests = []
        if not plugin.actions:
            self.action_rows.addWidget(label(tr("此插件没有可测试的动作。"), tone="muted"))
        for action in plugin.actions:
            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            text = QVBoxLayout()
            text.addWidget(label(action.title, "strong", wrap=True))
            text.addWidget(label(tr("超时：{seconds} 秒", seconds=action.timeout), "caption", wrap=True))
            layout.addLayout(text, 1)
            test = button(tr("使用默认参数测试"), lambda key=action.key: self.plugins.test_action(key))
            layout.addWidget(test)
            self._action_tests.append(test)
            self.action_rows.addWidget(holder)

    @staticmethod
    def _binding_combo(title):
        control = Combo()
        control.setAccessibleName(resolve(title))
        # Manifest titles and user gesture names must not widen the whole page.
        control.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        control.setMinimumContentsLength(10)
        control.setMinimumWidth(0)
        return control

    def _build_binding_rows(self, plugin):
        p = self.plugins
        _clear(self.binding_grid)
        self._binding_controls = []
        if not plugin.bindings:
            self.binding_grid.addWidget(label(tr("还没有绑定手势。"), tone="muted"), 0, 0, 1, 3)
        for index, binding in enumerate(plugin.bindings):
            holder = QWidget()
            layout = QVBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(theme.ELEMENT_GAP)
            if index:
                layout.addWidget(hairline())
            status = label(role="caption", wrap=True)
            layout.addWidget(status)
            gesture = self._binding_combo(tr("手势名称"))
            gesture.activated.connect(lambda _i, row=index, box=gesture: p.set_binding_gesture(row, box.currentText()))
            action = self._binding_combo(tr("触发动作"))
            for item in plugin.actions:
                action.addItem(resolve(item.title), item.key)
            if action.findData(binding.action) < 0:
                action.addItem(binding.action, binding.action)
            action.activated.connect(lambda _i, row=index, box=action: p.set_binding_action(row, box.currentData()))
            choices = QHBoxLayout()
            choices.addLayout(field(tr("手势名称"), gesture), 1)
            choices.addLayout(field(tr("触发动作"), action), 1)
            layout.addLayout(choices)
            enabled = Switch(tr("启用绑定"))
            enabled.toggled.connect(lambda value, row=index: p.set_binding_enabled(row, value))
            cooldown = line_edit(width=100)
            cooldown.setAccessibleName(resolve(tr("冷却（毫秒）")))
            cooldown.editingFinished.connect(
                lambda row=index, box=cooldown, plugin_id=plugin.id, binding_id=binding.id:
                self._commit_cooldown(row, box, plugin_id, binding_id))
            layout.addLayout(row(enabled, label(tr("冷却（毫秒）")), cooldown))
            test = button(tr("测试此绑定"), lambda row=index: p.test_binding(row))
            remove = button(tr("移除"), lambda row=index: p.remove_binding(row), kind="link")
            layout.addLayout(row(test, remove))
            parameters = []
            declaration = next((item for item in plugin.actions if item.key == binding.action), None)
            if declaration is not None:
                for spec in declaration.parameters:
                    item = self._form_row(spec,
                        lambda value, owner=plugin, binding_id=binding.id, key=spec.key:
                            self._commit_parameter(owner, binding_id, key, value), deferred=True)
                    layout.addWidget(item[1])
                    parameters.append(item)
            self.binding_grid.addWidget(holder, index, 0, 1, 3)
            self._binding_controls.append((status, gesture, action, enabled, cooldown, test, remove, parameters))

    def _commit_parameter(self, owner, binding_id, key, value):
        if self.plugins.selected is not owner:
            return
        for index, binding in enumerate(owner.bindings):
            if binding.id == binding_id:
                self.plugins.set_binding_parameter(index, key, value)
                break

    def _commit_cooldown(self, index, control, plugin_id, binding_id):
        # Validate on commit, not every keystroke (an empty intermediate value is
        # normal while typing). A deferred focus-out must not edit another row.
        if not control.isModified():
            return
        control.setModified(False)
        plugin = self.plugins.selected
        if (plugin is not None and plugin.id == plugin_id and index < len(plugin.bindings)
                and plugin.bindings[index].id == binding_id):
            self.plugins.set_binding_cooldown(index, control.text())

    def _render_bindings(self, plugin):
        p = self.plugins
        editable = p.can_edit(plugin)
        # Snapshot only structure, not mutable bindings/parameter dicts. Parameter edits
        # update existing controls so notifications do not destroy focus or the caret.
        key = (plugin.id, tuple((item.id, item.action) for item in plugin.bindings))
        if key != self._bindings_key:
            self._bindings_key = key
            self._build_binding_rows(plugin)
        for index, binding in enumerate(plugin.bindings):
            status, gesture, action, enabled, cooldown, test, remove, parameters = self._binding_controls[index]
            active_model = p.binding_visible(binding)
            set_text(status, tr("属于当前模型") if active_model else tr("不活跃：不属于当前模型（保留绑定）"))
            set_tone(status, "muted" if active_model else "warning")
            status.setToolTip(str(binding.model))
            # Keep unavailable gesture names visible rather than silently selecting another.
            names = list(p.free_gestures(plugin, index))
            if binding.gesture not in names:
                names.append(binding.gesture)
            with QSignalBlocker(gesture):
                if names != [gesture.itemText(i) for i in range(gesture.count())]:
                    gesture.clear()
                    gesture.addItems(names)
                gesture.setCurrentText(binding.gesture)
            with QSignalBlocker(action):
                action.setCurrentIndex(action.findData(binding.action))
            enabled.setChecked(binding.enabled)
            if not (cooldown.hasFocus() and cooldown.isModified()):
                set_field_text(cooldown, str(binding.cooldown_ms))
            for control in (gesture, action, enabled, cooldown, remove):
                control.setEnabled(editable)
            test.setEnabled(p.can_test(plugin))
            for spec, holder, control, error in parameters:
                holder.setVisible(p.parameter_visible(binding, spec))
                self._render_form_control(spec, control, binding.parameters.get(spec.key, spec.default))
                control.setEnabled(editable)
                self._render_error(error, p.binding_parameter_error(index, spec))

    @staticmethod
    def _render_form_control(spec, control, value):
        if spec.kind == "switch":
            control.setChecked(bool(value))
        elif spec.kind == "choice":
            with QSignalBlocker(control):
                control.setCurrentIndex(control.findData(value))
        else:
            if control.property("deferredCommit") and control.hasFocus() and control.isModified():
                return
            set_field_text(control, "" if value is None else str(value))

    @staticmethod
    def _render_error(error, message):
        error.setVisible(message is not None)
        if message is not None:
            set_text(error, message)

    def _render_settings(self, plugin):
        p = self.plugins
        editable = p.can_edit(plugin)
        for spec, holder, control, error in self._setting_controls:
            holder.setVisible(p.setting_visible(plugin, spec))
            self._render_form_control(spec, control, plugin.values.get(spec.key, spec.default))
            control.setEnabled(editable)
            self._render_error(error, p.setting_error(plugin, spec))
        self.reset_row.setVisible(bool(plugin.settings))
        self.reset.setEnabled(editable)
        self.save.setEnabled(p.can_save_settings)
        self.settings_dirty.setVisible(p.settings_dirty)

    def _render_detail(self, plugin):
        p = self.plugins
        # Reload/update can replace the object or mutate its manifest in place.
        # repr makes a value snapshot even when declaration fields contain dicts.
        detail_key = (id(plugin), plugin.id, plugin.version, repr(plugin.settings), repr(plugin.actions),
                      tuple((slot.kind, str(slot.path), resolve(slot.title)) for slot in plugin.storage))
        if self._detail_for != detail_key:
            self._detail_for = detail_key
            self._rebuild_detail(plugin)
        set_text(self.detail_title, plugin.name)
        set_text(self.detail_description, plugin.description)
        text, tone = p.status(plugin)
        set_text(self.detail_status, text)
        set_tone(self.detail_status, tone)
        set_text(self.detail_meta, _meta(plugin))
        self.detail_error.setVisible(bool(plugin.error))
        if plugin.error:
            set_text(self.detail_error, plugin.error)
        self.detail_enabled.setChecked(plugin.enabled)
        self.detail_enabled.setEnabled(p.can_toggle(plugin))
        self.remove.setEnabled(p.can_remove(plugin))
        self.reload.setEnabled(p.can_reload(plugin))
        self.dependencies.setEnabled(p.can_dependencies(plugin))
        self.detail_busy.set_busy(p.busy_text is not None, p.busy_text or "")
        if self._permissions_shown != (plugin.id, plugin.permissions):
            self._permissions_shown = (plugin.id, plugin.permissions)
            self._build_permission_rows(plugin)
        self._render_source(plugin)
        self.binding_tile.set_value(str(len(plugin.bindings)))
        self.action_tile.set_value(str(len(plugin.actions)))
        self.setting_tile.set_value(str(len(plugin.settings)))
        self.size_tile.set_value(format_size(plugin.data_size))
        self._render_settings(plugin)
        self._render_bindings(plugin)
        self.add_binding.setEnabled(p.can_add_binding(plugin))
        self.copy_bindings.setEnabled(p.can_copy_bindings(plugin))
        _set_path(self.model_fingerprint, p.model_fingerprint)
        self.model_fingerprint.setPlaceholderText(resolve(tr("未加载模型")))
        _set_path(self.saved_model_path, p.saved_model_path or "")
        self.saved_model_path.setPlaceholderText(resolve(tr("成功绑定后自动保存")))
        for test in self._action_tests:
            test.setEnabled(p.can_test(plugin))
        self.recording_events.setChecked(plugin.recording_events_enabled)
        self.recording_events.setEnabled(p.can_recording_events(plugin))
        with QSignalBlocker(self.recording_scope):
            self.recording_scope.setCurrentIndex(self.recording_scope.findData(plugin.recording_scope))
        self.recording_scope.setEnabled(p.can_recording_events(plugin))
        self.process_audio.setEnabled(p.can_process_audio_file(plugin))
        set_text(self.subscriptions, tr("声明的事件订阅：{events}", events=", ".join(plugin.subscriptions))
                 if plugin.subscriptions else tr("此插件未声明事件订阅。"))
        for slot, (path, size) in zip(plugin.storage, self._size_labels):
            _set_path(path, slot.path)
            set_text(size, format_size(slot.size))
        self.open_directory.setEnabled(p.can_open_data_directory(plugin))
        self.clear_data.setEnabled(p.can_clear_data(plugin))
        self._fill_events(plugin)

    # Rendering.

    def retranslate(self):
        # Combo items and table cells hold resolved text; rebuild them in the new language.
        self._detail_for = None
        self._permissions_shown = None
        self.recording_scope.setAccessibleName(resolve(tr("录音范围")))
        with QSignalBlocker(self.recording_scope):
            for index, (_value, text) in enumerate(_RECORDING_SCOPES):
                self.recording_scope.setItemText(index, resolve(text))

    def render_state(self):
        plugin = self.plugins.selected
        mode = plugin.id if plugin is not None else ""
        changed = mode != self._mode
        if changed and self._mode is not None and self.isVisible():
            self.window().fade_content()
        self._mode = mode
        self.list_section.setVisible(plugin is None)
        for section in self._detail_sections:
            section.setVisible(plugin is not None)
        if plugin is None:
            self._detail_for = None
            self._render_list()
        else:
            self._render_detail(plugin)
        if changed:
            self.verticalScrollBar().setValue(0)
