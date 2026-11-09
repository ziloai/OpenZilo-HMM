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
from ..plugins_controller import format_size
from ..widgets import (
    Card, Combo, Page, StatTile, Switch, button, field, hairline, label, line_edit, row, set_field_text, set_text,
    set_tone, table,
)

_PREVIEW_NOTE = tr("界面预览：这里全部是示例数据，不会运行插件代码或写入磁盘，修改在关闭 Studio 后丢失。")
_CONTROL_WIDTH = 280


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
        self.summary = label(role="caption")
        self.add(self.summary)

    def render_state(self, plugins):
        plugin = self.plugin
        text, tone = plugins.status(plugin)
        set_text(self.status, text)
        set_tone(self.status, tone)
        set_text(self.meta, _meta(plugin))
        self.enabled.setChecked(plugin.enabled)
        self.enabled.setEnabled(plugins.can_toggle(plugin))
        set_text(self.summary, tr("手势绑定：{bindings} · 数据：{size}", bindings=len(plugin.bindings),
                                  size=format_size(plugin.data_size)))


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
        section.add(row(self.registered, self.enabled, self.bindings, self.total_size, spacing=64))
        self.register = button(tr("注册插件…"), p.begin_registration, kind="primary")
        section.add(row(self.register))
        section.add(self._build_candidate())
        self.empty = label(tr("还没有注册插件。"), tone="muted")
        section.add(self.empty)
        self.list_layout = QVBoxLayout()
        self.list_layout.setSpacing(theme.ELEMENT_GAP)
        section.add(self.list_layout)

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
        card.add(field(tr("插件目录"), self.candidate_source))
        self.candidate_data = _path_field()
        card.add(field(tr("Studio 将分配的数据目录"), self.candidate_data))
        self.candidate_permissions = label(wrap=True)
        card.add(field(tr("请求的权限"), self.candidate_permissions))
        card.add(label(tr("示例清单：尚未读取你选择的目录。"), "caption", tone="warning", wrap=True))
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
        self.register.setEnabled(p.can_register)
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
            _set_path(self.candidate_data, candidate.storage[0].path)
            set_text(self.candidate_permissions, resolve(tr("、")).join(resolve(item.title) for item in candidate.permissions))

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

        self._detail_sections = (head, settings, gestures, storage, permissions, events)

    def _rebuild_detail(self, plugin):
        self._build_setting_rows(plugin)
        self._build_storage_rows(plugin)
        self._build_permission_rows(plugin)
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
