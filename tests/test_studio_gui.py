# SPDX-License-Identifier: MPL-2.0
"""GUI controller tests with fake widgets: no display, Tk, or ring required."""
from __future__ import annotations

from concurrent.futures import Future
import itertools
from pathlib import Path
import queue
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from hmm_gesture import GestureBundle, GestureRecognizer, PipelineConfig, SegmentationConfig, RejectionCalibration
from hmm_gesture_studio import gui
from hmm_gesture_studio.datasets import GestureDataset, dataset_path, load_dataset, save_dataset
from hmm_gesture_studio.training import train_datasets
from hmm_gesture_studio.device_history import DeviceHistory


class Var:
    def __init__(self, value=None):
        self.value = value
    def get(self):
        return self.value
    def set(self, value):
        self.value = value


class GuiControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        symbols = dict(GestureBundle=GestureBundle, GestureRecognizer=GestureRecognizer,
                       PipelineConfig=PipelineConfig, SegmentationConfig=SegmentationConfig,
                       GestureDataset=GestureDataset, load_dataset=load_dataset,
                       save_dataset=save_dataset, dataset_path=dataset_path, train_datasets=train_datasets,
                       filedialog=SimpleNamespace(askopenfilename=Mock(return_value=""),
                                                  asksaveasfilename=Mock(return_value="")),
                       messagebox=SimpleNamespace(askyesno=Mock(return_value=True), showerror=Mock()))
        self.patch = patch.multiple(gui, create=True, **symbols)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.app = app = gui.Studio.__new__(gui.Studio)
        app.root = Mock()
        app.worker = Mock()
        app.directory = Path(self.temp.name)
        app.events = queue.Queue()
        app.records, app.devices = {}, {}
        app.scanned_devices = []
        app.device_history = DeviceHistory(app.directory / "settings" / "devices.json")
        app.preview_repetitions = []
        app.preview_rate = None
        app.preview_is_pending = False
        app.preview_training_regions = []
        app.preview_path = None
        app.preview_editable = False
        app.pending_regions = []
        app.recognition_origin = 0
        app.preview_plot = Mock()
        app.preview_choice = Mock()
        app.preview_choice.current.return_value = -1
        def current(index=None):
            if index is not None:
                app.preview_choice.current.return_value = index
            return app.preview_choice.current.return_value
        app.preview_choice.current.side_effect = current
        app.preview_title = Var()
        app.tabs, app.preview_tab = Mock(), Mock()
        app.invalid_files = []
        app.pending, app.take = [], None
        app.take_rate, app.pending_rate, app.actual_rate = None, None, 25
        app.connected, app.connecting = True, False
        app.imu_active = True
        app.audio_state = "idle"
        app.audio_directory = app.directory / "audio"
        app.audio_files, app.remote_audio_files = {}, {}
        app.plot = Mock()
        app.plot.next_sample = 0
        def append_samples(samples):
            app.plot.next_sample += len(samples)
        app.plot.append_samples.side_effect = append_samples
        app.plot.set_sample_rate.side_effect = lambda rate: setattr(app.plot, "next_sample", 0)
        app.audio_tree = Mock()
        app.audio_tree.insert.return_value = "audio-1"
        app.training, app.closing, app.stale = False, False, False
        app.bundle, app.recognizer, app.origin = None, None, None
        app.revision, app.count = 0, 0
        app.evaluation_revision = None
        app.locked = [Mock()]
        app.tree = Mock()
        app.tree.get_children.return_value = []
        app.tree.insert.side_effect = (str(i) for i in itertools.count())
        for name in ("name_entry", "record_button", "export_button", "device_choice",
                     "audio_start_button", "audio_stop_button", "audio_dir_button",
                     "audio_list_button", "audio_download_button", "remote_audio_choice"):
            setattr(app, name, Mock())
        for name in ("status", "raw", "take_text", "result", "model_text", "dir_text", "address",
                     "stream_text", "audio_text", "audio_dir_text", "evaluation_text", "mode_text"):
            setattr(app, name, Var(""))
        app.name = Var("测试手势")
        app.live = Var(False)
        app.params = {key: Var(value) for key, value in dict(
            n_states="2", cutoff_hz="10", window_size="8", window_overlap="4",
            sample_rate_hz="25", energy_threshold="1500", median_kernel="5", segmentation_mode="motion").items()}
        app.log = Mock()
        app.error = Mock()
        app.executor = Mock()
        app.executor.submit.side_effect = lambda run: run()

    def record(self, count=16, value=1):
        self.app.toggle_record()
        self.app.samples([[value] * 6] * count)
        self.app.toggle_record()

    def test_recording_short_retry_and_append_save(self):
        self.record(count=5)
        self.assertEqual(self.app.pending, [])
        self.record(value=2)
        self.record(value=3)
        self.app.name_entry.configure.assert_called_with(state="disabled")
        self.app.save_takes()
        self.assertEqual(self.app.pending, [])
        self.app.name_entry.configure.assert_called_with(state="normal")
        path = dataset_path(self.app.name.get(), self.app.directory)
        saved = load_dataset(path)
        self.assertEqual([int(rep[0, 0]) for rep in saved.repetitions], [2, 3])
        self.record(value=4)
        self.app.save_takes()
        self.assertEqual(len(load_dataset(path).repetitions), 3)
        self.app.error.assert_not_called()

    def test_pending_samples_obey_recording_boundaries(self):
        self.app.events.put(("samples", [[900] * 6] * 12))
        self.app.toggle_record()
        self.app.events.put(("samples", [[100] * 6] * 16))
        self.app.toggle_record()
        np.testing.assert_array_equal(self.app.pending[0], [[100] * 6] * 16)

    def test_disconnect_discards_incomplete_take_and_stops_inference(self):
        self.app.toggle_record()
        self.app.samples([[1] * 6] * 16)
        self.app.recognizer = Mock()
        recognizer = self.app.recognizer
        self.app.handle("disconnected", None)
        self.assertIsNone(self.app.take)
        self.assertEqual(self.app.pending, [])
        self.assertFalse(self.app.connected)
        self.assertIsNone(self.app.recognizer)
        recognizer.reset.assert_called_once()

    def test_unrelated_scan_error_does_not_cancel_connection(self):
        self.app.connecting = True
        self.app.handle("error", "Cannot scan: connect already active")
        self.assertTrue(self.app.connecting)
        self.app.handle("connected", dict(address="test", sample_rate_hz=50,
                                          accel_range_g=8, gyro_range_dps=2000))
        self.assertTrue(self.app.connected)
        self.assertIsNone(self.app.actual_rate)
        self.app.handle("stream_state", dict(state="active", sample_rate_hz=50, accel_range_g=8, gyro_range_dps=2000))
        self.assertEqual(self.app.actual_rate, 50)
        self.app.worker.disconnect.assert_not_called()

    def test_save_failure_preserves_pending_recordings(self):
        self.record()
        self.app.save_takes()
        self.assertEqual(len(self.app.pending), 1)
        self.app.error.assert_called_once()
        self.assertFalse(list(self.app.directory.glob("*.json")))

    def test_existing_file_collision_and_rate_mismatch_never_overwrite(self):
        save_dataset(GestureDataset("a/b", [np.ones((16, 6))] * 2), self.app.directory)
        with self.assertRaisesRegex(ValueError, "冲突"):
            self.app.persist(GestureDataset("a\\b", [np.ones((16, 6))] * 2))
        with self.assertRaisesRegex(ValueError, "采样率"):
            self.app.persist(GestureDataset("a/b", [np.ones((16, 6))] * 2, 50))
        self.assertEqual(load_dataset(dataset_path("a/b", self.app.directory)).sample_rate_hz, 25)

    def test_background_training_export_and_stale_state(self):
        rng = np.random.default_rng(4)
        data = GestureDataset("训练", [rng.integers(-100, 100, (24, 6)) for _ in range(3)])
        save_dataset(data, self.app.directory)
        self.app.train()
        self.assertTrue(self.app.training)
        self.app.drain_events()
        self.assertFalse(self.app.training)
        self.assertIsNotNone(self.app.bundle)
        self.assertEqual(self.app.origin, "trained")
        self.app.error.assert_not_called()
        self.app.invalidate("修改数据")
        self.assertTrue(self.app.stale)
        self.app.export_button.configure.assert_called_with(state="disabled")
        self.app.live.set(True)
        self.app.toggle_live()
        self.assertIsNone(self.app.recognizer)
        self.assertFalse(self.app.live.get())

    def test_background_evaluation_preserves_existing_model(self):
        rng = np.random.default_rng(4)
        dataset = GestureDataset("训练", [rng.integers(-100, 100, (24, 6)) for _ in range(4)])
        save_dataset(dataset, self.app.directory)
        previous = self.app.bundle = Mock()
        self.app.origin = "trained"
        self.app.evaluate()
        self.assertTrue(self.app.training)
        self.app.drain_events()
        self.assertFalse(self.app.training)
        self.assertIs(self.app.bundle, previous)
        self.assertFalse(self.app.stale)
        self.assertIn("/4", self.app.evaluation_text.get())
        self.assertIn("正样本通过率", self.app.evaluation_text.get())
        self.app.error.assert_not_called()
        self.app.export_button.configure.assert_called_with(state="normal")
        self.app.invalidate("新数据")
        self.assertIn("重新", self.app.evaluation_text.get())

    def test_evaluation_failure_and_invalid_files_preserve_model(self):
        save_dataset(GestureDataset("少", [np.ones((16, 6))] * 2), self.app.directory)
        previous = self.app.bundle = Mock()
        self.app.origin = "trained"
        self.app.evaluate()
        self.app.drain_events()
        self.assertFalse(self.app.training)
        self.assertIs(self.app.bundle, previous)
        self.assertFalse(self.app.stale)
        self.assertIn("4 次", str(self.app.error.call_args))
        (self.app.directory / "bad.json").write_text("{broken")
        self.app.executor.submit.reset_mock()
        self.app.evaluate()
        self.app.executor.submit.assert_not_called()

    def test_bundle_dialogs_use_simple_json_type_and_round_trip(self):
        # Cocoa cannot resolve a compound extension such as "gesture.json" to
        # a file type. Keep that suffix in the filename, not native type filters.
        rng = np.random.default_rng(4)
        data = GestureDataset("向上", [rng.integers(-100, 100, (24, 6)) for _ in range(3)])
        original = train_datasets([data], PipelineConfig(), n_states=2)
        source = self.app.directory / "导入.gesture.json"
        original.save(source)
        gui.filedialog.askopenfilename.return_value = str(source)
        self.app.load_bundle()
        opened = gui.filedialog.askopenfilename.call_args.kwargs
        self.assertIs(opened["parent"], self.app.root)
        self.assertEqual([entry[1] for entry in opened["filetypes"]], ["*.json"])
        self.assertEqual(self.app.bundle.gesture_names, original.gesture_names)
        self.assertEqual(self.app.origin, "imported")
        self.assertFalse(self.app.stale)

        # Honor the exact filename returned by the native dialog: adding a
        # suffix afterwards could overwrite a different file without consent.
        target = self.app.directory / "用户选择.json"
        gui.filedialog.asksaveasfilename.return_value = str(target)
        self.app.export_bundle()
        saved = gui.filedialog.asksaveasfilename.call_args.kwargs
        self.assertIs(saved["parent"], self.app.root)
        self.assertEqual([entry[1] for entry in saved["filetypes"]], ["*.json"])
        self.assertEqual(saved["defaultextension"], ".json")
        self.assertTrue(saved["initialfile"].endswith(".gesture.json"))
        self.assertEqual(GestureBundle.load(target).gesture_names, original.gesture_names)
        self.assertFalse(target.with_suffix(".gesture.json").exists())
        self.app.error.assert_not_called()

    def test_cancelling_bundle_dialogs_preserves_loaded_model(self):
        previous = self.app.bundle = Mock()
        self.app.origin = "imported"
        self.app.live.set(True)
        with patch.object(GestureBundle, "load") as load:
            self.app.load_bundle()
        self.app.export_bundle()
        load.assert_not_called()
        previous.save.assert_not_called()
        self.assertIs(self.app.bundle, previous)
        self.assertEqual(self.app.origin, "imported")
        self.assertTrue(self.app.live.get())
        self.app.error.assert_not_called()

    def test_json_filter_still_validates_bundle_contents(self):
        previous = self.app.bundle = Mock()
        self.app.live.set(True)
        for name, contents in (("broken.json", "{broken"), ("ordinary.json", "{}")):
            with self.subTest(name=name):
                path = self.app.directory / name
                path.write_text(contents, encoding="utf-8")
                gui.filedialog.askopenfilename.return_value = str(path)
                self.app.error.reset_mock()
                self.app.load_bundle()
                self.app.error.assert_called_once()
                self.assertIs(self.app.bundle, previous)
                self.assertTrue(self.app.live.get())

    def test_bundle_dialog_python_errors_are_reported(self):
        previous = self.app.bundle = Mock()
        for dialog, action in ((gui.filedialog.askopenfilename, self.app.load_bundle),
                               (gui.filedialog.asksaveasfilename, self.app.export_bundle)):
            with self.subTest(action=action.__name__):
                dialog.side_effect = RuntimeError("dialog failed")
                self.app.error.reset_mock()
                action()
                self.app.error.assert_called_once_with("dialog failed")
                self.assertIs(self.app.bundle, previous)
        previous.save.assert_not_called()

    def test_bundle_export_write_error_is_reported(self):
        self.app.bundle = Mock()
        self.app.bundle.save.side_effect = OSError("read only")
        target = str(self.app.directory / "export.gesture.json")
        gui.filedialog.asksaveasfilename.return_value = target
        self.app.export_bundle()
        self.app.bundle.save.assert_called_once_with(target)
        self.app.error.assert_called_once_with("read only")
        self.app.log.assert_not_called()

    def test_invalid_dataset_does_not_silently_train_subset(self):
        save_dataset(GestureDataset("ok", [np.ones((16, 6))] * 2), self.app.directory)
        (self.app.directory / "broken.json").write_text("{broken", encoding="utf-8")
        self.app.train()
        self.app.executor.submit.assert_not_called()
        self.app.error.assert_called_once()
        self.assertIn("broken.json", str(self.app.error.call_args))

    def test_training_failure_clears_old_bundle(self):
        save_dataset(GestureDataset("one", [np.ones((16, 6))]), self.app.directory)
        self.app.bundle = Mock()
        self.app.origin = "imported"
        self.app.train()
        self.app.drain_events()
        self.assertIsNone(self.app.bundle)
        self.assertFalse(self.app.training)
        self.app.export_button.configure.assert_called_with(state="disabled")
        self.app.error.assert_called_once()

    def test_live_recognition_rejects_device_rate_mismatch(self):
        self.app.bundle = SimpleNamespace(pipeline=PipelineConfig(sample_rate_hz=50))
        self.app.live.set(True)
        self.app.toggle_live()
        self.assertFalse(self.app.live.get())
        self.assertIsNone(self.app.recognizer)
        self.assertIn("采样率", str(self.app.error.call_args))

    def test_close_waits_for_device_cleanup_without_blocking_tk(self):
        future = Future()
        self.app.worker.close.return_value = future
        self.app.close()
        self.app.root.destroy.assert_not_called()
        self.app.root.after.assert_called_once()
        future.set_result(None)
        self.app.root.after.call_args.args[1]()
        self.app.root.destroy.assert_called_once()
        self.app.executor.shutdown.assert_called_once_with(wait=False, cancel_futures=True)

    def test_mode_pause_retains_ble_and_discards_only_unfinished_take(self):
        self.record(value=2)
        self.app.toggle_record()
        self.app.samples([[9] * 6] * 20)
        self.app.handle("stream_state", {"state": "waiting", "message": "等待手势模式"})
        self.assertTrue(self.app.connected)
        self.assertFalse(self.app.imu_active)
        self.assertIsNone(self.app.take)
        self.assertEqual(len(self.app.pending), 1)
        self.assertIsNone(self.app.actual_rate)
        self.app.plot.set_status.assert_called_with("等待手势模式")
        self.app.worker.disconnect.assert_not_called()
        self.app.handle("stream_state", {"state": "active", "sample_rate_hz": 25})
        self.app.samples([[4] * 6] * 5)
        self.app.plot.set_sample_rate.assert_called_with(25)
        self.app.plot.append_samples.assert_called_with([[4] * 6] * 5)
        self.assertIn("4 / 4 / 4", self.app.raw.get())

    def test_gesture_recording_requires_active_imu(self):
        self.app.imu_active = False
        self.app.toggle_record()
        self.assertIsNone(self.app.take)
        self.app.error.assert_called_once()

    def test_reconnecting_accepts_next_connection_without_cancel(self):
        self.app.handle("reconnecting", {"message": "自动重连…", "attempt": 1})
        self.assertTrue(self.app.connecting)
        self.assertFalse(self.app.connected)
        self.app.handle("connected", {"address": "ring", "model": "Q", "firmware_version": "test"})
        self.assertTrue(self.app.connected)
        self.assertFalse(self.app.connecting)
        self.assertFalse(self.app.imu_active)
        self.app.worker.disconnect.assert_not_called()

    def test_audio_controls_arm_receiver_not_hardware_recording(self):
        self.app.start_audio()
        self.assertEqual(self.app.audio_state, "starting")
        self.app.worker.start_audio.assert_called_once_with(self.app.audio_directory)
        self.app.handle("audio_state", {"state": "listening", "message": "等待戒指松键后推送"})
        self.app.toggle_record()
        self.assertEqual(self.app.take, [])  # Passive listening no longer blocks IMU.
        self.app.stop_audio()
        self.app.worker.stop_audio.assert_called_once()
        self.assertEqual(self.app.audio_state, "stopping")

    def test_audio_transfer_progress_is_not_fabricated_recording_duration(self):
        self.app.handle("audio_progress", {"bytes": 2048, "total": 4096})
        self.assertIn("KiB", self.app.audio_text.get())
        self.assertIn("不是录音计时", self.app.audio_text.get())

    def test_audio_raw_only_save_and_unknown_duration(self):
        path = self.app.directory / "raw.bin"
        path.write_bytes(b"raw recording")
        self.app.handle("audio_saved", {"path": str(path), "raw_path": str(path),
                                       "duration_s": None, "file_index": 3})
        self.assertEqual(self.app.audio_files["audio-1"], path)
        self.assertEqual(self.app.audio_tree.insert.call_args.kwargs["values"][1], "未解码")

    def test_impulse_preset_uses_device_rate_and_exposes_median_off(self):
        self.app.actual_rate = 100
        self.app.bundle = Mock()
        self.app.origin = "trained"
        self.app.apply_preset("impulse")
        pipeline = self.app.pipeline_settings()
        segmentation = self.app.segmentation_settings(pipeline)
        self.assertEqual((pipeline.sample_rate_hz, pipeline.cutoff_hz, pipeline.median_kernel), (100, 30, 1))
        self.assertEqual(pipeline.filter_initialization, "steady")
        self.assertEqual(segmentation.mode, "impulse")
        self.assertEqual((segmentation.pre_roll, segmentation.post_roll, segmentation.max_gesture_len), (12, 28, 500))
        self.assertEqual(segmentation.energy_threshold, 8000)
        self.assertTrue(self.app.stale)
        self.assertIn("训练默认自动裁剪", self.app.mode_text.get())
        self.app.apply_preset("motion")
        self.assertEqual(self.app.pipeline_settings().median_kernel, 5)
        self.assertEqual(self.app.segmentation_settings(self.app.pipeline_settings()).max_gesture_len, 500)

    def test_preset_can_use_recorded_rate_without_connected_device(self):
        self.app.actual_rate = None
        self.app.records["saved"] = (Path("unused"), GestureDataset("响指", [np.ones((60, 6))], 100))
        self.app.apply_preset("impulse")
        self.assertEqual(self.app.params["sample_rate_hz"].get(), "100")
        self.assertEqual(self.app.params["cutoff_hz"].get(), "30")

    def test_single_old_model_cannot_enable_false_positive_stream(self):
        rng = np.random.default_rng(7)
        data = GestureDataset("旧响指", [rng.integers(-100, 100, (24, 6)) for _ in range(3)])
        self.app.bundle = train_datasets([data], PipelineConfig())
        self.app.live.set(True)
        self.app.toggle_live()
        self.assertFalse(self.app.live.get())
        self.assertIsNone(self.app.recognizer)
        self.assertIn("拒识标定", str(self.app.error.call_args))

    def test_connection_history_remembers_only_successes_and_survives_empty_scan(self):
        self.app.connected = False
        self.app.address.set("ring-uuid")
        self.app.handle("devices", [{"name": "我的戒指", "address": "ring-uuid", "rssi": -42}])
        self.app.connect()
        self.assertEqual(self.app.device_history.entries, [])
        self.app.handle("connected", {"address": "ring-uuid", "model": "Q"})
        self.assertEqual(self.app.device_history.entries[0]["name"], "我的戒指")
        self.app.handle("devices", [])
        self.assertEqual(list(self.app.devices.values()), ["ring-uuid"])
        self.assertIn("历史", next(iter(self.app.devices)))
        self.assertEqual(DeviceHistory(self.app.device_history.path).entries[0]["address"], "ring-uuid")
        self.app.clear_device_history()
        self.assertEqual(self.app.device_history.entries, [])
        self.assertTrue(self.app.connected)

    def test_history_write_failure_does_not_break_successful_connection(self):
        self.app.connecting = True
        with patch.object(self.app.device_history, "remember", side_effect=OSError("read only")):
            self.app.handle("connected", {"address": "ring-uuid"})
        self.assertTrue(self.app.connected)
        self.assertIn("BLE 已连接", self.app.status.get())
        self.assertTrue(any("无法保存历史" in str(call) for call in self.app.log.call_args_list))

    def test_each_take_previews_shared_filter_at_device_rate_and_undo_updates(self):
        self.app.actual_rate = 50
        self.record(value=5)
        samples, config = self.app.preview_plot.show_recording.call_args.args
        np.testing.assert_array_equal(samples, self.app.pending[0])
        self.assertEqual(config.sample_rate_hz, 50)
        self.assertEqual(config.cutoff_hz, 10)
        self.app.tabs.select.assert_called_with(self.app.preview_tab)
        self.record(value=6)
        self.assertEqual(len(self.app.preview_repetitions), 2)
        self.app.undo()
        self.assertEqual(len(self.app.preview_repetitions), 1)
        self.app.discard_takes()
        self.assertEqual(self.app.preview_repetitions, [])
        self.app.preview_plot.clear.assert_called()

    def test_preview_invalid_filter_and_saved_dataset(self):
        dataset = GestureDataset("已保存", [np.ones((20, 6))] * 2, 50)
        self.app.records["one"] = (Path("unused"), dataset)
        self.app.tree.selection.return_value = ["one"]
        self.app.preview_dataset()
        self.assertEqual(self.app.preview_rate, 50)
        self.assertFalse(self.app.preview_is_pending)
        self.app.params["cutoff_hz"].set("30")
        self.app.parameters_changed()
        self.assertIn("无法预览", self.app.preview_plot.clear.call_args.args[0])
        self.assertEqual(self.app.preview_repetitions, dataset.repetitions)

    def test_manual_regions_survive_switch_undo_save_append_and_reload(self):
        self.record(count=40, value=2)
        self.app.set_preview_training_region((5, 25))
        self.record(count=40, value=3)
        self.app.set_preview_training_region((10, 30))
        self.app.preview_choice.current(0)
        self.app.show_preview()
        self.assertEqual(self.app.preview_plot.show_recording.call_args.kwargs["training_region"], (5, 25))
        self.record(count=40, value=4)
        self.app.undo()
        self.assertEqual(self.app.pending_regions, [(5, 25), (10, 30)])
        self.app.save_takes()
        path = dataset_path(self.app.name.get(), self.app.directory)
        saved = load_dataset(path)
        self.assertEqual(saved.training_regions, [(5, 25), (10, 30)])
        self.assertEqual([len(rep) for rep in saved.repetitions], [40, 40])
        self.assertEqual(self.app.pending_regions, [])
        self.assertEqual(self.app.preview_path, path)
        self.record(count=40, value=5)
        self.app.set_preview_training_region((1, 31))
        self.app.save_takes()
        self.assertEqual(load_dataset(path).training_regions, [(5, 25), (10, 30), (1, 31)])
        self.assertEqual(len(self.app.preview_repetitions), 3)
        self.app.set_preview_training_region(None)
        self.assertEqual(load_dataset(path).training_regions, [(5, 25), (10, 30), None])
        self.app.error.assert_not_called()

    def test_manual_saved_edit_uses_exact_path_and_invalidates_model(self):
        dataset = GestureDataset("已保存", [np.ones((40, 6))] * 3)
        path = save_dataset(dataset, self.app.directory)
        renamed = path.rename(self.app.directory / "imported-custom-name.json")
        self.app.records["one"] = (renamed, dataset)
        self.app.tree.selection.return_value = ["one"]
        self.app.preview_dataset()
        self.app.bundle, self.app.origin = Mock(), "trained"
        self.app.evaluation_revision = self.app.revision
        self.app.set_preview_training_region((3, 30))
        self.assertFalse(path.exists())
        saved = load_dataset(renamed)
        self.assertEqual(saved.training_regions, [None, None, (3, 30)])
        np.testing.assert_array_equal(saved.repetitions[2], dataset.repetitions[2])
        self.assertEqual(self.app.records["one"][1].training_regions, saved.training_regions)
        self.assertTrue(self.app.stale)
        self.assertIsNone(self.app.evaluation_revision)
        self.app.refresh()
        self.assertEqual(self.app.preview_repetitions, [])
        key = next(iter(self.app.records))
        self.app.tree.selection.return_value = [key]
        self.app.preview_dataset()
        self.assertEqual(self.app.preview_plot.show_recording.call_args.kwargs["training_region"], (3, 30))
        self.app.set_preview_training_region(None)
        self.assertIsNone(load_dataset(renamed).training_regions)
        self.app.error.assert_not_called()

    def test_manual_region_rejects_short_out_of_bounds_and_busy_edits(self):
        self.record(count=40)
        self.app.set_preview_training_region((2, 30))
        for region in ((2, 10), (-1, 30), (2, 41), (30, 2)):
            with self.subTest(region=region):
                self.app.error.reset_mock()
                self.app.set_preview_training_region(region)
                self.app.error.assert_called_once()
                self.assertEqual(self.app.pending_regions, [(2, 30)])
        self.app.training = True
        self.app.set_preview_training_region(None)
        self.assertEqual(self.app.pending_regions, [(2, 30)])
        self.app.training = False
        self.record(count=5)
        self.assertFalse(self.app.preview_editable)
        self.app.set_preview_training_region(None)
        self.assertEqual(self.app.pending_regions, [(2, 30)])
        self.app.discard_takes()
        self.assertEqual(self.app.pending_regions, [])

    def test_manual_saved_edit_failure_preserves_annotation_and_file(self):
        dataset = GestureDataset("保存失败", [np.ones((40, 6))], training_regions=[(1, 25)])
        path = save_dataset(dataset, self.app.directory)
        self.app.preview_recordings(dataset.repetitions, 25, "已保存", path=path,
                                    training_regions=dataset.training_regions)
        original = path.read_bytes()
        revision = self.app.revision
        with patch("hmm_gesture_studio.datasets.save_dataset_file", side_effect=OSError("read only")):
            self.app.set_preview_training_region((2, 30))
        self.app.error.assert_called_once_with("read only")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.app.preview_training_regions, [(1, 25)])
        self.assertEqual(self.app.revision, revision)
        # An external raw-data edit cannot be overwritten from an old preview.
        external = load_dataset(path)
        external.repetitions[0][:] = 5
        save_dataset(external, self.app.directory)
        self.app.set_preview_training_region((2, 30))
        self.assertIn("文件已变化", self.app.error.call_args.args[0])
        self.assertTrue((load_dataset(path).repetitions[0] == 5).all())

    def enable_live(self):
        rng = np.random.default_rng(4)
        data = GestureDataset("动作", [rng.integers(-100, 100, (24, 6)) for _ in range(3)])
        self.app.bundle = train_datasets([data], PipelineConfig(), n_states=2)
        # This controller test exercises positions, not a learned acceptance limit.
        self.app.bundle.rejection = {"动作": RejectionCalibration(-1e30, 0, 12, 125)}
        self.app.live.set(True)
        self.app.toggle_live()
        self.assertIsNotNone(self.app.recognizer)

    def test_recognition_boxes_use_stream_origin_and_exclude_end_rest(self):
        self.app.samples([[0] * 6] * 100)  # Plot already has data before recognition starts.
        self.enable_live()
        self.app.samples([[0] * 6] * 30 + [[5000, 0, 0, 0, 0, 0]] * 20 + [[0] * 6] * 30)
        args = self.app.plot.add_recognition.call_args.args
        self.assertEqual(args[:2], (127, 150))
        self.assertEqual("动作", args[2])
        self.assertIn("无相对置信度", self.app.result.get())
        self.assertNotIn("0.80", self.app.result.get())
        self.app.error.assert_not_called()

    def test_listening_preserves_live_and_actual_transfer_pauses_then_resumes_it(self):
        self.enable_live()
        first = self.app.recognizer
        self.app.start_audio()
        self.app.handle("audio_state", {"state": "listening"})
        self.assertIs(self.app.recognizer, first)
        self.app.handle("stream_state", {"state": "suspended"})
        self.app.handle("audio_state", {"state": "receiving"})
        self.assertTrue(self.app.live.get())
        self.assertIsNone(self.app.recognizer)
        self.app.toggle_record()
        self.assertIsNone(self.app.take)
        self.app.handle("audio_state", {"state": "listening"})
        self.app.handle("stream_state", {"state": "active", "sample_rate_hz": 25})
        self.assertIsNotNone(self.app.recognizer)
        self.assertIsNot(self.app.recognizer, first)
        self.assertTrue(self.app.live.get())
        self.assertEqual(self.app.recognition_origin, 0)
        # A different device rate must NOT silently resume the model.
        self.app.handle("stream_state", {"state": "active", "sample_rate_hz": 50})
        self.assertIsNone(self.app.recognizer)
        self.assertFalse(self.app.live.get())

    def test_history_download_is_explicit_and_mutually_exclusive(self):
        self.app.list_audio()
        self.app.worker.list_audio.assert_called_once()
        self.app.handle("audio_files", [{"file_index": 0}, {"file_index": 1}])
        self.app.handle("audio_state", {"state": "idle", "message": "查询完成"})
        self.app.remote_audio_choice.get.return_value = "录音索引 1"
        self.app.download_audio()
        self.app.worker.download_audio.assert_called_once_with(1, self.app.audio_directory)
        self.app.start_audio()
        self.app.worker.start_audio.assert_not_called()
        self.app.error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
