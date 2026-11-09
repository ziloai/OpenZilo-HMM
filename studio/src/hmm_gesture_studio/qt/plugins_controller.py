# SPDX-License-Identifier: MPL-2.0
"""Plugin page state and rules. UI preview: sample data, kept in memory only.

Main-thread only and holds no widgets. No plugin code runs and nothing is written
to disk. Any modal dialog runs a nested event loop, so state is rechecked after it.
"""
from __future__ import annotations

from pathlib import Path

from ..device_history import history_path
from .i18n import tr
from .plugins_mock import MOCK_GESTURES, mock_candidate, mock_plugins


def format_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class PluginsController:
    def __init__(self, app, *, root=None):
        self.app = app
        self.root = Path(root) if root is not None else history_path().parent / "plugins"
        self.plugins = mock_plugins(self.root)
        self.gestures = MOCK_GESTURES
        self.selected_id = None
        self.candidate = None

    # Lookups.

    def plugin(self, plugin_id):
        return next((plugin for plugin in self.plugins if plugin.id == plugin_id), None)

    @property
    def selected(self):
        return self.plugin(self.selected_id)

    @property
    def enabled_count(self):
        return sum(plugin.enabled for plugin in self.plugins)

    @property
    def binding_count(self):
        return sum(len(plugin.bindings) for plugin in self.plugins)

    @property
    def total_size(self):
        return sum(plugin.data_size for plugin in self.plugins)

    # Rules; pages read these rather than deciding for themselves.

    @property
    def can_register(self):
        return not self.app.closing and self.candidate is None

    def can_toggle(self, plugin):
        return not self.app.closing and not plugin.error

    def can_edit(self, plugin):
        """A plugin that failed to load keeps its declarations visible, read-only."""
        return not self.app.closing and not plugin.error

    def free_gestures(self, plugin, index=None):
        """Each gesture triggers at most one action per plugin."""
        taken = {gesture for row, (gesture, _action) in enumerate(plugin.bindings) if row != index}
        return [gesture for gesture in self.gestures if gesture not in taken]

    def can_add_binding(self, plugin):
        return self.can_edit(plugin) and bool(plugin.actions) and bool(self.free_gestures(plugin))

    def can_clear_data(self, plugin):
        return not self.app.closing and any(slot.kind in ("data", "cache") and slot.size for slot in plugin.storage)

    def status(self, plugin):
        if plugin.error:
            return tr("加载失败"), "critical"
        return (tr("已启用"), "good") if plugin.enabled else (tr("已停用"), "muted")

    def setting_error(self, plugin, spec):
        if spec.kind != "number":
            return None
        try:
            value = float(plugin.values[spec.key])
        except (TypeError, ValueError):
            value = None
        if value is None or not spec.minimum <= value <= spec.maximum:
            return tr("请输入 {minimum} 到 {maximum} 之间的数值。", minimum=f"{spec.minimum:g}", maximum=f"{spec.maximum:g}")
        return None

    # Navigation.

    def open_plugin(self, plugin_id):
        if self.plugin(plugin_id) is not None:
            self.selected_id = plugin_id
            self.app.notify()

    def close_plugin(self):
        self.selected_id = None
        self.app.notify()

    # Registration.

    def begin_registration(self):
        if not self.can_register:
            return
        source = self.app.dialogs.directory(str(Path.home()))
        if not source or not self.can_register:  # The picker runs a nested event loop.
            return
        candidate = mock_candidate(self.root, source)
        if self.plugin(candidate.id) is not None:
            self.app.error(tr("插件 {plugin_id} 已注册。", plugin_id=candidate.id))
            return
        self.candidate = candidate
        self.app.notify()

    def cancel_registration(self):
        self.candidate = None
        self.app.notify()

    def confirm_registration(self):
        candidate = self.candidate
        if candidate is None or self.app.closing:
            return
        self.candidate = None
        self.plugins.append(candidate)
        self.selected_id = candidate.id
        self.app.log(tr("预览：已添加示例插件 {name}，未写入磁盘。", name=candidate.name))
        self.app.notify()

    # Changes to one plugin. Every path notifies, so a rejected toggle snaps back.

    def set_enabled(self, plugin_id, enabled):
        plugin = self.plugin(plugin_id)
        if plugin is not None and self.can_toggle(plugin):
            plugin.enabled = bool(enabled)
        self.app.notify()

    def set_setting(self, key, value):
        plugin = self.selected
        if plugin is not None and self.can_edit(plugin) and key in plugin.values:
            plugin.values[key] = value
        self.app.notify()

    def reset_settings(self):
        plugin = self.selected
        if plugin is not None and self.can_edit(plugin):
            plugin.values = {spec.key: spec.default for spec in plugin.settings}
        self.app.notify()

    def add_binding(self):
        plugin = self.selected
        if plugin is not None and self.can_add_binding(plugin):
            plugin.bindings.append((self.free_gestures(plugin)[0], plugin.actions[0].key))
        self.app.notify()

    def set_binding_gesture(self, index, gesture):
        plugin = self.selected
        if plugin is not None and self.can_edit(plugin) and gesture in self.free_gestures(plugin, index):
            plugin.bindings[index] = (gesture, plugin.bindings[index][1])
        self.app.notify()

    def set_binding_action(self, index, action):
        plugin = self.selected
        if plugin is not None and self.can_edit(plugin) and action in {item.key for item in plugin.actions}:
            plugin.bindings[index] = (plugin.bindings[index][0], action)
        self.app.notify()

    def remove_binding(self, index):
        plugin = self.selected
        if plugin is not None and self.can_edit(plugin):
            del plugin.bindings[index]
        self.app.notify()

    def clear_data(self):
        plugin = self.selected
        if plugin is None or not self.can_clear_data(plugin):
            return
        if not self.app.dialogs.ask(tr("清除插件数据？"),
                                    tr("将清空 {name} 的数据目录和缓存目录，设置和手势绑定不受影响。此操作无法撤销。",
                                       name=plugin.name), danger=True):
            return
        if self.plugin(plugin.id) is not plugin or not self.can_clear_data(plugin):  # Recheck after the dialog.
            return
        for slot in plugin.storage:
            if slot.kind in ("data", "cache"):
                slot.size = 0
        self.app.log(tr("预览：已清空 {name} 的示例数据，未改动磁盘。", name=plugin.name))
        self.app.notify()

    def remove_plugin(self):
        plugin = self.selected
        if plugin is None or self.app.closing:
            return
        if not self.app.dialogs.ask(tr("移除插件？"),
                                    tr("将取消注册 {name}，并删除 Studio 分配给它的目录（{size}）。此操作无法撤销。",
                                       name=plugin.name, size=format_size(plugin.data_size)), danger=True):
            return
        if self.plugin(plugin.id) is not plugin or self.app.closing:  # Recheck after the dialog.
            return
        self.plugins.remove(plugin)
        if self.selected_id == plugin.id:
            self.selected_id = None
        self.app.log(tr("预览：已移除示例插件 {name}，未改动磁盘。", name=plugin.name))
        self.app.notify()

    def preview_only(self, action):
        """For controls whose effect needs the plugin runtime or real directories."""
        self.app.log(tr("预览：尚未实现“{action}”，未执行任何操作。", action=action))
