# SPDX-License-Identifier: MPL-2.0
"""Plugin recording submission and save-local provenance; no device or display."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID, uuid4

import openzilo as sdk
from hmm_gesture_studio import device, remote_audio
from hmm_gesture_studio.device import RingWorker
from hmm_gesture_studio.qt.tools_controller import ToolsController
from hmm_gesture_studio.sdk_controls import recording_options


class RecordingToolsTests(unittest.TestCase):
    def setUp(self):
        self.app = SimpleNamespace(
            closing=False, connected=True, take=None, audio_state="idle", live=True,
            worker=Mock(), error=Mock(), stop_live=Mock(), notify=Mock(),
            audio_directory="recordings", dialogs=Mock(), status="connected",
        )

        def set_audio_state(payload):
            self.app.audio_state = payload["state"]

        def request(operation):
            try:
                operation()
                return True
            except Exception as exc:
                self.app.error(str(exc))
                return False

        self.app.set_audio_state = set_audio_state
        self.app.request = request
        self.tools = ToolsController(self.app)
        self.tools.capabilities = dict(recording_control=True, live_audio=True,
                                       audio_files=True, imu=True, ota=True)
        self.session = str(uuid4())

    def start(self, **kwargs):
        return self.tools.start_recording(owner="plugin.one", session_id=self.session, **kwargs)

    def test_sdk_pre_reserves_audio_without_changing_live_or_ui_fields(self):
        original = dict(self.tools.fields)

        def submit(*args, **kwargs):
            self.assertEqual(self.app.audio_state, "starting")
            self.assertTrue(self.tools.remote_session)
            self.assertFalse(self.tools.available("record"))
            self.assertFalse(self.tools.available("quick"))
            self.assertTrue(self.app.live)

        self.app.worker.start_remote_recording.side_effect = submit
        self.assertTrue(self.start(max_seconds=1.24))
        expected = recording_options(dict(original, duration_s=1.24))
        self.app.worker.start_remote_recording.assert_called_once_with(
            expected, "recordings", live=False, playback=False,
            owner="plugin.one", session_id=self.session,
        )
        self.assertEqual(self.tools.fields, original)
        self.app.stop_live.assert_not_called()
        self.app.error.assert_not_called()

    def test_ui_start_and_read_config_preserve_live_and_old_worker_kwargs(self):
        self.tools.read_config()
        self.app.worker.read_recording_config.assert_called_once_with()
        self.app.audio_state = "idle"
        self.tools.playback = True
        self.assertTrue(self.tools.start_recording(live=True))
        self.app.worker.start_remote_recording.assert_called_once_with(
            recording_options(self.tools.fields), "recordings", live=True, playback=True,
        )
        self.app.stop_live.assert_not_called()
        self.assertTrue(self.app.live)

    def test_sdk_state_checks_are_identical_and_nonmodal(self):
        for target, attribute, value in (
            (self.app, "take", object()), (self.app, "audio_state", "listening"),
            (self.app, "audio_state", "saving"), (self.app, "connected", False),
            (self.app, "closing", True), (self.tools, "tool_state", "firmware"),
            (self.tools, "tool_state", "diagnostics"),
            (self.tools, "capabilities", {}),
        ):
            with self.subTest(attribute=attribute, value=value):
                previous = getattr(target, attribute)
                setattr(target, attribute, value)
                try:
                    with self.assertRaises(ValueError):
                        self.start()
                finally:
                    setattr(target, attribute, previous)
        self.app.worker.start_remote_recording.assert_not_called()
        self.app.error.assert_not_called()
        self.assertFalse(self.tools.remote_session)

    def test_invalid_sdk_options_are_nonmodal_and_do_not_mutate_fields(self):
        original = dict(self.tools.fields)
        for duration in (True, 0, -1, 61, 0.03, float("nan"), float("inf"), "bad"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.start(max_seconds=duration)
        with self.assertRaises(ValueError):
            self.start(live="yes")
        for owner, session in (("", self.session), (1, self.session), ("plugin", None)):
            with self.subTest(owner=owner, session=session), self.assertRaises(ValueError):
                self.tools.start_recording(owner=owner, session_id=session)
        self.tools.fields["channels"] = "invalid"
        with self.assertRaises(ValueError):
            self.start()
        self.tools.fields["channels"] = original["channels"]
        self.assertEqual(self.tools.fields, original)
        self.app.error.assert_not_called()
        self.app.worker.start_remote_recording.assert_not_called()
        self.assertEqual(self.app.audio_state, "idle")

    def test_sdk_submission_exception_rolls_back_and_propagates_without_dialog(self):
        self.app.worker.start_remote_recording.side_effect = RuntimeError("worker failed")
        with self.assertRaisesRegex(RuntimeError, "worker failed"):
            self.start()
        self.assertFalse(self.tools.remote_session)
        self.assertEqual(self.app.audio_state, "idle")
        self.app.error.assert_not_called()
        self.assertFalse(self.tools.start_recording())
        self.app.error.assert_called_once()

    def test_finish_returns_submission_result_and_idle_clears_remote_session(self):
        self.assertTrue(self.start())
        self.app.audio_state = "recording"
        self.assertTrue(self.tools.finish_recording(owner="plugin.one"))
        self.app.worker.finish_remote_recording.assert_called_once_with()
        self.assertEqual(self.app.audio_state, "stopping")
        self.assertTrue(self.tools.remote_session)
        self.tools.handle("error", "unrelated")
        self.assertTrue(self.tools.remote_session)
        self.assertFalse(self.tools.handle("audio_state", {"state": "idle"}))
        self.assertFalse(self.tools.remote_session)
        self.app.stop_live.assert_not_called()

    def test_finish_failure_restores_retry_state(self):
        self.app.audio_state = "recording"
        self.app.worker.finish_remote_recording.side_effect = RuntimeError("stop failed")
        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            self.tools.finish_recording(owner="plugin.one")
        self.assertEqual(self.app.audio_state, "recording")
        self.app.error.assert_not_called()
        self.assertFalse(self.tools.finish_recording())
        self.assertEqual(self.app.audio_state, "recording")
        self.app.audio_state = "saving"
        with self.assertRaises(ValueError):
            self.tools.finish_recording(owner="plugin.one")

    def test_maintenance_still_stops_live_and_audio_excludes_ota(self):
        self.tools.start_diagnostics()
        self.app.stop_live.assert_called_once_with()
        self.assertEqual(self.tools.tool_state, "diagnostics")
        with self.assertRaises(ValueError):
            self.start()
        self.tools.tool_state = "idle"
        self.tools.verified = SimpleNamespace(summary="verified firmware")
        self.app.audio_state = "recording"
        self.assertFalse(self.tools.available("install"))
        self.app.audio_state = "idle"
        self.app.dialogs.ask.return_value = True
        self.tools.install_firmware()
        self.assertEqual(self.app.stop_live.call_count, 2)
        self.app.worker.install_firmware.assert_called_once_with(self.tools.verified)
        self.assertTrue(self.tools.firmware_locked)


class RecordingProvenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.events = []
        self.worker = RingWorker(lambda event, payload: self.events.append((event, payload)))
        self.worker._audio_wake = asyncio.Event()
        self.ring = SimpleNamespace(is_connected=True, audio_observer=None,
                                    audio_extract_index=None)
        self.worker._ring = self.ring
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def request(self, owner=None, session_id=None, **kwargs):
        with patch.object(self.worker, "_control", side_effect=lambda fn, *args: fn(*args)):
            options = {"channels": 2}
            self.worker.start_remote_recording(options, self.directory.name,
                                                owner=owner, session_id=session_id, **kwargs)
            options["channels"] = 1
        operation = self.worker._audio_pending
        self.assertEqual(len(operation), 3)
        self.assertEqual(operation[1].options, {"channels": 2})
        return operation

    def saved(self):
        return [payload for event, payload in self.events if event == "audio_saved"]

    async def test_each_remote_save_captures_its_request_not_latest_or_device_session(self):
        callbacks = []

        async def record(*args, save, **kwargs):
            callbacks.append(save)

        first_id, second_id = str(uuid4()), str(uuid4())
        first = self.request("first.plugin", first_id)
        self.worker._audio_busy = False
        second = self.request("second.plugin", second_id, live=True)
        with patch.object(remote_audio, "record", side_effect=record):
            await self.worker._run_audio(self.ring, *first)
            await self.worker._run_audio(self.ring, *second)
        result = {"path": "complete.wav", "file_index": 7}
        with patch.object(device, "save_recording", return_value=result):
            await callbacks[1](b"second", 7, {"session_id": 82})
            await callbacks[0](b"first", 7, {"session_id": 81})
            # Ordinary historical download must not inherit the last owner.
            with patch.object(sdk, "download_audio_file", new_callable=AsyncMock,
                              return_value=(SimpleNamespace(record_time=1, data_size=7), b"history")):
                await self.worker._run_audio(self.ring, "download", 7, self.directory.name)
        second_saved, first_saved, history = self.saved()
        self.assertEqual((second_saved["owner"], second_saved["session_id"],
                          second_saved["device_session_id"]), ("second.plugin", second_id, 82))
        self.assertEqual((first_saved["owner"], first_saved["session_id"],
                          first_saved["device_session_id"]), ("first.plugin", first_id, 81))
        self.assertIs(history, result)
        self.assertNotIn("owner", history)
        self.assertNotIn("session_id", history)

    async def test_ui_raw_only_complete_save_has_no_plugin_owner(self):
        operation = self.request()
        session = operation[1].session_id
        UUID(session)

        async def record(*args, save, **kwargs):
            await save(b"complete raw data", 3, {"session_id": 999})

        with patch.object(remote_audio, "record", side_effect=record), \
             patch.object(sdk, "decode_audio_to_wav", side_effect=sdk.AudioDecodeError("bad codec")):
            await self.worker._run_audio(self.ring, *operation)
        self.assertEqual(len(self.saved()), 1)
        payload = self.saved()[0]
        self.assertIsNone(payload["owner"])
        self.assertEqual(payload["session_id"], session)
        self.assertEqual(payload["device_session_id"], 999)
        self.assertEqual(Path(payload["path"]).suffix, ".bin")
        self.assertEqual(Path(payload["path"]).read_bytes(), b"complete raw data")
        self.assertIsNotNone(payload["warning"])

    async def test_plain_save_identity_even_with_device_session_metadata(self):
        result = {"path": "historical.bin", "warning": "raw only"}
        with patch.object(device, "save_recording", return_value=result):
            await self.worker._save(b"complete", 2, self.directory.name, {"session_id": 17})
        self.assertIs(self.saved()[0], result)
        self.assertNotIn("owner", result)
        self.assertNotIn("session_id", result)

    async def test_incomplete_remote_audio_never_emits_saved_event(self):
        operation = self.request("plugin.one", str(uuid4()))
        with patch.object(remote_audio, "record", side_effect=sdk.ProtocolError("incomplete")), \
             patch.object(device, "save_recording") as save:
            await self.worker._run_audio(self.ring, *operation)
        save.assert_not_called()
        self.assertEqual(self.saved(), [])
        self.assertIsNone(self.worker._recording_stop)


if __name__ == "__main__":
    unittest.main()
