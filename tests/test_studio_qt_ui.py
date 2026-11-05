# SPDX-License-Identifier: MPL-2.0
"""Real widgets on Qt's offscreen platform, with a fake worker: no display or ring."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QCloseEvent, QMouseEvent
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError as exc:  # Headless Linux without libEGL/libGL; CI installs them.
    raise unittest.SkipTest(f"Qt GUI libraries unavailable: {exc}")

import numpy as np

from hmm_gesture_studio.datasets import GestureDataset, load_dataset, save_dataset
from hmm_gesture_studio.device_history import DeviceHistory
from hmm_gesture_studio.qt.controller import StudioController
from hmm_gesture_studio.qt.theme import apply_theme
from hmm_gesture_studio.qt.widgets import Switch
from hmm_gesture_studio.qt.window import MainWindow, NAV

CAPABILITIES = dict(recording_control=True, live_audio=True, audio_files=True, imu=True, ota=True)
ACTIVE = dict(state="active", message="IMU 正常上报", sample_rate_hz=25.0, accel_range_g=8, gyro_range_dps=2000)


class WindowTests(unittest.TestCase):
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
        self.worker = Mock()
        self.c = StudioController(self.dialogs, worker=self.worker,
                                  history=DeviceHistory(self.dir / "devices.json"),
                                  directory=self.dir / "data", audio_directory=self.dir / "audio")
        self.addCleanup(self.c.executor.shutdown, wait=False)
        self.win = MainWindow(controller=self.c)
        self.win.resize(1280, 860)
        self.win.show()
        self.c.start()
        self.c._timer.stop()  # Tests drive the event pump explicitly.
        self.addCleanup(self._destroy_window)
        self.pump()

    def _destroy_window(self):
        self.win._may_close = True
        self.win.close()
        self.win.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def pump(self):
        self.qt.processEvents()
        self.c.poll()
        self.qt.processEvents()

    def connect(self):
        c = self.c
        c.set_address("AA:BB")
        c.connect_device()
        c.handle("capabilities", CAPABILITIES)
        c.handle("connected", dict(address="AA:BB", model="ZR1", firmware_version="1", battery_percent=15,
                                   battery_charging=False))
        c.handle("stream_state", ACTIVE)

    def test_all_destinations_render_and_sidebar_tracks_current_page(self):
        for name, _text in NAV:
            self.win.nav_buttons[name].click()
            self.pump()
            self.assertIs(self.win.stack.currentWidget(), self.win.pages[name])
            self.assertEqual([k for k, b in self.win.nav_buttons.items() if b.isChecked()], [name])
        self.win.device_card.click()
        self.assertIs(self.win.stack.currentWidget(), self.win.pages["device"])
        self.assertFalse(any(b.isChecked() for b in self.win.nav_buttons.values()))

    def test_connection_state_reaches_sidebar_and_notice_links_to_device_page(self):
        page = self.win.pages["capture"]
        self.assertTrue(page.notice.isVisible())
        self.assertEqual(self.win.device_card.name.text(), "未连接")
        self.connect()
        self.pump()
        self.assertFalse(page.notice.isVisible())
        self.assertEqual(self.win.device_card.name.text(), "ZR1")
        self.assertIn("低电量", self.win.device_card.detail.text())
        self.assertEqual(self.win.device_card.detail.property("tone"), "critical")

    def test_recording_flow_through_widgets_switches_views(self):
        page = self.win.pages["capture"]
        self.assertFalse(page.record.isEnabled())
        self.connect()
        self.pump()
        QTest.keyClicks(page.name, "snap")
        self.assertEqual(self.c.name, "snap")
        page.record.click()
        self.assertEqual(page.record.text(), "停止并保留本次")
        self.assertEqual(page.record.property("kind"), "record")
        self.assertEqual(page.chart_stack.currentIndex(), 0)
        self.c.handle("samples", [[i, 2, 3, 4, 5, 6] for i in range(20)])
        self.pump()
        self.assertIn("正在录制：20 样本", page.takes.text())
        page.record.click()
        self.pump()
        self.assertEqual(page.chart_stack.currentIndex(), 1, "stopping reveals the waveform")
        self.assertEqual(page.choice.count(), 1)
        self.assertFalse(page.name.isEnabled())
        self.assertTrue(page.save.isEnabled())
        self.assertTrue(self.c.stream.next_sample == 20)

    def drag_chart(self, chart_panel, x0, x1, *, leave=False):
        """Real mouse events through the plot's graphics scene."""
        view = chart_panel.plot.viewport()
        rect = chart_panel.view_box.sceneBoundingRect()
        y = int(rect.center().y())
        def point(fraction):
            scene = chart_panel.plot.mapFromScene(rect.left() + rect.width() * fraction, y)
            return QPoint(scene.x(), scene.y())
        def send(kind, position, button, buttons):
            # QTest.mouseMove cannot carry a held button, which a drag needs.
            local, screen = QPointF(position), QPointF(view.mapToGlobal(position))
            event = QMouseEvent(kind, local, screen, button, buttons, Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(view, event)
            QTest.qWait(15)  # pyqtgraph drops moves faster than its 100/s rate limit.
        left = Qt.MouseButton.LeftButton
        send(QEvent.Type.MouseButtonPress, point(x0), left, left)
        for step in range(1, 6):
            send(QEvent.Type.MouseMove, point(x0 + (x1 - x0) * step / 5), Qt.MouseButton.NoButton, left)
        release = QPoint(point(x1).x(), -500) if leave else point(x1)
        send(QEvent.Type.MouseMove, release, Qt.MouseButton.NoButton, left)
        send(QEvent.Type.MouseButtonRelease, release, left, Qt.MouseButton.NoButton)

    def saved_preview(self):
        self.c.directory.mkdir(parents=True)
        path = save_dataset(GestureDataset("手势", [np.arange(240).reshape(40, 6) % 11] * 2), self.c.directory)
        self.c.refresh()
        page = self.win.pages["capture"]
        page.table.selectRow(0)
        self.pump()
        self.assertEqual(page.chart_stack.currentIndex(), 1)
        return path, page

    def test_mouse_drag_saves_region_replaces_it_and_restores_automatic(self):
        path, page = self.saved_preview()
        panel = page.wave_chart.accel
        self.drag_chart(panel, 0.1, 0.6)
        self.assertIsNone(load_dataset(path).training_regions, "no selection without selection mode")
        QTest.mouseClick(page.selecting, Qt.MouseButton.LeftButton)
        self.assertTrue(page.selecting.isChecked())
        self.drag_chart(panel, 0.1, 0.6)
        first = load_dataset(path).training_regions[1]
        self.assertAlmostEqual(first[0], 4, delta=2)
        self.assertAlmostEqual(first[1], 24, delta=2)
        self.assertTrue(page.reset_crop.isEnabled())
        # A second drag over the existing box replaces it: region items must not swallow it.
        self.drag_chart(page.wave_chart.gyro, 0.3, 0.9)
        second = load_dataset(path).training_regions[1]
        self.assertAlmostEqual(second[0], 12, delta=2)
        self.assertAlmostEqual(second[1], 36, delta=2)
        page.reset_crop.click()
        self.pump()
        self.assertIsNone(load_dataset(path).training_regions)
        self.assertFalse(page.selecting.isChecked())
        self.dialogs.error.assert_not_called()

    def test_drag_released_outside_chart_or_too_short_changes_nothing(self):
        path, page = self.saved_preview()
        QTest.mouseClick(page.selecting, Qt.MouseButton.LeftButton)
        self.drag_chart(page.wave_chart.accel, 0.1, 0.6, leave=True)
        self.assertIsNone(load_dataset(path).training_regions)
        self.drag_chart(page.wave_chart.accel, 0.4, 0.42)
        self.assertIsNone(load_dataset(path).training_regions)
        self.assertIn("过短", self.dialogs.error.call_args.args[1])

    def test_training_locks_selection_and_dataset_controls(self):
        _path, page = self.saved_preview()
        self.c.training = True
        self.c.notify()
        self.pump()
        for widget in (page.selecting, page.choose_dir, page.refresh, page.delete, page.import_csv):
            self.assertFalse(widget.isEnabled())
        self.win.navigate("train")
        self.pump()
        train = self.win.pages["train"]
        self.assertFalse(train.train.isEnabled())
        self.assertFalse(train.inputs["cutoff_hz"].isEnabled())
        self.assertEqual(train.model_state.value.text(), "进行中")

    def test_train_fields_and_presets_stay_in_sync(self):
        self.win.navigate("train")
        self.pump()
        page = self.win.pages["train"]
        page.inputs["cutoff_hz"].clear()
        QTest.keyClicks(page.inputs["cutoff_hz"], "7")
        self.assertEqual(self.c.params["cutoff_hz"], "7")
        page.impulse.click()
        self.pump()
        self.assertEqual(page.inputs["median_kernel"].text(), "1")
        self.assertEqual(page.inputs["energy_threshold"].text(), "8000")

    def test_live_switch_reflects_controller_rejection(self):
        self.connect()
        self.win.navigate("live")
        self.pump()
        page = self.win.pages["live"]
        self.assertTrue(page.switch.isEnabled())
        QTest.mouseClick(page.switch, Qt.MouseButton.LeftButton)
        self.pump()
        self.assertFalse(page.switch.isChecked(), "no model: the switch must fall back off")
        self.assertIn("训练有效模型", self.dialogs.error.call_args.args[1])

    def test_disabled_switch_ignores_clicks(self):
        switch = Switch("x")
        switch.setEnabled(False)
        changed = Mock()
        switch.toggled.connect(changed)
        switch.show()
        QTest.mouseClick(switch, Qt.MouseButton.LeftButton)
        changed.assert_not_called()
        switch.close()

    def test_live_chart_draws_samples_and_recognition_box_after_pause(self):
        self.connect()
        self.win.navigate("live")
        page = self.win.pages["live"]
        self.c.handle("samples", [[i, 0, 0, 0, 0, 0] for i in range(50)])
        self.c.stream.add_recognition(10, 30, "甲")
        self.pump()
        x, y = page.chart.accel.curves[0].getData()
        self.assertEqual(len(x), 50)
        self.assertEqual(y[-1], 49)
        self.assertEqual(x[-1], 0)
        self.assertTrue(page.chart.gyro._region_items[0][0].isVisible())
        page.pause.click()
        self.c.handle("samples", [[100, 0, 0, 0, 0, 0]] * 10)
        self.pump()
        self.assertEqual(len(page.chart.accel.curves[0].getData()[0]), 50)
        self.assertEqual(self.c.stream.next_sample, 60)

    def test_hover_reports_nearest_values_on_both_panels(self):
        self.connect()
        self.win.navigate("live")
        page = self.win.pages["live"]
        self.c.handle("samples", [[i, 0, 0, i * 2, 0, 0] for i in range(25)])
        self.pump()
        page.chart.accel.on_hover(-0.4)
        self.assertEqual(page.chart.accel._legend[0][1].text(), "ax 14")
        self.assertEqual(page.chart.gyro._legend[0][1].text(), "gx 28")
        page.chart.accel.on_hover(None)
        self.assertEqual(page.chart.accel._legend[0][1].text(), "ax")

    def test_diagnostics_shows_physical_units_only_with_known_range(self):
        self.connect()
        self.win.navigate("diagnostics")
        page = self.win.pages["diagnostics"]
        self.c.handle("samples", [[4096, 0, 0, 0, 0, 0]])
        self.pump()
        self.assertEqual(page.magnitude.value.text(), "1.0000 g")
        self.assertIsNotNone(page.dials[0].direction)
        self.c.handle("stream_state", dict(state="waiting", message="停"))
        self.c.handle("stream_state", dict(state="active", message="ok", sample_rate_hz=25.0))
        self.c.handle("samples", [[4096, 0, 0, 0, 0, 0]])
        self.pump()
        self.assertEqual(page.magnitude.value.text(), "—")
        self.assertEqual(page.raw.value.text(), "4096.00")
        self.assertIn("量程未知", page.chart.title.text())

    def test_tools_page_reflects_exclusive_firmware_lock(self):
        self.connect()
        self.win.navigate("tools")
        self.c.tools.handle("tool_state", dict(state="firmware", message="升级中"))
        self.c.notify()
        self.pump()
        page = self.win.pages["tools"]
        for widget in (page.accel, page.install, page.zip, page.inspect):
            self.assertFalse(widget.isEnabled())
        self.assertEqual(page.firmware_status.text(), "升级中")
        self.win.navigate("device")
        self.pump()
        self.assertFalse(self.win.pages["device"].disconnect_button.isEnabled())
        event = QCloseEvent()
        self.win.closeEvent(event)
        self.assertFalse(event.isAccepted())
        self.worker.close.assert_not_called()

    def test_audio_page_gates_and_history_combo(self):
        self.connect()
        self.win.navigate("audio")
        self.pump()
        page = self.win.pages["audio"]
        self.assertTrue(page.record.isEnabled())
        self.assertFalse(page.download.isEnabled())
        self.c.handle("audio_files", [dict(file_index=1), dict(file_index=2)])
        self.pump()
        self.assertEqual(page.remote.count(), 2)
        page.remote.setCurrentIndex(1)
        self.assertEqual(self.c.remote_audio_selected, "录音索引 2")
        self.assertTrue(page.download.isEnabled())
        self.c.handle("recording_config", dict(channels=1, bitrate=8000, gain_left_q8=1, gain_right_q8=2,
                                               max_duration_ms=1000))
        self.assertEqual(page.inputs["bitrate"].text(), "8000")

    def test_log_bar_shows_latest_line_and_expands(self):
        self.c.log("新消息")
        self.assertIn("新消息", self.win.log_bar.latest.text())
        self.assertFalse(self.win.log_bar.view.isVisible())
        self.win.log_bar.toggle.click()
        self.assertTrue(self.win.log_bar.view.isVisible())
        self.assertIn("新消息", self.win.log_bar.view.toPlainText())

    def test_every_page_fits_minimum_window_width(self):
        self.win.resize(1100, 720)
        for name in self.win.pages:
            self.win.navigate(name)
            self.pump()
            page = self.win.pages[name]
            with self.subTest(page=name):
                self.assertLessEqual(page.widget().minimumSizeHint().width(), page.viewport().width())


if __name__ == "__main__":
    unittest.main()
