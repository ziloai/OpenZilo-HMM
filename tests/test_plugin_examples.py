"""Offline tests: no real HTTP, pynput backend, audio device or host required."""
import builtins
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
import urllib.error
from uuid import uuid4
import wave


ROOT = Path(__file__).resolve().parents[1] / "examples" / "plugins"


def load(name):
    spec = importlib.util.spec_from_file_location("example_" + name, ROOT / name / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context(directory=None, **settings):
    return SimpleNamespace(settings=settings, data_dir=Path(directory or "."),
                           cache_dir=Path(directory or ".") / "cache", log=Mock(),
                           recording=Mock(), call=Mock())


class ManifestTests(unittest.TestCase):
    def test_install_units_and_lazy_backend(self):
        original = builtins.__import__

        def forbid_pynput(name, *args, **kwargs):
            if name == "pynput" or name.startswith("pynput."):
                raise AssertionError("pynput imported during discovery")
            return original(name, *args, **kwargs)

        for name in ("keyboard", "media", "webhook", "recorder"):
            with self.subTest(name=name):
                manifest = json.loads((ROOT / name / "plugin.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["id"], "example." + name)
                self.assertEqual(manifest["api_version"], 1)
                self.assertEqual(manifest["entry"], "main.py")
                self.assertTrue((ROOT / name / manifest["entry"]).is_file())
                self.assertEqual(manifest["requirements"],
                                 ["pynput==1.8.1"] if name in {"keyboard", "media"} else [])
                for action in manifest["actions"]:
                    self.assertGreater(action["timeout"], 0)
                    for field in action["parameters"]:
                        self.assertIn(field["kind"], {"switch", "choice", "text", "secret", "number"})
                with patch("builtins.__import__", side_effect=forbid_pynput):
                    module = load(name)
                self.assertTrue(callable(module.handle_action))


class KeyboardMediaTests(unittest.TestCase):
    def test_keyboard_hotkey_and_text(self):
        module = load("keyboard")
        controller = Mock()
        keys = SimpleNamespace(cmd="COMMAND", ctrl="CTRL", shift="SHIFT")
        ctx = context()
        with patch.object(module, "_keyboard", return_value=(controller, keys)), patch.object(module.sys, "platform", "darwin"):
            module.handle_action(ctx, "hotkey", {"type": "gesture", "confidence": None}, {"keys": "primary+shift+s"})
            self.assertEqual([c.args[0] for c in controller.press.call_args_list], ["COMMAND", "SHIFT", "s"])
            self.assertEqual([c.args[0] for c in controller.release.call_args_list], ["s", "SHIFT", "COMMAND"])
            with self.assertRaises(ValueError):
                module.handle_action(ctx, "type_text", {}, {"text": "hello"})
            ctx.settings["allow_text"] = True
            module.handle_action(ctx, "type_text", {"type": "manual"}, {"text": "hello"})
            controller.type.assert_called_once_with("hello")

    def test_keyboard_releases_on_error(self):
        module = load("keyboard")
        controller = Mock()
        controller.press.side_effect = [None, RuntimeError("backend failed")]
        with patch.object(module, "_keyboard", return_value=(controller, SimpleNamespace(ctrl="CTRL"))):
            with self.assertRaises(RuntimeError):
                module.handle_action(context(), "hotkey", {}, {"keys": "ctrl+c"})
        self.assertEqual([c.args[0] for c in controller.release.call_args_list], ["c", "CTRL"])

    def test_linux_rejected_without_backend_import(self):
        for name in ("keyboard", "media"):
            module = load(name)
            with patch.object(module.sys, "platform", "linux"), self.assertRaisesRegex(RuntimeError, "Linux"):
                module._keyboard()

    def test_media_parameters(self):
        module = load("media")
        controller = Mock()
        with patch.object(module, "_keyboard", return_value=(controller, SimpleNamespace(media_volume_up="UP"))), patch.object(module.time, "sleep") as sleep:
            result = module.handle_action(context(repeat_interval=0.2), "send", {}, {"key": "volume_up", "repeat": 2})
            self.assertEqual(result["sent"], 2)
            self.assertEqual(controller.press.call_count, 2)
            self.assertEqual(controller.release.call_count, 2)
            sleep.assert_called_once_with(0.2)
            for value in (True, 0, 1.5, 6, float("nan")):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    module.handle_action(context(), "send", {}, {"repeat": value})


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.module = load("webhook")
        self.ctx = context(url="https://hooks.example.test/receive", confirmed_target=True,
                           token="test-secret-token", timeout_seconds=3)

    def test_request_uses_action_payload(self):
        response = MagicMock()
        response.__enter__.return_value.status = 204
        opener = Mock()
        opener.open.return_value = response
        with patch.object(self.module.urllib.request, "build_opener", return_value=opener) as build:
            result = self.module.handle_action(self.ctx, "post", {"gesture": "private", "confidence": None},
                                             {"payload": '{"message":"你好","count":2}'})
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, "https://hooks.example.test/receive")
        self.assertEqual(json.loads(request.data), {"message": "你好", "count": 2})
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret-token")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 3.0})
        self.assertEqual(result, {"status": 204})
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], self.module.NoRedirect)
        response.__enter__.return_value.read.assert_not_called()
        self.assertNotIn("test-secret-token", str(self.ctx.log.call_args_list))

    def test_redirects_are_not_followed(self):
        handler = self.module.NoRedirect()
        for code in (301, 302, 303, 307, 308):
            self.assertIsNone(handler.redirect_request(None, None, code, "redirect", {}, "https://elsewhere.test"))
        opener = self.module.urllib.request.build_opener(handler)
        redirects = [h for h in opener.handlers if isinstance(h, self.module.urllib.request.HTTPRedirectHandler)]
        self.assertEqual(redirects, [handler])

    def test_errors_are_sanitized_and_not_retried(self):
        opener = Mock()
        errors = [TimeoutError("test-secret-token"),
                  urllib.error.HTTPError("https://secret.test/test-secret-token", 302, "test-secret-token", {}, None)]
        with patch.object(self.module.urllib.request, "build_opener", return_value=opener):
            for error in errors:
                opener.reset_mock()
                opener.open.side_effect = error
                with self.assertRaises(RuntimeError) as raised:
                    self.module.handle_action(self.ctx, "post", {}, {"payload": "{}"})
                self.assertNotIn("test-secret-token", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)
                opener.open.assert_called_once()
        self.ctx.log.assert_not_called()

    def test_rejects_invalid_inputs_before_network(self):
        settings = [
            {"url": "http://example.test"}, {"url": "https://user:secret@example.test"},
            {"url": "https://example.test/#secret"}, {"url": "https://example.test:99999"},
            {"url": "https://example.test/\nheader"}, {"confirmed_target": False},
            {"token": "secret\r\nHeader: value"}, {"timeout_seconds": float("nan")},
        ]
        with patch.object(self.module.urllib.request, "build_opener") as build:
            for change in settings:
                with self.subTest(change=change), self.assertRaises(ValueError):
                    self.module.handle_action(context(**(self.ctx.settings | change)), "post", {}, {"payload": "{}"})
            for payload in ("[]", "not JSON", '{"n":NaN}', '{"n":Infinity}', "x" * 65537):
                with self.subTest(payload=payload[:20]), self.assertRaises(ValueError):
                    self.module.handle_action(self.ctx, "post", {}, {"payload": payload})
            build.assert_not_called()


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.module = load("recorder")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ctx = context(self.temporary.name, analyze_completed=True)
        self.audio = Path(self.temporary.name) / "complete.wav"
        with wave.open(str(self.audio), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b"\x00\x00" * 800)
        self.event = {"type": "recording.completed", "session_id": "../../session",
                      "owner": "example.recorder", "path": str(self.audio),
                      "raw_path": None, "duration_s": 0.1}

    def test_start_stop_status_and_persistent_session(self):
        self.ctx.recording.start.return_value = {"session_id": self.event["session_id"], "state": "starting"}
        self.ctx.recording.stop.return_value = {"session_id": self.event["session_id"], "state": "stopping"}
        self.module.handle_action(self.ctx, "start", {"type": "manual"}, {"max_seconds": 10, "live": False})
        self.ctx.recording.start.assert_called_once_with(max_seconds=10, live=False)
        restarted = load("recorder")
        restarted.handle_action(self.ctx, "stop", {}, {})
        self.ctx.recording.stop.assert_called_once_with(self.event["session_id"])
        self.assertTrue((self.ctx.data_dir / "session.json").exists())
        self.assertFalse(list(self.ctx.data_dir.glob("recording-*.json")))
        restarted.handle_action(self.ctx, "status", {}, {})
        self.ctx.recording.status.assert_called_once_with()
        self.module.handle_event(self.ctx, self.event)
        self.assertFalse((self.ctx.data_dir / "session.json").exists())

    def test_local_wave_metadata_and_hash_without_rpc(self):
        original = self.audio.read_bytes()
        result = self.module.handle_event(self.ctx, self.event)
        path = Path(result["result_path"])
        self.assertEqual(path.parent, self.ctx.data_dir)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["wave"], {"channels": 1, "sample_width_bytes": 2, "sample_rate_hz": 8000,
                                        "frames": 800, "compression": "NONE", "duration_s": 0.1})
        self.assertEqual(data["sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.audio.read_bytes(), original)
        self.assertEqual(self.ctx.recording.mock_calls, [])
        self.ctx.call.assert_not_called()

    def test_only_complete_events_and_no_raw_fallback(self):
        self.assertEqual(self.module.handle_event(self.ctx, self.event | {"type": "recording.stopped"}), {"ignored": True})
        self.assertFalse(list(self.ctx.data_dir.glob("recording-*.json")))
        with self.assertRaises(ValueError):
            self.module.handle_event(self.ctx, self.event | {"path": None, "raw_path": str(self.audio)})
        self.ctx.settings["analyze_completed"] = False
        self.assertEqual(self.module.handle_event(self.ctx, self.event), {"ignored": True})

    def test_real_manifest_and_sdk_recording_contract(self):
        studio_source = str(ROOT.parents[1] / "studio" / "src")
        with patch.object(sys, "path", [studio_source, *sys.path]):
            from hmm_gesture_studio.plugins.manifest import load_manifest, validate_values
            from hmm_gesture_studio.plugins.sdk import Context
        plugin = load_manifest(ROOT / "recorder")
        permissions = {permission.key for permission in plugin.permissions}
        self.assertIn("recording.control", permissions)
        self.assertNotIn("recording", permissions)
        start = next(action for action in plugin.actions if action.key == "start")
        seconds = next(field for field in start.parameters if field.key == "max_seconds")
        self.assertEqual((seconds.minimum, seconds.maximum, seconds.default), (1, 60, 60))
        self.assertIn("recording.completed", plugin.subscriptions)
        host_session = {"session_id": str(uuid4()), "owner": plugin.id}
        rpc = Mock(side_effect=[host_session | {"state": "starting"},
                                host_session | {"state": "stopping"},
                                host_session | {"state": "stopping"}])
        ctx = Context(plugin.values, self.ctx.data_dir, self.ctx.cache_dir,
                      {"type": "manual"}, rpc, Mock())
        started = self.module.handle_action(ctx, "start", {"type": "manual"}, validate_values(start.parameters, {}))
        self.assertEqual(started, host_session | {"state": "starting"})
        rpc.assert_called_with("recording.start", {"max_seconds": 60, "live": False})
        self.assertEqual(json.loads((ctx.data_dir / "session.json").read_text())["session_id"], host_session["session_id"])
        stopped = self.module.handle_action(ctx, "stop", {"type": "manual"}, {})
        self.assertEqual(stopped, host_session | {"state": "stopping"})
        rpc.assert_called_with("recording.stop", {"session_id": host_session["session_id"]})
        status = self.module.handle_action(ctx, "status", {"type": "manual"}, {})
        self.assertEqual(status["state"], "stopping")
        rpc.assert_called_with("recording.status", {})
        self.assertTrue((ctx.data_dir / "session.json").exists())
        self.assertFalse(list(ctx.data_dir.glob("recording-*.json")))

    def test_start_duration_is_integer_seconds_within_hardware_range(self):
        for seconds in (True, 0, 0.02, 0.98, 1.01, 1.02, 1.5, 60.02, 61, 300,
                        float("inf"), float("nan"), "60"):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.module.handle_action(self.ctx, "start", {}, {"max_seconds": seconds})
        self.ctx.recording.start.assert_not_called()
        self.ctx.recording.start.return_value = {"session_id": str(uuid4()), "state": "starting"}
        for seconds in (1, 10.0, 60):
            with self.subTest(seconds=seconds):
                self.module.handle_action(self.ctx, "start", {}, {"max_seconds": seconds})
                self.ctx.recording.start.assert_called_with(max_seconds=seconds, live=False)

    def test_all_scope_preserves_actual_owner_and_only_clears_matching_own_session(self):
        session_path = self.ctx.data_dir / "session.json"
        session_path.write_text(json.dumps({"session_id": self.event["session_id"]}))
        # Scope authorization belongs to the host; foreign owners and unmatched
        # sessions still produce results without clearing our outstanding ID.
        for change in ({"owner": "another.plugin"}, {"owner": None}, {"session_id": "different"}):
            event = self.event | change
            with self.subTest(change=change):
                result = self.module.handle_event(self.ctx, event)
                data = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
                self.assertEqual(data["owner"], event["owner"])
                self.assertEqual(data["session_id"], event["session_id"])
                self.assertTrue(session_path.exists())
        self.module.handle_event(self.ctx, self.event)
        self.assertFalse(session_path.exists())

    def test_manual_wav_without_session_uses_event_id_or_uuid(self):
        manual = self.event | {"manual": True, "owner": None, "session_id": None}
        # The manual handler must also create data_dir without a prior start.
        self.ctx.data_dir = self.ctx.data_dir / "new-plugin-data"
        results = []
        for extra in ({"id": "../../external-id"}, {}, {}):
            result = self.module.handle_event(self.ctx, manual | extra)
            path = Path(result["result_path"])
            results.append(path)
            self.assertEqual(path.parent, self.ctx.data_dir)
            self.assertRegex(path.name, r"^recording-[0-9a-f]{64}\.json$")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNone(data["owner"])
            self.assertIsNone(data["session_id"])
            self.assertEqual(data["wave"]["frames"], 800)
            self.assertEqual(data["sha256"], hashlib.sha256(self.audio.read_bytes()).hexdigest())
        self.assertEqual(results[0].name, "recording-" + hashlib.sha256(b"../../external-id").hexdigest() + ".json")
        self.assertEqual(len(set(results)), 3)
        self.assertFalse((self.ctx.data_dir / "session.json").exists())
        self.assertEqual(self.ctx.recording.mock_calls, [])

    def test_raw_only_is_explicit_skip_without_reading_wave(self):
        session_path = self.ctx.data_dir / "session.json"
        session_path.write_text(json.dumps({"session_id": self.event["session_id"]}))
        raw_event = self.event | {"raw_only": True, "path": "/not-opened.raw", "raw_path": "/not-opened.raw"}
        with patch.object(self.module.wave, "open") as open_wave:
            result = self.module.handle_event(self.ctx, raw_event)
            open_wave.assert_not_called()
        self.assertEqual(result, {"ignored": True, "reason": "raw_only"})
        self.assertIn("尚未解码", self.ctx.log.call_args.args[0])
        self.assertFalse(session_path.exists())
        self.assertFalse(list(self.ctx.data_dir.glob("recording-*.json")))

    def test_truncated_audio_is_not_success(self):
        self.audio.write_bytes(self.audio.read_bytes()[:-20])
        with self.assertRaises(ValueError):
            self.module.handle_event(self.ctx, self.event)
        self.assertFalse(list(self.ctx.data_dir.glob("recording-*.json")))

    def test_completed_event_cannot_restart_recording(self):
        with self.assertRaises(ValueError):
            self.module.handle_action(self.ctx, "start", self.event, {})
        self.ctx.recording.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
