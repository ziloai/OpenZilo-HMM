# SPDX-License-Identifier: MPL-2.0
"""End-to-end local install -> private Python -> gesture/RPC -> complete WAV.

Only the hardware is mocked. No network, key injection, microphone or keyring.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication
except ImportError as exc:
    raise unittest.SkipTest(str(exc))

from hmm_gesture_studio.device_history import DeviceHistory
from hmm_gesture_studio.plugins.models import Binding
from hmm_gesture_studio.qt.controller import StudioController


class PluginIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_real_recorder_process_gesture_start_stop_and_completed_file(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
                "hmm_gesture_studio.plugins.installer._download", side_effect=AssertionError("Network forbidden")), patch(
                "keyring.get_keyring", side_effect=RuntimeError("No system credentials in test")):
            root = Path(temporary)
            dialogs = Mock()
            dialogs.ask.return_value = True
            worker = Mock()
            c = StudioController(dialogs, worker=worker, history=DeviceHistory(root / "devices.json"),
                                 plugin_root=root / "plugins", audio_directory=root / "audio")
            p = c.plugins
            try:
                def wait(predicate, timeout=30):
                    deadline = time.monotonic() + timeout
                    while time.monotonic() < deadline:
                        c.poll()
                        self.qt.processEvents()
                        if predicate():
                            return
                        time.sleep(0.005)
                    self.fail(f"Timeout; dialogs={dialogs.error.call_args_list}; events={p.plugins[0].events if p.plugins else []}")

                source = Path(__file__).resolve().parents[1] / "examples/plugins/recorder"
                dialogs.directory.return_value = str(source)
                p.begin_registration()
                wait(lambda: p.busy_text is None)
                self.assertIsNotNone(p.candidate)
                p.confirm_registration()
                wait(lambda: p.busy_text is None)
                plugin = p.selected
                self.assertEqual(plugin.id, "example.recorder")
                self.assertFalse(p.service_running)
                self.assertFalse(plugin.enabled)
                p.install_dependencies()  # actual no-dependency venv, on install worker
                wait(lambda: p.busy_text is None)
                self.assertFalse(dialogs.error.called, dialogs.error.call_args_list)
                p.set_recording_events(True)
                p.set_service_running(True)
                p.set_enabled(plugin.id, True)
                wait(lambda: p.runtime.running(plugin.id))
                c.connected = c.imu_active = c.live = True
                c.recognizer = Mock()
                c.actual_rate = 25
                c.tools.capabilities = dict(imu=True, recording_control=True, live_audio=True)
                p.model_fingerprint = "fixture-inference-content"
                p.gestures = ("begin", "end")
                plugin.bindings = [
                    Binding("begin-binding", "begin", "start", p.model_fingerprint, {"max_seconds": 10, "live": False}),
                    Binding("end-binding", "end", "stop", p.model_fingerprint),
                ]
                self.assertTrue(p._persist(plugin))
                p.set_gesture_control(True)
                self.assertTrue(p.gesture_control)
                def gesture(name):
                    c._record_prediction(SimpleNamespace(name=name, confidence=None, score=-1,
                                                         start_sample=None, end_sample=None))
                gesture("begin")
                wait(lambda: any(e.action == "start" for e in plugin.events))
                self.assertEqual(plugin.events[0].error, "")
                worker.start_remote_recording.assert_called_once()
                session = dict(p.recording_session)
                self.assertEqual(worker.start_remote_recording.call_args.kwargs["session_id"], session["session_id"])
                self.assertTrue(c.live, "Audio startup must not disable gesture recognition")
                c.handle("audio_state", dict(state="recording", message="Recording"))
                gesture("end")
                wait(lambda: any(e.action == "stop" for e in plugin.events))
                self.assertEqual(plugin.events[0].error, "")
                worker.finish_remote_recording.assert_called_once()
                self.assertEqual(c.audio_state, "stopping")
                self.assertIsNotNone(p.recording_session, "Stop is not completion")
                audio = root / "complete.wav"
                with wave.open(str(audio), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(8000)
                    output.writeframes(b"\0\0" * 800)
                c.handle("audio_saved", dict(path=audio, duration_s=0.1, **session))
                c.handle("audio_state", dict(state="idle", message="Saved"))
                wait(lambda: any(e.action == "@event" for e in plugin.events))
                self.assertEqual(plugin.events[0].error, "")
                results = list((p.root / plugin.id / "data").glob("recording-*.json"))
                self.assertEqual(len(results), 1)
                result = json.loads(results[0].read_text())
                self.assertEqual(result["wave"]["frames"], 800)
                self.assertEqual(result["owner"], plugin.id)
                self.assertIsNone(p.recording_session)
                c.handle("disconnected", {})
                self.assertFalse(p.gesture_control)
                self.assertFalse(dialogs.error.called, dialogs.error.call_args_list)
            finally:
                p.close()
                p.executor.shutdown(wait=True, cancel_futures=True)
                c.executor.shutdown(wait=True, cancel_futures=True)
