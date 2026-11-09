# SPDX-License-Identifier: MPL-2.0
"""Registered plugins and, per plugin, its settings, gesture bindings and storage.

UI preview over sample data; see plugins_mock.py. Settings forms are generated from
what each plugin declares, so this page holds no plugin-specific layout.
"""
from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLineEdit, QTableWidgetItem, QVBoxLayout, QWidget

from .. import theme
from ..i18n import resolve, tr
from ..motion import Busy
from ..plugins_controller import format_size, join_titles
from ..widgets import (
    Card, Combo, Page, ReadOnlyText, StatTile, Switch, button, field, hairline, label, line_edit, row, set_field_text, set_text,
    set_tone, table,
)

_PREVIEW_NOTE = tr("界面预览：这里全部是示例数据，不会运行插件代码或写入磁盘，修改在关闭 Studio 后丢失。")
_CONTROL_WIDTH = 280
_CHANNELS = (("stable", tr("正式版")), ("prerelease", tr("包含预发布版")))


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
    text = str(path)
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
        self.name = label(plugin.name, "cardTitle")
        self.status = label(role="strong")
        self.enabled = Switch(tr("启用"))
        self.enabled.toggled.connect(lambda checked: page.plugins.set_enabled(plugin.id, checked))
        self.manage = button(tr("管理 ›"), lambda: page.plugins.open_plugin(plugin.id), kind="link")
        top = QHBoxLayout()
        top.setSpacing(theme.ELEMENT_GAP)
        top.addWidget(self.name)
        top.addWidget(self.status)
        top.addStretch(1)
        top.addWidget(self.enabled)
        top.addSpacing(8)
        top.addWidget(self.manage)
        self.add(top)
        self.meta = label(_meta(plugin), "caption", selectable=True)
        self.add(self.meta)
        self.add(label(plugin.description, wrap=True))
        self.summary = label(role="caption", wrap=True)
        self.add(self.summary)
        self.update_badge = label(role="caption", tone="warning", wrap=True)
        self.add(self.update_badge)

    def render_state(self, plugins):
        plugin = self.plugin
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
        section.add(label(_PREVIEW_NOTE, "caption", tone="warning", wrap=True))
        self.registered = StatTile(tr("已注册"))
        self.enabled = StatTile(tr("已启用"))
        self.bindings = StatTile(tr("手势绑定"))
        self.total_size = StatTile(tr("数据占用"))
        self.updates = StatTile(tr("可更新"))
        section.add(row(self.registered, self.enabled, self.updates, self.bindings, self.total_size, spacing=64))
        self.add_github = button(tr("从 GitHub 添加"), p.open_github_form, kind="primary")
        self.add_local = button(tr("从本地目录添加…"), p.begin_registration)
        self.check_all = button(tr("检查更新"), p.check_updates)
        self.check_all_busy = Busy(tr("正在检查更新…"))
        section.add(row(self.add_github, self.add_local, self.check_all, self.check_all_busy))
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
        self.candidate_name = label(role="strong")
        card.add(self.candidate_name)
        self.candidate_meta = label(role="caption")
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
        self.candidate_auto = Switch(tr("自动更新"))
        self.candidate_auto.toggled.connect(p.set_candidate_auto_update)
        card.add(self.candidate_auto)
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
        for card in self._cards.values():
            card.render_state(p)
        candidate = p.candidate
        self.candidate_card.setVisible(candidate is not None)
        if candidate is not None:
            set_text(self.candidate_name, candidate.name)
            set_text(self.candidate_meta, _meta(candidate))
            set_text(self.candidate_description, candidate.description)
            _set_path(self.candidate_source, candidate.source)
            from_github = candidate.github is not None
            set_text(self.candidate_source_caption, tr("仓库地址") if from_github else tr("插件目录"))
            set_text(self.candidate_note, tr("示例清单：尚未访问 GitHub。插件会在本机运行第三方代码，请只添加你信任的仓库。")
                     if from_github else tr("示例清单：尚未读取你选择的目录。"))
            self.candidate_auto.setVisible(from_github)
            if from_github:
                self.candidate_auto.setChecked(candidate.github.auto_update)
            _set_path(self.candidate_data, candidate.storage[0].path)
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
        self.detail_title = label(role="title")
        head.add(self.detail_title)
        self.detail_description = label(tone="muted", wrap=True)
        head.add(self.detail_description)
        head.add(label(_PREVIEW_NOTE, "caption", tone="warning", wrap=True))
        self.detail_status = label(role="strong")
        self.detail_meta = label(role="caption", selectable=True)
        head.add(row(self.detail_status, self.detail_meta))
        self.detail_error = label(tone="critical", wrap=True)
        head.add(self.detail_error)
        self.detail_enabled = Switch(tr("启用"))
        self.detail_enabled.toggled.connect(lambda checked: p.set_enabled(p.selected_id, checked))
        self.reload = button(tr("重新加载"), lambda: p.preview_only(tr("重新加载")))
        self.remove = button(tr("移除插件"), p.remove_plugin, kind="danger")
        head.add(row(self.detail_enabled, self.reload, self.remove))
        self.binding_tile = StatTile(tr("手势绑定"))
        self.action_tile = StatTile(tr("可用动作"))
        self.setting_tile = StatTile(tr("设置项"))
        self.size_tile = StatTile(tr("数据占用"))
        head.add(row(self.binding_tile, self.action_tile, self.setting_tile, self.size_tile, spacing=64))

        source = self._build_source_section()

        settings = self.settings_section = self.add_section()
        settings.add(label(tr("插件设置"), "section"))
        settings.add(label(tr("表单由插件声明，Studio 负责显示和保存。"), tone="muted", wrap=True))
        self.settings_card = Card()
        settings.add(self.settings_card)
        self.settings_rows = QVBoxLayout()
        self.settings_rows.setSpacing(theme.ELEMENT_GAP)
        self.settings_card.add(self.settings_rows)
        self.reset = button(tr("恢复默认"), p.reset_settings)
        self.reset_row = QWidget()
        reset_layout = QHBoxLayout(self.reset_row)
        reset_layout.setContentsMargins(0, 0, 0, 0)
        reset_layout.addWidget(self.reset)
        reset_layout.addStretch(1)
        self.settings_card.add(self.reset_row)

        gestures = self.gestures_section = self.add_section()
        gestures.add(label(tr("手势绑定"), "section"))
        gestures.add(label(tr("识别到手势时触发插件动作。同一插件内，一个手势只能绑定一个动作。"), tone="muted", wrap=True))
        self.bindings_card = Card()
        gestures.add(self.bindings_card)
        self.binding_grid = QGridLayout()
        self.binding_grid.setHorizontalSpacing(theme.ELEMENT_GAP)
        self.binding_grid.setVerticalSpacing(theme.ELEMENT_GAP)
        self.binding_grid.setColumnStretch(0, 1)
        self.binding_grid.setColumnStretch(2, 1)
        self.bindings_card.add(self.binding_grid)
        self.add_binding = button(tr("添加绑定"), p.add_binding)
        self.bindings_card.add(row(self.add_binding))

        storage = self.storage_section = self.add_section()
        storage.add(label(tr("数据与存储"), "section"))
        storage.add(label(tr("路径由 Studio 分配，每个插件彼此隔离。"), tone="muted", wrap=True))
        storage_card = Card()
        storage.add(storage_card)
        self.storage_grid = QGridLayout()
        self.storage_grid.setHorizontalSpacing(theme.ELEMENT_GAP)
        self.storage_grid.setVerticalSpacing(theme.ELEMENT_GAP)
        self.storage_grid.setColumnStretch(1, 1)
        storage_card.add(self.storage_grid)
        self.open_directory = button(tr("打开数据目录"), lambda: p.preview_only(tr("打开数据目录")))
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

        self._detail_sections = (head, source, settings, gestures, storage, permissions, events)

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
        local.addLayout(field(tr("本地目录"), self.local_path))
        local.addWidget(label(tr("本地目录中的插件不会自动更新；修改代码后请重新加载。"), "caption", wrap=True))
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
        self.channel = Combo()
        self.channel.setFixedWidth(_CONTROL_WIDTH)
        self.channel.setAccessibleName(resolve(tr("更新通道")))
        for value, text in _CHANNELS:
            self.channel.addItem(resolve(text), value)
        self.channel.activated.connect(lambda index: p.set_channel(self.channel.itemData(index)))
        self.installed = label(selectable=True)
        self.last_checked = label()
        for index, (caption, widget) in enumerate(((tr("仓库地址"), self.repo_path), (tr("更新通道"), self.channel),
                                                   (tr("当前版本"), self.installed), (tr("上次检查"), self.last_checked))):
            grid.addWidget(label(caption), index, 0)
            grid.addWidget(widget, index, 1, Qt.AlignmentFlag.AlignLeft if widget is not self.repo_path
                           else Qt.AlignmentFlag(0))
        box.addLayout(grid)
        box.addWidget(hairline())
        auto = QHBoxLayout()
        auto.setSpacing(24)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(tr("自动更新")))
        text.addWidget(label(tr("发现新版本时在后台安装。新版本增加权限时会暂停，等你确认后才安装。"), "caption", wrap=True))
        auto.addLayout(text, 1)
        self.auto_update = Switch()
        self.auto_update.setAccessibleName(resolve(tr("自动更新")))
        self.auto_update.toggled.connect(p.set_auto_update)
        auto.addWidget(self.auto_update, 0, Qt.AlignmentFlag.AlignVCenter)
        box.addLayout(auto)
        box.addWidget(hairline())
        self.update_status = label(role="strong", wrap=True)
        self.update_busy = Busy(tr("正在检查更新…"))
        box.addWidget(self.update_status)
        box.addWidget(self.update_busy, 0, Qt.AlignmentFlag.AlignLeft)
        self.notes_title = label(role="caption")
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
        self.local_box.setVisible(github is None)
        self.github_box.setVisible(github is not None)
        if github is None:
            _set_path(self.local_path, plugin.source)
            return
        _set_path(self.repo_path, plugin.source)
        with QSignalBlocker(self.channel):
            self.channel.setCurrentIndex(self.channel.findData(github.channel))
        set_text(self.installed, f"v{plugin.version} ({github.commit})")
        set_text(self.last_checked, github.last_checked)
        self.auto_update.setChecked(github.auto_update)
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
        self._fill_events(plugin)
        self._bindings_key = None

    def _build_setting_rows(self, plugin):
        _clear(self.settings_rows)
        self._setting_controls = []
        if not plugin.settings:
            self.settings_rows.addWidget(label(tr("此插件没有设置项。"), tone="muted"))
        for index, spec in enumerate(plugin.settings):
            if index:
                self.settings_rows.addWidget(hairline())
            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(24)
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(label(spec.title))
            if spec.help:
                text.addWidget(label(spec.help, "caption", wrap=True))
            error = label(role="caption", tone="critical", wrap=True)
            error.hide()
            text.addWidget(error)
            layout.addLayout(text, 1)
            control = self._setting_control(spec)
            layout.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)
            self.settings_rows.addWidget(holder)
            self._setting_controls.append((spec, control, error))

    def _setting_control(self, spec):
        p = self.plugins
        if spec.kind == "switch":
            control = Switch()
            control.setAccessibleName(resolve(spec.title))
            control.toggled.connect(lambda checked: p.set_setting(spec.key, checked))
            return control
        if spec.kind == "choice":
            control = Combo()
            control.setFixedWidth(_CONTROL_WIDTH)
            control.setAccessibleName(resolve(spec.title))
            for value, text in spec.options:
                control.addItem(resolve(text), value)
            control.activated.connect(lambda index: p.set_setting(spec.key, control.itemData(index)))
            return control
        control = line_edit(on_edit=lambda value: p.set_setting(spec.key, value), width=_CONTROL_WIDTH)
        control.setAccessibleName(resolve(spec.title))
        if spec.kind == "secret":
            control.setEchoMode(QLineEdit.EchoMode.Password)
        return control

    def _build_storage_rows(self, plugin):
        _clear(self.storage_grid)
        self._size_labels = []
        for index, slot in enumerate(plugin.storage):
            self.storage_grid.addWidget(label(slot.title), index, 0)
            path = _path_field()
            _set_path(path, slot.path)
            path.setAccessibleName(resolve(slot.title))
            self.storage_grid.addWidget(path, index, 1)
            size = label(role="caption")
            size.setMinimumWidth(64)
            size.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.storage_grid.addWidget(size, index, 2)
            self._size_labels.append((slot, size))

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
            layout.addWidget(label(title, "strong", tone="warning" if permission.sensitive else None))
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

    def _render_bindings(self, plugin):
        p = self.plugins
        editable = p.can_edit(plugin)
        key = (plugin.id, tuple(plugin.bindings), editable)
        if key == self._bindings_key:
            return
        self._bindings_key = key
        _clear(self.binding_grid)
        if not plugin.bindings:
            self.binding_grid.addWidget(label(tr("还没有绑定手势。"), tone="muted"), 0, 0, 1, 4)
            return
        self.binding_grid.addWidget(label(tr("手势名称"), "caption"), 0, 0)
        self.binding_grid.addWidget(label(tr("触发动作"), "caption"), 0, 2)
        for index, (gesture, action) in enumerate(plugin.bindings):
            line = index + 1
            gesture_box = Combo()
            gesture_box.setAccessibleName(resolve(tr("手势名称")))
            gesture_box.addItems(p.free_gestures(plugin, index))  # User names; never translated.
            gesture_box.setCurrentText(gesture)
            gesture_box.setEnabled(editable)
            gesture_box.activated.connect(lambda _i, row=index, box=gesture_box: p.set_binding_gesture(row, box.currentText()))
            self.binding_grid.addWidget(gesture_box, line, 0)
            self.binding_grid.addWidget(label("→", tone="muted"), line, 1)
            action_box = Combo()
            action_box.setAccessibleName(resolve(tr("触发动作")))
            for item in plugin.actions:
                action_box.addItem(resolve(item.title), item.key)
            action_box.setCurrentIndex(action_box.findData(action))
            action_box.setEnabled(editable)
            action_box.activated.connect(lambda _i, row=index, box=action_box: p.set_binding_action(row, box.currentData()))
            self.binding_grid.addWidget(action_box, line, 2)
            remove = button(tr("移除"), lambda row=index: p.remove_binding(row), kind="link")
            remove.setEnabled(editable)
            self.binding_grid.addWidget(remove, line, 3)

    def _render_settings(self, plugin):
        editable = self.plugins.can_edit(plugin)
        for spec, control, error in self._setting_controls:
            value = plugin.values[spec.key]
            if spec.kind == "switch":
                control.setChecked(bool(value))
            elif spec.kind == "choice":
                with QSignalBlocker(control):
                    control.setCurrentIndex(control.findData(value))
            else:
                set_field_text(control, str(value))
            control.setEnabled(editable)
            message = self.plugins.setting_error(plugin, spec)
            error.setVisible(message is not None)
            if message is not None:
                set_text(error, message)
        self.reset_row.setVisible(bool(plugin.settings))
        self.reset.setEnabled(editable)

    def _render_detail(self, plugin):
        p = self.plugins
        if self._detail_for != plugin.id:
            self._detail_for = plugin.id
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
        for slot, size in self._size_labels:
            set_text(size, format_size(slot.size))
        self.clear_data.setEnabled(p.can_clear_data(plugin))

    # Rendering.

    def retranslate(self):
        # Combo items and table cells hold resolved text; rebuild them in the new language.
        self._detail_for = None
        self._permissions_shown = None
        with QSignalBlocker(self.channel):
            for index, (_value, text) in enumerate(_CHANNELS):
                self.channel.setItemText(index, resolve(text))

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
