# SPDX-License-Identifier: MPL-2.0
"""Offline security/regression tests for plugin manifests, persistence and installs."""
from __future__ import annotations

from concurrent.futures import CancelledError
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import zipfile

from hmm_gesture_studio.plugins import installer as installer_module
from hmm_gesture_studio.plugins.installer import (
    Installer, dependencies_ready, install_dependencies, python_executable,
)
from hmm_gesture_studio.plugins.manifest import load_manifest, validate_values
from hmm_gesture_studio.plugins.models import Binding
from hmm_gesture_studio.plugins.storage import PluginStore


class PluginStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # No desktop credential prompt and no dependency on a system keyring.
        self.keyring_patch = patch.dict(sys.modules, {"keyring": None})
        self.keyring_patch.start()
        self.addCleanup(self.keyring_patch.stop)
        self.store = PluginStore(self.root / "store")
        self.installer = Installer(self.store)
        self.source = self.root / "source"
        self.source.mkdir()
        self.raw = {
            "id": "test.plugin", "name": "Test", "version": "1.0.0", "author": "Tester",
            "description": "Offline fixture", "entry": "main.py", "api_version": 1,
            "settings": [
                {"key": "token", "kind": "secret", "default": ""},
                {"key": "count", "kind": "number", "default": 2, "minimum": 1, "maximum": 10},
                {"key": "active", "kind": "switch", "default": True},
            ],
            "actions": [{"key": "send", "parameters": [
                {"key": "password", "kind": "secret", "default": ""},
                {"key": "text", "kind": "text", "default": "hello"},
            ]}],
            "permissions": [{"key": "network", "title": "Network", "detail": "HTTPS", "sensitive": True}],
            "events": ["recording.completed"], "platforms": ["darwin", "linux", "win32"],
        }
        self.write_manifest()
        (self.source / "main.py").write_text("# Version one; do not execute.\n", encoding="utf-8")

    def write_manifest(self):
        (self.source / "plugin.json").write_text(json.dumps(self.raw), encoding="utf-8")

    def install(self):
        return self.installer.commit(self.installer.prepare_local(self.source))

    def zip(self, entries, name="plugin.zip", compression=zipfile.ZIP_STORED):
        path = self.root / name
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            for key, value in entries:
                archive.writestr(key, value)
        return path

    def fake_process(self, command, **kwargs):
        installer_module._check_install(kwargs.get("cancel_event"), kwargs["deadline"])
        if command[1:4] == ["-I", "-m", "venv"]:
            environment = Path(command[-1])
            executable = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            executable.parent.mkdir(parents=True)
            executable.write_text("fake python", encoding="utf-8")
        if command[-2:] == ["freeze", "--all"]:
            return "pynput==1.8.1\nsix==1.17.0\npyobjc-core==11.1\npip==25.1.1\n"
        return ""

    def test_manifest_and_value_normalization(self):
        plugin = load_manifest(self.source)
        self.assertEqual(plugin.subscriptions, ("recording.completed",))
        self.assertEqual(plugin.permissions[0].key, "network")
        self.assertEqual(plugin.actions[0].timeout, 30)
        values = validate_values(plugin.settings, {"count": "3.5"})
        self.assertEqual(values, {"count": 3.5, "token": "", "active": True})
        self.assertEqual(plugin.values["count"], 2)
        for bad in ({"unknown": 1}, {"count": "nan"}, {"count": float("inf")},
                    {"count": 0}, {"count": True}, {"active": 1}, {"token": 5}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_values(plugin.settings, bad)

    def test_manifest_strict_schema_and_numeric_defaults(self):
        mutations = [
            ("id", "../escape"), ("id", "CON"), ("id", "x/evil"),
            ("entry", "../main.py"), ("entry", "C:\\main.py"),
            ("entry", "/main.py"), ("entry", "missing.py"),
            ("api_version", True), ("platforms", ["android"]),
            ("requirements", ["example>=1"]), ("requirements", ["x @ https://evil.invalid/x.whl"]),
            ("requirements", ["--index-url=evil"]),
            ("actions", [{"key": "go"}, {"key": "go"}]),
            ("actions", [{"key": "go", "timeout": 0}]),
            ("actions", [{"key": "go", "timeout": float("inf")}]),
            ("settings", [{"key": "x", "kind": "number", "default": "2"}]),
            ("settings", [{"key": "x", "kind": "secret", "default": "plaintext"}]),
            ("settings", [{"key": "x", "kind": "number", "default": 2, "minimum": 3}]),
            ("settings", [{"key": "x", "kind": "switch", "default": "false"}]),
        ]
        original = dict(self.raw)
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                self.raw = dict(original, **{key: value})
                self.write_manifest()
                with self.assertRaises(ValueError):
                    load_manifest(self.source)

    def test_choice_visibility_and_required_drafts(self):
        self.raw["settings"] = [
            {"key": "mode", "kind": "choice", "default": "a", "options": [["a", "A"], ["b", "B"]]},
            {"key": "endpoint", "kind": "text", "default": "", "required": True,
             "group": "Network", "visible_when": {"mode": "b"}},
        ]
        self.write_manifest()
        plugin = self.install()  # incomplete drafts can be installed/saved
        self.assertEqual(validate_values(plugin.settings, {})["endpoint"], "")
        with self.assertRaises(ValueError):
            validate_values(plugin.settings, {"mode": "b"})
        with self.assertRaises(ValueError):
            validate_values(plugin.settings, {"mode": "a", "endpoint": 7})
        self.assertEqual(self.store.load_settings(plugin)["endpoint"], "")
        self.raw["settings"][1]["visible_when"] = {"missing": True}
        self.write_manifest()
        with self.assertRaises(ValueError):
            load_manifest(self.source)

    def test_manifest_symlink_is_rejected(self):
        outside = self.root / "outside.py"
        outside.write_text("raise RuntimeError('never run')", encoding="utf-8")
        (self.source / "main.py").unlink()
        (self.source / "main.py").symlink_to(outside)
        with self.assertRaises(ValueError):
            load_manifest(self.source)
        with self.assertRaises(ValueError):
            self.installer.prepare_local(self.source)
        self.assertEqual(list((self.store.root / ".staging").iterdir()), [])

    def test_zip_traversal_absolute_windows_symlink_and_duplicates(self):
        for index, name in enumerate(("../outside", "/absolute", "C:/drive", "a\\..\\escape", "a/../../escape")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.installer.prepare_local(self.zip([(name, "bad")], f"bad{index}.zip"))
        symlink = zipfile.ZipInfo("link")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            self.installer.prepare_local(self.zip([(symlink, "../outside")], "symlink.zip"))
        with self.assertRaises(ValueError):
            self.installer.prepare_local(self.zip([("File", "a"), ("file", "b")], "collision.zip"))
        self.assertFalse((self.root / "outside").exists())

    def test_zip_bomb_and_size_limits(self):
        archive = self.zip([("huge", "0" * 1000000)], compression=zipfile.ZIP_DEFLATED)
        with self.assertRaises(ValueError):
            self.installer.prepare_local(archive)
        with patch.object(installer_module, "MAX_FILES", 1), self.assertRaises(ValueError):
            self.installer.prepare_local(self.source)
        with patch.object(installer_module, "MAX_FILE_BYTES", 1), self.assertRaises(ValueError):
            self.installer.prepare_local(self.source)

    def test_zip_single_root_and_candidate_cleanup(self):
        archive = self.zip([("repo-commit/plugin.json", json.dumps(self.raw)),
                            ("repo-commit/main.py", "# no execution")])
        plugin = self.installer.prepare_local(archive)
        staged = plugin.directory
        self.assertTrue(staged.is_dir())
        self.assertEqual(self.store.discover(), [])
        self.installer.cleanup(plugin)
        self.assertFalse(staged.exists())
        with self.assertRaises(ValueError):
            self.installer.commit(plugin)

    def test_session_secrets_are_never_plaintext_in_store(self):
        plugin = self.install()
        plugin.values["token"] = "TOP-SECRET-settings-unique"
        plugin.values["count"] = "4"
        plugin.enabled = True
        plugin.recording_scope = "all"
        plugin.recording_events_enabled = True
        plugin.bindings = [Binding("binding-1", "snap", "send", "sha256:model-fingerprint",
                                   {"password": "TOP-SECRET-parameter-unique"}, False, 1500)]
        self.store.save(plugin)
        plugin.values["token"] = "unsaved-draft"
        self.assertEqual(self.store.load_settings(plugin)["token"], "TOP-SECRET-settings-unique")
        for path in self.store.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"TOP-SECRET", path.read_bytes(), str(path))
        loaded = self.store.discover()[0]
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.recording_scope, "all")
        self.assertTrue(loaded.recording_events_enabled)
        self.assertEqual(loaded.bindings[0].parameters["password"], "TOP-SECRET-parameter-unique")
        self.assertEqual(loaded.bindings[0].model, "sha256:model-fingerprint")
        self.assertFalse(loaded.bindings[0].enabled)
        self.assertEqual(loaded.bindings[0].cooldown_ms, 1500)
        self.assertEqual(tuple(loaded.bindings[0]), ("snap", "send"))
        self.assertEqual(loaded.values["count"], 4)
        fresh = PluginStore(self.store.root)
        self.assertEqual(fresh.discover()[0].values["token"], "")
        self.assertTrue(fresh.warnings)

    def test_native_keyring_and_failed_keyring_fallback(self):
        class Vault:
            def __init__(self):
                self.values = {}

            def set_password(self, service, name, value):
                self.values[service, name] = value

            def get_password(self, service, name):
                return self.values.get((service, name))

            def delete_password(self, service, name):
                del self.values[service, name]

        vault = Vault()
        self.store._keyring = vault
        plugin = self.install()
        plugin.values["token"] = "native-vault-secret"
        self.store.save(plugin)
        fresh = PluginStore(self.store.root)
        fresh._keyring = vault
        self.assertEqual(fresh.load_settings(plugin)["token"], "native-vault-secret")
        self.assertEqual(len(vault.values), 1)
        with patch.object(vault, "set_password", side_effect=RuntimeError("vault locked")):
            plugin.values["token"] = "session-secret"
            self.store.save(plugin)
        self.assertEqual(self.store.load_settings(plugin)["token"], "session-secret")
        self.assertTrue(any("write failed" in warning for warning in self.store.warnings))
        self.assertEqual(len(vault.values), 0)

    def test_failed_save_is_atomic_including_secrets(self):
        plugin = self.install()
        plugin.values["token"] = "previous-secret"
        self.store.save(plugin)
        state_file = self.store.base(plugin) / "settings.json"
        previous = state_file.read_bytes()
        plugin.values["token"] = "new-secret"
        with patch("hmm_gesture_studio.plugins.storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.save(plugin)
        self.assertEqual(state_file.read_bytes(), previous)
        self.assertEqual(self.store.load_settings(plugin)["token"], "previous-secret")
        self.assertEqual(len(self.store._session_secrets), 1)
        self.assertEqual(list(state_file.parent.glob(".state-*")), [])

    def test_update_preserves_configuration_and_data_but_disables(self):
        plugin = self.install()
        plugin.values["token"] = "keep-secret"
        plugin.values["count"] = 7
        plugin.enabled = True
        plugin.recording_events_enabled = True
        plugin.bindings = [Binding("b", "wave", "send", "model-sha", {"text": "saved"})]
        self.store.save(plugin)
        base = self.store.base(plugin)
        (base / "data" / "user-data").write_text("keep", encoding="utf-8")
        (base / "venv").mkdir()
        (base / "venv" / "old").write_text("old environment", encoding="utf-8")
        self.raw["version"] = "2.0.0"
        self.write_manifest()
        candidate = self.installer.prepare_local(self.source)
        with self.assertRaises(FileExistsError):
            self.installer.commit(candidate)
        updated = self.installer.commit(candidate, replace=True)
        self.assertFalse(updated.enabled)
        self.assertEqual(updated.version, "2.0.0")
        self.assertEqual(updated.values["token"], "keep-secret")
        self.assertEqual(updated.values["count"], 7)
        self.assertEqual(updated.bindings[0], replace(plugin.bindings[0], parameters={"password": "", "text": "saved"}))
        self.assertTrue(updated.recording_events_enabled)
        self.assertEqual((base / "data" / "user-data").read_text(), "keep")
        self.assertFalse((base / "venv").exists())
        self.assertEqual(updated.directory, base / "code")
        self.installer.cleanup(candidate)
        self.assertTrue(updated.directory.exists())

    def test_failed_update_restores_code_settings_venv_and_user_data(self):
        plugin = self.install()
        plugin.enabled = True
        plugin.values["token"] = "old-secret"
        self.store.save(plugin)
        base = self.store.base(plugin)
        (base / "data" / "recording").write_text("user", encoding="utf-8")
        (base / "venv").mkdir()
        (base / "venv" / "marker").write_text("old", encoding="utf-8")
        previous_state = (base / "settings.json").read_bytes()
        previous_code = (base / "code" / "main.py").read_bytes()
        self.raw["version"] = "2.0.0"
        self.write_manifest()
        (self.source / "main.py").write_text("# Version two", encoding="utf-8")
        candidate = self.installer.prepare_local(self.source)
        with patch("hmm_gesture_studio.plugins.storage.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.installer.commit(candidate, replace=True)
        self.assertEqual((base / "code" / "main.py").read_bytes(), previous_code)
        self.assertEqual((base / "settings.json").read_bytes(), previous_state)
        self.assertEqual((base / "venv" / "marker").read_text(), "old")
        self.assertEqual((base / "data" / "recording").read_text(), "user")
        loaded = self.store.discover()[0]
        self.assertTrue(loaded.enabled)
        self.assertEqual(loaded.version, "1.0.0")
        self.assertEqual(loaded.values["token"], "old-secret")
        self.assertTrue(candidate.directory.exists())
        self.installer.cleanup(candidate)

    def test_incompatible_update_keeps_old_installation(self):
        plugin = self.install()
        plugin.bindings = [Binding("b", "wave", "send", "fingerprint")]
        self.store.save(plugin)
        self.raw["actions"] = []
        self.write_manifest()
        candidate = self.installer.prepare_local(self.source)
        with self.assertRaises(ValueError):
            self.installer.commit(candidate, replace=True)
        self.assertEqual(self.store.discover()[0].actions[0].key, "send")

    def test_storage_path_protection_and_preferences(self):
        plugin = self.install()
        original_id = plugin.id
        plugin.id = "../outside"
        for operation in (self.store.save, self.store.remove, self.store.clear_data):
            with self.assertRaises(ValueError):
                operation(plugin)
        plugin.id = original_id
        self.store.save_preferences({"language": "en", "plugin_tab": "installed"})
        self.assertEqual(self.store.load_preferences()["language"], "en")
        path = self.store.base(plugin) / "settings.json"
        path.unlink()
        outside = self.root / "outside"
        outside.write_text("do not overwrite", encoding="utf-8")
        path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.store.save(plugin)
        self.assertEqual(outside.read_text(), "do not overwrite")

    def test_storage_sizes_are_measured_without_following_symlinks(self):
        plugin = self.install()
        base = self.store.base(plugin)
        (base / "data" / "nested").mkdir()
        (base / "data" / "nested" / "file").write_bytes(b"12345")
        (base / "cache" / "cache-file").write_bytes(b"123")
        (base / "plugin.log").write_bytes(b"1234567")
        outside = self.root / "large-outside"
        outside.write_bytes(b"x" * 10000)
        (base / "data" / "ignored-link").symlink_to(outside)
        slots = {slot.kind: slot for slot in self.store.refresh_storage(plugin)}
        self.assertEqual(slots["data"].size, 5)
        self.assertEqual(slots["cache"].size, 3)
        self.assertEqual(slots["log"].size, 7)
        self.assertEqual(slots["settings"].size, (base / "settings.json").stat().st_size)
        self.assertEqual(plugin.data_size, 15 + slots["settings"].size)
        discovered = self.store.discover()[0]
        self.assertEqual(discovered.data_size, plugin.data_size)
        original = Path.lstat

        def unreadable(path):
            if path == base / "data":
                raise PermissionError("denied")
            return original(path)

        with patch.object(Path, "lstat", unreadable):
            self.store.refresh_storage(plugin)
        self.assertIsNone(plugin.data_size)
        self.assertIsNone(next(slot.size for slot in plugin.storage if slot.kind == "data"))

    def test_keyring_is_lazy_for_plugins_without_credentials(self):
        self.assertFalse(self.store._keyring_checked)
        self.install()
        self.store.discover()
        self.assertFalse(self.store._keyring_checked)

    def test_clear_data_preserves_settings_remove_deletes_them(self):
        plugin = self.install()
        plugin.values["token"] = "secret"
        self.store.save(plugin)
        base = self.store.base(plugin)
        for name in ("data/a", "cache/b", "plugin.log"):
            (base / name).write_text("test", encoding="utf-8")
        self.store.clear_data(plugin)
        self.assertEqual(list((base / "data").iterdir()), [])
        self.assertEqual(list((base / "cache").iterdir()), [])
        self.assertFalse((base / "plugin.log").exists())
        self.assertEqual(self.store.load_settings(plugin)["token"], "secret")
        self.store.remove(plugin)
        self.assertFalse(base.exists())
        self.assertFalse(self.store._session_secrets)

    def test_github_release_is_resolved_and_download_is_pinned_offline(self):
        sha = "a" * 40
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("repo/plugin.json", json.dumps(self.raw))
            archive.writestr("repo/main.py", "# not executed")

        def download(url, host, maximum, destination):
            self.assertEqual(url, f"https://codeload.github.com/owner/repo/zip/{sha}")
            self.assertEqual(host, "codeload.github.com")
            destination.write_bytes(payload.getvalue())

        with patch.object(installer_module, "_api", side_effect=[{"tag_name": "v1"}, {"sha": sha}]) as api:
            with patch.object(installer_module, "_download", side_effect=download):
                candidate = self.installer.prepare_github("https://github.com/owner/repo.git")
        self.assertEqual(api.call_args_list[1].args[0], "/repos/owner/repo/commits/v1")
        self.assertEqual(candidate.github.commit, sha)
        self.assertFalse(candidate.github.auto_update)
        self.assertEqual(candidate.source, "https://github.com/owner/repo")
        self.installer.cleanup(candidate)

    def test_github_default_branch_fallback_and_unsafe_urls(self):
        error = HTTPError("https://api.github.com/test", 404, "missing", {}, None)
        with patch.object(installer_module, "_api", side_effect=[error, {"default_branch": "main"}, {"sha": "short"}]) as api:
            with self.assertRaises(ValueError):
                self.installer.prepare_github("owner/repo")
        self.assertEqual(api.call_args_list[-1].args[0], "/repos/owner/repo/commits/main")
        for repo in ("http://github.com/a/b", "https://evil.invalid/a/b", "https://user@github.com/a/b",
                     "https://github.com:444/a/b", "https://github.com/a/b?token=secret", "../repo"):
            with self.subTest(repo=repo), self.assertRaises(ValueError):
                self.installer.prepare_github(repo)
        for url in ("http://codeload.github.com/x", "https://evil.invalid/x", "https://codeload.github.com.evil/x"):
            with self.assertRaises(ValueError):
                installer_module._safe_url(url, "codeload.github.com")
        redirect = installer_module._Redirects("api.github.com")
        with self.assertRaises(ValueError):
            redirect.redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid/steal")

    def test_environment_is_only_created_explicitly_without_network(self):
        plugin = self.install()
        executable = python_executable(plugin)
        self.assertFalse(executable.exists())
        self.assertFalse(dependencies_ready(plugin))
        logs = []

        with patch.object(installer_module, "_run_process", side_effect=self.fake_process) as runner:
            install_dependencies(plugin, logs.append)
        commands = [call.args[0] for call in runner.call_args_list]
        self.assertEqual(commands[0][1:4], ["-I", "-m", "venv"])
        self.assertFalse(any("install" in command for command in commands))
        self.assertTrue(dependencies_ready(plugin))
        self.assertTrue(logs)
        plugin.requirements = ("example==1.0",)
        self.assertFalse(dependencies_ready(plugin))
        plugin.requirements = ("example>=1.0",)
        with self.assertRaises(ValueError):
            install_dependencies(plugin)

    def test_wheel_transitives_are_resolved_locked_and_reused(self):
        plugin = self.install()
        plugin.requirements = ("pynput==1.8.1",)
        with patch.object(installer_module, "_run_process", side_effect=self.fake_process) as runner:
            install_dependencies(plugin)
            first = next(call.args[0] for call in runner.call_args_list if "install" in call.args[0])
            self.assertNotIn("--no-deps", first)
            self.assertIn("--only-binary=:all:", first)
            self.assertIn("pynput==1.8.1", first)
            self.assertNotIn("six==1.17.0", first)
            lock_path = self.store.base(plugin) / "requirements.lock.json"
            lock = json.loads(lock_path.read_text())
            self.assertIn("six==1.17.0", lock["resolved"])
            self.assertTrue(dependencies_ready(plugin))
            # Venv, install, check, freeze, smoke all share one total deadline.
            self.assertEqual(len({call.kwargs["deadline"] for call in runner.call_args_list}), 1)
            runner.reset_mock()
            install_dependencies(plugin)
            second = next(call.args[0] for call in runner.call_args_list if "install" in call.args[0])
            self.assertNotIn("--no-deps", second)
            self.assertIn("six==1.17.0", second)
            self.assertIn("pyobjc-core==11.1", second)
            self.assertTrue(any(call.args[0][-2:] == ["freeze", "--all"] for call in runner.call_args_list))

    def test_update_cannot_turn_a_saved_secret_into_plaintext(self):
        plugin = self.install()
        plugin.values["token"] = "must-stay-secret"
        self.store.save(plugin)
        self.raw["settings"][0]["kind"] = "text"
        self.write_manifest()
        candidate = self.installer.prepare_local(self.source)
        with self.assertRaises(ValueError):
            self.installer.commit(candidate, replace=True)
        self.assertEqual(self.store.discover()[0].values["token"], "must-stay-secret")
        self.assertNotIn("must-stay-secret", (self.store.base(plugin) / "settings.json").read_text())

    def test_visibility_is_order_independent_but_hidden_types_ranges_are_checked(self):
        self.raw["settings"] = [
            {"key": "text", "kind": "text", "default": "", "required": True,
             "visible_when": {"count": "2"}},
            {"key": "count", "kind": "number", "default": 1, "minimum": 1, "maximum": 3},
            {"key": "hidden", "kind": "number", "default": 1, "minimum": 1,
             "visible_when": {"count": 3}},
        ]
        self.write_manifest()
        specs = load_manifest(self.source).settings
        self.assertEqual(validate_values(specs, {})["text"], "")
        with self.assertRaises(ValueError):
            validate_values(specs, {"count": "2"})
        self.assertEqual(validate_values(specs, {"count": "2", "text": "value"})["count"], 2)
        for values in ({"hidden": 0}, {"hidden": False}, {"text": 0}, {"unknown": "x"},
                       {"text": "x" * 16385}):
            with self.subTest(values=str(values)[:80]), self.assertRaises(ValueError):
                validate_values(specs, values)

    def test_schema_control_counts_and_text_lengths_are_bounded(self):
        fields = [{"key": f"field{i}", "kind": "text", "default": ""} for i in range(65)]
        mutations = [
            ("settings", fields),
            ("actions", [{"key": f"action{i}"} for i in range(65)]),
            ("actions", [{"key": "action", "parameters": fields}]),
            ("actions", [{"key": f"action{i}", "parameters": fields[:64]} for i in range(5)]),
            ("settings", [{"key": "choice", "kind": "choice", "default": 0,
                           "options": [[i, str(i)] for i in range(65)]}]),
            ("settings", [{"key": "text", "kind": "text", "default": "x" * 16385}]),
            ("settings", [{"key": "text", "kind": "text", "default": "", "title": "x" * 257}]),
            ("settings", [{"key": "text", "kind": "text", "default": "", "help": "x" * 4097}]),
            ("name", "x" * 257), ("description", "x" * 4097),
        ]
        original = dict(self.raw)
        for key, value in mutations:
            with self.subTest(key=key, size=len(value)):
                self.raw = dict(original, **{key: value})
                self.write_manifest()
                with self.assertRaises(ValueError):
                    load_manifest(self.source)

    def test_real_prepared_venv_imports_after_commit_relocation(self):
        old = self.install()
        self.raw["version"] = "2.0.0"
        self.write_manifest()
        # Neither preparation nor smoke tests may execute the plugin.
        (self.source / "main.py").write_text("raise RuntimeError('must not run during install')", encoding="utf-8")
        candidate = self.installer.prepare_local(self.source)
        original_directory = candidate.directory
        self.installer.prepare_environment(candidate)  # real ensurepip, no network/dependencies
        self.assertEqual(candidate.directory, original_directory)
        self.assertEqual(load_manifest(old.directory).version, "1.0.0")
        prepared = self.installer._prepared_envs[id(candidate)]
        staged = replace(candidate, directory=prepared / "code")
        self.assertTrue(dependencies_ready(staged))
        purelib = installer_module.subprocess.check_output(
            [str(python_executable(staged)), "-I", "-c",
             "import sysconfig; print(sysconfig.get_paths()['purelib'])"], text=True, timeout=15).strip()
        (Path(purelib) / "staged_fixture.py").write_text("VALUE = 'moved-import-ok'\n", encoding="utf-8")
        updated = self.installer.commit(candidate, replace=True)
        self.assertTrue(dependencies_ready(updated))
        result = installer_module.subprocess.check_output(
            [str(python_executable(updated)), "-I", "-c",
             "import json, sys, pip, staged_fixture; print(json.dumps([staged_fixture.VALUE, sys.prefix]))"],
            text=True, timeout=15)
        value, prefix = json.loads(result)
        self.assertEqual(value, "moved-import-ok")
        self.assertEqual(Path(prefix).resolve(), (self.store.base(updated) / "venv").resolve())
        self.assertFalse(prepared.exists())
        self.assertFalse(original_directory.exists())
        self.assertNotIn(id(candidate), self.installer._prepared_envs)

    def test_preparation_failure_and_cancellation_keep_old_version_ready(self):
        old = self.install()
        with patch.object(installer_module, "_run_process", side_effect=self.fake_process):
            install_dependencies(old)
        old_code = (old.directory / "plugin.json").read_bytes()
        old_lock = (self.store.base(old) / "requirements.lock.json").read_bytes()
        candidate = self.installer.prepare_local(self.source)
        original_directory = candidate.directory
        with patch.object(installer_module, "_run_process", side_effect=RuntimeError("pip failed")):
            with self.assertRaises(RuntimeError):
                self.installer.prepare_environment(candidate)
        self.assertEqual(candidate.directory, original_directory)
        self.assertEqual((old.directory / "plugin.json").read_bytes(), old_code)
        self.assertEqual((self.store.base(old) / "requirements.lock.json").read_bytes(), old_lock)
        self.assertTrue(dependencies_ready(old))
        self.assertNotIn(id(candidate), self.installer._prepared_envs)
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            self.installer.prepare_environment(candidate, cancel_event=event)
        self.assertTrue(dependencies_ready(old))

    def test_prepared_commit_failure_rolls_back_environment_and_lock(self):
        old = self.install()
        base = self.store.base(old)
        with patch.object(installer_module, "_run_process", side_effect=self.fake_process):
            install_dependencies(old)
        python_executable(old).write_text("old-interpreter", encoding="utf-8")
        old_lock = (base / "requirements.lock.json").read_bytes()
        old_state = (base / "settings.json").read_bytes()
        self.raw["version"] = "2.0.0"
        self.write_manifest()
        candidate = self.installer.prepare_local(self.source)
        with patch.object(installer_module, "_run_process", side_effect=self.fake_process):
            self.installer.prepare_environment(candidate)
            with patch("hmm_gesture_studio.plugins.storage.atomic_json", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.installer.commit(candidate, replace=True)
        self.assertEqual(python_executable(old).read_text(), "old-interpreter")
        self.assertEqual((base / "requirements.lock.json").read_bytes(), old_lock)
        self.assertEqual((base / "settings.json").read_bytes(), old_state)
        self.assertEqual(load_manifest(old.directory).version, "1.0.0")
        self.assertTrue(dependencies_ready(old))
        prepared = self.installer._prepared_envs[id(candidate)]
        self.assertTrue(dependencies_ready(replace(candidate, directory=prepared / "code")))
        self.installer.cleanup(candidate)
        self.assertFalse(prepared.exists())
        self.assertTrue(python_executable(old).exists())

    def test_relocated_environment_smoke_failure_rolls_back(self):
        old = self.install()
        with patch.object(installer_module, "_run_process", side_effect=self.fake_process):
            install_dependencies(old)
            candidate = self.installer.prepare_local(self.source)
            self.installer.prepare_environment(candidate)
        old_state = (self.store.base(old) / "settings.json").read_bytes()
        with patch.object(installer_module, "_smoke_environment", side_effect=RuntimeError("cannot import")):
            with self.assertRaises(RuntimeError):
                self.installer.commit(candidate, replace=True)
        self.assertTrue(dependencies_ready(old))
        self.assertEqual((self.store.base(old) / "settings.json").read_bytes(), old_state)
        self.assertTrue(candidate.directory.exists())

    def test_runner_cancels_silent_process_and_terminates_process_group(self):
        event = threading.Event()
        timer = threading.Timer(0.15, event.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with patch.object(installer_module, "_terminate_process_tree",
                          wraps=installer_module._terminate_process_tree) as terminate:
            with self.assertRaises(CancelledError):
                installer_module._run_process(
                    [sys.executable, "-c", "import sys,time; sys.stdout.write('no newline'); sys.stdout.flush(); time.sleep(30)"],
                    deadline=time.monotonic() + 10, cancel_event=event)
        terminate.assert_called_once()
        self.assertIsNotNone(terminate.call_args.args[0].poll())
        self.assertLess(time.monotonic() - started, 8)

    def test_runner_deadline_and_bounded_output(self):
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            installer_module._run_process([sys.executable, "-c", "import time; time.sleep(30)"],
                                          deadline=time.monotonic() + 0.15)
        self.assertLess(time.monotonic() - started, 8)
        with patch.object(installer_module, "MAX_PROCESS_OUTPUT", 100):
            with self.assertRaises(RuntimeError):
                installer_module._run_process([sys.executable, "-c", "print('x'*1000)"],
                                              deadline=time.monotonic() + 10, capture=True)

    def test_install_cancellation_and_deadline_leave_no_ready_marker(self):
        plugin = self.install()
        for failure in (CancelledError("cancelled"), TimeoutError("deadline")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(installer_module, "_run_process", side_effect=failure):
                    with self.assertRaises(type(failure)):
                        install_dependencies(plugin)
                self.assertFalse(dependencies_ready(plugin))
        event = threading.Event()
        event.set()
        with patch.object(installer_module, "_run_process") as runner:
            with self.assertRaises(CancelledError):
                install_dependencies(plugin, cancel_event=event)
        runner.assert_not_called()

    def test_check_and_freeze_honor_shared_cancellation(self):
        plugin = self.install()
        plugin.requirements = ("pynput==1.8.1",)
        for stop_command in ("check", "freeze"):
            event = threading.Event()

            def cancel_at(command, **kwargs):
                if stop_command in command:
                    event.set()
                return self.fake_process(command, **kwargs)

            with self.subTest(stop_command=stop_command):
                with patch.object(installer_module, "_run_process", side_effect=cancel_at):
                    with self.assertRaises(CancelledError):
                        install_dependencies(plugin, cancel_event=event)
                self.assertFalse(dependencies_ready(plugin))

    def test_manifest_event_timeout_defaults_and_bounds(self):
        self.assertEqual(load_manifest(self.source).event_timeout, 120)
        for value in (0.25, 180, 3600):
            self.raw["event_timeout"] = value
            self.write_manifest()
            self.assertEqual(load_manifest(self.source).event_timeout, value)
        for value in (0, -1, 3600.1, True, "120", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.raw["event_timeout"] = value
                self.write_manifest()
                with self.assertRaises(ValueError):
                    load_manifest(self.source)

    def test_duplicate_json_keys_and_finite_numbers(self):
        path = self.source / "plugin.json"
        path.write_text('{"id":"a","id":"b"}', encoding="utf-8")
        with self.assertRaises(ValueError):
            load_manifest(path)
        self.write_manifest()
        self.raw["settings"][1]["default"] = float("nan")
        self.write_manifest()
        with self.assertRaises(ValueError):
            load_manifest(path)


if __name__ == "__main__":
    unittest.main()
