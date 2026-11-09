# SPDX-License-Identifier: MPL-2.0
"""The plugin page preview: sample data, in memory, with no disk or plugin code."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:
    raise unittest.SkipTest(f"Qt GUI libraries unavailable: {exc}")

from hmm_gesture_studio.device_history import DeviceHistory
from hmm_gesture_studio.qt.controller import StudioController
from hmm_gesture_studio.qt.i18n import language_manager
from hmm_gesture_studio.qt.plugins_controller import parse_github_repo
from hmm_gesture_studio.qt.theme import apply_theme
from hmm_gesture_studio.qt.window import MainWindow

MEDIA, WEBHOOK, SLIDES = "example.media-control", "example.webhook", "example.slides"


def setUpModule():
    previous = language_manager.preference
    language_manager.set_preference("zh", persist=False)
    unittest.addModuleCleanup(language_manager.set_preference, previous, persist=False)


class PluginTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        apply_theme(cls.qt)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.dialogs = Mock()
        self.dialogs.ask.return_value = True
        self.dialogs.directory.return_value = str(self.dir / "source")
        self.c = StudioController(self.dialogs, worker=Mock(), history=DeviceHistory(self.dir / "devices.json"),
                                  directory=self.dir / "data", audio_directory=self.dir / "audio")
        self.addCleanup(self.c.executor.shutdown, wait=False)
        self.p = self.c.plugins
        self.pending = []  # Finish simulated work explicitly, without timers.
        self.p.schedule = self.pending.append

    def finish_pending(self):
        while self.pending:
            self.pending.pop(0)()


class PluginRulesTests(PluginTestCase):
    def test_every_plugin_gets_isolated_storage_under_the_studio_root(self):
        paths = [slot.path for plugin in self.p.plugins for slot in plugin.storage]
        self.assertEqual(len(paths), len(set(paths)))
        for plugin in self.p.plugins:
            for slot in plugin.storage:
                self.assertTrue(slot.path.is_relative_to(self.p.root / plugin.id))

    def test_gesture_binds_once_per_plugin(self):
        self.p.open_plugin(MEDIA)
        plugin = self.p.selected
        before = list(plugin.bindings)
        self.p.set_binding_gesture(0, plugin.bindings[1][0])
        self.assertEqual(plugin.bindings, before)
        self.assertNotIn(plugin.bindings[1][0], self.p.free_gestures(plugin, 0))
        while self.p.can_add_binding(plugin):
            self.p.add_binding()
        self.assertEqual(len(plugin.bindings), len(self.p.gestures))
        self.assertEqual(len({gesture for gesture, _ in plugin.bindings}), len(self.p.gestures))

    def test_failed_plugin_is_read_only(self):
        self.p.open_plugin(SLIDES)
        plugin = self.p.selected
        self.p.set_enabled(SLIDES, True)
        self.p.set_setting("loop", True)
        self.p.remove_binding(0)
        self.assertFalse(plugin.enabled)
        self.assertFalse(plugin.values["loop"])
        self.assertEqual(len(plugin.bindings), 1)

    def test_number_settings_report_range_and_reset_restores_default(self):
        self.p.open_plugin(MEDIA)
        plugin = self.p.selected
        spec = next(item for item in plugin.settings if item.key == "volume_step")
        for bad in ("", "abc", "0", "21", "nan"):
            self.p.set_setting("volume_step", bad)
            self.assertIsNotNone(self.p.setting_error(plugin, spec), bad)
        self.p.set_setting("volume_step", "10")
        self.assertIsNone(self.p.setting_error(plugin, spec))
        self.p.reset_settings()
        self.assertEqual(plugin.values["volume_step"], spec.default)

    def test_registration_previews_then_adds_and_rejects_duplicates(self):
        self.p.begin_registration()
        candidate = self.p.candidate
        self.assertEqual(candidate.source, str(self.dir / "source"))
        self.assertFalse(self.p.can_register)
        self.p.confirm_registration()
        self.assertIs(self.p.selected, candidate)
        self.p.begin_registration()
        self.assertIsNone(self.p.candidate)
        self.dialogs.error.assert_called_once()

    def test_cancelled_picker_and_cancelled_review_add_nothing(self):
        count = len(self.p.plugins)
        self.dialogs.directory.return_value = ""
        self.p.begin_registration()
        self.assertIsNone(self.p.candidate)
        self.dialogs.directory.return_value = "/somewhere"
        self.p.begin_registration()
        self.p.cancel_registration()
        self.assertEqual(len(self.p.plugins), count)

    def test_destructive_actions_need_confirmation_and_recheck(self):
        self.p.open_plugin(MEDIA)
        plugin = self.p.selected
        size = plugin.data_size
        self.dialogs.ask.return_value = False
        self.p.clear_data()
        self.p.remove_plugin()
        self.assertEqual(plugin.data_size, size)
        self.assertIn(plugin, self.p.plugins)

        def removed_during_dialog(*_args, **_kwargs):
            self.p.plugins.remove(plugin)
            return True

        self.dialogs.ask.side_effect = removed_during_dialog
        self.p.clear_data()
        self.assertEqual(plugin.data_size, size)

    def test_clear_data_keeps_settings_file_and_remove_returns_to_list(self):
        self.p.open_plugin(MEDIA)
        plugin = self.p.selected
        self.p.clear_data()
        sizes = {slot.kind: slot.size for slot in plugin.storage}
        self.assertEqual((sizes["data"], sizes["cache"]), (0, 0))
        self.assertGreater(sizes["settings"], 0)
        self.assertFalse(self.p.can_clear_data(plugin))
        self.p.remove_plugin()
        self.assertIsNone(self.p.selected)
        self.assertIsNone(self.p.plugin(MEDIA))


class GitHubRepoParsingTests(unittest.TestCase):
    def test_accepts_only_github_https_and_shorthand(self):
        for text in ("owner/repo", " owner/repo ", "owner/repo.git", "owner/repo/", "github.com/owner/repo",
                     "https://github.com/owner/repo", "https://GitHub.com/owner/repo.git"):
            self.assertEqual(parse_github_repo(text), "owner/repo", text)
        for text in ("", "owner", "a/b/c", "http://github.com/owner/repo", "https://github.com.evil.test/owner/repo",
                     "https://user@github.com/owner/repo", "https://github.com:444/owner/repo",
                     "https://github.com/owner/repo?x=1", "https://gitlab.com/owner/repo", "owner/..", "../repo",
                     "own er/repo", "https://[bad/owner/repo"):
            self.assertIsNone(parse_github_repo(text), text)


class GitHubFlowTests(PluginTestCase):
    def test_fetch_shows_busy_then_review_and_adds_with_chosen_auto_update(self):
        self.p.open_github_form()
        self.assertFalse(self.p.can_register)
        self.p.set_github_input("https://github.com/Someone/Focus")
        self.p.fetch_github()
        self.assertTrue(self.p.github_fetching)
        self.assertFalse(self.p.can_fetch_github)
        self.finish_pending()
        candidate = self.p.candidate
        self.assertFalse(self.p.github_form_open)
        self.assertEqual(candidate.github.repo, "Someone/Focus")
        self.p.set_candidate_auto_update(False)
        self.p.confirm_registration()
        self.assertFalse(self.p.selected.github.auto_update)
        self.assertTrue(self.p.selected.storage[0].path.is_relative_to(self.p.root / candidate.id))

    def test_invalid_and_duplicate_repositories_report_inline(self):
        self.p.open_github_form()
        self.p.set_github_input("https://example.com/a/b")
        self.p.fetch_github()
        self.assertIsNotNone(self.p.github_error)
        self.assertFalse(self.pending)
        self.p.set_github_input("a/b")
        self.assertIsNone(self.p.github_error)
        self.p.fetch_github()
        self.finish_pending()
        self.p.confirm_registration()
        self.p.open_github_form()
        self.p.set_github_input("A/B")
        self.p.fetch_github()
        self.assertIn("已添加", self.p.github_error)
        self.assertFalse(self.pending)

    def test_cancelled_fetch_is_ignored_when_it_finishes(self):
        self.p.open_github_form()
        self.p.set_github_input("a/b")
        self.p.fetch_github()
        self.p.close_github_form()
        self.finish_pending()
        self.assertIsNone(self.p.candidate)
        self.assertFalse(self.p.github_form_open)

    def test_update_locks_plugin_until_finished(self):
        plugin = self.p.plugin(MEDIA)
        self.p.open_plugin(MEDIA)
        self.p.install_update(MEDIA)
        self.assertFalse(self.p.can_edit(plugin))
        self.assertFalse(self.p.can_toggle(plugin))
        self.assertFalse(self.p.can_install(plugin))
        self.p.remove_plugin()
        self.dialogs.ask.assert_not_called()
        self.assertEqual(plugin.version, "1.2.0")
        self.finish_pending()
        self.assertEqual(plugin.version, "1.3.0")
        self.assertIsNone(plugin.github.update)
        self.assertTrue(self.p.can_edit(plugin))

    def test_new_permissions_need_confirmation_even_with_auto_update(self):
        plugin = self.p.plugin(WEBHOOK)
        self.assertTrue(plugin.github.auto_update)
        count = len(plugin.permissions)
        self.assertIn("暂停", self.p.update_status(plugin)[0])
        self.dialogs.ask.return_value = False
        self.p.install_update(WEBHOOK)
        self.assertFalse(self.pending)
        self.dialogs.ask.return_value = True
        self.p.install_update(WEBHOOK)
        self.finish_pending()
        self.assertEqual(plugin.version, "0.5.0")
        self.assertEqual(len(plugin.permissions), count + 1)

    def test_update_rechecks_after_permission_dialog(self):
        plugin = self.p.plugin(WEBHOOK)

        def removed_during_dialog(*_args, **_kwargs):
            self.p.plugins.remove(plugin)
            return True

        self.dialogs.ask.side_effect = removed_during_dialog
        self.p.install_update(WEBHOOK)
        self.assertFalse(self.pending)
        self.assertFalse(self.p.updating)

    def test_check_skips_local_and_tolerates_removal(self):
        self.p.check_updates()
        self.assertEqual(self.p.checking, {MEDIA, WEBHOOK})
        self.assertFalse(self.p.can_check_all)
        self.p.plugins.remove(self.p.plugin(WEBHOOK))
        self.finish_pending()
        self.assertEqual(self.p.checking, set())
        self.assertEqual(self.p.plugin(MEDIA).github.last_checked, "刚刚")


class PluginPageTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.win = MainWindow(controller=self.c, merge_title_bar=False)
        self.win.resize(1280, 860)
        self.win.show()
        self.c.start()
        self.c._timer.stop()
        self.addCleanup(self._destroy)
        self.win.navigate("plugins")
        self.page = self.win.pages["plugins"]
        self.pump()

    def _destroy(self):
        self.win._may_close = True
        self.win.close()
        self.win.deleteLater()
        self.pump()

    def pump(self):
        self.qt.processEvents()

    def test_list_and_detail_swap_and_preview_is_labelled(self):
        self.assertTrue(self.page.list_section.isVisible())
        self.assertFalse(self.page.detail_head.isVisible())
        self.page._cards[MEDIA].manage.click()
        self.pump()
        self.assertFalse(self.page.list_section.isVisible())
        self.assertEqual(self.page.detail_title.text(), "媒体控制")
        shown = [child.text() for child in self.page.detail_head.findChildren(type(self.page.detail_title))
                 if child.isVisible()]
        self.assertTrue(any("示例数据" in text for text in shown))

    def test_settings_form_comes_from_the_declaration(self):
        self.p.open_plugin(WEBHOOK)
        self.pump()
        self.assertEqual([spec.kind for spec, _, _ in self.page._setting_controls],
                         ["text", "secret", "number", "switch"])
        _spec, timeout, error = self.page._setting_controls[2]
        self.assertFalse(error.isVisible())
        timeout.setText("999")
        timeout.textEdited.emit("999")
        self.pump()
        self.assertEqual(self.p.selected.values["timeout"], "999")
        self.assertTrue(error.isVisible())

    def test_failed_plugin_states_the_reason_and_disables_controls(self):
        self.p.open_plugin(SLIDES)
        self.pump()
        self.assertEqual(self.page.detail_status.text(), "加载失败")
        self.assertIn("main.py", self.page.detail_error.text())
        self.assertFalse(self.page.detail_enabled.isEnabled())
        self.assertFalse(self.page.add_binding.isEnabled())

    def test_detail_retranslates_generated_rows(self):
        self.p.open_plugin(MEDIA)
        self.pump()
        language_manager.set_preference("en", persist=False)
        self.addCleanup(language_manager.set_preference, "zh", persist=False)
        self.pump()
        self.assertEqual(self.page.detail_title.text(), "Media control")
        self.assertEqual(self.page.events_table.item(0, 2).text(), "Play / pause")
        self.assertEqual(self.page.events_table.item(0, 1).text(), "snap")  # Gesture names stay as given.

    def test_detail_fits_minimum_window_width(self):
        self.win.resize(1100, 720)
        for plugin_id in (MEDIA, WEBHOOK, SLIDES):
            self.p.open_plugin(plugin_id)
            self.pump()
            with self.subTest(plugin=plugin_id):
                self.assertLessEqual(self.page.widget().minimumSizeHint().width(), self.page.viewport().width())

    def test_github_form_busy_error_and_review_render(self):
        page = self.page
        page.add_github.click()
        self.pump()
        self.assertTrue(page.github_card.isVisible())
        self.assertFalse(page.github_fetch.isEnabled())
        self.assertFalse(page.github_error.isVisible())
        page.github_input.setText("not a repo")
        page.github_input.textEdited.emit("not a repo")
        page.github_fetch.click()
        self.pump()
        self.assertTrue(page.github_error.isVisible())
        page.github_input.setText("a/b")
        page.github_input.textEdited.emit("a/b")
        page.github_fetch.click()
        self.pump()
        self.assertTrue(page.github_busy.isVisible())
        self.finish_pending()
        self.pump()
        self.assertFalse(page.github_card.isVisible())
        self.assertTrue(page.candidate_card.isVisible())
        self.assertTrue(page.candidate_auto.isVisible())
        self.assertIn("GitHub", page.candidate_note.text())

    def test_source_section_follows_plugin_source(self):
        page = self.page
        self.assertIn("v1.3.0", page._cards[MEDIA].update_badge.text())
        self.assertFalse(page._cards[SLIDES].update_badge.isVisible())
        self.p.open_plugin(SLIDES)
        self.pump()
        self.assertTrue(page.local_box.isVisible())
        self.assertFalse(page.github_box.isVisible())
        self.p.open_plugin(WEBHOOK)
        self.pump()
        self.assertTrue(page.github_box.isVisible())
        self.assertTrue(page.new_permissions.isVisible())
        self.assertIn("暂停", page.update_status.text())

    def test_update_progress_and_new_permission_row_render(self):
        page = self.page
        self.p.open_plugin(WEBHOOK)
        self.pump()
        rows = page.permission_rows.count()
        page.install.click()
        self.pump()
        self.assertTrue(page.update_busy.isVisible())
        self.assertFalse(page.update_status.isVisible())
        self.assertFalse(page.remove.isEnabled())
        self.finish_pending()
        self.pump()
        self.assertFalse(page.update_busy.isVisible())
        self.assertEqual(page.update_status.text(), "已是最新版本")
        self.assertFalse(page.install.isVisible())
        self.assertEqual(page.permission_rows.count(), rows + 2)  # A hairline and the new row.

    def test_removed_binding_row_disappears(self):
        self.p.open_plugin(MEDIA)
        self.pump()
        rows = self.page.binding_grid.rowCount()
        self.p.remove_binding(0)
        self.pump()
        self.assertEqual(len(self.p.selected.bindings), 2)
        self.assertLessEqual(self.page.binding_grid.count(), rows * 4)


if __name__ == "__main__":
    unittest.main()
