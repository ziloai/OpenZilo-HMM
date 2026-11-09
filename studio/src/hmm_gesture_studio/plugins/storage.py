# SPDX-License-Identifier: MPL-2.0
"""Atomic, local plugin state and OS-keyring credentials.

Layout: root/id/{code,data,cache,settings.json,plugin.log,venv}. settings.json
contains ONE atomic snapshot of non-secret draft values, bindings (including
model fingerprints), consent, source and credential references. It never
contains secret setting/parameter values. Secret defaults in manifests must be
empty. Required-but-empty draft fields may be saved; runtime validation remains
mandatory. Keyring failure falls back to this store instance's memory only and
adds a human-readable message to ``warnings``. No plaintext keyring backends are
accepted. A new process cannot recover session-only secrets.

Methods raise ValueError/OSError on invalid input or failed writes. discover()
skips broken installations and records warnings. Callers must serialize store
mutations across processes; one instance serializes its own operations.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from uuid import uuid4

from .manifest import contained_path, load_manifest, read_json, validate_id, validate_values
from .models import Binding, GitHubSource, Plugin, assigned_storage

_SERVICE = "org.openzilo.studio.plugins"
_NATIVE_BACKENDS = ("keyring.backends.macOS", "keyring.backends.Windows",
                    "keyring.backends.SecretService", "keyring.backends.kwallet",
                    "keyring.backends.libsecret")


def atomic_json(path: Path, value):
    """Replace a JSON snapshot using a same-directory fsynced private temp file."""
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing to overwrite a symlink")
    fd, name = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # Directory fsync is unavailable on some supported platforms/filesystems.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _draft(specs, values):
    return validate_values(tuple(replace(spec, required=False) for spec in specs), values)


class PluginStore:
    """Own plugin files beneath root; construction/discovery never accesses HTTP."""

    def __init__(self, root):
        root = Path(root).expanduser()
        if root.is_symlink():
            raise ValueError("Storage root cannot be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        self.warnings: list[str] = []
        self._session_secrets: dict[str, str] = {}
        self._lock = threading.RLock()
        self._keyring = None
        self._keyring_checked = False

    def _get_keyring(self):
        """Load the OS backend only on an actual credential operation.

        discover still hydrates saved secrets for snapshot compatibility; that
        can prompt for OS consent when credential references exist. Construction
        and discovery of plugins without saved secrets never touch the vault.
        """
        if self._keyring is not None or self._keyring_checked:
            return self._keyring
        self._keyring_checked = True
        try:
            import keyring
            backend = keyring.get_keyring()
            module = type(backend).__module__
            if module not in _NATIVE_BACKENDS or backend.priority <= 0:
                raise RuntimeError("Not a supported native credential vault")
            self._keyring = backend
        except Exception:
            self._warn("System keyring unavailable; plugin secrets are kept for this session only.")
        return self._keyring

    def _warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def base(self, plugin_or_id) -> Path:
        """Validated installation root, shared with installer/runtime adapters."""
        plugin_id = plugin_or_id.id if isinstance(plugin_or_id, Plugin) else plugin_or_id
        return contained_path(self.root, validate_id(plugin_id))

    def _snapshot(self, plugin):
        path = contained_path(self.base(plugin), "settings.json")
        if not path.exists():
            return {"format": 1, "values": {}, "secret_refs": {}, "state": {}}
        result = read_json(path, maximum=8 * 1024 * 1024)
        if (not isinstance(result, dict) or result.get("format") != 1
                or any(not isinstance(result.get(key), dict) for key in ("values", "secret_refs", "state"))):
            raise ValueError("Invalid stored plugin snapshot")
        return result

    @staticmethod
    def _refs(snapshot):
        result = list(snapshot.get("secret_refs", {}).values())
        for binding in snapshot.get("state", {}).get("bindings", []):
            result.extend(binding.get("secret_refs", {}).values())
        if any(not isinstance(ref, str) or len(ref) != 32 or any(c not in "0123456789abcdef" for c in ref)
               for ref in result):
            raise ValueError("Invalid credential references")
        return result

    def _put_secret(self, plugin_id, value, created):
        ref = uuid4().hex
        username = f"{plugin_id}/{ref}"
        created.append(ref)
        keyring = self._get_keyring()
        if keyring is not None:
            try:
                keyring.set_password(_SERVICE, username, value)
                return ref
            except Exception:
                self._warn("System keyring write failed; plugin secrets are kept for this session only.")
        self._session_secrets[username] = value
        return ref

    def _get_secret(self, plugin_id, ref):
        username = f"{plugin_id}/{ref}"
        if username in self._session_secrets:
            return self._session_secrets[username]
        keyring = self._get_keyring()
        if keyring is not None:
            try:
                value = keyring.get_password(_SERVICE, username)
                if isinstance(value, str):
                    return value
            except Exception:
                pass
        self._warn(f"A saved secret for {plugin_id} is unavailable; enter it again before running.")
        return ""

    def _delete_refs(self, plugin_id, refs):
        for ref in refs:
            username = f"{plugin_id}/{ref}"
            session_only = username in self._session_secrets
            self._session_secrets.pop(username, None)
            keyring = None if session_only else self._get_keyring()
            if keyring is not None:
                try:
                    keyring.delete_password(_SERVICE, username)
                except Exception:
                    self._warn(f"Could not remove an obsolete credential for {plugin_id} from the keyring.")

    def _split_values(self, plugin_id, specs, values, created):
        normalized = _draft(specs, values)
        public, refs = {}, {}
        for spec in specs:
            if spec.kind == "secret":
                if normalized[spec.key]:
                    refs[spec.key] = self._put_secret(plugin_id, normalized[spec.key], created)
            else:
                public[spec.key] = normalized[spec.key]
        return public, refs

    def _join_values(self, plugin_id, specs, public, refs):
        if not isinstance(public, dict) or not isinstance(refs, dict):
            raise ValueError("Invalid stored settings")
        keys = {spec.key for spec in specs}
        secret_keys = {spec.key for spec in specs if spec.kind == "secret"}
        if set(public) - keys or set(refs) - secret_keys or set(public) & secret_keys:
            raise ValueError("Stored settings do not match the manifest")
        values = dict(public)
        for key, ref in refs.items():
            values[key] = self._get_secret(plugin_id, ref)
        return _draft(specs, values)

    def load_settings(self, plugin: Plugin) -> dict:
        """Return saved normalized drafts, including keyring/session secrets."""
        with self._lock:
            snapshot = self._snapshot(plugin)
            self._refs(snapshot)
            return self._join_values(plugin.id, plugin.settings, snapshot["values"], snapshot["secret_refs"])

    def save(self, plugin: Plugin) -> None:
        """Atomically persist draft values and consent; never execute code."""
        with self._lock:
            base = self.base(plugin)
            snapshot = self._snapshot(plugin)
            old_refs = self._refs(snapshot)
            if type(plugin.enabled) is not bool or type(plugin.recording_events_enabled) is not bool:
                raise ValueError("Enabled state must be boolean")
            if plugin.recording_scope not in ("own", "all"):
                raise ValueError("recording_scope must be own or all")
            created = []
            try:
                public, refs = self._split_values(plugin.id, plugin.settings, plugin.values, created)
                bindings = []
                ids = set()
                actions = {action.key: action for action in plugin.actions}
                for binding in plugin.bindings:
                    if (not isinstance(binding, Binding) or binding.id in ids
                            or not all(isinstance(value, str) and value for value in
                                       (binding.id, binding.gesture, binding.action, binding.model))
                            or type(binding.enabled) is not bool
                            or type(binding.cooldown_ms) is not int or binding.cooldown_ms < 0
                            or binding.action not in actions):
                        raise ValueError("Invalid binding (id, action, model fingerprint or cooldown)")
                    ids.add(binding.id)
                    params, param_refs = self._split_values(plugin.id, actions[binding.action].parameters,
                                                           binding.parameters, created)
                    item = asdict(binding)
                    item.update(parameters=params, secret_refs=param_refs)
                    bindings.append(item)
                github = None
                if plugin.github is not None:
                    github = {key: getattr(plugin.github, key) for key in
                              ("repo", "commit", "last_checked", "channel")}
                    github["auto_update"] = False  # manual updates are a policy, not a UI preference
                state = {"enabled": plugin.enabled, "bindings": bindings, "source": plugin.source,
                         "github": github, "recording_scope": plugin.recording_scope,
                         "recording_events_enabled": plugin.recording_events_enabled}
                base.mkdir(parents=True, exist_ok=True, mode=0o700)
                for name in ("data", "cache"):
                    contained_path(base, name).mkdir(exist_ok=True, mode=0o700)
                atomic_json(contained_path(base, "settings.json"),
                            {"format": 1, "values": public, "secret_refs": refs, "state": state})
            except Exception:
                self._delete_refs(plugin.id, created)
                raise
            self._delete_refs(plugin.id, old_refs)
            self.refresh_storage(plugin)

    def refresh_storage(self, plugin: Plugin):
        """Measure data/cache/settings/log bytes without following symlinks.

        Missing paths have an actual size of zero. Unreadable paths have size
        None (unknown), which also makes plugin.data_size None. Code and venv
        are deliberately excluded from these user-storage UI slots.
        """
        def measure(path):
            try:
                info = path.lstat()
            except FileNotFoundError:
                return 0
            if stat.S_ISREG(info.st_mode):
                return info.st_size
            if stat.S_ISDIR(info.st_mode):
                return sum(measure(child) for child in path.iterdir())
            return 0  # Never follow symlinks or count devices/sockets.

        with self._lock:
            self.base(plugin)
            slots = assigned_storage(self.root, plugin.id)
            for slot in slots:
                try:
                    slot.size = measure(slot.path)
                except OSError:
                    slot.size = None
                    self._warn(f"Could not measure {plugin.id}/{slot.path.name}; size is unknown.")
            plugin.storage = slots
        return slots

    def discover(self) -> list[Plugin]:
        """Load installed code/manifest + saved state; skip staging/invalid entries."""
        plugins = []
        with self._lock:
            for base in sorted(self.root.iterdir()):
                if base.name.startswith(".") or not base.is_dir():
                    continue
                try:
                    base = self.base(base.name)
                    plugin = load_manifest(contained_path(base, "code"))
                    if plugin.id != base.name:
                        raise ValueError("Directory/manifest id mismatch")
                    snapshot = self._snapshot(plugin)
                    self._refs(snapshot)
                    plugin.values = self._join_values(plugin.id, plugin.settings, snapshot["values"],
                                                     snapshot["secret_refs"])
                    state = snapshot["state"]
                    plugin.enabled = state.get("enabled", False)
                    plugin.recording_scope = state.get("recording_scope", "own")
                    plugin.recording_events_enabled = state.get("recording_events_enabled", False)
                    if (type(plugin.enabled) is not bool or type(plugin.recording_events_enabled) is not bool
                            or plugin.recording_scope not in ("own", "all")):
                        raise ValueError("Invalid saved consent")
                    plugin.source = state.get("source", "")
                    if not isinstance(plugin.source, str):
                        raise ValueError("Invalid saved source")
                    if state.get("github") is not None:
                        plugin.github = GitHubSource(**state["github"])
                        plugin.github.auto_update = False
                    actions = {action.key: action for action in plugin.actions}
                    ids = set()
                    for item in state.get("bindings", []):
                        item = dict(item)
                        refs = item.pop("secret_refs", {})
                        action = actions[item["action"]]
                        item["parameters"] = self._join_values(plugin.id, action.parameters,
                                                              item.get("parameters", {}), refs)
                        binding = Binding(**item)
                        if (binding.id in ids or not all(isinstance(value, str) and value for value in
                                                        (binding.id, binding.gesture, binding.action, binding.model))
                                or type(binding.enabled) is not bool or type(binding.cooldown_ms) is not int
                                or binding.cooldown_ms < 0):
                            raise ValueError("Invalid stored binding")
                        ids.add(binding.id)
                        plugin.bindings.append(binding)
                    self.refresh_storage(plugin)
                    plugins.append(plugin)
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    self._warn(f"Could not load plugin {base.name}; its files were left untouched.")
        return plugins

    def load_preferences(self) -> dict:
        """Read non-secret host UI preferences (never put credentials here)."""
        with self._lock:
            path = contained_path(self.root, "preferences.json")
            if not path.exists():
                return {}
            value = read_json(path)
            if not isinstance(value, dict):
                raise ValueError("Preferences must be an object")
            return value

    def save_preferences(self, preferences: dict):
        """Atomically save JSON-only, non-secret host preferences."""
        if not isinstance(preferences, dict):
            raise ValueError("Preferences must be an object")
        with self._lock:
            atomic_json(contained_path(self.root, "preferences.json"), preferences)

    def remove(self, plugin: Plugin):
        """Permanently remove this installation, its user data and credentials."""
        with self._lock:
            base = self.base(plugin)
            refs = self._refs(self._snapshot(plugin))
            if base.exists():
                shutil.rmtree(base)
            self._delete_refs(plugin.id, refs)

    def clear_cache(self, plugin: Plugin):
        """Clear assigned cache and logs, including the rotated log."""
        self._clear_storage(plugin, include_data=False)

    def clear_data(self, plugin: Plugin):
        """Clear data/cache/log only; preserve code, settings, bindings and venv."""
        self._clear_storage(plugin, include_data=True)

    def _clear_storage(self, plugin: Plugin, *, include_data: bool):
        with self._lock:
            base = self.base(plugin)
            names = ("cache", "plugin.log", "plugin.log.1")
            if include_data:
                names = ("data", *names)
            # Validate every target before deleting any of this plugin's files.
            paths = [contained_path(base, name) for name in names]
            for path in paths:
                if path.is_dir():
                    shutil.rmtree(path)
                    path.mkdir(mode=0o700)
                else:
                    path.unlink(missing_ok=True)
            self.refresh_storage(plugin)
