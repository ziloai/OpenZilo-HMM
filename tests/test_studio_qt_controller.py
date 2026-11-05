# SPDX-License-Identifier: MPL-2.0
"""New-UI rules with a fake worker and dialogs: no display or ring required."""
from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import numpy as np
from hmm_gesture import GestureBundle

from hmm_gesture_studio.datasets import GestureDataset, dataset_path, load_dataset, save_dataset
from hmm_gesture_studio.device_history import DeviceHistory
from hmm_gesture_studio.firmware import VerifiedFirmware
from hmm_gesture_studio.qt.controller import StudioController
from hmm_gesture_studio.qt.models import (
    AccelerationModel, LiveStreamModel, analyze_recording, drag_to_region,
)

CAPABILITIES = dict(recording_control=True, live_audio=True, audio_files=True, imu=True, ota=True)
ACTIVE = dict(state="active", message="IMU 正常上报", sample_rate_hz=25.0, accel_range_g=8, gyro_range_dps=2000)
ADDRESS = "AA:BB:CC:DD:EE:01"


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.dialogs = Mock()
        self.dialogs.ask.return_value = True
        self.worker = Mock()
        self.c = StudioController(self.dialogs, worker=self.worker,
                                  history=DeviceHistory(self.dir / "settings" / "devices.json"),
                                  directory=self.dir / "data", audio_directory=self.dir / "audio")
        self.addCleanup(self.c.executor.shutdown, wait=False)
        self.c.executor = Mock()
        self.c.executor.submit.side_effect = lambda run: run()

    def connect(self, *, capabilities=CAPABILITIES, stream=True):
        c = self.c
        c.set_address(ADDRESS)
        c.connect_device()
        c.handle("capabilities", capabilities)
        c.handle("connected", dict(address=ADDRESS, model="ZR1", firmware_version="1.2.3",
                                   battery_percent=82, battery_charging=False))
        if stream:
            c.handle("stream_state", ACTIVE)

    def record(self, count=16, value=1):
        self.c.set_name(self.c.name or "测试手势")
        self.c.toggle_record()
        self.c.handle("samples", [[value] * 6] * count)
        self.c.toggle_record()

    def errors(self):
        return [call.args[1] for call in self.dialogs.error.call_args_list]


class DeviceTests(Base):
    def test_connection_gates_and_history_records_only_success(self):
        c = self.c
        self.assertTrue(c.can_connect)
        self.assertFalse(c.can_disconnect)
        self.assertFalse(c.can_record)
        c.set_address(ADDRESS)
        c.connect_device()
        self.assertTrue(c.connecting)
        self.assertEqual(c.device_history.entries, [])
        self.assertFalse(c.can_connect)
        self.assertTrue(c.can_disconnect)
        self.worker.connect.assert_called_once_with(ADDRESS)
        c.handle("capabilities", CAPABILITIES)
        c.handle("connected", dict(address=ADDRESS, model="ZR1", firmware_version="1"))
        self.assertTrue(c.connected)
        self.assertEqual(c.device_history.entries[0]["address"], ADDRESS)
        self.assertEqual(c.device_summary, "ZR1")

    def test_late_connected_event_after_cancel_disconnects(self):
        self.c.handle("connected", dict(address=ADDRESS))
        self.assertFalse(self.c.connected)
        self.worker.disconnect.assert_called_once()

    def test_scan_keeps_history_entries_first_without_duplicates(self):
        c = self.c
        c.device_history.remember(ADDRESS, "戒指")
        c.handle("devices", [dict(address=ADDRESS.lower(), name="戒指新名", rssi=-40),
                             dict(address="11:22", name="", rssi=-60)])
        self.assertEqual([(e["name"], e["source"], e["rssi"]) for e in c.device_entries],
                         [("戒指新名", "历史", -40), ("未命名", "扫描", -60)])

    def test_scan_state_ends_only_when_worker_reports_it(self):
        c = self.c
        c.scan()
        self.worker.scan.assert_called_once()
        self.assertTrue(c.scanning)
        self.assertFalse(c.can_scan or c.can_connect)
        c.handle("devices", [])
        self.assertTrue(c.scanning)
        c.handle("scan_finished", None)
        self.assertFalse(c.scanning)
        self.assertTrue(c.can_scan and c.can_connect)

    def test_failed_scan_request_does_not_leave_progress_running(self):
        self.worker.scan.side_effect = RuntimeError("蓝牙不可用")
        self.c.scan()
        self.assertFalse(self.c.scanning)
        self.assertIn("蓝牙不可用", self.errors()[-1])

    def test_scan_is_unavailable_while_a_connection_session_is_active(self):
        self.connect()
        self.assertFalse(self.c.can_scan)
        self.c.scan()
        self.worker.scan.assert_not_called()
        self.assertTrue(self.errors())

    def test_connect_during_scan_is_rejected_before_the_worker(self):
        self.c.set_address(ADDRESS)
        self.c.scan()
        self.c.connect_device()
        self.worker.connect.assert_not_called()
        self.assertFalse(self.c.connecting)

    def test_battery_renders_only_reported_values(self):
        c = self.c
        for payload, text, tone in (
                (dict(battery_percent=0, battery_charging=False), "电量：0% · 低电量", "critical"),
                (dict(battery_percent=20, battery_charging=True), "电量：20% · 充电中", "good"),
                (dict(battery_percent=21, battery_charging=False), "电量：21%", "normal"),
                (dict(battery_percent=101), "电量：未知", "muted"),
                (dict(battery_percent=True), "电量：未知", "muted"),
                (None, "电量：—", "muted")):
            c.set_battery_status(payload)
            self.assertEqual((c.battery_text, c.battery_tone), (text, tone))

    def test_disconnect_discards_incomplete_take_and_keeps_completed(self):
        self.connect()
        self.record()
        c = self.c
        c.toggle_record()
        c.handle("samples", [[1] * 6] * 20)
        c.disconnect_device()
        self.assertIsNone(c.take)
        self.assertEqual(len(c.pending), 1)
        self.assertEqual(c.tools.capabilities, {})
        self.assertFalse(c.connected)
        self.assertEqual(c.battery_text, "电量：—")
        self.worker.disconnect.assert_called_once()

    def test_stream_pause_does_not_claim_ble_loss_and_late_samples_are_ignored(self):
        self.connect()
        c = self.c
        c.handle("samples", [[5] * 6])
        c.handle("stream_state", dict(state="waiting", message="等待"))
        self.assertTrue(c.connected)
        self.assertIsNone(c.latest_sample)
        self.assertIn("未断言 BLE 断开", c.raw_note)
        count = c.count
        c.handle("samples", [[9] * 6])
        self.assertEqual(c.count, count)

    def test_retry_requires_supported_idle_imu(self):
        self.c.retry_imu()
        self.worker.retry_imu.assert_not_called()
        self.assertTrue(self.errors())
        self.connect()
        self.c.retry_imu()
        self.worker.retry_imu.assert_called_once()


class CaptureTests(Base):
    def test_short_take_is_previewed_but_never_kept(self):
        self.connect()
        revealed = Mock()
        self.c.previewRevealed.connect(revealed)
        self.record(count=5)
        self.assertEqual(self.c.pending, [])
        self.assertEqual(self.c.preview_title, "本次过短（未保留）")
        self.assertFalse(self.c.preview_editable)
        revealed.assert_called_once()

    def test_record_requires_name_and_active_imu(self):
        self.connect(stream=False)
        self.c.set_name("手势")
        self.c.toggle_record()
        self.assertIsNone(self.c.take)
        self.c.handle("stream_state", ACTIVE)
        self.c.set_name("  ")
        self.c.toggle_record()
        self.assertIsNone(self.c.take)
        self.assertEqual(len(self.errors()), 2)

    def test_samples_received_before_start_do_not_leak_and_final_batch_is_kept(self):
        self.connect()
        c = self.c
        c.set_name("手势")
        c.events.put(("samples", [[1] * 6] * 3))
        c.toggle_record()
        self.assertEqual(c.take, [])
        c.handle("samples", [[2] * 6] * 12)
        c.events.put(("samples", [[3] * 6] * 4))
        c.toggle_record()
        self.assertEqual(len(c.pending[0]), 16)

    def test_changing_name_discards_pending_recordings(self):
        self.connect()
        self.record()
        self.assertFalse(self.c.name_editable)
        self.c.set_name("新名称")
        self.assertEqual(self.c.pending, [])
        self.assertTrue(self.c.name_editable)

    def test_undo_and_save_gates(self):
        self.connect()
        self.assertFalse(self.c.can_save)
        self.record()
        self.record()
        self.assertTrue(self.c.can_undo and self.c.can_save and self.c.can_discard)
        self.c.undo()
        self.assertEqual(len(self.c.pending), 1)

    def test_save_requires_two_reps_keeps_pending_then_appends_with_confirmation(self):
        self.connect()
        c = self.c
        self.record()
        c.save_takes()
        self.assertEqual(len(c.pending), 1)
        self.assertTrue(self.errors())
        self.record()
        c.save_takes()
        self.assertEqual(c.pending, [])
        path = dataset_path("测试手势", c.directory)
        self.assertEqual(len(load_dataset(path).repetitions), 2)
        self.record()
        self.record()
        self.dialogs.ask.return_value = False
        c.save_takes()
        self.assertEqual(len(c.pending), 2)
        self.assertEqual(len(load_dataset(path).repetitions), 2)
        self.dialogs.ask.return_value = True
        c.save_takes()
        self.assertEqual(len(load_dataset(path).repetitions), 4)

    def test_same_name_at_a_different_rate_never_overwrites(self):
        self.connect()
        c = self.c
        path = save_dataset(GestureDataset("测试手势", [np.ones((20, 6))] * 2, 50.0), self._mkdir(c.directory))
        before = path.read_bytes()
        self.record()
        self.record()
        c.save_takes()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(c.pending), 2)

    @staticmethod
    def _mkdir(path):
        path.mkdir(parents=True, exist_ok=True)
        return path


class PreviewTests(Base):
    def saved(self, name="手势"):
        self.c.directory.mkdir(parents=True, exist_ok=True)
        path = save_dataset(GestureDataset(name, [np.arange(120).reshape(20, 6) % 7] * 2), self.c.directory)
        self.c.refresh()
        return path

    def test_preview_uses_recording_rate_not_training_rate(self):
        self.c.directory.mkdir(parents=True)
        save_dataset(GestureDataset("手势", [np.ones((20, 6))] * 2, 50.0), self.c.directory)
        self.c.refresh()
        self.c.set_param("sample_rate_hz", "25")
        self.c.select_datasets(list(self.c.records))
        self.assertEqual(self.c.preview_view.rate, 50.0)

    def test_invalid_filter_reports_without_raising(self):
        self.saved()
        self.c.select_datasets(list(self.c.records))
        self.c.set_param("cutoff_hz", "abc")
        self.assertIsNone(self.c.preview_view)
        self.assertIn("无法预览", self.c.preview_message)

    def test_manual_saved_edit_persists_annotation_and_invalidates_trained_model(self):
        path = self.saved()
        c = self.c
        raw = load_dataset(path).repetitions[1].copy()
        c.select_datasets(list(c.records))
        c.bundle, c.origin = Mock(), "trained"
        c.set_preview_training_region((2, 18))
        saved = load_dataset(path)
        self.assertEqual(saved.training_regions[1], (2, 18))
        self.assertTrue(np.array_equal(saved.repetitions[1], raw))
        self.assertTrue(c.stale)
        self.assertFalse(c.can_export)
        c.set_preview_training_region(None)
        self.assertIsNone(load_dataset(path).training_regions)

    def test_short_or_externally_changed_edit_is_rejected(self):
        path = self.saved()
        c = self.c
        c.select_datasets(list(c.records))
        c.set_preview_training_region((0, 3))
        self.assertIn("过短", self.errors()[-1])
        save_dataset(GestureDataset("手势", [np.zeros((20, 6))] * 2), c.directory)
        before = path.read_bytes()
        c.set_preview_training_region((2, 18))
        self.assertIn("已变化", self.errors()[-1])
        self.assertEqual(path.read_bytes(), before)

    def test_edits_are_blocked_while_training(self):
        path = self.saved()
        c = self.c
        c.select_datasets(list(c.records))
        c.training = True
        self.assertFalse(c.preview_can_edit)
        c.set_preview_training_region((2, 18))
        self.assertIsNone(load_dataset(path).training_regions)

    def test_pending_region_saves_with_recording(self):
        self.connect()
        c = self.c
        self.record(count=20)
        c.set_preview_training_region((1, 19))
        self.record(count=20)
        c.save_takes()
        self.assertEqual(load_dataset(dataset_path("测试手势", c.directory)).training_regions, [(1, 19), None])


class DatasetTests(Base):
    def make(self, *names):
        self.c.directory.mkdir(parents=True, exist_ok=True)
        paths = [save_dataset(GestureDataset(name, [np.ones((20, 6))] * 2), self.c.directory) for name in names]
        self.c.refresh()
        return paths

    def test_delete_requires_selection_and_cancel_preserves_files(self):
        a, = self.make("一")
        self.c.delete_datasets()
        self.assertTrue(self.errors())
        self.c.select_datasets(list(self.c.records))
        self.dialogs.ask.return_value = False
        self.c.delete_datasets()
        self.assertTrue(a.exists())
        self.assertTrue(self.dialogs.ask.call_args.kwargs["danger"])

    def test_delete_only_confirmed_targets_after_modal_state_change(self):
        a, b = self.make("一", "二")
        c = self.c
        c.select_datasets([next(iter(c.records))])

        def reselect(*_args, **_kwargs):
            c.selected_ids = list(c.records)
            return True
        self.dialogs.ask.side_effect = reselect
        c.delete_datasets()
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())

    def test_delete_is_abandoned_if_training_started_or_rows_changed_during_modal(self):
        a, = self.make("一")
        c = self.c
        c.select_datasets(list(c.records))
        self.dialogs.ask.side_effect = lambda *_a, **_k: setattr(c, "training", True) or True
        c.delete_datasets()
        self.assertTrue(a.exists())
        c.training = False
        c.select_datasets(list(c.records))
        self.dialogs.ask.side_effect = lambda *_a, **_k: c.refresh() or True
        c.delete_datasets()
        self.assertTrue(a.exists())
        self.assertIn("已变化", self.errors()[-1])

    def test_delete_preserves_unsaved_recordings_and_imported_model(self):
        self.make("一")
        self.connect()
        c = self.c
        self.record()
        bundle = Mock()
        c.bundle, c.origin = bundle, "imported"
        c.select_datasets(list(c.records))
        c.delete_datasets()
        self.assertEqual(c.records, {})
        self.assertEqual(len(c.pending), 1)
        self.assertIs(c.bundle, bundle)
        self.assertFalse(c.stale)

    def test_failed_directory_refresh_never_keeps_cached_rows(self):
        self.make("一")
        c = self.c
        c.directory = c.directory / "一.json"  # A file, so iterdir raises.
        (self.dir / "x").touch()
        c.directory = self.dir / "x"
        self.assertFalse(c.refresh())
        self.assertEqual(c.records, {})

    def test_missing_directory_is_empty_and_not_created(self):
        self.assertTrue(self.c.refresh())
        self.assertFalse(self.c.directory.exists())

    def test_invalid_file_blocks_training(self):
        self.make("一")
        (self.c.directory / "bad.json").write_text("{", encoding="utf-8")
        self.c.train()
        self.assertIn("无效数据", self.errors()[-1])
        self.assertFalse(self.c.training)
        self.c.executor.submit.assert_not_called()

    def test_import_csv_requires_name(self):
        self.c.import_csv()
        self.assertTrue(self.errors())
        self.dialogs.open_files.assert_not_called()


class ModelTests(Base):
    def train(self):
        rng = np.random.default_rng(4)
        self.c.set_param("n_states", "2")
        self.c.directory.mkdir(parents=True, exist_ok=True)
        save_dataset(GestureDataset("训练", [rng.integers(-100, 100, (24, 6)) for _ in range(3)]), self.c.directory)
        self.c.train()
        self.assertTrue(self.c.training)
        self.assertFalse(self.c.can_export)
        self.assertFalse(self.c.can_record)
        self.c.drain_events()
        self.assertEqual(self.errors(), [])

    def test_training_export_load_round_trip_and_staleness(self):
        self.train()
        c = self.c
        self.assertFalse(c.training)
        self.assertEqual(c.origin, "trained")
        target = self.dir / "m.gesture.json"
        self.dialogs.save_file.return_value = str(target)
        c.export_bundle()
        self.assertEqual(GestureBundle.load(target).gesture_names, ("训练",))
        c.set_param("cutoff_hz", "9")
        self.assertTrue(c.stale)
        self.assertFalse(c.can_export)
        self.dialogs.open_file.return_value = str(target)
        c.load_bundle()
        self.assertEqual(c.origin, "imported")
        self.assertTrue(c.can_export)
        c.set_param("cutoff_hz", "8")
        self.assertFalse(c.stale, "an imported bundle is independent of training parameters")

    def test_cancelled_dialogs_keep_model(self):
        self.train()
        bundle = self.c.bundle
        self.dialogs.open_file.return_value = ""
        self.dialogs.save_file.return_value = ""
        self.c.load_bundle()
        self.c.export_bundle()
        self.assertIs(self.c.bundle, bundle)

    def test_failed_training_never_leaves_old_model_current(self):
        self.c.directory.mkdir(parents=True)
        self.c.bundle, self.c.origin = Mock(), "imported"
        save_dataset(GestureDataset("过短", [np.ones((13, 6))] * 2), self.c.directory)
        self.c.set_param("window_size", "1000")
        self.c.train()
        self.c.drain_events()
        self.assertFalse(self.c.training)
        self.assertIsNone(self.c.bundle)
        self.assertIn("训练失败", self.c.model_text)

    def test_evaluation_does_not_replace_model(self):
        self.train()
        bundle = self.c.bundle
        self.c.evaluate()
        self.c.drain_events()
        self.assertIs(self.c.bundle, bundle)
        self.assertFalse(self.c.training)

    def test_data_change_during_training_marks_result_stale(self):
        c = self.c
        c.training = True
        bundle = Mock(gesture_names=["甲"], rejection=object())
        bundle.segmentation.mode = "motion"
        bundle.pipeline.sample_rate_hz = 25.0
        bundle.pipeline.median_kernel = 5
        bundle.pipeline.cutoff_hz = 10.0
        c.revision = 3
        c.handle("trained", (bundle, "摘要", 2))
        self.assertTrue(c.stale)
        self.assertFalse(c.can_export)

    def test_presets_use_device_rate_and_expose_median_off(self):
        self.connect()
        replaced = Mock()
        self.c.paramsReplaced.connect(replaced)
        self.c.handle("stream_state", dict(state="waiting", message="x"))
        self.c.handle("stream_state", dict(ACTIVE, sample_rate_hz=100.0))
        self.c.apply_preset("impulse")
        p = self.c.params
        self.assertEqual((p["sample_rate_hz"], p["cutoff_hz"], p["median_kernel"], p["window_size"], p["window_overlap"],
                          p["energy_threshold"], p["segmentation_mode"]),
                         ("100", "30", "1", "8", "4", "8000", "impulse"))
        replaced.assert_called_once()
        self.assertIn("短促动作", self.c.mode_text)

    def test_live_rejects_rate_mismatch_and_stale_model(self):
        self.train()
        self.connect()
        c = self.c
        c.handle("stream_state", dict(state="waiting", message="x"))
        c.handle("stream_state", dict(ACTIVE, sample_rate_hz=100.0))
        c.set_live(True)
        self.assertFalse(c.live)
        self.assertIn("采样率不匹配", self.errors()[-1])
        c.handle("stream_state", dict(state="waiting", message="x"))
        c.handle("stream_state", ACTIVE)
        c.set_live(True)
        self.assertTrue(c.live)
        self.assertIsNotNone(c.recognizer)
        c.set_param("cutoff_hz", "9")
        self.assertFalse(c.live)
        self.assertIsNone(c.recognizer)

    def test_live_pauses_with_stream_and_resumes_when_it_recovers(self):
        self.train()
        self.connect()
        c = self.c
        c.set_live(True)
        c.handle("stream_state", dict(state="waiting", message="x"))
        self.assertTrue(c.live)
        self.assertIsNone(c.recognizer)
        self.assertIn("识别已暂停", c.result_text)
        c.handle("stream_state", ACTIVE)
        self.assertIsNotNone(c.recognizer)

    def test_recognition_regions_are_offset_by_stream_origin(self):
        self.connect()
        c = self.c
        c.handle("samples", [[0] * 6] * 100)
        c.recognition_origin = 100
        c.recognizer = Mock()
        prediction = Mock(confidence=0.9, score=-1.0, start_sample=10, end_sample=30)
        prediction.name = "甲"
        c.recognizer.feed.return_value = [prediction]
        c.handle("samples", [[0] * 6] * 32)
        self.assertEqual(c.prediction[0], "甲")
        _x, _data, spans = c.stream.snapshot()
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][2], "甲 0.90")
        self.assertAlmostEqual(spans[0][0], (110 - 0.5 - 131) / 25)


class ToolAndAudioTests(Base):
    def verified(self):
        return VerifiedFirmware(b"bundle", b"key", "摘要")

    def test_capabilities_gate_each_feature_independently(self):
        self.connect(capabilities=dict(CAPABILITIES, audio_files=False, ota=False, live_audio=False))
        t = self.c.tools
        self.assertFalse(t.available("audio"))
        self.assertTrue(t.available("record"))
        self.assertFalse(t.available("live_audio"))
        t.verified = self.verified()
        self.assertFalse(t.available("install"))
        self.assertTrue(t.available("accel"))

    def test_tool_blocks_recording_and_connection(self):
        self.connect()
        c = self.c
        c.tools.start_diagnostics()
        self.worker.start_diagnostics.assert_called_once()
        self.assertTrue(c.tools.busy)
        self.assertFalse(c.can_record)
        self.assertFalse(c.can_scan)
        c.set_name("手势")
        c.toggle_record()
        self.assertIsNone(c.take)
        count = c.count
        c.handle("samples", [[1] * 6])
        self.assertEqual(c.count, count)

    def test_calibration_continue_only_after_prompt(self):
        self.connect()
        t = self.c.tools
        t.start_calibration("accel")
        self.assertFalse(t.available("continue"))
        t.handle("calibration_prompt", "摆放 +X")
        self.assertTrue(t.available("continue"))
        t.continue_calibration()
        self.worker.continue_calibration.assert_called_once()
        self.assertFalse(t.available("continue"))

    def test_diagnostic_sample_never_enters_gesture_stream_and_clears_on_disconnect(self):
        self.connect()
        c = self.c
        c.tools.start_diagnostics()
        c.handle("calibration_sample", dict(accel_g=[0, 0, 1], gyro_dps=[0, 0, 0], applied_components=[]))
        self.assertIn("未应用校准", c.tools.sample_text)
        self.assertEqual(len(c.stream.buffer), 0)
        c.handle("disconnected", None)
        self.assertIn("无实时数据", c.tools.sample_text)

    def test_reset_requires_danger_confirmation(self):
        self.connect()
        self.dialogs.ask.return_value = False
        self.c.tools.reset_profile()
        self.worker.reset_calibration.assert_not_called()
        self.assertTrue(self.dialogs.ask.call_args.kwargs["danger"])

    def test_firmware_lock_blocks_disconnect_close_and_is_set_before_enqueue(self):
        self.connect()
        c, t = self.c, self.c.tools
        t.verified = self.verified()
        seen = []
        self.worker.install_firmware.side_effect = lambda v: seen.append(t.firmware_locked)
        t.install_firmware()
        self.assertEqual(seen, [True])
        self.assertFalse(c.can_disconnect)
        c.disconnect_device()
        self.worker.disconnect.assert_not_called()
        c.request_close()
        self.assertFalse(c.closing)
        self.worker.close.assert_not_called()
        # A generic error must not release an accepted update.
        c.handle("error", "无关错误")
        self.assertTrue(t.firmware_locked)
        t.handle("tool_state", dict(state="idle", message="结束"))
        self.assertFalse(t.firmware_locked)

    def test_firmware_rechecks_after_confirmation_dialog(self):
        self.connect()
        c, t = self.c, self.c.tools
        verified = self.verified()

        def lose_connection(*_a, **_k):
            c.handle("reconnecting", dict(address=ADDRESS, attempt=1, message="断开"))
            return True
        for mutate in (lose_connection, lambda *_a, **_k: t.invalidate_inspection() or True):
            self.connect() if not c.connected else None
            t.verified = verified
            self.dialogs.ask.side_effect = mutate
            t.install_firmware()
            self.worker.install_firmware.assert_not_called()
            self.assertFalse(t.firmware_locked)
            c.connected, c.connecting = False, False
            self.dialogs.ask.side_effect = None

    def test_firmware_submit_failure_releases_lock(self):
        self.connect()
        t = self.c.tools
        t.verified = self.verified()
        self.worker.install_firmware.side_effect = RuntimeError("不能提交")
        t.install_firmware()
        self.assertFalse(t.firmware_locked)
        self.assertIn("未开始升级", t.firmware_status)

    def test_progress_at_100_percent_is_not_reported_as_success(self):
        t = self.c.tools
        t.handle("firmware_progress", dict(sent=100, total=100))
        self.assertEqual(t.progress, 100)
        self.assertIn("仍需等待重启与版本核验", t.firmware_status)

    def test_changing_firmware_file_invalidates_inspection_and_stale_result_is_ignored(self):
        t = self.c.tools
        t.zip_path, t.key_path = "a.zip", "k.pem"
        self.c.executor.submit.side_effect = lambda run: None
        t.inspect_firmware()
        self.assertTrue(t.inspection_pending)
        token = t.inspection_id
        t.set_firmware_path("zip", "b.zip")
        t.handle("firmware_inspected", dict(token=token, verified=self.verified()))
        self.assertFalse(t.inspection_pending)
        self.assertIsNone(t.verified)

    def test_inspection_failure_is_reported(self):
        t = self.c.tools
        t.zip_path, t.key_path = str(self.dir / "none.zip"), str(self.dir / "none.pem")
        t.inspect_firmware()
        self.c.drain_events()
        self.assertIsNone(t.verified)
        self.assertIn("验签失败", t.firmware_text)
        self.assertTrue(self.errors())

    def test_recording_options_are_validated_before_any_hardware_call(self):
        self.connect()
        t = self.c.tools
        t.set_field("duration_s", "0.03")
        t.start_recording()
        self.worker.start_remote_recording.assert_not_called()
        self.assertFalse(t.remote_session)
        t.set_field("duration_s", "1")
        t.set_playback(True)
        t.start_recording()
        self.assertFalse(self.worker.start_remote_recording.call_args.kwargs["playback"])
        t.handle("audio_state", dict(state="idle", message=""))
        self.c.set_audio_state(dict(state="idle", message=""))
        t.start_recording(live=True)
        self.assertTrue(self.worker.start_remote_recording.call_args.kwargs["playback"])

    def test_recording_config_event_updates_fields(self):
        replaced = Mock()
        self.c.audioFieldsChanged.connect(replaced)
        self.c.tools.handle("recording_config", dict(channels=1, bitrate=8000, gain_left_q8=1, gain_right_q8=2,
                                                     max_duration_ms=1500))
        self.assertEqual(self.c.tools.fields["duration_s"], "1.5")
        replaced.assert_called_once()

    def test_listening_keeps_gestures_but_transfer_pauses_recording(self):
        self.connect()
        c = self.c
        c.start_audio()
        self.worker.start_audio.assert_called_once()
        c.handle("audio_state", dict(state="listening", message="监听"))
        self.assertTrue(c.can_record)
        self.assertFalse(c.can_retry_imu)
        c.handle("audio_state", dict(state="receiving", message="接收"))
        self.assertFalse(c.can_record)
        self.dialogs.ask.return_value = False
        c.stop_audio()
        self.worker.stop_audio.assert_not_called()
        self.dialogs.ask.return_value = True
        c.stop_audio()
        self.worker.stop_audio.assert_called_once()

    def test_history_download_needs_listing_and_passes_chosen_index(self):
        self.connect()
        c = self.c
        self.assertFalse(c.can_download_audio)
        c.download_audio()
        self.worker.download_audio.assert_not_called()
        c.handle("audio_files", [dict(file_index=3), dict(file_index=7)])
        self.assertEqual(c.remote_audio_selected, "录音索引 3")
        c.set_remote_audio_selected("录音索引 7")
        c.download_audio()
        self.worker.download_audio.assert_called_once_with(7, c.audio_directory)

    def test_saved_audio_without_duration_is_labelled_undecoded(self):
        path = self.dir / "a.bin"
        path.write_bytes(b"x" * 2048)
        added = Mock()
        self.c.audioFilesChanged.connect(added)
        self.c.handle("audio_saved", dict(path=str(path)))
        self.assertEqual((self.c.audio_rows[0]["duration"], self.c.audio_rows[0]["size"]), ("未解码", "2.0 KiB"))
        added.assert_called_once()

    def test_transfer_progress_is_not_presented_as_duration(self):
        self.c.handle("audio_progress", dict(bytes=2048, total=None))
        self.assertIn("不是录音计时", self.c.audio_text)


class CloseTests(Base):
    def test_waits_for_device_cleanup_then_finishes(self):
        future = Future()
        self.worker.close.return_value = future
        finished = Mock()
        self.c.closeFinished.connect(finished)
        self.c.request_close()
        self.assertTrue(self.c.closing)
        finished.assert_not_called()
        self.c.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)
        future.set_result(None)
        self.c.wait_closed()
        finished.assert_called_once()

    def test_cleanup_deadline_reports_and_finishes(self):
        self.worker.close.return_value = Future()
        finished = Mock()
        self.c.closeFinished.connect(finished)
        self.c.request_close()
        self.c._close_deadline = 0
        self.c.wait_closed()
        finished.assert_called_once()
        self.assertIn("超过 8 秒", self.errors()[-1])

    def test_declining_any_warning_keeps_app_open(self):
        self.connect()
        self.record()
        self.dialogs.ask.return_value = False
        self.c.request_close()
        self.assertFalse(self.c.closing)
        self.assertEqual(len(self.c.pending), 1)
        self.worker.close.assert_not_called()

    def test_ota_starting_during_confirmation_blocks_close(self):
        self.connect()
        c = self.c
        self.record()
        self.dialogs.ask.side_effect = lambda *_a, **_k: setattr(c.tools, "tool_state", "firmware") or True
        c.request_close()
        self.assertFalse(c.closing)
        self.worker.close.assert_not_called()


class ModelsTests(unittest.TestCase):
    def test_drag_selects_touched_cells_and_clamps(self):
        seconds = 39 / 25
        self.assertEqual(drag_to_region(0, seconds, 40, seconds), (0, 40))
        self.assertEqual(drag_to_region(seconds, 0, 40, seconds), (0, 40))
        self.assertEqual(drag_to_region(-5, 99, 40, seconds), (0, 40))
        self.assertEqual(drag_to_region(0.4, 0.8, 40, seconds), (10, 21))
        self.assertIsNone(drag_to_region(0.4, 0.4, 40, seconds))

    def test_clearing_stream_keeps_clock_and_pause_freezes_only_display(self):
        model = LiveStreamModel()
        model.append_samples([[1] * 6] * 10)
        model.toggle_pause()
        model.append_samples([[2] * 6] * 10)
        _x, frozen, _spans = model.snapshot()
        self.assertEqual(len(frozen), 10)
        self.assertEqual(model.next_sample, 20)
        model.toggle_pause()
        model.clear()
        self.assertEqual(model.next_sample, 20)
        self.assertEqual(len(model.snapshot()[1]), 0)

    def test_recognition_region_leaving_window_is_dropped(self):
        model = LiveStreamModel(sample_rate_hz=1.0, seconds=5.0)
        model.append_samples([[0] * 6] * 5)
        model.add_recognition(0, 2, "甲")
        self.assertEqual(len(model.snapshot()[2]), 1)
        model.append_samples([[0] * 6] * 5)
        self.assertEqual(model.snapshot()[2], [])
        with self.assertRaises(ValueError):
            model.add_recognition(5, 99, "乙")

    def test_acceleration_never_guesses_range_and_clears_on_change(self):
        model = AccelerationModel()
        model.set_stream_state(dict(state="active", sample_rate_hz=25.0))
        model.append_samples([[4096, 0, 0, 0, 0, 0]])
        _x, values, vector, scale = model.snapshot()
        self.assertIsNone(scale)
        self.assertEqual(values[0], 4096)
        self.assertIsNone(vector.magnitude_g)
        model.set_stream_state(dict(state="active", sample_rate_hz=25.0, accel_range_g=8))
        self.assertEqual(len(model.snapshot()[1]), 0)
        model.append_samples([[4096, 0, 0, 0, 0, 0]])
        self.assertAlmostEqual(model.snapshot()[2].magnitude_g, 1.0)
        model.set_stream_state(dict(state="waiting", message="停"))
        self.assertIsNone(model.snapshot()[2])

    def test_analysis_never_modifies_raw_and_reports_impulse_failure(self):
        from hmm_gesture import PipelineConfig, SegmentationConfig
        pipeline = PipelineConfig(sample_rate_hz=100.0, cutoff_hz=30.0, median_kernel=1)
        segmentation = SegmentationConfig.for_sample_rate(100.0, mode="impulse", energy_threshold=8000.0,
                                                          min_samples=pipeline.min_samples)
        raw = np.zeros((100, 6))
        view = analyze_recording(raw, pipeline, segmentation=segmentation)
        self.assertIn("无法训练", view.status)
        self.assertTrue(np.array_equal(view.raw, raw))
        manual = analyze_recording(raw, pipeline, segmentation=segmentation, training_region=(10, 60))
        self.assertIn("手动训练窗口", manual.status)
        self.assertEqual(manual.training_region, (10, 60))


if __name__ == "__main__":
    unittest.main()
