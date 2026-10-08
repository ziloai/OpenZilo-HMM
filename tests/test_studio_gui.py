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
from hmm_gesture import GestureBundle, GestureRecognizer, PipelineConfig, SegmentationConfig
from hmm_gesture_studio import gui
from hmm_gesture_studio.datasets import GestureDataset, dataset_path, load_dataset, save_dataset
from hmm_gesture_studio.training import train_datasets


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
        app.invalid_files = []
        app.pending, app.take = [], None
        app.take_rate, app.pending_rate, app.actual_rate = None, None, 25
        app.connected, app.connecting = True, False
        app.imu_active = True
        app.audio_state = "idle"
        app.audio_directory = app.directory / "audio"
        app.audio_files, app.remote_audio_files = {}, {}
        app.plot = Mock()
        app.audio_tree = Mock()
        app.audio_tree.insert.return_value = "audio-1"
        app.training, app.closing, app.stale = False, False, False
        app.bundle, app.recognizer, app.origin = None, None, None
        app.revision, app.count = 0, 0
        app.locked = [Mock()]
        app.tree = Mock()
        app.tree.get_children.return_value = []
        app.tree.insert.side_effect = (str(i) for i in itertools.count())
        for name in ("name_entry", "record_button", "export_button", "device_choice",
                     "audio_start_button", "audio_stop_button", "audio_dir_button",
                     "audio_list_button", "audio_download_button", "remote_audio_choice"):
            setattr(app, name, Mock())
        for name in ("status", "raw", "take_text", "result", "model_text", "dir_text", "address",
                     "stream_text", "audio_text", "audio_dir_text"):
            setattr(app, name, Var())
        app.name = Var("测试手势")
        app.live = Var(False)
        app.params = {key: Var(value) for key, value in dict(
            n_states="2", cutoff_hz="10", window_size="8", window_overlap="4",
            sample_rate_hz="25", energy_threshold="1500").items()}
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
        self.assertIsNone(self.app.take)
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
