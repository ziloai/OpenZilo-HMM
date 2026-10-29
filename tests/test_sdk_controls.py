# SPDX-License-Identifier: MPL-2.0
"""SDK UI tests using fake widgets/worker: no display or hardware operations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import queue
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hmm_gesture_studio.gui import Studio
from hmm_gesture_studio.sdk_controls import SDKControls, recording_options


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


@dataclass(frozen=True)
class Verified:
    bundle: bytes = b"verified snapshot"
    public_key: bytes = b"trusted key"
    summary: str = "signature verified; model Q; board 1; version 1 -> 2"


class RecordingOptionsTests(unittest.TestCase):
    def setUp(self):
        self.values = dict(channels="2", bitrate="16000", gain_left_q8="256",
                           gain_right_q8="256", duration_s="10")

    def test_defaults_and_exact_duration_conversion(self):
        self.assertEqual(recording_options(self.values), dict(channels=2, bitrate=16000,
                         gain_left_q8=256, gain_right_q8=256, max_duration_ms=10000))
        for seconds, milliseconds in ((".02", 20), ("1.02", 1020), ("60", 60000)):
            self.values["duration_s"] = seconds
            self.assertEqual(recording_options(self.values)["max_duration_ms"], milliseconds)

    def test_invalid_fields_rejected_not_coerced(self):
        for key, invalid in (
            ("channels", [True, "", "0", "3", "1.0"]),
            ("bitrate", [True, "7999", "32001", "16000.1", "NaN"]),
            ("gain_left_q8", ["-1", "1025", "2.5"]),
            ("gain_right_q8", ["-1", "1025", "inf"]),
            ("duration_s", ["0", "60.02", "-1", "1.001", "nan", "inf", "", True]),
        ):
            for value in invalid:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    recording_options({**self.values, key: value})

    def test_boundary_gains_and_bitrate(self):
        options = recording_options({**self.values, "channels": "1", "bitrate": "8000",
                                     "gain_left_q8": "0", "gain_right_q8": "1024"})
        self.assertEqual(options["gain_left_q8"], 0)
        self.assertEqual(options["gain_right_q8"], 1024)


class SDKControlsTests(unittest.TestCase):
    def setUp(self):
        app = self.app = Studio.__new__(Studio)
        app.root = Mock()
        app.worker = Mock()
        app.executor = Mock()
        app.events = queue.Queue()
        app.connected, app.connecting, app.closing = True, False, False
        app.training = False
        app.audio_state = "idle"
        app.take = None
        app.imu_active = True
        app.remote_audio_files = {"record 1": 1}
        app.audio_directory = Path("/tmp/test-sdk-audio")
        app.live = Var(False)
        app.recognizer = None
        app.status = Var("connected ring-1")
        app.audio_text = Var()
        app.result = Var()
        app.name = Var("gesture")
        app.log = Mock()
        app.error = Mock()
        app.confirm_discard = Mock(return_value=True)
        app.stop_live = Mock()
        app.cancel_take = Mock()
        for name in ("audio_start_button", "audio_stop_button", "audio_dir_button", "audio_list_button",
                     "audio_download_button", "record_button", "live_button", "scan_button", "retry_imu_button",
                     "connect_button", "disconnect_button"):
            setattr(app, name, Mock())
        self.ui = app.sdk_controls = SDKControls(app)
        self.ui.capabilities = dict(recording_control=True, live_audio=True, audio_files=True, imu=True, ota=True)
        self.ui.fields = {key: Var(value) for key, value in dict(channels="2", bitrate="16000",
                         gain_left_q8="256", gain_right_q8="256", duration_s="10").items()}
        self.ui.texts = {key: Var() for key in ("recording", "calibration", "profile", "sample", "firmware", "firmware_status")}
        self.ui.buttons = {key: Mock() for key in ("record", "read", "live_audio", "finish", "accel", "quick",
                          "full", "continue", "cancel", "profile", "reset", "diagnostics", "inspect", "install")}
        self.ui.zip_path, self.ui.key_path = Var("/tmp/signed.zip"), Var("/tmp/trusted.pem")
        self.ui.progress = Mock()
        self.ui.verified = Verified()

    def assert_state(self, widget, state):
        widget.configure.assert_called_with(state=state)

    def test_imports_do_not_import_tk_or_initialize_hardware(self):
        result = subprocess.run([sys.executable, "-c", "import sys; "
            "import hmm_gesture_studio.gui, hmm_gesture_studio.sdk_controls; "
            "assert 'tkinter' not in sys.modules; assert 'openzilo' not in sys.modules"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_capabilities_gate_buttons_and_callbacks(self):
        self.ui.handle("capabilities", dict(recording_control=False, live_audio=False, imu=False, ota=False))
        for action in ("record", "read", "live_audio", "accel", "quick", "full", "profile", "reset", "diagnostics", "install"):
            self.assertFalse(self.ui.available(action))
            self.assert_state(self.ui.buttons[action], "disabled")
        self.ui.start_recording()
        self.ui.start_diagnostics()
        self.ui.install_firmware()
        self.app.worker.assert_not_called()
        self.assertEqual(self.app.worker.mock_calls, [])
        self.assert_state(self.app.audio_start_button, "disabled")
        self.assert_state(self.app.record_button, "disabled")
        self.assert_state(self.app.retry_imu_button, "disabled")
        self.assertTrue(self.ui.available("inspect"))

    def test_audio_files_capability_is_independent_of_remote_recording(self):
        for files, remote in ((True, False), (False, True), (False, False), (True, True)):
            with self.subTest(audio_files=files, recording_control=remote):
                self.ui.handle("capabilities", dict(audio_files=files, recording_control=remote, live_audio=True))
                self.assertEqual(self.ui.available("audio"), files)
                self.assertEqual(self.ui.available("record"), remote)
                self.assertEqual(self.ui.available("read"), remote)
                self.assertEqual(self.ui.available("live_audio"), remote)
                for widget in (self.app.audio_start_button, self.app.audio_list_button, self.app.audio_download_button):
                    self.assert_state(widget, "normal" if files else "disabled")
        self.ui.handle("capabilities", dict(recording_control=True))
        self.assertFalse(self.ui.available("audio"))

    def test_live_requires_both_audio_capabilities(self):
        self.ui.capabilities["live_audio"] = False
        self.assertTrue(self.ui.available("record"))
        self.assertFalse(self.ui.available("live_audio"))
        self.ui.capabilities.update(recording_control=False, live_audio=True)
        self.assertFalse(self.ui.available("live_audio"))

    def test_audio_tool_and_take_exclusion(self):
        for state in ("starting", "recording", "live", "listening", "receiving", "downloading", "saving", "stopping"):
            with self.subTest(audio_state=state):
                self.app.audio_state = state
                for action in ("record", "read", "accel", "diagnostics", "profile", "reset", "install"):
                    self.assertFalse(self.ui.available(action))
        self.app.audio_state = "idle"
        self.app.take = []
        self.ui.start_calibration("quick")
        self.ui.start_diagnostics()
        self.ui.start_recording()
        self.assertEqual(self.app.worker.mock_calls, [])
        self.app.take = None
        self.ui.tool_state = "diagnostics"
        self.app.start_audio()
        self.app.list_audio()
        self.app.stop_audio()
        self.assertEqual(self.app.worker.mock_calls, [])
        self.assertFalse(self.ui.available("record"))

    def test_remote_record_and_pcm_use_validated_options_and_local_lock(self):
        for live in (False, True):
            with self.subTest(live=live):
                self.app.audio_state = "idle"
                self.app.worker.reset_mock()
                self.ui.start_recording(live=live)
                self.app.worker.start_remote_recording.assert_called_once_with(
                    dict(channels=2, bitrate=16000, gain_left_q8=256, gain_right_q8=256, max_duration_ms=10000),
                    self.app.audio_directory, live=live)
                self.app.stop_live.assert_called()
                self.assertEqual(self.app.audio_state, "starting")
                self.assertFalse(self.ui.available("record"))
                self.assertFalse(self.ui.available("install"))
                self.assert_state(self.app.audio_start_button, "disabled")
                self.app.handle("audio_state", {"state": "live" if live else "recording"})
                self.ui.finish_recording()
                self.app.worker.finish_remote_recording.assert_called_once()
                self.assertEqual(self.app.audio_state, "stopping")
                self.assertFalse(self.ui.available("finish"))
                self.app.handle("audio_state", {"state": "idle"})
                self.assertFalse(self.ui.remote_session)

    def test_invalid_options_never_call_worker(self):
        self.ui.fields["duration_s"].set("1.001")
        self.ui.start_recording()
        self.assertEqual(self.app.worker.mock_calls, [])
        self.assertEqual(self.app.audio_state, "idle")
        self.app.error.assert_called_once()

    def test_sync_audio_failure_restores_idle(self):
        self.app.worker.start_remote_recording.side_effect = RuntimeError("failed")
        self.ui.start_recording()
        self.assertEqual(self.app.audio_state, "idle")
        self.assertFalse(self.ui.remote_session)
        self.assertTrue(self.ui.available("record"))

    def test_recording_config_is_rendered_only_on_queued_event(self):
        self.ui.read_config()
        self.app.worker.read_recording_config.assert_called_once()
        self.assertEqual(self.app.audio_state, "listing")
        self.app.events.put(("recording_config", dict(channels=1, bitrate=32000, gain_left_q8=0,
                              gain_right_q8=1024, max_duration_ms=1020)))
        self.assertEqual(self.ui.fields["channels"].get(), "2")
        self.app.drain_events()
        self.assertEqual(self.ui.fields["channels"].get(), "1")
        self.assertEqual(self.ui.fields["duration_s"].get(), "1.02")
        self.app.handle("recording_status", "saved")
        self.assertEqual(self.ui.texts["recording"].get(), "saved")

    def test_calibration_requires_prompt_and_continuation_is_single_use(self):
        self.ui.start_calibration("accel")
        self.assertEqual(self.ui.tool_state, "calibrating")
        self.app.worker.start_calibration.assert_called_once_with("accel")
        self.assertFalse(self.ui.available("continue"))
        self.ui.continue_calibration()
        self.app.worker.continue_calibration.assert_not_called()
        self.app.handle("calibration_prompt", "Put +X up and release")
        self.assertTrue(self.ui.available("continue"))
        self.assertEqual(self.ui.texts["calibration"].get(), "Put +X up and release")
        self.ui.continue_calibration()
        self.ui.continue_calibration()
        self.app.worker.continue_calibration.assert_called_once()
        self.assertFalse(self.ui.awaiting_continue)
        self.app.handle("calibration_progress", "Validating")
        self.assertFalse(self.ui.available("continue"))
        self.app.handle("tool_state", dict(state="idle", message="done"))
        self.app.handle("calibration_prompt", "late prompt")
        self.assertFalse(self.ui.available("continue"))

    def test_calibration_sample_stays_out_of_raw_pipeline(self):
        self.ui.start_diagnostics()
        self.app.samples = Mock()
        self.app.plot = Mock()
        self.app.acceleration_view = Mock()
        self.app.handle("calibration_sample", dict(accel_g=[0, 0, 1], gyro_dps=[0, 0, 0], applied_components=["accel"]))
        self.assertIn("g：", self.ui.texts["sample"].get())
        self.assertIn("°/s", self.ui.texts["sample"].get())
        self.app.samples.assert_not_called()
        self.app.plot.append_samples.assert_not_called()
        self.app.acceleration_view.append_samples.assert_not_called()

    def test_diagnostics_reports_actual_components_without_accuracy_claim(self):
        self.ui.start_diagnostics()
        for applied in ([], ["accel"], ["accel", "gyro"]):
            with self.subTest(applied=applied):
                self.app.handle("calibration_sample", dict(accel_g=[0, 0, 1], gyro_dps=[0, 0, 0], applied_components=applied))
                text = self.ui.texts["sample"].get()
                self.assertIn(f"实际应用校准组件：{applied}", text)
                if not applied:
                    self.assertIn("未应用校准", text)
                    self.assertIn("原始物理单位", text)
                    self.assertIn("不能据此认定精度已验证", text)
                else:
                    self.assertIn("仅列出的组件", text)
                    self.assertIn("不代表精度已验证", text)

    def test_diagnostics_stop_and_disconnect_clear_and_ignore_late_samples(self):
        sample = dict(accel_g=[0, 0, 1], gyro_dps=[0, 0, 0], applied_components=[])
        for ending in ("stop", "idle", "disconnected", "reconnecting"):
            with self.subTest(ending=ending):
                self.ui.tool_state = "idle"
                self.ui.capabilities["imu"] = True
                self.ui.start_diagnostics()
                self.app.handle("calibration_sample", sample)
                self.assertIn("加速度 g", self.ui.texts["sample"].get())
                if ending == "stop":
                    self.ui.stop_tool()
                    # A queued startup state must not re-enable stopped samples.
                    self.ui.handle("tool_state", dict(state="diagnostics"))
                elif ending == "idle":
                    self.ui.handle("tool_state", dict(state="idle"))
                else:
                    self.ui.handle(ending, None)
                self.assertIn("无实时数据", self.ui.texts["sample"].get())
                self.app.handle("calibration_sample", sample)
                self.assertNotIn("加速度 g", self.ui.texts["sample"].get())

    def test_tool_blocks_queued_raw_samples_and_gesture_start(self):
        self.ui.start_diagnostics()
        self.app.plot = Mock()
        self.app.acceleration_view = Mock()
        self.app.handle("samples", [[1] * 6])
        self.app.toggle_record()
        self.app.plot.append_samples.assert_not_called()
        self.assertIsNone(self.app.take)
        self.assert_state(self.app.record_button, "disabled")
        self.ui.stop_tool()
        self.app.worker.stop_tool.assert_called_once()
        self.assertTrue(self.ui.busy)  # cancellation is not complete yet
        self.app.handle("tool_state", dict(state="idle", message="cancelled"))
        self.assertFalse(self.ui.busy)

    def test_calibration_start_sync_failure_releases(self):
        self.app.worker.start_calibration.side_effect = RuntimeError("not connected")
        self.ui.start_calibration("full")
        self.assertFalse(self.ui.busy)

    def test_profile_read_and_reset_stop_recognition_and_lock(self):
        self.ui.show_profile()
        self.app.stop_live.assert_called_once()
        self.app.worker.show_calibration.assert_called_once()
        self.assertTrue(self.ui.busy)
        self.app.handle("calibration_profile", "profile summary")
        self.assertEqual(self.ui.texts["profile"].get(), "profile summary")
        self.app.handle("tool_state", dict(state="idle"))
        with patch("tkinter.messagebox.askyesno", return_value=False):
            self.ui.reset_profile()
        self.app.worker.reset_calibration.assert_not_called()
        with patch("tkinter.messagebox.askyesno", return_value=True):
            self.ui.reset_profile()
        self.app.worker.reset_calibration.assert_called_once()
        self.assertTrue(self.ui.busy)

    def test_inspection_is_offline_and_only_queue_updates_ui(self):
        self.app.connected = False
        verified = Verified()
        service = SimpleNamespace(inspect_bundle=Mock(return_value=verified))
        with patch.dict(sys.modules, {"hmm_gesture_studio.firmware": service}):
            self.ui.inspect_firmware()
            self.assertTrue(self.ui.inspection_pending)
            self.assertIsNone(self.ui.verified)
            service.inspect_bundle.assert_not_called()
            task = self.app.executor.submit.call_args.args[0]
            task()
            service.inspect_bundle.assert_called_once_with(Path("/tmp/signed.zip"), Path("/tmp/trusted.pem"))
        self.assertIsNone(self.ui.verified)
        self.app.drain_events()
        self.assertIs(self.ui.verified, verified)
        self.assertEqual(self.ui.texts["firmware"].get(), verified.summary)
        self.assertFalse(self.ui.inspection_pending)
        self.assertFalse(self.ui.available("install"))
        self.assertEqual(self.app.worker.mock_calls, [])

    def test_inspection_failure_and_stale_result_cannot_enable_install(self):
        self.ui.inspection_id = 10
        self.ui.inspection_pending = True
        self.ui.invalidate_inspection()
        self.app.handle("firmware_inspected", dict(token=10, verified=Verified()))
        self.assertIsNone(self.ui.verified)
        self.app.handle("firmware_inspection_failed", dict(token=11, error="test firmware rejected"))
        self.assertIn("test firmware rejected", self.ui.texts["firmware"].get())
        self.assertFalse(self.ui.available("install"))

    def test_inspection_worker_failure_is_delivered_on_main_thread(self):
        service = SimpleNamespace(inspect_bundle=Mock(side_effect=ValueError("bad signature")))
        with patch.dict(sys.modules, {"hmm_gesture_studio.firmware": service}):
            self.ui.inspect_firmware()
            self.app.executor.submit.call_args.args[0]()
        self.app.error.assert_not_called()
        self.app.drain_events()
        self.app.error.assert_called_once_with("bad signature")
        self.assertFalse(self.ui.inspection_pending)
        self.assertIsNone(self.ui.verified)

    def test_confirmation_cancel_never_installs(self):
        with patch("tkinter.messagebox.askyesno", return_value=False) as ask:
            self.ui.install_firmware()
        self.assertIn(self.ui.verified.summary, ask.call_args.args[1])
        self.assertIn("ring-1", ask.call_args.args[1])
        self.assertEqual(ask.call_args.kwargs["default"], "no")
        self.app.worker.install_firmware.assert_not_called()
        self.assertFalse(self.ui.firmware_locked)

    def test_confirmation_rechecks_connection_snapshot_and_exclusivity(self):
        for change in (lambda: setattr(self.ui, "connection_epoch", 99),
                       lambda: setattr(self.ui, "verified", Verified(bundle=b"different")),
                       lambda: setattr(self.app, "audio_state", "listening")):
            self.setUp()
            def confirm(*args, **kwargs):
                change()
                return True
            with patch("tkinter.messagebox.askyesno", side_effect=confirm):
                self.ui.install_firmware()
            self.app.worker.install_firmware.assert_not_called()
            self.assertFalse(self.ui.firmware_locked)

    def test_ota_locks_immediately_and_only_idle_unlocks(self):
        verified = self.ui.verified
        def submitted(snapshot):
            self.assertIs(snapshot, verified)
            self.assertTrue(self.ui.firmware_locked)
            self.assert_state(self.app.disconnect_button, "disabled")
            self.assert_state(self.app.audio_start_button, "disabled")
            self.assert_state(self.app.record_button, "disabled")
            self.assert_state(self.ui.buttons["install"], "disabled")
        self.app.worker.install_firmware.side_effect = submitted
        with patch("tkinter.messagebox.askyesno", return_value=True):
            self.ui.install_firmware()
        self.app.close()
        self.app.disconnect()
        self.app.confirm_discard.assert_not_called()
        self.app.worker.close.assert_not_called()
        self.app.worker.disconnect.assert_not_called()
        self.ui.start_recording()
        self.ui.start_diagnostics()
        self.app.device_request(self.app.worker.scan)
        self.app.worker.scan.assert_not_called()
        self.app.worker.start_remote_recording.assert_not_called()
        self.app.worker.start_diagnostics.assert_not_called()
        self.app.handle("firmware_progress", dict(sent=100, total=100))
        self.assertIn("仍需等待", self.ui.texts["firmware_status"].get())
        self.assertTrue(self.ui.firmware_locked)
        self.app.handle("firmware_complete", "verified after reboot")
        self.assertTrue(self.ui.firmware_locked)
        self.app.handle("error", "unrelated error")
        self.assertTrue(self.ui.firmware_locked)
        self.app.handle("tool_state", dict(state="idle", message="finished"))
        self.assertFalse(self.ui.firmware_locked)
        self.assert_state(self.app.disconnect_button, "normal")
        self.assert_state(self.ui.buttons["record"], "normal")

    def test_ota_sync_submission_failure_unlocks(self):
        self.app.worker.install_firmware.side_effect = RuntimeError("not connected")
        with patch("tkinter.messagebox.askyesno", return_value=True):
            self.ui.install_firmware()
        self.assertFalse(self.ui.firmware_locked)
        self.app.error.assert_called_once_with("not connected")
        self.assert_state(self.app.disconnect_button, "normal")

    def test_ota_async_failure_releases_on_idle_not_generic_error(self):
        self.ui.tool_state = "firmware"
        self.app.handle("error", "upgrade failed")
        self.assertTrue(self.ui.firmware_locked)
        self.assertEqual(self.ui.texts["firmware_status"].get(), "upgrade failed")
        self.app.handle("tool_state", dict(state="idle", message="failed safely"))
        self.assertFalse(self.ui.firmware_locked)

    def test_disconnect_clears_capabilities_without_releasing_ota(self):
        self.ui.tool_state = "firmware"
        self.ui.handle("disconnected", None)
        self.assertTrue(self.ui.firmware_locked)
        self.assertEqual(self.ui.capabilities, {})
        self.assertFalse(self.ui.available("record"))

    def test_close_rechecks_lock_after_modal_confirmation(self):
        def confirm():
            self.ui.tool_state = "firmware"
            return True
        self.app.confirm_discard.side_effect = confirm
        self.app.close()
        self.assertFalse(self.app.closing)
        self.app.worker.close.assert_not_called()
        self.app.executor.shutdown.assert_not_called()

    def test_training_lock_is_not_overridden_by_sdk_refresh(self):
        self.app.training = True
        self.ui.refresh()
        self.assert_state(self.app.record_button, "disabled")
        self.assert_state(self.app.live_button, "disabled")

    def test_closing_disables_every_action(self):
        self.app.closing = True
        self.ui.refresh()
        for widget in self.ui.buttons.values():
            self.assert_state(widget, "disabled")


if __name__ == "__main__":
    unittest.main()
