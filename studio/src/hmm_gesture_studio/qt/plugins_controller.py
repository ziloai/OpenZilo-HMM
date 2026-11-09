# SPDX-License-Identifier: MPL-2.0
"""Main-thread plugin orchestration. Python plugins are trusted code, NOT a sandbox.

Workers only enqueue results. Installation, imports and dependency resolution never
run on the Qt thread. No network request or plugin import occurs in construction.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4
import wave

from ..device_history import history_path
from ..plugins.installer import Installer, dependencies_ready, install_dependencies, python_executable
from ..plugins.manifest import validate_values
from ..plugins.models import Binding, PluginEvent, Release, assigned_storage
from ..plugins.runtime import Runtime
from ..plugins.storage import PluginStore
from .i18n import resolve, tr

_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_TRUST = tr("此插件将以你的账户权限执行 Python，可读写文件、联网和运行程序。独立进程不是安全沙箱，声明的权限不能阻止恶意代码。仅运行你信任的插件。")


def parse_github_repo(text):
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


def format_size(size):
    if size is None:
        return resolve(tr("未知"))
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def model_fingerprint(bundle):
    """Hash inference content, not filesystem location or training timestamps."""
    if bundle is None:
        return ""
    from hmm_gesture.bundle import _model_arrays
    content = dict(pipeline=asdict(bundle.pipeline), segmentation=asdict(bundle.segmentation),
                   gestures=[(name, {key: value.tolist() for key, value in _model_arrays(model).items()})
                             for name, model in bundle.models.items()],
                   rejection={name: asdict(value) for name, value in bundle.rejection.items()}
                   if bundle.rejection is not None else None)
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class PluginsController:
    def __init__(self, app, *, root=None, store=None, installer=None, runtime=None):
        self.app = app
        self.root = Path(root) if root is not None else history_path().parent / "plugins"
        self.store = store or PluginStore(self.root)
        self.installer = installer or Installer(self.store)
        self.runtime = runtime or Runtime(app.events)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plugin-install")
        self._cancel = threading.Event()
        self.plugins = self.store.discover()
        self._saved_values = {p.id: dict(p.values) for p in self.plugins}
        try:
            self.preferences = self.store.load_preferences()
        except (OSError, ValueError):
            self.preferences = {}
            self.store.warnings.append("Invalid plugin preferences; automatic startup disabled.")
        self.autostart = bool(self.preferences.get("autostart", False))
        self.service_running = False
        self.gesture_control = False
        self.model_fingerprint = ""
        self.gestures = ()
        self.selected_id = None
        self.candidate = None
        self.github_form_open = False
        self.github_input = ""
        self.github_error = None
        self.github_fetching = False
        self.checking = set()
        self.updating = set()
        self.busy_text = None
        self._job_token = 0
        self._job_callback = None
        self._fetch_token = 0
        self._updates = {}
        self._cooldowns = {}
        self.recording_session = None
        self._after_recording = None
        self._warning_index = 0
        self._secret_history = set()

    def start(self):
        self._warnings()
        if self.autostart:
            self.set_service_running(True)

    def _warnings(self):
        warnings = getattr(self.store, "warnings", [])
        for message in warnings[self._warning_index:]:
            self.app.log(tr("插件存储提示：{message}", message=message), "warning")
        self._warning_index = len(warnings)

    def plugin(self, plugin_id):
        return next((p for p in self.plugins if p.id == plugin_id), None)

    @property
    def selected(self):
        return self.plugin(self.selected_id)

    @property
    def enabled_count(self):
        return sum(p.enabled for p in self.plugins)

    @property
    def binding_count(self):
        return sum(len(p.bindings) for p in self.plugins)

    @property
    def total_size(self):
        sizes = [p.data_size for p in self.plugins]
        return sum(sizes) if all(size is not None for size in sizes) else None

    @property
    def update_count(self):
        return len(self._updates)

    @property
    def can_register(self):
        return not self.app.closing and self.busy_text is None and self.candidate is None and not self.github_form_open

    @property
    def can_fetch_github(self):
        return not self.app.closing and self.busy_text is None and self.github_form_open and bool(self.github_input.strip())

    @property
    def can_control_service(self):
        return not self.app.closing and self.busy_text is None and self._after_recording is None

    @property
    def can_set_autostart(self):
        return not self.app.closing

    def can_reload(self, plugin):
        return self.can_toggle(plugin) and self.service_running

    def can_copy_bindings(self, plugin):
        return self.can_add_binding(plugin) and any(b.model != self.model_fingerprint and b.gesture in self.gestures
                                                   for b in plugin.bindings)

    def can_process_audio_file(self, plugin):
        return self.can_test(plugin) and "recording.completed" in plugin.subscriptions

    def can_open_data_directory(self, plugin):
        return not self.app.closing and (self.root / plugin.id / "data").is_dir()

    def can_edit(self, plugin):
        return not self.app.closing and self.busy_text is None and plugin.id not in self.updating

    def can_toggle(self, plugin):
        return self.can_edit(plugin) and (not plugin.platforms or sys.platform in plugin.platforms)

    def can_remove(self, plugin):
        return self.can_edit(plugin)

    def can_check(self, plugin):
        return self.can_edit(plugin) and plugin.github is not None and plugin.id not in self.checking

    @property
    def can_check_all(self):
        return any(self.can_check(p) for p in self.plugins)

    def can_install(self, plugin):
        return self.can_check(plugin) and plugin.id in self._updates

    def can_dependencies(self, plugin):
        return self.can_toggle(plugin) and not self.runtime.running(plugin.id)

    def can_test(self, plugin):
        return (not self.app.closing and plugin.id not in self.updating
                and self.service_running and plugin.enabled and self.runtime.running(plugin.id))

    def can_recording_events(self, plugin):
        return self.can_edit(plugin) and "recording.completed" in plugin.subscriptions

    def can_clear_data(self, plugin):
        return self.can_edit(plugin) and not self.runtime.running(plugin.id)

    def source_text(self, plugin):
        return tr("GitHub · {repo}", repo=plugin.github.repo) if plugin.github else tr("本地快照")

    def status(self, plugin):
        if plugin.error:
            return tr("运行失败"), "critical"
        if plugin.platforms and sys.platform not in plugin.platforms:
            return tr("当前平台不支持"), "warning"
        if self.runtime.running(plugin.id):
            return tr("正在运行"), "good"
        return (tr("已启用 · 尚未运行"), "warning") if plugin.enabled else (tr("已停用"), "muted")

    def update_busy_text(self, plugin):
        if plugin.id in self.updating or plugin.id in self.checking:
            return self.busy_text
        return None

    def update_status(self, plugin):
        if plugin.github.update:
            return tr("可更新到 v{version}", version=plugin.github.update.version), "warning"
        return tr("尚无待安装更新"), "muted"

    def update_badge(self, plugin):
        return self.update_busy_text(plugin) or (self.update_status(plugin)[0] if plugin.github and plugin.github.update else None)

    def _redact(self, message):
        text = str(message)
        for plugin in self.plugins:
            for spec in plugin.settings:
                if spec.kind == "secret":
                    for values in (plugin.values, self._saved_values.get(plugin.id, {})):
                        value = values.get(spec.key)
                        if value:
                            text = text.replace(str(value), "[REDACTED]")
            actions = {a.key: a for a in plugin.actions}
            for binding in plugin.bindings:
                action = actions.get(binding.action)
                for spec in action.parameters if action else ():
                    value = binding.parameters.get(spec.key)
                    if spec.kind == "secret" and value:
                        text = text.replace(str(value), "[REDACTED]")
        for secret in self._secret_history:
            text = text.replace(secret, "[REDACTED]")
        return text[:8000]

    def _remember_secrets(self, plugin, parameters=None, action=None):
        values = self._saved_values.get(plugin.id, {})
        for spec in plugin.settings:
            if spec.kind == "secret" and values.get(spec.key):
                self._secret_history.add(str(values[spec.key]))
        for spec in action.parameters if action else ():
            if spec.kind == "secret" and parameters.get(spec.key):
                self._secret_history.add(str(parameters[spec.key]))

    def _error(self, exc):
        self.app.error(tr("插件操作失败：{error}", error=self._redact(exc)))

    def _persist(self, plugin):
        # Non-settings operations must not implicitly commit unfinished form edits.
        draft = plugin.values
        plugin.values = self._saved_values.get(plugin.id, draft)
        try:
            self.store.save(plugin)
            self._warnings()
            return True
        except Exception as exc:
            self._error(exc)
            return False
        finally:
            plugin.values = draft

    def _job(self, message, operation, done):
        if self.busy_text is not None or self.app.closing:
            return False
        self._job_token += 1
        token = self._job_token
        self.busy_text, self._job_callback = message, done
        events = self.app.events
        def work():
            try:
                if self._cancel.is_set():
                    return
                result = operation()
                if self._cancel.is_set():
                    candidates = [item[1] for item in result] if isinstance(result, list) else [result]
                    for candidate in candidates:
                        self.installer.cleanup(candidate)
                    return
                events.put(("plugin_job", dict(token=token, result=result, error=None)))
            except Exception as exc:
                events.put(("plugin_job", dict(token=token, result=None, error=str(exc))))
        self.executor.submit(work)
        self.app.notify()
        return True

    def handle_job(self, payload):
        if payload["token"] != self._job_token or self.app.closing:
            return
        callback = self._job_callback
        self.busy_text = self._job_callback = None
        self.checking.clear()
        self.updating.clear()
        self.github_fetching = False
        if payload["error"]:
            self._error(payload["error"])
        elif callback:
            try:
                callback(payload["result"])
            except Exception as exc:
                self._error(exc)
        self.app.notify()

    def open_plugin(self, plugin_id):
        if self.plugin(plugin_id):
            self.selected_id = plugin_id
            self.app.notify()

    def close_plugin(self):
        self.selected_id = None
        self.app.notify()

    def begin_registration(self):
        if not self.can_register:
            return
        source = self.app.dialogs.directory(str(Path.home()))
        if source and self.can_register:
            self._prepare_local(source)

    def begin_zip_registration(self):
        if not self.can_register:
            return
        source = self.app.dialogs.open_file(tr("插件 ZIP (*.zip)"))
        if source and self.can_register:
            self._prepare_local(source)

    def _prepare_local(self, source):
        self._job(tr("正在读取插件快照…"), lambda: self.installer.prepare_local(Path(source)), self._candidate_ready)

    def _candidate_ready(self, candidate):
        if self.plugin(candidate.id):
            self.installer.cleanup(candidate)
            raise ValueError(tr("插件 {plugin_id} 已注册。", plugin_id=candidate.id))
        self.github_form_open = False
        candidate.storage = assigned_storage(self.root, candidate.id)
        self.candidate = candidate

    def open_github_form(self):
        if self.can_register:
            self.github_form_open = True
            self.github_input = ""
            self.github_error = None
            self.app.notify()

    def close_github_form(self):
        self._fetch_token += 1
        self.github_form_open = False
        self.github_error = None
        self.app.notify()

    def set_github_input(self, value):
        self.github_input, self.github_error = value, None
        self.app.notify()

    def fetch_github(self):
        if not self.can_fetch_github:
            return
        repo = parse_github_repo(self.github_input)
        if repo is None:
            self.github_error = tr("请输入 owner/repo 或 https://github.com/owner/repo。")
            self.app.notify()
            return
        self._fetch_token += 1
        token = self._fetch_token
        def done(candidate):
            if token != self._fetch_token:
                self.installer.cleanup(candidate)
                return
            self._candidate_ready(candidate)
        self.github_fetching = True
        self._job(tr("正在下载固定版本并读取清单…"), lambda: self.installer.prepare_github(repo), done)

    def cancel_registration(self):
        if self.candidate is not None and self.busy_text is None:
            self.installer.cleanup(self.candidate)
            self.candidate = None
        self.app.notify()

    def confirm_registration(self):
        candidate = self.candidate
        if candidate is None or self.app.closing or self.busy_text:
            return
        if not self.app.dialogs.ask(tr("安装第三方插件？"), _TRUST):
            return
        if self.candidate is not candidate or self.app.closing or self.busy_text or self.plugin(candidate.id):
            return
        def done(plugin):
            self.candidate = None
            self.plugins.append(plugin)
            self._saved_values[plugin.id] = dict(plugin.values)
            self.selected_id = plugin.id
            self.app.log(tr("已安装 {name}；请显式准备运行环境后启用。", name=plugin.name))
        self._job(tr("正在安装插件…"), lambda: self.installer.commit(candidate), done)

    def check_updates(self, plugin_id=None):
        candidates = self.plugins if plugin_id is None else [self.plugin(plugin_id)]
        candidates = [p for p in candidates if p is not None and self.can_check(p)]
        if not candidates:
            return
        self.checking = {p.id for p in candidates}
        def work():
            results = []
            try:
                for p in candidates:
                    results.append((p.id, self.installer.prepare_github(p.github.repo)))
                return results
            except Exception:
                for _, candidate in results:
                    self.installer.cleanup(candidate)
                raise
        def done(results):
            for plugin_id, candidate in results:
                plugin = self.plugin(plugin_id)
                if plugin is None or candidate.id != plugin_id:
                    self.installer.cleanup(candidate)
                    self.app.log(tr("更新清单的插件标识不匹配，已拒绝。"), "warning")
                    continue
                plugin.github.last_checked = datetime.now().isoformat(timespec="seconds")
                if candidate.github.commit == plugin.github.commit:
                    self.installer.cleanup(candidate)
                    continue
                old = self._updates.pop(plugin_id, None)
                if old:
                    self.installer.cleanup(old)
                known = {p.key for p in plugin.permissions}
                new = tuple(p for p in candidate.permissions if p.key not in known)
                plugin.github.update = Release(candidate.version, candidate.github.commit,
                                               tr("安装后插件将暂停，确认信任新代码后才能重新启用。"), new)
                self._updates[plugin_id] = candidate
                self._persist(plugin)
        self._job(tr("正在检查并准备更新…"), work, done)

    def install_update(self, plugin_id):
        plugin = self.plugin(plugin_id)
        if plugin is None or not self.can_install(plugin):
            return
        candidate = self._updates[plugin_id]
        release = plugin.github.update
        if release.new_permissions and not self.app.dialogs.ask(
                tr("确认新版本权限？"), tr("新增用途：{permissions}", permissions=join_titles(release.new_permissions))):
            return
        if self.plugin(plugin_id) is not plugin or not self.can_install(plugin) or self._updates.get(plugin_id) is not candidate:
            return
        if not self.app.dialogs.ask(tr("安装更新并停用插件？"), tr("{warning}\n依赖：{requirements}\n将创建独立环境，必要时从 Python 包索引下载固定版本 wheel。",
                warning=_TRUST, requirements=", ".join(candidate.requirements) or resolve(tr("无第三方依赖")))):
            return
        if self.plugin(plugin_id) is not plugin or not self.can_install(plugin) or self._updates.get(plugin_id) is not candidate:
            return
        if self._defer_recording(plugin_id, lambda: self.install_update(plugin_id)):
            return
        self.runtime.stop(plugin_id, "update")
        self.updating.add(plugin_id)
        def done(updated):
            index = self.plugins.index(plugin)
            self.plugins[index] = updated
            self._saved_values[plugin_id] = dict(updated.values)
            self._updates.pop(plugin_id, None)
            self.app.log(tr("更新已安装；请准备依赖并确认后重新启用。"))
        events = self.app.events
        def work():
            self.installer.prepare_environment(candidate, progress_callback=lambda line: events.put(
                ("plugin", dict(plugin_id=plugin_id, kind="log", message=str(line), level="info"))),
                cancel_event=self._cancel)
            if self._cancel.is_set():
                raise RuntimeError("Installation cancelled")
            return self.installer.commit(candidate, replace=True)
        self._job(tr("正在安装更新…"), work, done)

    def install_dependencies(self):
        plugin = self.selected
        if plugin is None or not self.can_dependencies(plugin):
            return
        if not self.app.dialogs.ask(tr("准备插件运行环境？"), tr("{warning}\n依赖：{requirements}\n将创建独立环境，必要时从 Python 包索引下载固定版本 wheel。",
                warning=_TRUST, requirements=", ".join(plugin.requirements) or resolve(tr("无第三方依赖")))):
            return
        if self.selected is not plugin or not self.can_dependencies(plugin):
            return
        events, plugin_id = self.app.events, plugin.id
        def progress(message):
            events.put(("plugin", dict(plugin_id=plugin_id, kind="log", message=str(message), level="info")))
        def done(_):
            plugin.error = ""
            self.app.log(tr("插件运行环境已准备完成。"))
        self._job(tr("正在准备独立运行环境…"), lambda: install_dependencies(plugin, progress_callback=progress, cancel_event=self._cancel), done)

    def _start_plugin(self, plugin):
        executable = python_executable(plugin)
        if not dependencies_ready(plugin):
            raise ValueError(tr("请先点击“准备运行环境”，不会在启动时自动安装依赖。"))
        if plugin.platforms and sys.platform not in plugin.platforms:
            raise ValueError(tr("当前平台不支持此插件。"))
        values = self.store.load_settings(plugin)
        self._saved_values[plugin.id] = validate_values(plugin.settings, values)
        self._remember_secrets(plugin)
        self.runtime.start(plugin.id, executable, plugin.directory, plugin.entry,
                           self.root / plugin.id / "data", self.root / plugin.id / "cache")
        plugin.error = ""

    def set_service_running(self, enabled):
        if self.app.closing or self.busy_text:
            self.app.notify()
            return
        enabled = bool(enabled)
        if not enabled and self.recording_session and self._defer_recording(
                self.recording_session["owner"], lambda: self.set_service_running(False)):
            return
        self.service_running = enabled
        if enabled:
            for plugin in self.plugins:
                if plugin.enabled and not self.runtime.running(plugin.id):
                    try:
                        self._start_plugin(plugin)
                    except Exception as exc:
                        plugin.error = self._redact(exc)
                        self.app.log(tr("插件 {name} 未启动：{error}", name=plugin.name, error=plugin.error), "error")
        else:
            self.disarm()
            for plugin in self.plugins:
                self.runtime.stop(plugin.id, "service stopped")
        self.app.notify()

    def set_autostart(self, enabled):
        if self.app.closing:
            return
        if enabled and not self.app.dialogs.ask(tr("启动时加载第三方代码？"), _TRUST):
            self.app.notify()
            return
        if self.app.closing:
            return
        old = self.autostart
        self.autostart = bool(enabled)
        try:
            self.store.save_preferences(dict(self.preferences, autostart=self.autostart))
        except Exception as exc:
            self.autostart = old
            self._error(exc)
        self.app.notify()

    @property
    def can_control_gestures(self):
        return (self.service_running and not self.app.closing and bool(self.model_fingerprint)
                and self.app.live and self.app.recognizer is not None and not self.app.stale
                and self.app.take is None and not self.app.training and not self.app.tools.busy)

    def set_gesture_control(self, enabled):
        if enabled and not self.can_control_gestures:
            self._error(tr("请先启动插件服务，并连接设备开启有效模型的实时识别。"))
        else:
            self.gesture_control = bool(enabled)
            if not enabled:
                self.runtime.cancel_pending(event_type="gesture")
        self.app.notify()

    def disarm(self):
        self.gesture_control = False
        self._cooldowns.clear()
        self.runtime.cancel_pending(event_type="gesture")

    def set_model(self, bundle):
        self.disarm()
        try:
            self.model_fingerprint = model_fingerprint(bundle)
        except (TypeError, ValueError, AttributeError) as exc:
            self.model_fingerprint = ""
            self.app.log(tr("无法确定模型指纹，插件手势控制不可用：{error}", error=str(exc)), "warning")
        self.gestures = tuple(bundle.gesture_names) if bundle is not None and self.model_fingerprint else ()

    def set_enabled(self, plugin_id, enabled):
        plugin = self.plugin(plugin_id)
        if plugin is None or not self.can_toggle(plugin):
            self.app.notify()
            return
        if enabled and not plugin.enabled:
            if not self.app.dialogs.ask(tr("信任并启用插件？"), _TRUST):
                self.app.notify()
                return
            if self.plugin(plugin_id) is not plugin or not self.can_toggle(plugin):
                return
        if not enabled and self._defer_recording(plugin_id, lambda: self.set_enabled(plugin_id, False)):
            return
        old = plugin.enabled
        plugin.enabled = bool(enabled)
        if not self._persist(plugin):
            plugin.enabled = old
            self.app.notify()
            return
        try:
            if enabled and self.service_running and not self.runtime.running(plugin_id):
                self._start_plugin(plugin)
            elif not enabled:
                self.runtime.stop(plugin_id, "disabled")
        except Exception as exc:
            plugin.error = self._redact(exc)
            self._error(exc)
        self.app.notify()

    def reload_plugin(self):
        plugin = self.selected
        if plugin is None or not self.can_toggle(plugin):
            return
        if self._defer_recording(plugin.id, lambda: self.reload_plugin() if self.selected is plugin else None):
            return
        self.runtime.stop(plugin.id, "reload")
        plugin.error = ""
        if self.service_running and plugin.enabled:
            try:
                self._start_plugin(plugin)
            except Exception as exc:
                plugin.error = self._redact(exc)
                self._error(exc)
        self.app.notify()

    def open_data_directory(self):
        if self.selected is not None:
            self.app.open_path(self.root / self.selected.id / "data")

    @property
    def settings_dirty(self):
        return bool(self.selected and self.selected.values != self._saved_values.get(self.selected.id, {}))

    @property
    def can_save_settings(self):
        return bool(self.selected and self.can_edit(self.selected) and self.settings_dirty
                    and not any(self.setting_error(self.selected, s) for s in self.selected.settings))

    @staticmethod
    def setting_visible(plugin, spec):
        return all(plugin.values.get(k) == v for k, v in spec.visible_when.items())

    @staticmethod
    def parameter_visible(binding, spec):
        return all(binding.parameters.get(k) == v for k, v in spec.visible_when.items())

    @staticmethod
    def _value_error(spec, value):
        try:
            validate_values((spec,), {spec.key: value})
        except (ValueError, TypeError) as exc:
            return tr("配置无效：{error}", error=str(exc))
        return None

    def setting_error(self, plugin, spec):
        if not self.setting_visible(plugin, spec):
            spec = replace(spec, required=False)
        return self._value_error(spec, plugin.values.get(spec.key))

    def set_setting(self, key, value):
        plugin = self.selected
        if plugin is not None and self.can_edit(plugin) and key in plugin.values:
            plugin.values[key] = value
        self.app.notify()

    def save_settings(self):
        plugin = self.selected
        if plugin is None or not self.can_save_settings:
            return
        old = self._saved_values.get(plugin.id, {})
        try:
            values = validate_values(plugin.settings, plugin.values)
            self._saved_values[plugin.id] = values
            if not self._persist(plugin):
                self._saved_values[plugin.id] = old
            else:
                plugin.values = dict(values)
        except Exception as exc:
            self._saved_values[plugin.id] = old
            self._error(exc)
        self.app.notify()

    def reset_settings(self):
        if self.selected is not None and self.can_edit(self.selected):
            self.selected.values = {s.key: s.default for s in self.selected.settings}
        self.app.notify()

    def free_gestures(self, plugin, index=None):
        return list(self.gestures)

    def can_add_binding(self, plugin):
        return self.can_edit(plugin) and bool(plugin.actions) and bool(self.gestures)

    def binding_visible(self, binding):
        return binding.model == self.model_fingerprint and binding.gesture in self.gestures

    def add_binding(self):
        plugin = self.selected
        if plugin is not None and self.can_add_binding(plugin):
            gesture = self.gestures[0]
            if any(b.gesture == gesture and self.binding_visible(b) for p in self.plugins for b in p.bindings):
                if not self.app.dialogs.ask(tr("此手势已有动作"), tr("继续添加会同时触发多个动作。继续？")):
                    return
                if self.selected is not plugin or not self.can_add_binding(plugin):
                    return
            action = plugin.actions[0]
            plugin.bindings.append(Binding(uuid4().hex, gesture, action.key, self.model_fingerprint,
                                           {s.key: s.default for s in action.parameters}))
            self._persist(plugin)
        self.app.notify()

    def _change_binding(self, index, **changes):
        plugin = self.selected
        if plugin is None or not self.can_edit(plugin) or not 0 <= index < len(plugin.bindings):
            return
        old = plugin.bindings[index]
        plugin.bindings[index] = replace(old, **changes)
        if not self._persist(plugin):
            plugin.bindings[index] = old
        self.runtime.cancel_pending(plugin.id, event_type="gesture")
        self.app.notify()

    def set_binding_gesture(self, index, gesture):
        if gesture not in self.gestures or self.selected is None:
            return
        binding = self.selected.bindings[index]
        if any(b.id != binding.id and b.gesture == gesture and self.binding_visible(b)
               for p in self.plugins for b in p.bindings):
            plugin = self.selected
            if not self.app.dialogs.ask(tr("此手势已有动作"), tr("继续添加会同时触发多个动作。继续？")):
                return
            if self.selected is not plugin or index >= len(plugin.bindings) or plugin.bindings[index] is not binding:
                return
        self._change_binding(index, gesture=gesture, model=self.model_fingerprint)

    def set_binding_action(self, index, action):
        if self.selected:
            spec = next((a for a in self.selected.actions if a.key == action), None)
            if spec:
                self._change_binding(index, action=action, parameters={s.key: s.default for s in spec.parameters})

    def set_binding_enabled(self, index, enabled):
        self._change_binding(index, enabled=bool(enabled))

    def set_binding_cooldown(self, index, value):
        try:
            cooldown = int(value)
            if not 0 <= cooldown <= 3600000:
                raise ValueError
            self._change_binding(index, cooldown_ms=cooldown)
        except (ValueError, TypeError):
            self._error(tr("冷却时间必须是 0 到 3600000 毫秒的整数。"))

    def set_binding_parameter(self, index, key, value):
        if self.selected and 0 <= index < len(self.selected.bindings):
            binding = self.selected.bindings[index]
            self._change_binding(index, parameters=dict(binding.parameters, **{key: value}))

    def binding_parameter_error(self, index, spec):
        return self._value_error(spec, self.selected.bindings[index].parameters.get(spec.key, spec.default))

    def remove_binding(self, index):
        if self.selected and self.can_edit(self.selected) and 0 <= index < len(self.selected.bindings):
            old = self.selected.bindings.pop(index)
            if not self._persist(self.selected):
                self.selected.bindings.insert(index, old)
            self.runtime.cancel_pending(self.selected.id, event_type="gesture")
        self.app.notify()

    def copy_bindings(self):
        plugin = self.selected
        if plugin is None or not self.can_add_binding(plugin):
            return
        model = self.model_fingerprint
        if not self.app.dialogs.ask(tr("按同名手势复制绑定？"), tr("将其他模型的同名手势绑定复制到当前模型，不会修改原绑定。请检查动作是否合适。")):
            return
        if self.selected is not plugin or not self.can_add_binding(plugin) or self.model_fingerprint != model:
            return
        existing = {(b.gesture, b.action) for b in plugin.bindings if b.model == model}
        for binding in list(plugin.bindings):
            key = binding.gesture, binding.action
            if binding.model != model and binding.gesture in self.gestures and key not in existing:
                plugin.bindings.append(replace(binding, id=uuid4().hex, model=model, parameters=dict(binding.parameters)))
                existing.add(key)
        self._persist(plugin)
        self.app.notify()

    def _submit(self, plugin, action, event, parameters):
        if not self.can_test(plugin):
            return False
        spec = next((a for a in plugin.actions if a.key == action), None)
        if spec is None:
            return False
        try:
            params = validate_values(spec.parameters, parameters)
            values = validate_values(plugin.settings, self._saved_values[plugin.id])
            self._remember_secrets(plugin, params, spec)
            return self.runtime.submit(plugin.id, action, event, params, values, spec.timeout)
        except Exception as exc:
            self._log(plugin, self._redact(exc), "error")
            return False

    def prediction(self, prediction):
        if not self.gesture_control or not self.can_control_gestures:
            return
        event = dict(type="gesture", id=uuid4().hex, gesture=prediction.name,
                     timestamp=datetime.now(timezone.utc).isoformat(), confidence=prediction.confidence,
                     model=self.model_fingerprint)
        now = time.monotonic()
        for plugin in self.plugins:
            for binding in plugin.bindings:
                if (binding.enabled and self.binding_visible(binding) and binding.gesture == prediction.name
                        and now - self._cooldowns.get(binding.id, float("-inf")) >= binding.cooldown_ms / 1000):
                    if self._submit(plugin, binding.action, event, binding.parameters):
                        self._cooldowns[binding.id] = now

    def test_binding(self, index):
        plugin = self.selected
        if plugin is None or not self.can_test(plugin) or not 0 <= index < len(plugin.bindings):
            return
        binding = plugin.bindings[index]
        if not self.app.dialogs.ask(tr("执行真实动作？"), tr("测试会产生真实的键盘、网络或录音操作，不是模拟运行。")):
            return
        if self.selected is plugin and self.can_test(plugin) and binding in plugin.bindings:
            self._submit(plugin, binding.action, dict(type="manual", timestamp=datetime.now(timezone.utc).isoformat()), binding.parameters)

    def test_action(self, action_key):
        plugin = self.selected
        if plugin is None or not self.can_test(plugin):
            return
        if not self.app.dialogs.ask(tr("执行真实动作？"), tr("测试会使用默认动作参数并产生真实副作用。")):
            return
        if self.selected is plugin and self.can_test(plugin):
            self._submit(plugin, action_key, dict(type="manual", timestamp=datetime.now(timezone.utc).isoformat()), {})

    def _log(self, plugin, message, level="info"):
        message = self._redact(message)
        self.app.log(tr("[{plugin}] {message}", plugin=plugin.name, message=message), level)
        try:
            path = self.root / plugin.id / "plugin.log"
            if path.exists() and path.stat().st_size >= 1024 * 1024:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now().isoformat(timespec='seconds')} [{level}] {message}\n")
        except OSError:
            pass

    def handle(self, payload):
        plugin = self.plugin(payload.get("plugin_id"))
        if plugin is None or self.app.closing:
            return
        kind = payload.get("kind")
        if kind == "rpc":
            if not self.runtime.has_request(plugin.id, payload.get("request_id")):
                return
            try:
                result = self._rpc(plugin, payload)
                self.runtime.reply(plugin.id, payload["request_id"], result=result)
            except Exception as exc:
                self.runtime.reply(plugin.id, payload["request_id"], error=self._redact(exc))
        elif kind == "log":
            self._log(plugin, payload.get("message", ""), payload.get("level", "info"))
        elif kind in {"finished", "error"}:
            event = payload.get("event") or {}
            error = self._redact(payload.get("error", payload.get("message", ""))) if kind == "error" else ""
            plugin.events = (PluginEvent(time.strftime("%H:%M:%S"), event.get("gesture", event.get("type", "")),
                                         payload.get("action", ""), error), *plugin.events[:99])
            if error:
                self._log(plugin, error, "error")
                if not self.runtime.running(plugin.id):
                    plugin.error = error
        elif kind == "started":
            plugin.error = ""
            self._log(plugin, resolve(tr("插件已启动。")))
        elif kind == "stopped":
            self._log(plugin, payload.get("reason", resolve(tr("插件已停止。"))))
        if kind in {"finished", "error", "stopped"}:
            self.store.refresh_storage(plugin)
        self.app.notify()

    def poll(self):
        self.runtime.poll()

    def _rpc(self, plugin, payload):
        if not self.can_test(plugin):
            raise ValueError("Plugin is not enabled or host is busy")
        method, params = payload.get("method"), payload.get("params", {})
        declared = {p.key for p in plugin.permissions}
        if method in {"recording.start", "recording.stop", "recording.status"} and "recording.control" not in declared:
            raise ValueError("Manifest must declare recording.control")
        if method == "recording.status":
            session = self.recording_session
            return dict(state=self.app.audio_state, session_id=session["session_id"] if session and session["owner"] == plugin.id else None,
                        owned=bool(session and session["owner"] == plugin.id))
        if method == "recording.start":
            if (payload.get("event") or {}).get("type") == "recording.completed":
                raise ValueError("Recording completion handlers cannot start another recording")
            if self.recording_session or self._after_recording:
                raise ValueError("Recording is already owned by another operation")
            session = dict(owner=plugin.id, session_id=uuid4().hex)
            self.recording_session = session
            try:
                accepted = self.app.tools.start_recording(live=params.get("live", False), owner=plugin.id,
                    session_id=session["session_id"], max_seconds=params.get("max_seconds", 60))
                if not accepted:
                    raise ValueError("Recording was not accepted")
            except Exception:
                self.recording_session = None
                raise
            return dict(session, state="starting")
        if method == "recording.stop":
            session = self.recording_session
            if not session or session["owner"] != plugin.id or params.get("session_id") != session["session_id"]:
                raise ValueError("Recording is not owned by this plugin/session")
            if not self.app.tools.available("finish"):
                raise ValueError("Recording is not ready to stop; wait for recording state")
            if not self.app.tools.finish_recording(owner=plugin.id):
                raise ValueError("Stop request was not accepted")
            return dict(session, state="stopping")
        raise ValueError(f"Unsupported host method: {method}")

    def set_recording_events(self, enabled):
        plugin = self.selected
        if plugin is None or not self.can_recording_events(plugin):
            return
        if enabled and not self.app.dialogs.ask(tr("允许插件读取录音文件？"), tr("插件将收到新录音的文件路径，并能读取完整音频。若插件会上传，请先确认设置中的目标站点和用途。任意 Python 不受此订阅开关的安全约束。")):
            self.app.notify()
            return
        if self.selected is not plugin or not self.can_recording_events(plugin):
            return
        plugin.recording_events_enabled = bool(enabled)
        self.runtime.cancel_pending(plugin.id, event_type="recording.completed")
        self._persist(plugin)
        self.app.notify()

    def set_recording_scope(self, scope):
        plugin = self.selected
        if plugin is None or not self.can_recording_events(plugin) or scope not in {"own", "all"}:
            return
        if scope == "all" and not self.app.dialogs.ask(tr("处理所有新录音？"), tr("包括界面或其他插件发起的新录音。不回放历史录音；请确认上传目的地和处理用途。")):
            self.app.notify()
            return
        if self.selected is not plugin or not self.can_recording_events(plugin):
            return
        plugin.recording_scope = scope
        self.runtime.cancel_pending(plugin.id, event_type="recording.completed")
        self._persist(plugin)
        self.app.notify()

    def audio_saved(self, payload):
        event = dict(type="recording.completed", id=uuid4().hex, timestamp=datetime.now(timezone.utc).isoformat(),
                     path=str(payload["path"]), raw_path=str(payload["raw_path"]) if payload.get("raw_path") else None,
                     duration_s=payload.get("duration_s"), session_id=payload.get("session_id"), owner=payload.get("owner"),
                     raw_only=Path(payload["path"]).suffix.lower() != ".wav")
        for plugin in self.plugins:
            if (plugin.recording_events_enabled and "recording.completed" in plugin.subscriptions
                    and (plugin.recording_scope == "all" or event["owner"] == plugin.id)):
                self._recording_event(plugin, event)

    def _recording_event(self, plugin, event):
        if self.can_test(plugin):
            self._remember_secrets(plugin)
            self.runtime.submit(plugin.id, "@event", event, {}, self._saved_values[plugin.id], plugin.event_timeout)

    def process_audio_file(self):
        plugin = self.selected
        if plugin is None or not self.can_test(plugin) or "recording.completed" not in plugin.subscriptions:
            return
        path = self.app.dialogs.open_file(tr("完整 WAV 录音 (*.wav)"))
        if not path or self.selected is not plugin or not self.can_test(plugin):
            return
        try:
            with wave.open(path, "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
            if not self.app.dialogs.ask(tr("将此音频交给插件处理？"), tr("插件可以读取文件并执行真实操作，包括自行上传。文件：{path}", path=path)):
                return
            if self.selected is not plugin or not self.can_test(plugin):
                return
            self._recording_event(plugin, dict(type="recording.completed", path=str(Path(path).resolve()), raw_path=None,
                duration_s=duration, session_id=None, owner=None, manual=True, timestamp=datetime.now(timezone.utc).isoformat()))
        except Exception as exc:
            self._error(exc)

    def audio_state_changed(self, state):
        if state == "idle":
            self.recording_session = None
            callback, self._after_recording = self._after_recording, None
            if callback and not self.app.closing:
                callback()

    def _defer_recording(self, plugin_id, callback):
        session = self.recording_session
        if not session or session["owner"] != plugin_id:
            return False
        if self._after_recording:
            return True
        if not self.app.dialogs.ask(tr("插件仍在录音"), tr("先停止并等待完整保存，再执行此操作？")):
            return True
        if self.recording_session is not session or self.app.closing:
            return True
        if not self.app.tools.available("finish"):
            self._error(tr("录音正在启动或保存，请等待状态稳定后重试。"))
            return True
        self._after_recording = callback
        if not self.app.tools.finish_recording():
            self._after_recording = None
        return True

    def clear_data(self):
        plugin = self.selected
        if plugin is None or not self.can_clear_data(plugin):
            return
        if not self.app.dialogs.ask(tr("清除插件数据？"), tr("将永久清空数据与缓存，保留设置和绑定。"), danger=True):
            return
        if self.selected is not plugin or not self.can_clear_data(plugin):
            return
        try:
            self.store.clear_data(plugin)
        except Exception as exc:
            self._error(exc)
        self.app.notify()

    def remove_plugin(self):
        plugin = self.selected
        if plugin is None or not self.can_remove(plugin):
            return
        if not self.app.dialogs.ask(tr("移除插件？"), tr("将删除插件托管代码、环境、数据、设置、绑定和密钥。不会删除原始来源目录。"), danger=True):
            return
        if self.selected is not plugin or not self.can_remove(plugin):
            return
        if self._defer_recording(plugin.id, lambda: self.remove_plugin() if self.selected is plugin else None):
            return
        try:
            self.runtime.stop(plugin.id, "uninstalled")
            self.store.remove(plugin)
            self._warnings()
            self.plugins.remove(plugin)
            self._saved_values.pop(plugin.id, None)
            candidate = self._updates.pop(plugin.id, None)
            if candidate:
                self.installer.cleanup(candidate)
            self.selected_id = None
        except Exception as exc:
            self._error(exc)
        self.app.notify()

    def close(self):
        self._cancel.set()
        self.disarm()
        self.service_running = False
        self._after_recording = None
        self.runtime.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.busy_text is None:
            if self.candidate:
                self.installer.cleanup(self.candidate)
            for candidate in self._updates.values():
                self.installer.cleanup(candidate)
