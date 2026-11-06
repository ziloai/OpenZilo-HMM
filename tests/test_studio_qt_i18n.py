# SPDX-License-Identifier: MPL-2.0
"""Language preferences and in-place retranslation, without hardware/network."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import string
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QEvent, QSettings
    from PySide6.QtWidgets import QApplication
except ImportError as exc:
    raise unittest.SkipTest(f"Qt GUI libraries unavailable: {exc}")

from hmm_gesture_studio.device_history import DeviceHistory
from hmm_gesture_studio.qt import i18n
from hmm_gesture_studio.qt.controller import StudioController
from hmm_gesture_studio.qt.theme import apply_theme
from hmm_gesture_studio.qt.window import MainWindow


class LanguageTests(unittest.TestCase):
    def test_system_language_and_unsupported_fallback(self):
        for locale, expected in (("zh-CN", "zh"), ("zh-Hant-TW", "zh"),
                                 ("en-US", "en"), ("fr-FR", "en"), ("ja-JP", "en")):
            with self.subTest(locale=locale), patch.object(i18n.QLocale, "system") as system:
                system.return_value.uiLanguages.return_value = [locale]
                self.assertEqual(i18n.system_language(), expected)

    def test_preference_persists_and_system_is_resolved_on_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "settings.ini")
            settings = QSettings(path, QSettings.Format.IniFormat)
            manager = i18n.LanguageManager()
            with patch.object(i18n, "system_language", return_value="en"):
                manager.initialize(settings)
            self.assertEqual((manager.preference, manager.language), ("system", "en"))
            manager.set_preference("zh")
            restarted = i18n.LanguageManager()
            restarted.initialize(QSettings(path, QSettings.Format.IniFormat))
            self.assertEqual((restarted.preference, restarted.language), ("zh", "zh"))
            with patch.object(i18n, "system_language", return_value="en"):
                restarted.set_preference("system")
            self.assertEqual(settings.value("ui/language"), "system")
            with patch.object(i18n, "system_language", return_value="zh"):
                manager.initialize(settings)
            self.assertEqual(manager.language, "zh")
            settings.setValue("ui/language", "invalid")
            manager.initialize(settings)
            self.assertEqual(manager.preference, "system")

    def test_catalog_templates_are_complete_and_keep_placeholders(self):
        directory = Path(i18n.__file__).parent
        formatter = string.Formatter()
        entries = {}
        for path in (directory / "locales").glob("*_en.json"):
            for source, target in json.loads(path.read_text()).items():
                self.assertTrue(target, source)
                source_fields = sorted((name, spec, conversion) for _, name, spec, conversion in formatter.parse(source)
                                       if name is not None)
                target_fields = sorted((name, spec, conversion) for _, name, spec, conversion in formatter.parse(target)
                                       if name is not None)
                self.assertEqual(source_fields, target_fields, source)
                if source in entries:
                    self.assertEqual(entries[source], target, f"Conflicting translation: {source}")
                entries[source] = target
        for path in directory.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "tr"
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and any("\u4e00" <= char <= "\u9fff" for char in node.args[0].value)):
                    self.assertTrue(node.args[0].value in entries,
                                    f"Missing translation at {path}:{node.lineno}: {node.args[0].value}")


class WindowLanguageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        directory = Path(self.temp.name)
        manager = i18n.language_manager
        self.previous = manager.preference, manager.settings
        manager.initialize(QSettings(str(directory / "settings.ini"), QSettings.Format.IniFormat))
        manager.set_preference("zh")
        self.addCleanup(self.restore_language)
        self.worker, self.dialogs = Mock(), Mock()
        self.controller = StudioController(self.dialogs, worker=self.worker,
                                           history=DeviceHistory(directory / "devices.json"),
                                           directory=directory / "data", audio_directory=directory / "audio")
        self.addCleanup(self.controller.executor.shutdown, wait=False)
        self.window = MainWindow(controller=self.controller, merge_title_bar=False)
        self.window.resize(1100, 720)
        self.window.show()
        self.addCleanup(self.destroy_window)
        self.pump()

    def restore_language(self):
        preference, settings = self.previous
        i18n.language_manager.settings = settings
        i18n.language_manager.set_preference(preference, persist=False)

    def destroy_window(self):
        self.window._may_close = True
        self.window.close()
        self.window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def pump(self):
        self.app.processEvents()

    def choose(self, preference):
        combo = self.window.pages["settings"].language
        combo.setCurrentIndex(combo.findData(preference))
        self.pump()

    def test_settings_shows_only_headings_and_language_selector(self):
        from PySide6.QtWidgets import QLabel
        page = self.window.pages["settings"]
        for language, headings in (("zh", ["设置", "语言"]), ("en", ["Settings", "Language"])):
            self.choose(language)
            self.assertEqual([label.text() for label in page.findChildren(QLabel)], headings)
            self.assertEqual(page.language.count(), 3)
            self.assertEqual(page.language.accessibleName(), i18n.resolve(i18n.tr("界面语言")))

    def test_switch_preserves_recording_inputs_and_user_text(self):
        c, win = self.controller, self.window
        c.set_address("AA:BB")
        c.connect_device()
        c.handle("capabilities", dict(recording_control=True, live_audio=True, audio_files=True, imu=True, ota=True))
        c.handle("connected", dict(address="AA:BB", model="ZR1", firmware_version="1"))
        c.handle("stream_state", dict(state="active", sample_rate_hz=25., accel_range_g=8, gyro_range_dps=2000))
        c.set_name("采集")  # A user name equal to a translation key must stay untouched.
        c.toggle_record()
        c.handle("samples", [[1, 2, 3, 4, 5, 6]] * 20)
        original_take = c.take
        self.assertIsNotNone(original_take)
        capture = win.pages["capture"]
        original_chart = capture.chart
        self.worker.reset_mock()
        self.choose("en")
        self.assertEqual(win.nav_buttons["capture"].text(), "Capture")
        self.assertEqual(win.nav_buttons["settings"].text(), "Settings")
        self.assertEqual(capture.name.text(), "采集")
        self.assertIs(c.take, original_take)
        self.assertIs(capture.chart, original_chart)
        self.assertEqual(capture.record.text(), i18n.resolve(i18n.tr("停止并保留本次")))
        self.assertIn("20", capture.takes.text())
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in capture.takes.text()))
        self.assertEqual(self.worker.mock_calls, [])
        self.choose("zh")
        self.assertEqual(capture.record.text(), "停止并保留本次")
        self.assertIs(c.take, original_take)
        self.assertEqual(win.nav_buttons["capture"].text(), "采集")

    def test_static_dynamic_log_and_nested_text_retranslate(self):
        c, win = self.controller, self.window
        message = i18n.tr("无法打开：{path}", path="/数据/采集.json")
        c.log(message, "warning")
        self.choose("en")
        self.assertEqual(win.log_bar.view.entries()[-1][2], "Warning: Cannot open: /数据/采集.json")
        self.assertEqual(i18n.resolve(message), "Cannot open: /数据/采集.json")
        self.assertEqual(win.pages["capture"].table.horizontalHeaderItem(0).text(), "Name")
        self.assertEqual(win.pages["settings"].language.currentData(), "en")
        self.choose("zh")
        self.assertEqual(win.log_bar.view.entries()[-1][2], "警告：无法打开：/数据/采集.json")
        self.assertEqual(win.pages["capture"].table.horizontalHeaderItem(0).text(), "名称")

    def test_device_and_audio_selections_keep_internal_identifiers(self):
        c, win = self.controller, self.window
        c.handle("devices", [dict(address="AA:BB", name="采集", rssi=-50)])
        device = win.pages["device"]
        device.table.selectRow(0)
        c.handle("audio_files", [dict(file_index=7), dict(file_index=12)])
        audio = win.pages["audio"]
        audio.remote.setCurrentIndex(1)
        selected = c.remote_audio_selected
        self.choose("en")
        self.assertEqual(device.table.item(0, 0).text(), "采集")
        self.assertEqual(device.table.item(0, 2).text(), "Scan")
        self.assertEqual(c.address, "AA:BB")
        self.assertTrue(device.table.selectedIndexes())
        self.assertEqual(c.remote_audio_selected, selected)
        self.assertEqual(audio.remote.currentData(), selected)
        self.assertIn("12", audio.remote.currentText())
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for char in audio.remote.currentText()))
        self.choose("zh")
        self.assertEqual(device.table.item(0, 2).text(), "扫描")
        self.assertEqual(audio.remote.currentData(), selected)

    def test_language_switch_does_not_release_ota_lock(self):
        c = self.controller
        c.tools.tool_state = "firmware"
        c.tools.progress = 100.0
        self.choose("en")
        c.disconnect_device()
        c.request_close()
        self.assertTrue(c.tools.firmware_locked)
        self.worker.disconnect.assert_not_called()
        self.worker.close.assert_not_called()
        self.assertFalse(c.closing)
        self.assertEqual(self.dialogs.error.call_count, 2)
        self.assertIn("firmware", i18n.resolve(self.dialogs.error.call_args.args[1]).lower())

    def test_native_confirmation_uses_selected_language(self):
        from PySide6.QtWidgets import QMessageBox
        from hmm_gesture_studio.qt.dialogs import QtDialogs
        self.choose("en")
        def inspect(box):
            # macOS intentionally ignores QMessageBox window titles.
            self.assertEqual(box.text(), "Cannot open: /数据")
            self.assertEqual(box.informativeText(), "Continue")
            self.assertEqual(box.button(QMessageBox.StandardButton.No).text(), "Cancel")
            self.assertIs(box.defaultButton(), box.button(QMessageBox.StandardButton.No))
            return QMessageBox.StandardButton.No
        with patch.object(QMessageBox, "exec", inspect):
            self.assertFalse(QtDialogs(self.window).ask(i18n.tr("无法打开：{path}", path="/数据"),
                                                        i18n.tr("继续"), danger=True))

    def test_all_pages_fit_in_both_languages_and_about_does_not_refetch(self):
        with patch.object(self.window.pages["about"].fetcher, "fetch") as fetch:
            for language in ("en", "zh", "en"):
                self.choose(language)
                for name, page in self.window.pages.items():
                    self.window.navigate(name)
                    self.pump()
                    with self.subTest(language=language, page=name):
                        self.assertLessEqual(page.widget().minimumSizeHint().width(), page.viewport().width())
            # The page may fetch on show; language changes themselves must not fetch.
            self.window.navigate("about")
            self.pump()
            fetch.reset_mock()
            self.choose("zh")
            fetch.assert_not_called()
