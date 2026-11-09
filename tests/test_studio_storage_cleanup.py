# SPDX-License-Identifier: MPL-2.0
"""Bulk plugin cleanup contracts, using real storage and queued worker jobs."""
from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from test_studio_qt_plugins import OTHER, PLUGIN, PluginTestCase
from hmm_gesture_studio.qt.i18n import language_manager, resolve


def setUpModule():
    previous = language_manager.preference
    language_manager.set_preference("zh", persist=False)
    unittest.addModuleCleanup(language_manager.set_preference, previous, persist=False)


class StorageCleanupTests(PluginTestCase):
    def seed_storage(self, plugin):
        base = self.root / plugin.id
        paths = [base / "cache" / "nested" / "cached.bin",
                 base / "data" / "persistent.bin",
                 base / "plugin.log", base / "plugin.log.1"]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"storage sentinel")
        self.p.store.refresh_storage(plugin)
        return paths

    def assert_untouched(self, paths):
        for path in paths:
            self.assertEqual(path.read_bytes(), b"storage sentinel", str(path))

    def successful_cleanup(self, *, include_data):
        first = self.install()
        self.set_model()
        self.p.add_binding()
        self.p.set_setting("target", "keep configuration")
        self.p.set_setting("token", "keep secret")
        self.p.save_settings()
        self.ready_environment(first)
        second = self.install(OTHER)
        self.ready_environment(second)
        plugins = (first, second)
        paths = [self.seed_storage(plugin) for plugin in plugins]
        self.p.store.save_preferences({"autostart": False})
        sentinels = [self.dir / "data" / "capture.bin",
                     self.dir / "audio" / "recording.wav",
                     self.root / ".models" / "unrelated-model.bin"]
        for path in sentinels:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"not plugin storage")
        preserved = sentinels + [self.root / "preferences.json"]
        preserved += list((self.root / ".models").rglob("*"))
        for plugin in plugins:
            base = self.root / plugin.id
            preserved += [base / "settings.json"]
            preserved += list((base / "code").rglob("*"))
            preserved += list((base / "venv").rglob("*"))
        before = {path: path.read_bytes() for path in preserved if path.is_file()}
        secrets = dict(self.vault.values)
        storage = [plugin.storage for plugin in plugins]
        self.p.close_plugin()  # Bulk cleanup must not depend on a selection.
        self.dialogs.ask.reset_mock()
        method = "clear_data" if include_data else "clear_cache"
        unused = "clear_cache" if include_data else "clear_data"
        with patch.object(self.p.store, method, wraps=getattr(self.p.store, method)) as clear, \
                patch.object(self.p.store, unused, wraps=getattr(self.p.store, unused)) as other:
            if include_data:
                self.p.clear_storage(include_data=True)
            else:
                self.p.clear_storage()  # Exercise the default, not an explicit False.
            self.dialogs.ask.assert_called_once()
            self.assertTrue(self.dialogs.ask.call_args.kwargs["danger"])
            clear.assert_not_called()
            self.assertEqual(len(self.p.executor.pending), 1)
            self.assertIsNotNone(self.p.busy_text)
            self.assertIsNone(self.p.storage_cleanup_status)
            for files in paths:
                self.assert_untouched(files)
            self.p.executor.run_next()
            self.assertIsNotNone(self.p.busy_text)
            self.assertIsNone(self.p.storage_cleanup_status)
            for plugin, old_storage in zip(plugins, storage):
                self.assertIs(plugin.storage, old_storage, "Worker must not mutate UI models")
            self.c.drain_events()
            self.assertEqual([call.args[0].id for call in clear.call_args_list], [PLUGIN, OTHER])
            other.assert_not_called()
        for files in paths:
            cache, data, log, rotated_log = files
            for path in (cache, log, rotated_log):
                self.assertFalse(path.exists(), str(path))
            self.assertEqual(data.exists(), not include_data)
            if not include_data:
                self.assertEqual(data.read_bytes(), b"storage sentinel")
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertEqual(self.vault.values, secrets)
        restored = {plugin.id: plugin for plugin in self.p.store.discover()}
        self.assertEqual(restored[PLUGIN].bindings, first.bindings)
        self.assertEqual(restored[PLUGIN].values["target"], "keep configuration")
        self.assertEqual(restored[PLUGIN].values["token"], "keep secret")
        self.assertIsNone(self.p.busy_text)
        self.assertTrue(self.p.can_clear_storage)
        expected = "已清除所有插件的数据、缓存和日志。" if include_data else "已清理所有插件的缓存和日志。"
        self.assertEqual(resolve(self.p.storage_cleanup_status), expected)
        self.assertEqual(resolve(self.p.storage_cleanup_hint), expected)
        self.assertIn(("info", expected), self.logs)
        self.assertFalse(self.p.service_running)
        self.runtime.start.assert_not_called()
        self.runtime.stop.assert_not_called()
        self.dialogs.error.assert_not_called()

    def test_default_cleans_all_plugin_caches_and_logs_but_preserves_data(self):
        self.successful_cleanup(include_data=False)

    def test_include_data_cleans_all_plugin_data_and_preserves_host_files(self):
        self.successful_cleanup(include_data=True)

    def test_cancel_does_not_queue_work_or_delete_in_either_mode(self):
        files = self.seed_storage(self.install())
        self.dialogs.ask.return_value = False
        for include_data in (False, True):
            with self.subTest(include_data=include_data):
                self.dialogs.ask.reset_mock()
                self.p.clear_storage(include_data=include_data)
                self.dialogs.ask.assert_called_once()
                self.assertTrue(self.dialogs.ask.call_args.kwargs["danger"])
                self.assertFalse(self.p.executor.pending)
                self.assertIsNone(self.p.storage_cleanup_status)
                self.assert_untouched(files)
        self.runtime.stop.assert_not_called()

    def blockers(self):
        return [(self.p, "service_running", True),
                (self.p, "busy_text", "another operation"),
                (self.p, "updating", {PLUGIN}),
                (self.p, "recording_session", {"owner": PLUGIN}),
                (self.p, "_after_recording", lambda: None),
                (self.c, "closing", True)]

    def test_running_busy_recording_updating_and_closing_disable_cleanup(self):
        files = self.seed_storage(self.install())
        for target, attribute, value in self.blockers():
            for include_data in (False, True):
                with self.subTest(attribute=attribute, include_data=include_data), \
                        patch.object(target, attribute, value):
                    self.dialogs.ask.reset_mock()
                    self.assertFalse(self.p.can_clear_storage)
                    self.assertTrue(resolve(self.p.storage_cleanup_hint))
                    self.p.clear_storage(include_data=include_data)
                    self.dialogs.ask.assert_not_called()
                    self.assertFalse(self.p.executor.pending)
                    self.assertIs(getattr(target, attribute), value)
                    self.assert_untouched(files)
        self.runtime.stop.assert_not_called()

    def test_any_running_runtime_blocks_even_when_service_flag_is_off(self):
        self.install()
        other = self.install(OTHER)
        files = self.seed_storage(other)
        self.runtime.start(other.id)
        self.dialogs.ask.reset_mock()
        self.assertFalse(self.p.service_running)
        self.assertFalse(self.p.can_clear_storage)
        self.p.clear_storage(include_data=True)
        self.dialogs.ask.assert_not_called()
        self.assertFalse(self.p.executor.pending)
        self.assertTrue(self.runtime.running(other.id))
        self.runtime.stop.assert_not_called()
        self.assert_untouched(files)

    def test_no_plugins_disables_cleanup_and_explains_why(self):
        self.assertFalse(self.p.can_clear_storage)
        self.assertIn("尚未安装插件", resolve(self.p.storage_cleanup_hint))
        self.p.clear_storage()
        self.dialogs.ask.assert_not_called()
        self.assertFalse(self.p.executor.pending)

    def test_confirmation_rechecks_every_state_gate(self):
        plugin = self.install()
        files = self.seed_storage(plugin)
        for target, attribute, value in self.blockers():
            with self.subTest(attribute=attribute), patch.object(target, attribute, getattr(target, attribute)):
                def confirm(*_args, **_kwargs):
                    setattr(target, attribute, value)
                    return True
                self.dialogs.ask.side_effect = confirm
                self.p.clear_storage(include_data=True)
                self.assertFalse(self.p.executor.pending)
                self.assert_untouched(files)
        self.dialogs.ask.side_effect = lambda *_a, **_k: (self.runtime.start(plugin.id), True)[1]
        self.p.clear_storage()
        self.assertFalse(self.p.executor.pending)
        self.assert_untouched(files)
        self.runtime.stop.assert_not_called()

    def test_confirmation_rechecks_plugin_object_list_not_only_ids(self):
        first, second = self.install(), self.install(OTHER)
        files = self.seed_storage(first) + self.seed_storage(second)
        original = self.p.plugins
        replacements = ([first], [first, second, replace(second)],
                        [second, first], [replace(first), second])
        for replacement in replacements:
            with self.subTest(ids=[plugin.id for plugin in replacement]), \
                    patch.object(self.p, "plugins", original):
                self.dialogs.ask.side_effect = lambda *_a, **_k: (setattr(self.p, "plugins", replacement), True)[1]
                self.p.clear_storage(include_data=True)
                self.assertFalse(self.p.executor.pending)
                self.assert_untouched(files)

    def test_service_and_duplicate_cleanup_stay_disabled_until_completion_is_drained(self):
        plugin = self.install()
        self.ready_environment(plugin)
        plugin.enabled = True
        self.seed_storage(plugin)
        self.p.clear_storage()
        self.dialogs.ask.reset_mock()
        for worker_finished in (False, True):
            with self.subTest(worker_finished=worker_finished):
                if worker_finished:
                    self.p.executor.run_next()
                self.assertFalse(self.p.can_clear_storage)
                self.assertEqual(self.p.storage_cleanup_hint, self.p.busy_text)
                self.p.set_service_running(True)
                self.p.clear_storage(include_data=True)
                self.assertFalse(self.p.service_running)
                self.runtime.start.assert_not_called()
                self.dialogs.ask.assert_not_called()
        self.c.drain_events()
        self.assertTrue(self.p.can_clear_storage)
        self.p.set_service_running(True)
        self.assertTrue(self.p.service_running)
        self.runtime.start.assert_called_once()

    def test_partial_failure_reports_error_and_continues_with_remaining_plugins(self):
        first, second = self.install(), self.install(OTHER)
        first_files, second_files = self.seed_storage(first), self.seed_storage(second)
        clear = self.p.store.clear_cache
        def fail_first(plugin):
            if plugin.id == first.id:
                first_files[2].unlink()  # A failed operation may already have deleted files.
                raise OSError("fixture permission denied")
            clear(plugin)
        with patch.object(self.p.store, "clear_cache", side_effect=fail_first) as cleanup:
            self.p.clear_storage()
            self.finish_jobs()
        self.assertEqual([call.args[0].id for call in cleanup.call_args_list], [PLUGIN, OTHER])
        self.assertTrue(first_files[0].exists())
        self.assertFalse(first_files[2].exists())
        self.assertFalse(second_files[0].exists())
        self.assertFalse(second_files[3].exists())
        self.assertTrue(second_files[1].exists())
        self.assertIn("未全部完成", resolve(self.p.storage_cleanup_status))
        self.assertEqual(self.p.storage_cleanup_hint, self.p.storage_cleanup_status)
        self.dialogs.error.assert_called_once()
        error = " ".join(resolve(arg) for arg in self.dialogs.error.call_args.args)
        self.assertIn(PLUGIN, error)
        self.assertIn("fixture permission denied", error)
        self.assertIn(("warning", resolve(self.p.storage_cleanup_status)), self.logs)
        self.assertIsNone(self.p.busy_text)
        self.assertTrue(self.p.can_clear_storage)
        self.runtime.stop.assert_not_called()
        self.p.clear_storage()
        self.assertIsNone(self.p.storage_cleanup_status)
        self.finish_jobs()
        self.assertEqual(resolve(self.p.storage_cleanup_status), "已清理所有插件的缓存和日志。")

    def test_store_rejects_external_target_symlinks_before_any_deletion(self):
        plugin = self.install()
        base = self.root / plugin.id
        outside = self.dir / "outside"
        outside.mkdir()
        sentinel = outside / "keep.bin"
        sentinel.write_bytes(b"outside storage")
        for method, names in ((self.p.store.clear_cache, ("cache", "plugin.log", "plugin.log.1")),
                              (self.p.store.clear_data, ("data", "cache", "plugin.log", "plugin.log.1"))):
            for name in names:
                with self.subTest(method=method.__name__, target=name):
                    target = base / name
                    if target.exists():
                        if target.is_dir():
                            target.rmdir()
                        else:
                            target.unlink()
                    target.symlink_to(outside if name in ("cache", "data") else sentinel)
                    untouched = []
                    for other_name in ("data", "cache", "plugin.log", "plugin.log.1"):
                        if other_name == name:
                            continue
                        path = base / other_name
                        if other_name in ("data", "cache"):
                            path = path / "validation-sentinel.bin"
                        path.write_bytes(b"storage sentinel")
                        untouched.append(path)
                    try:
                        with self.assertRaises(ValueError):
                            method(plugin)
                        self.assertTrue(target.is_symlink())
                        self.assert_untouched(untouched)
                        self.assertEqual(sentinel.read_bytes(), b"outside storage")
                    finally:
                        for path in untouched:
                            path.unlink(missing_ok=True)
                        target.unlink()
                        if name in ("cache", "data"):
                            target.mkdir()

    def test_store_nested_symlinks_and_rotated_logs_do_not_touch_external_files(self):
        plugin = self.install()
        outside = self.dir / "outside"
        outside.mkdir()
        sentinel = outside / "keep.bin"
        sentinel.write_bytes(b"outside storage")
        for include_data in (False, True):
            with self.subTest(include_data=include_data):
                files = self.seed_storage(plugin)
                cache = self.root / plugin.id / "cache"
                (cache / "directory-link").symlink_to(outside, target_is_directory=True)
                (cache / "file-link").symlink_to(sentinel)
                method = self.p.store.clear_data if include_data else self.p.store.clear_cache
                method(plugin)
                self.assertEqual(list(cache.iterdir()), [])
                self.assertFalse(files[2].exists())
                self.assertFalse(files[3].exists())
                self.assertEqual(files[1].exists(), not include_data)
                self.assertEqual(sentinel.read_bytes(), b"outside storage")
                method(plugin)  # Missing logs and already-empty directories are harmless.


if __name__ == "__main__":
    unittest.main()
