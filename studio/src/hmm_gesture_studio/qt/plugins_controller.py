# SPDX-License-Identifier: MPL-2.0
"""Plugin page state and rules. UI preview: sample data, kept in memory only.

Main-thread only and holds no widgets. No plugin code runs and nothing is written
to disk, and GitHub is never contacted: a short timer stands in for the worker so
busy states can be seen. Any modal dialog runs a nested event loop, so state is
rechecked after it.
"""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import urlsplit

from PySide6.QtCore import QTimer

from ..device_history import history_path
from .i18n import resolve, tr
from .plugins_mock import MOCK_GESTURES, github_plugin_id, mock_candidate, mock_github_candidate, mock_plugins

MOCK_LATENCY_MS = 900
_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")


def parse_github_repo(text):
    """Return owner/name from owner/name or an https://github.com URL, else None."""
    text = text.strip()
    if "://" in text:
        try:
            parts = urlsplit(text)
        except ValueError:
            return None
        if parts.scheme != "https" or parts.netloc.lower() != "github.com" or parts.query or parts.fragment:
            return None
        text = parts.path
    elif text.lower().startswith("github.com/"):
        text = text[len("github.com/"):]
    pieces = text.strip("/").removesuffix(".git").split("/")
    if len(pieces) != 2 or not _OWNER.fullmatch(pieces[0]) or not _REPO.fullmatch(pieces[1]):
        return None
    return None if pieces[1] in (".", "..") else "/".join(pieces)


def join_titles(permissions):
    return resolve(tr("、")).join(resolve(item.title) for item in permissions)


def _after_delay(callback):
    QTimer.singleShot(MOCK_LATENCY_MS, callback)


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
        self.github_form_open = False
        self.github_input = ""
        self.github_error = None
        self.github_fetching = False
        self.checking = set()
        self.updating = set()
        self.schedule = _after_delay
        self._fetch_token = 0

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

    @property
    def update_count(self):
        return sum(plugin.github is not None and plugin.github.update is not None for plugin in self.plugins)

    def source_text(self, plugin):
        if plugin.github is None:
            return tr("本地目录")
        if plugin.github.auto_update:
            return tr("GitHub · {repo} · 自动更新", repo=plugin.github.repo)
        return tr("GitHub · {repo}", repo=plugin.github.repo)

    def update_busy_text(self, plugin):
        if plugin.id in self.updating:
            return tr("正在更新到 v{version}…", version=plugin.github.update.version)
        return tr("正在检查更新…") if plugin.id in self.checking else None

    def update_status(self, plugin):
        update = plugin.github.update
        if update is None:
            return tr("已是最新版本"), "good"
        if update.new_permissions and plugin.github.auto_update:
            return tr("v{version} 需要确认权限，自动更新已暂停", version=update.version), "warning"
        return tr("可更新到 v{version}", version=update.version), "warning"

    def update_badge(self, plugin):
        """List-row text; quiet when there is nothing to act on."""
        if plugin.github is None:
            return None
        busy = self.update_busy_text(plugin)
        if busy is not None:
            return busy
        return self.update_status(plugin)[0] if plugin.github.update is not None else None

    # Rules; pages read these rather than deciding for themselves.

    @property
    def can_register(self):
        return not self.app.closing and self.candidate is None and not self.github_form_open

    @property
    def can_fetch_github(self):
        return (not self.app.closing and self.github_form_open and not self.github_fetching
                and bool(self.github_input.strip()))

    def can_check(self, plugin):
        return (not self.app.closing and plugin.github is not None
                and plugin.id not in self.checking and plugin.id not in self.updating)

    @property
    def can_check_all(self):
        return any(self.can_check(plugin) for plugin in self.plugins)

    def can_install(self, plugin):
        return self.can_check(plugin) and plugin.github.update is not None

    def can_remove(self, plugin):
        return not self.app.closing and plugin.id not in self.updating

    def can_toggle(self, plugin):
        return not self.app.closing and not plugin.error and plugin.id not in self.updating

    def can_edit(self, plugin):
        """A plugin that failed to load keeps its declarations visible, read-only."""
        return not self.app.closing and not plugin.error and plugin.id not in self.updating

    def free_gestures(self, plugin, index=None):
        """Each gesture triggers at most one action per plugin."""
        taken = {gesture for row, (gesture, _action) in enumerate(plugin.bindings) if row != index}
        return [gesture for gesture in self.gestures if gesture not in taken]

    def can_add_binding(self, plugin):
        return self.can_edit(plugin) and bool(plugin.actions) and bool(self.free_gestures(plugin))

    def can_clear_data(self, plugin):
        return self.can_remove(plugin) and any(slot.kind in ("data", "cache") and slot.size for slot in plugin.storage)

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

    def open_github_form(self):
        if not self.can_register:
            return
        self.github_form_open = True
        self.github_input = ""
        self.github_error = None
        self.app.notify()

    def close_github_form(self):
        self._fetch_token += 1  # An in-flight fetch must not reopen anything.
        self.github_form_open = False
        self.github_fetching = False
        self.github_error = None
        self.app.notify()

    def set_github_input(self, value):
        self.github_input = value
        self.github_error = None
        self.app.notify()

    def fetch_github(self):
        if not self.can_fetch_github:
            return
        repo = parse_github_repo(self.github_input)
        if repo is None:
            self.github_error = tr("请输入 owner/repo 或 https://github.com/owner/repo。")
        elif self.plugin(github_plugin_id(repo)) is not None:
            self.github_error = tr("{repo} 已添加。", repo=repo)
        else:
            self._fetch_token += 1
            token = self._fetch_token
            self.github_fetching = True
            self.schedule(lambda: self._finish_fetch(token, repo))
        self.app.notify()

    def _finish_fetch(self, token, repo):
        if token != self._fetch_token or self.app.closing:
            return
        self.github_form_open = False
        self.github_fetching = False
        self.candidate = mock_github_candidate(self.root, repo)
        self.app.notify()

    def set_candidate_auto_update(self, enabled):
        if self.candidate is not None and self.candidate.github is not None:
            self.candidate.github.auto_update = bool(enabled)
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

    def set_auto_update(self, enabled):
        plugin = self.selected
        if plugin is not None and plugin.github is not None and not self.app.closing:
            plugin.github.auto_update = bool(enabled)
        self.app.notify()

    def set_channel(self, channel):
        plugin = self.selected
        if plugin is not None and plugin.github is not None and not self.app.closing:
            plugin.github.channel = channel
        self.app.notify()

    def check_updates(self, plugin_id=None):
        """Check one plugin, or every GitHub plugin that is free to check."""
        candidates = self.plugins if plugin_id is None else [self.plugin(plugin_id)]
        ids = {plugin.id for plugin in candidates if plugin is not None and self.can_check(plugin)}
        if ids:
            self.checking |= ids
            self.schedule(lambda: self._finish_check(ids))
        self.app.notify()

    def _finish_check(self, ids):
        self.checking -= ids
        for plugin_id in ids:
            plugin = self.plugin(plugin_id)
            if plugin is not None:
                plugin.github.last_checked = tr("刚刚")
        self.app.log(tr("预览：已模拟检查更新，未访问 GitHub。"))
        self.app.notify()

    def install_update(self, plugin_id):
        plugin = self.plugin(plugin_id)
        if plugin is None or not self.can_install(plugin):
            return
        release = plugin.github.update
        if release.new_permissions and not self.app.dialogs.ask(
                tr("确认新版本权限？"),
                tr("{name} v{version} 新增权限：{permissions}。同意后才会安装这次更新。",
                   name=plugin.name, version=release.version, permissions=join_titles(release.new_permissions))):
            return
        if (self.plugin(plugin_id) is not plugin or not self.can_install(plugin)
                or plugin.github.update is not release):  # Recheck after the dialog.
            return
        self.updating.add(plugin_id)
        self.schedule(lambda: self._finish_update(plugin_id, release))
        self.app.notify()

    def _finish_update(self, plugin_id, release):
        self.updating.discard(plugin_id)
        plugin = self.plugin(plugin_id)
        if plugin is not None:
            plugin.version = release.version
            plugin.github.commit = release.commit
            plugin.github.update = None
            plugin.github.last_checked = tr("刚刚")
            plugin.permissions = (*plugin.permissions, *release.new_permissions)
            self.app.log(tr("预览：已模拟更新 {name} 到 v{version}，未下载任何内容。",
                            name=plugin.name, version=release.version))
        self.app.notify()

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
        if plugin is None or not self.can_remove(plugin):
            return
        if not self.app.dialogs.ask(tr("移除插件？"),
                                    tr("将取消注册 {name}，并删除 Studio 分配给它的目录（{size}）。此操作无法撤销。",
                                       name=plugin.name, size=format_size(plugin.data_size)), danger=True):
            return
        if self.plugin(plugin.id) is not plugin or not self.can_remove(plugin):  # Recheck after the dialog.
            return
        self.plugins.remove(plugin)
        if self.selected_id == plugin.id:
            self.selected_id = None
        self.app.log(tr("预览：已移除示例插件 {name}，未改动磁盘。", name=plugin.name))
        self.app.notify()

    def preview_only(self, action):
        """For controls whose effect needs the plugin runtime or real directories."""
        self.app.log(tr("预览：尚未实现“{action}”，未执行任何操作。", action=action))
