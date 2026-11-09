# SPDX-License-Identifier: MPL-2.0
"""Qt-free plugin declarations and editable UI state.

Plugins are trusted Python programs, NOT sandboxed. Permissions are consent
metadata, not OS security boundaries. ``values`` contains draft settings; the
runtime must validate them before use. Binding.model is the model fingerprint,
not its display name. Only Binding objects should be used for real plugins.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SettingSpec:
    key: str
    title: str
    kind: str  # switch | choice | text | secret | number
    default: object
    help: str = ""
    options: tuple = ()  # (JSON scalar value, label) pairs
    minimum: float | None = None
    maximum: float | None = None
    group: str = ""
    visible_when: dict = field(default_factory=dict)
    required: bool = False


@dataclass(frozen=True)
class Action:
    key: str
    title: str
    parameters: tuple = ()
    timeout: float = 30


@dataclass(frozen=True)
class Permission:
    title: str
    detail: str
    sensitive: bool = False
    key: str = ""


@dataclass(frozen=True)
class PluginEvent:
    when: str
    gesture: str
    action: str
    error: str = ""


@dataclass
class StorageSlot:
    kind: str
    title: str
    path: Path
    size: int | None  # None if a filesystem measurement failed
    is_directory: bool = True


@dataclass(frozen=True)
class Release:
    version: str
    commit: str
    notes: str
    new_permissions: tuple = ()


@dataclass
class GitHubSource:
    repo: str
    commit: str
    last_checked: str
    channel: str = "stable"
    auto_update: bool = False
    update: Release | None = None


@dataclass(frozen=True)
class Binding:
    id: str
    gesture: str
    action: str
    model: str
    parameters: dict = field(default_factory=dict)
    enabled: bool = True
    cooldown_ms: int = 800

    def __iter__(self):
        """Legacy UI can still unpack a binding as (gesture, action)."""
        yield self.gesture
        yield self.action

    __hash__ = None


@dataclass
class Plugin:
    id: str
    name: str
    version: str
    author: str
    description: str
    settings: tuple = ()
    actions: tuple = ()
    permissions: tuple = ()
    bindings: list[Binding] = field(default_factory=list)
    events: tuple = ()  # recent UI PluginEvent objects, NOT subscriptions
    storage: list = field(default_factory=list)
    enabled: bool = False
    error: str = ""
    source: str = ""
    github: GitHubSource | None = None
    values: dict = field(default_factory=dict)
    entry: str = "main.py"
    api_version: int = 1
    platforms: tuple = ()
    requirements: tuple = ()
    subscriptions: tuple = ()  # manifest events, e.g. recording.completed
    directory: Path | None = None
    recording_scope: str = "own"
    recording_events_enabled: bool = False
    event_timeout: float = 120

    def __post_init__(self):
        self.values = {spec.key: deepcopy(spec.default) for spec in self.settings} | self.values

    def action_title(self, key):
        return next((action.title for action in self.actions if action.key == key), key)

    @property
    def data_size(self):
        if any(slot.size is None for slot in self.storage):
            return None
        return sum(slot.size for slot in self.storage)


def assigned_storage(root, plugin_id, *, data=0, cache=0, settings=0, log=0):
    """Describe host-owned paths; this does not create files or grant isolation."""
    base = Path(root) / plugin_id
    return [StorageSlot("data", "数据目录", base / "data", data),
            StorageSlot("cache", "缓存目录", base / "cache", cache),
            StorageSlot("settings", "设置文件", base / "settings.json", settings, False),
            StorageSlot("log", "日志文件", base / "plugin.log", log, False)]
