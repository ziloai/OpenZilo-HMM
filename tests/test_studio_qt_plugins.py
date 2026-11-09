# SPDX-License-Identifier: MPL-2.0
"""Real plugin controller/UI contracts; no network, credentials, child or device.

Only the execution boundary is faked. Manifests, installation snapshots, settings,
model fingerprints, consent rules and Qt widgets use their production code.
Worker completions always pass through StudioController's event queue.
"""
from __future__ import annotations

from concurrent.futures import Future
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit
except ImportError as exc:
    raise unittest.SkipTest(f"Qt GUI libraries unavailable: {exc}")

from hmmlearn.hmm import GaussianHMM
import numpy as np

from hmm_gesture import GestureBundle
from hmm_gesture.config import N_FEATURES
from hmm_gesture_studio.device_history import DeviceHistory
from hmm_gesture_studio.plugins.installer import _environment_signature, dependencies_ready, python_executable
from hmm_gesture_studio.plugins.models import GitHubSource
from hmm_gesture_studio.plugins.runtime import Runtime
from hmm_gesture_studio.qt import plugins_controller
from hmm_gesture_studio.qt.controller import StudioController
from hmm_gesture_studio.qt.i18n import language_manager, resolve
from hmm_gesture_studio.qt.plugins_controller import model_fingerprint, parse_github_repo
from hmm_gesture_studio.qt.theme import apply_theme
from hmm_gesture_studio.qt.window import MainWindow

PLUGIN = "test.fixture"
OTHER = "test.other"
GESTURE = "我的手势 snap"


def setUpModule():
    previous = language_manager.preference
    language_manager.set_preference("zh", persist=False)
    unittest.addModuleCleanup(language_manager.set_preference, previous, persist=False)


class MemoryVault:
    """Recognized native-backend interface, without importing the real keyring."""
    __module__ = "keyring.backends.macOS"
    priority = 1

    def __init__(self):
        self.values = {}

    def set_password(self, service, username, password):
        self.values[service, username] = password

    def get_password(self, service, username):
        return self.values.get((service, username))

    def delete_password(self, service, username):
        self.values.pop((service, username), None)


class ManualExecutor:
    """Explicit background work, without threads or the old preview scheduler."""
    def __init__(self):
        self.pending = []

    def submit(self, function, *args, **kwargs):
        future = Future()
        self.pending.append((future, function, args, kwargs))
        return future

    def run_next(self):
        future, function, args, kwargs = self.pending.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(function(*args, **kwargs))
            except BaseException as exc:
                future.set_exception(exc)
                raise

    def shutdown(self, wait=False, cancel_futures=False):
        if cancel_futures:
            for future, *_ in self.pending:
                future.cancel()
        self.pending.clear()


def bundle(offset=0):
    """Small valid fitted HMM; hashing must use inference data, not a fake id."""
    model = GaussianHMM(n_components=1, covariance_type="diag", init_params="")
    model.startprob_ = np.array([1.0])
    model.transmat_ = np.array([[1.0]])
    model.means_ = np.full((1, N_FEATURES), float(offset))
    model.covars_ = np.ones((1, N_FEATURES))
    return GestureBundle({GESTURE: model})


class PluginTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        apply_theme(cls.qt)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name).resolve()
        self.root = self.dir / "plugins"
        self.vault = MemoryVault()
        self.keep_patch(patch.dict(sys.modules, {"keyring": SimpleNamespace(get_keyring=lambda: self.vault)}))
        self.network = self.keep_patch(patch("hmm_gesture_studio.plugins.installer._download",
                                             side_effect=AssertionError("Network forbidden in Qt tests")))
        self.process = self.keep_patch(patch("subprocess.Popen",
                                             side_effect=AssertionError("Real subprocess forbidden in Qt tests")))
        self.dependencies = self.keep_patch(patch.object(plugins_controller, "install_dependencies"))
        self.dialogs = Mock()
        self.dialogs.ask.return_value = True
        self.dialogs.directory.return_value = ""
        self.dialogs.open_file.return_value = ""
        self.worker = Mock()
        self.c = self.make_controller()
        self.p = self.c.plugins
        self.runtime = self.p.runtime
        self.logs = []
        self.c.logged.connect(lambda when, level, text: self.logs.append((level, resolve(text))))

    def keep_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def make_controller(self):
        c = StudioController(self.dialogs, worker=self.worker,
                             history=DeviceHistory(self.dir / "devices.json"),
                             directory=self.dir / "data", audio_directory=self.dir / "audio",
                             plugin_root=self.root)
        c.executor.shutdown(wait=True)
        c.executor = ManualExecutor()
        c.plugins.executor.shutdown(wait=True)
        c.plugins.executor = ManualExecutor()
        c.plugins.runtime.close()
        runtime = Mock(spec=Runtime)
        running = set()
        runtime.running.side_effect = running.__contains__
        runtime.start.side_effect = lambda plugin_id, *_args: running.add(plugin_id)
        runtime.stop.side_effect = lambda plugin_id, *_args: running.discard(plugin_id)
        runtime.close.side_effect = running.clear
        runtime.submit.return_value = True
        runtime.has_request.return_value = True
        c.plugins.runtime = runtime
        self.addCleanup(c.executor.shutdown, cancel_futures=True)
        self.addCleanup(c.plugins.close)
        return c

    def source(self, plugin_id=PLUGIN):
        source = self.dir / ("source-" + plugin_id)
        source.mkdir(exist_ok=True)
        manifest = dict(
            id=plugin_id, name="Fixture plugin", version="1.0.0", author="Test author",
            description="A real manifest; its Python entry must never be imported by these tests.",
            entry="main.py", api_version=1, requirements=["example-package==1.2.3"],
            settings=[
                dict(key="target", title="Fixture target", kind="text", default="https://example.invalid", required=True),
                dict(key="token", title="Fixture credential", kind="secret", default=""),
                dict(key="timeout", title="Fixture timeout", kind="number", default=5, minimum=1, maximum=20),
                dict(key="advanced", title="Fixture advanced", kind="switch", default=False),
                dict(key="mode", title="Fixture mode", kind="choice", default="one",
                     options=[["one", "One"], ["two", "Two"]]),
                dict(key="extra", title="Fixture conditional", kind="text", default="",
                     visible_when={"advanced": True}, group="Fixture group", help="Fixture help"),
            ],
            actions=[
                dict(key="send", title="Fixture action", timeout=7, parameters=[
                    dict(key="payload", title="Fixture payload", kind="text", default="default payload", required=True),
                    dict(key="count", title="Fixture count", kind="number", default=1, minimum=1, maximum=4),
                ]),
                dict(key="other", title="Another action", timeout=9, parameters=[]),
            ],
            permissions=[dict(key="recording.control", title="Recording control", detail="Start/stop owned recordings.", sensitive=True)],
            events=["recording.completed"],
        )
        (source / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        (source / "main.py").write_text("raise AssertionError('Tests must not import plugin code')\n", encoding="utf-8")
        return source

    def install(self, plugin_id=PLUGIN):
        """Use the same two-phase controller flow as the local install UI."""
        self.p.close_plugin()
        self.dialogs.directory.return_value = str(self.source(plugin_id))
        self.p.begin_registration()
        self.finish_jobs()
        self.assertIsNotNone(self.p.candidate)
        self.p.confirm_registration()
        self.finish_jobs()
        plugin = self.p.plugin(plugin_id)
        self.assertIsNotNone(plugin)
        return plugin

    def finish_jobs(self):
        while self.p.executor.pending:
            self.p.executor.run_next()
            self.c.drain_events()

    def ready_environment(self, plugin):
        """Metadata fixture only; FakeRuntime never executes this placeholder."""
        executable = python_executable(plugin)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.write_text("not an executable\n", encoding="utf-8")
        marker = _environment_signature(plugin)
        (executable.parent.parent / ".studio-ready.json").write_text(json.dumps(marker), encoding="utf-8")
        self.assertTrue(dependencies_ready(plugin))

    def enable(self, plugin):
        self.ready_environment(plugin)
        self.p.set_service_running(True)
        self.p.set_enabled(plugin.id, True)
        self.assertTrue(self.runtime.running(plugin.id))

    def set_model(self, offset=0):
        model = bundle(offset)
        self.c.set_bundle(model, "fixture", "Fixture model")
        return model

    def arm(self):
        self.c.connected = self.c.imu_active = self.c.live = True
        self.c.actual_rate = 25.0
        self.c.tools.capabilities["imu"] = True
        self.c.recognizer = Mock()
        self.p.set_gesture_control(True)
        self.assertTrue(self.p.gesture_control)

    def prediction(self):
        return SimpleNamespace(name=GESTURE, confidence=None, score=-2.5,
                               start_sample=None, end_sample=None)

    def event(self, kind, plugin_id=PLUGIN, **payload):
        self.c.events.put(("plugin", dict(plugin_id=plugin_id, kind=kind, **payload)))
        self.c.drain_events()

    def rpc(self, method, plugin_id=PLUGIN, **params):
        self.runtime.reply.reset_mock()
        self.event("rpc", plugin_id, request_id="request-1", method=method, params=params)
        self.runtime.reply.assert_called_once()
        return self.runtime.reply.call_args.kwargs


class PluginRulesTests(PluginTestCase):
    def test_empty_discovery_and_start_do_not_enable_services_or_touch_network(self):
        self.assertEqual(self.p.root, self.root)
        self.assertEqual(self.p.plugins, [])
        self.p.start()
        self.assertFalse(self.p.service_running)
        self.assertFalse(self.p.gesture_control)
        self.assertFalse(self.p.autostart)
        self.assertEqual(self.p.binding_count, 0)
        self.runtime.start.assert_not_called()
        self.dependencies.assert_not_called()
        self.network.assert_not_called()
        self.process.assert_not_called()

    def test_autostart_requires_opt_in_persists_and_never_arms_gestures(self):
        plugin = self.install()
        self.enable(plugin)
        self.dialogs.ask.return_value = False
        self.p.set_autostart(True)
        self.assertFalse(self.p.autostart)
        self.assertFalse(self.p.store.load_preferences().get("autostart", False))
        self.dialogs.ask.return_value = True
        self.p.set_autostart(True)
        rebuilt = self.make_controller()
        self.assertTrue(rebuilt.plugins.autostart)
        self.assertFalse(rebuilt.plugins.service_running)
        rebuilt.plugins.runtime.start.assert_not_called()
        rebuilt.plugins.start()
        self.assertTrue(rebuilt.plugins.service_running)
        rebuilt.plugins.runtime.start.assert_called_once()
        self.assertFalse(rebuilt.plugins.gesture_control)
        self.dependencies.assert_not_called()
        self.network.assert_not_called()

    def test_install_reads_real_manifest_and_worker_results_wait_for_event_drain(self):
        source = self.source()
        self.dialogs.directory.return_value = str(source)
        self.p.begin_registration()
        self.assertIsNone(self.p.candidate)
        self.assertIsNotNone(self.p.busy_text)
        self.p.executor.run_next()
        self.assertIsNone(self.p.candidate)
        self.c.drain_events()
        candidate = self.p.candidate
        self.assertEqual(candidate.id, PLUGIN)
        self.assertEqual(candidate.requirements, ("example-package==1.2.3",))
        self.assertEqual(candidate.actions[0].parameters[0].key, "payload")
        self.p.confirm_registration()
        self.p.executor.run_next()
        self.assertIsNone(self.p.plugin(PLUGIN))
        self.c.drain_events()
        plugin = self.p.selected
        self.assertEqual(plugin.directory, self.root / PLUGIN / "code")
        self.assertTrue((plugin.directory / "plugin.json").is_file())
        self.assertTrue((self.root / PLUGIN / "settings.json").is_file())
        self.assertFalse(plugin.enabled)
        self.assertFalse(plugin.recording_events_enabled)
        self.assertEqual(plugin.recording_scope, "own")
        self.assertFalse(self.p.service_running or self.p.gesture_control)
        for slot in plugin.storage:
            self.assertTrue(slot.path.is_relative_to(self.root / PLUGIN))
        self.runtime.start.assert_not_called()
        self.dependencies.assert_not_called()
        self.process.assert_not_called()
        self.network.assert_not_called()

    def test_cancelled_install_and_modal_candidate_removal_do_not_commit(self):
        self.dialogs.directory.return_value = str(self.source())
        self.p.begin_registration()
        self.finish_jobs()
        self.dialogs.ask.return_value = False
        self.p.confirm_registration()
        self.assertFalse(self.p.executor.pending)
        self.assertFalse((self.root / PLUGIN / "code").exists())
        self.dialogs.ask.side_effect = lambda *_a, **_k: (self.p.cancel_registration(), True)[1]
        self.p.confirm_registration()
        self.assertIsNone(self.p.candidate)
        self.assertEqual(self.p.plugins, [])
        self.assertFalse(self.p.executor.pending)

    def test_duplicate_install_is_rejected_without_replacing_saved_configuration(self):
        plugin = self.install()
        self.p.set_setting("target", "saved target")
        self.p.save_settings()
        self.p.close_plugin()
        self.p.begin_registration()
        self.finish_jobs()
        self.assertIsNone(self.p.candidate)
        self.assertIs(self.p.plugin(PLUGIN), plugin)
        self.assertEqual(plugin.values["target"], "saved target")
        self.dialogs.error.assert_called_once()

    def test_dependencies_require_explicit_confirmation_and_recheck_selection(self):
        plugin = self.install()
        self.p.set_service_running(True)
        self.p.set_enabled(plugin.id, True)
        self.assertTrue(plugin.error)
        self.dependencies.assert_not_called()
        self.runtime.start.assert_not_called()
        self.dialogs.ask.return_value = False
        self.p.install_dependencies()
        self.assertFalse(self.p.executor.pending)
        self.dependencies.assert_not_called()
        self.dialogs.ask.side_effect = lambda *_a, **_k: (self.p.close_plugin(), True)[1]
        self.p.install_dependencies()
        self.assertFalse(self.p.executor.pending)
        self.dependencies.assert_not_called()
        self.p.open_plugin(plugin.id)
        self.dialogs.ask.side_effect = None
        self.dialogs.ask.return_value = True
        self.p.install_dependencies()
        self.dependencies.assert_not_called()
        self.assertIsNotNone(self.p.busy_text)
        self.finish_jobs()
        self.dependencies.assert_called_once()
        self.assertIs(self.dependencies.call_args.args[0], plugin)
        self.assertIsNone(self.p.busy_text)
        self.network.assert_not_called()

    def test_incomplete_environment_is_not_started_or_implicitly_repaired(self):
        plugin = self.install()
        executable = python_executable(plugin)
        executable.parent.mkdir(parents=True)
        executable.write_text("incomplete environment", encoding="utf-8")
        self.p.set_service_running(True)
        self.p.set_enabled(plugin.id, True)
        self.runtime.start.assert_not_called()
        self.dependencies.assert_not_called()
        self.assertTrue(plugin.error)

    def test_enable_trust_cancellation_and_modal_removal_recheck(self):
        plugin = self.install()
        self.ready_environment(plugin)
        self.p.set_service_running(True)
        self.dialogs.ask.return_value = False
        self.p.set_enabled(plugin.id, True)
        self.assertFalse(plugin.enabled)
        self.runtime.start.assert_not_called()
        warning = resolve(self.dialogs.ask.call_args.args[1])
        self.assertIn("账户权限", warning)
        self.assertIn("不是安全沙箱", warning)
        self.dialogs.ask.side_effect = lambda *_a, **_k: (self.p.plugins.remove(plugin), True)[1]
        self.p.set_enabled(plugin.id, True)
        self.assertFalse(plugin.enabled)
        self.runtime.start.assert_not_called()
        self.assertFalse(self.p.store.discover()[0].enabled)

    def test_enable_rechecks_busy_state_after_modal_dialog(self):
        plugin = self.install()
        self.ready_environment(plugin)
        self.p.set_service_running(True)
        self.dialogs.ask.side_effect = lambda *_a, **_k: (setattr(self.p, "busy_text", "other job"), True)[1]
        self.p.set_enabled(plugin.id, True)
        self.assertFalse(plugin.enabled)
        self.runtime.start.assert_not_called()

    def test_same_gesture_multiple_actions_require_confirmation_then_both_dispatch(self):
        plugin = self.install()
        self.set_model()
        self.p.add_binding()
        self.dialogs.ask.return_value = False
        self.p.add_binding()
        self.assertEqual(len(plugin.bindings), 1)
        self.dialogs.ask.return_value = True
        self.p.add_binding()
        self.p.set_binding_action(1, "other")
        self.assertEqual([b.gesture for b in plugin.bindings], [GESTURE, GESTURE])
        self.assertNotEqual(plugin.bindings[0].id, plugin.bindings[1].id)
        self.enable(plugin)
        self.arm()
        self.p.set_binding_cooldown(0, 0)
        self.p.set_binding_cooldown(1, 1000)
        with patch.object(plugins_controller.time, "monotonic", return_value=10):
            self.p.prediction(self.prediction())
            self.assertEqual([call.args[1] for call in self.runtime.submit.call_args_list], ["send", "other"])
            self.p.prediction(self.prediction())
            self.assertEqual([call.args[1] for call in self.runtime.submit.call_args_list], ["send", "other", "send"])

    def test_model_fingerprint_uses_content_and_switching_models_disarms(self):
        plugin = self.install()
        original = self.set_model()
        self.p.add_binding()
        original.metadata["path"] = "/different/location"
        self.assertEqual(model_fingerprint(original), model_fingerprint(bundle()))
        self.assertNotEqual(model_fingerprint(original), model_fingerprint(bundle(1)))
        self.enable(plugin)
        self.arm()
        self.set_model(1)
        self.assertFalse(self.p.gesture_control)
        self.assertFalse(self.p.binding_visible(plugin.bindings[0]))
        self.arm()
        self.p.prediction(self.prediction())
        self.runtime.submit.assert_not_called()
        self.assertEqual(len(plugin.bindings), 1)

    def test_cooldown_boundary_and_rejected_submission_does_not_consume_it(self):
        plugin = self.install()
        self.set_model()
        self.p.add_binding()
        self.p.set_binding_cooldown(0, 1000)
        self.enable(plugin)
        self.arm()
        with patch.object(plugins_controller.time, "monotonic", return_value=10) as clock:
            self.runtime.submit.return_value = False
            self.p.prediction(self.prediction())
            self.runtime.submit.return_value = True
            self.p.prediction(self.prediction())
            self.assertEqual(self.runtime.submit.call_count, 2)
            clock.return_value = 10.999
            self.p.prediction(self.prediction())
            self.assertEqual(self.runtime.submit.call_count, 2)
            clock.return_value = 11
            self.p.prediction(self.prediction())
            self.assertEqual(self.runtime.submit.call_count, 3)
        self.p.set_binding_enabled(0, False)
        self.p.prediction(self.prediction())
        self.assertEqual(self.runtime.submit.call_count, 3)

    def test_dispatch_rechecks_host_gates_even_if_gesture_control_was_armed(self):
        plugin = self.install()
        self.set_model()
        self.p.add_binding()
        self.enable(plugin)
        self.arm()
        for target, attribute, value in ((self.c, "stale", True), (self.c, "training", True),
                                         (self.c, "take", []), (self.c, "live", False),
                                         (self.c, "recognizer", None),
                                         (self.c.tools, "tool_state", "firmware")):
            with self.subTest(attribute=attribute):
                old = getattr(target, attribute)
                try:
                    setattr(target, attribute, value)
                    self.p.prediction(self.prediction())
                    self.runtime.submit.assert_not_called()
                finally:
                    setattr(target, attribute, old)
        self.p.set_gesture_control(False)
        self.p.prediction(self.prediction())
        self.runtime.submit.assert_not_called()
        self.runtime.cancel_pending.assert_called_with(event_type="gesture")

    def test_settings_drafts_never_leak_into_action_snapshots_or_unrelated_saves(self):
        plugin = self.install()
        self.set_model()
        self.p.add_binding()
        self.p.set_binding_parameter(0, "payload", "first action")
        self.p.set_setting("target", "saved target")
        self.p.set_setting("timeout", "12")
        self.p.save_settings()
        self.enable(plugin)
        self.p.set_setting("target", "unsaved draft")
        self.p.set_binding_cooldown(0, 1500)  # Persist binding, NOT draft settings.
        self.p.test_binding(0)
        args = self.runtime.submit.call_args.args
        self.assertEqual(args[1], "send")
        self.assertEqual(args[2]["type"], "manual")
        self.assertEqual(args[3], {"payload": "first action", "count": 1})
        self.assertEqual(args[4]["target"], "saved target")
        self.assertEqual(args[4]["timeout"], 12)
        self.assertEqual(args[5], 7)
        self.p.set_binding_parameter(0, "payload", "next action")
        self.p.set_setting("target", "next saved target")
        self.p.save_settings()
        self.assertEqual(args[3]["payload"], "first action")
        self.assertEqual(args[4]["target"], "saved target")
        self.p.test_binding(0)
        self.assertEqual(self.runtime.submit.call_args.args[4]["target"], "next saved target")

    def test_save_rebuild_preserves_settings_secrets_and_model_bound_parameters_but_not_services(self):
        plugin = self.install()
        self.set_model()
        self.p.add_binding()
        self.p.set_binding_parameter(0, "payload", "persistent parameter")
        self.p.set_setting("target", "persistent target")
        self.p.set_setting("token", "private-test-token")
        self.p.save_settings()
        self.enable(plugin)
        self.p.set_setting("target", "unsaved draft")
        self.p.set_binding_cooldown(0, 1234)
        disk = (self.root / PLUGIN / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn("private-test-token", disk)
        self.assertNotIn("unsaved draft", disk)
        rebuilt = self.make_controller()
        restored = rebuilt.plugins.plugin(PLUGIN)
        self.assertEqual(restored.values["target"], "persistent target")
        self.assertEqual(restored.values["token"], "private-test-token")
        self.assertEqual(restored.bindings, plugin.bindings)
        self.assertTrue(restored.enabled)
        rebuilt.plugins.start()
        self.assertFalse(rebuilt.plugins.service_running)
        self.assertFalse(rebuilt.plugins.gesture_control)
        self.assertFalse(rebuilt.plugins.autostart)
        self.assertFalse(restored.recording_events_enabled)
        rebuilt.plugins.runtime.start.assert_not_called()

    def test_number_validation_rejects_nonfinite_out_of_range_and_reset_is_only_a_draft(self):
        plugin = self.install()
        spec = next(s for s in plugin.settings if s.key == "timeout")
        for value in ("", "abc", "0", "21", "nan", "inf"):
            with self.subTest(value=value):
                self.p.set_setting("timeout", value)
                self.assertIsNotNone(self.p.setting_error(plugin, spec))
                self.assertFalse(self.p.can_save_settings)
                self.p.save_settings()
                self.assertEqual(self.p.store.load_settings(plugin)["timeout"], 5)
        self.p.set_setting("timeout", "10")
        self.p.save_settings()
        self.p.reset_settings()
        self.assertEqual(plugin.values["timeout"], 5)
        self.assertEqual(self.p.store.load_settings(plugin)["timeout"], 10)
        self.assertTrue(self.p.settings_dirty)

    def test_manual_action_requires_confirmation_and_rechecks_runtime_after_modal(self):
        plugin = self.install()
        self.enable(plugin)
        self.dialogs.ask.return_value = False
        self.p.test_action("send")
        self.runtime.submit.assert_not_called()
        self.dialogs.ask.side_effect = lambda *_a, **_k: (self.p.set_service_running(False), True)[1]
        self.p.test_action("send")
        self.runtime.submit.assert_not_called()

    def test_live_samples_dispatch_prediction_through_existing_controller_chain(self):
        plugin = self.install()
        self.set_model()
        self.p.add_binding()
        self.enable(plugin)
        self.arm()
        self.c.recognizer.feed.return_value = [self.prediction()]
        self.c.events.put(("samples", [[1, 2, 3, 4, 5, 6]] * 3))
        self.c.drain_events()
        self.c.recognizer.feed.assert_called_once()
        self.runtime.submit.assert_called_once()
        event = self.runtime.submit.call_args.args[2]
        self.assertEqual((event["type"], event["gesture"], event["model"]),
                         ("gesture", GESTURE, self.p.model_fingerprint))
        self.assertIsNone(event["confidence"])
        self.assertEqual(self.c.prediction[0], GESTURE)
        self.worker.connect.assert_not_called()

    def test_runtime_timeout_event_is_displayable_logged_and_redacts_secret(self):
        plugin = self.install()
        self.p.set_setting("token", "private-test-token")
        self.p.save_settings()
        self.event("error", action="send", event={"type": "gesture", "gesture": GESTURE},
                   phase="action", fatal=True, message="Action timed out private-test-token")
        self.assertEqual(plugin.events[0].gesture, GESTURE)
        self.assertEqual(plugin.events[0].action, "send")
        self.assertIn("timed out", plugin.events[0].error)
        self.assertIn("[REDACTED]", plugin.error)
        self.assertNotIn("private-test-token", repr(self.logs))
        self.assertTrue(any(level == "error" and "timed out" in text for level, text in self.logs))
        self.assertEqual(resolve(self.p.status(plugin)[0]), "运行失败")

    def test_clear_data_preserves_settings_and_uninstall_does_not_delete_source(self):
        plugin = self.install()
        self.p.set_setting("target", "keep me")
        self.p.save_settings()
        data = self.root / PLUGIN / "data" / "recording.txt"
        data.write_text("plugin data", encoding="utf-8")
        self.dialogs.ask.return_value = False
        self.p.clear_data()
        self.assertTrue(data.is_file())
        self.dialogs.ask.return_value = True
        self.p.clear_data()
        self.assertFalse(data.exists())
        self.assertEqual(self.p.store.load_settings(plugin)["target"], "keep me")
        self.p.remove_plugin()
        self.assertIsNone(self.p.selected)
        self.assertIsNone(self.p.plugin(PLUGIN))
        self.assertFalse((self.root / PLUGIN).exists())
        self.assertTrue((self.dir / ("source-" + PLUGIN) / "main.py").is_file())


class PluginRecordingTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.owner = self.install()
        self.other = self.install(OTHER)
        self.enable(self.owner)
        self.enable(self.other)
        self.p.open_plugin(PLUGIN)
        self.c.connected = True
        self.c.tools.capabilities = dict(recording_control=True, live_audio=True, audio_files=True, imu=True)

    def start_owned_recording(self):
        reply = self.rpc("recording.start", max_seconds=2, live=False)
        self.assertNotIn("error", reply)
        return reply["result"]

    def test_sdk_recording_owner_and_session_checked_before_stopping_worker(self):
        started = self.start_owned_recording()
        self.assertEqual(started["owner"], PLUGIN)
        self.assertEqual(self.c.audio_state, "starting")
        kwargs = self.worker.start_remote_recording.call_args.kwargs
        self.assertEqual(kwargs["owner"], PLUGIN)
        self.assertEqual(kwargs["session_id"], started["session_id"])
        self.c.handle("audio_state", dict(state="recording"))
        reply = self.rpc("recording.stop", OTHER, session_id=started["session_id"])
        self.assertIn("error", reply)
        self.worker.finish_remote_recording.assert_not_called()
        reply = self.rpc("recording.stop", session_id="wrong-session")
        self.assertIn("error", reply)
        self.worker.finish_remote_recording.assert_not_called()
        reply = self.rpc("recording.stop", session_id=started["session_id"])
        self.assertNotIn("error", reply)
        self.worker.finish_remote_recording.assert_called_once_with()
        self.assertEqual(self.c.audio_state, "stopping")

    def test_sdk_status_hides_other_owners_session_and_start_rejects_active_owner(self):
        started = self.start_owned_recording()
        status = self.rpc("recording.status", OTHER)["result"]
        self.assertFalse(status["owned"])
        self.assertIsNone(status["session_id"])
        self.assertEqual(self.rpc("recording.status")["result"]["session_id"], started["session_id"])
        self.assertIn("error", self.rpc("recording.start", OTHER))
        self.assertEqual(self.p.recording_session["owner"], PLUGIN)
        self.worker.start_remote_recording.assert_called_once()

    def test_stale_rpc_from_replaced_runtime_process_cannot_operate_device(self):
        self.runtime.has_request.return_value = False
        self.event("rpc", request_id="old-process-request", method="recording.start", params={})
        self.runtime.has_request.assert_called_once_with(PLUGIN, "old-process-request")
        self.runtime.reply.assert_not_called()
        self.worker.start_remote_recording.assert_not_called()
        self.assertIsNone(self.p.recording_session)

    def test_sdk_stop_remains_available_during_another_plugins_background_install(self):
        started = self.start_owned_recording()
        self.c.handle("audio_state", dict(state="recording"))
        self.p.busy_text = "Installing another plugin"
        reply = self.rpc("recording.stop", session_id=started["session_id"])
        self.assertNotIn("error", reply)
        self.worker.finish_remote_recording.assert_called_once_with()

    def test_sdk_recording_rejects_maintenance_without_device_calls_or_modal(self):
        self.dialogs.ask.reset_mock()
        self.c.tools.tool_state = "firmware"
        self.assertIn("error", self.rpc("recording.start", max_seconds=2))
        self.assertIsNone(self.p.recording_session)
        self.worker.start_remote_recording.assert_not_called()
        self.dialogs.ask.assert_not_called()
        self.dialogs.error.assert_not_called()

    def test_recording_subscription_defaults_off_and_requires_separate_consent(self):
        self.assertFalse(self.owner.recording_events_enabled)
        self.assertFalse(self.other.recording_events_enabled)
        self.dialogs.ask.return_value = False
        self.p.set_recording_events(True)
        self.assertFalse(self.owner.recording_events_enabled)
        self.dialogs.ask.return_value = True
        self.p.set_recording_events(True)
        self.assertTrue(self.owner.recording_events_enabled)
        self.assertEqual(self.owner.recording_scope, "own")
        self.dialogs.ask.return_value = False
        self.p.set_recording_scope("all")
        self.assertEqual(self.owner.recording_scope, "own")
        self.assertEqual(self.p.store.discover()[0].recording_scope, "own")

    def test_default_subscription_does_not_dispatch_even_a_matching_complete_save(self):
        recording = self.dir / "complete.bin"
        recording.write_bytes(b"complete raw audio")
        self.c.events.put(("audio_saved", dict(path=str(recording), owner=PLUGIN, session_id="saved-session")))
        self.c.drain_events()
        self.runtime.submit.assert_not_called()
        self.assertEqual(len(self.c.audio_rows), 1)

    def test_subscription_modal_rechecks_selected_plugin_and_all_scope_needs_consent(self):
        self.dialogs.ask.side_effect = lambda *_a, **_k: (self.p.open_plugin(OTHER), True)[1]
        self.p.set_recording_events(True)
        self.assertFalse(self.owner.recording_events_enabled)
        self.assertFalse(self.other.recording_events_enabled)
        self.dialogs.ask.side_effect = None
        self.p.open_plugin(PLUGIN)
        self.p.set_recording_events(True)
        self.p.set_recording_scope("all")
        recording = self.dir / "ui-recording.bin"
        recording.write_bytes(b"complete raw audio")
        self.c.events.put(("audio_saved", dict(path=str(recording), owner=None, session_id="ui-session")))
        self.c.drain_events()
        self.runtime.submit.assert_called_once()
        self.assertTrue(self.runtime.submit.call_args.args[2]["raw_only"])
        self.assertIsNone(self.runtime.submit.call_args.args[2]["owner"])

    def test_completion_uses_saved_payload_owner_not_current_recording_session(self):
        self.p.set_recording_events(True)
        self.start_owned_recording()
        recording = self.dir / "complete.wav"
        recording.write_bytes(b"complete file fixture")
        payload = dict(path=str(recording), raw_path=None, duration_s=2,
                       owner=OTHER, session_id="previous-session")
        self.c.events.put(("audio_saved", payload))
        self.c.drain_events()
        self.runtime.submit.assert_not_called()
        payload = dict(payload, owner=PLUGIN)
        self.c.events.put(("audio_saved", payload))
        self.c.drain_events()
        self.runtime.submit.assert_called_once()
        args = self.runtime.submit.call_args.args
        self.assertEqual(args[:2], (PLUGIN, "@event"))
        self.assertEqual(args[2]["type"], "recording.completed")
        self.assertEqual(args[2]["owner"], PLUGIN)
        self.assertEqual(args[2]["session_id"], "previous-session")
        self.assertEqual(args[2]["path"], str(recording))
        self.assertFalse(args[2]["raw_only"])
        self.assertFalse(self.other.recording_events_enabled)

    def test_idle_and_incomplete_error_never_create_completion_and_handler_cannot_recurse(self):
        self.p.set_recording_events(True)
        self.start_owned_recording()
        self.c.events.put(("error", "Recording transfer incomplete"))
        self.c.events.put(("audio_state", dict(state="idle")))
        self.c.drain_events()
        self.assertIsNone(self.p.recording_session)
        self.runtime.submit.assert_not_called()
        self.assertEqual(self.c.audio_rows, [])
        self.worker.start_remote_recording.reset_mock()
        self.event("rpc", request_id="no-recursion", method="recording.start", params={},
                   event={"type": "recording.completed"})
        self.assertIn("error", self.runtime.reply.call_args.kwargs)
        self.worker.start_remote_recording.assert_not_called()


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
    def candidate(self, repo):
        candidate = self.p.installer.prepare_local(self.source())
        candidate.github = GitHubSource(repo, "a" * 40, "")
        candidate.source = "https://github.com/" + repo
        return candidate

    def test_fetch_requires_user_action_and_staged_candidate_is_installed_with_fixed_source(self):
        with patch.object(self.p.installer, "prepare_github", side_effect=self.candidate) as fetch:
            self.p.open_github_form()
            self.p.set_github_input("owner/repo")
            fetch.assert_not_called()
            self.p.fetch_github()
            self.assertTrue(self.p.github_fetching)
            self.assertFalse(self.p.can_fetch_github)
            self.finish_jobs()
            fetch.assert_called_once_with("owner/repo")
            self.assertFalse(self.p.github_form_open)
            self.assertEqual(self.p.candidate.github.commit, "a" * 40)
            self.p.confirm_registration()
            self.finish_jobs()
        self.assertEqual(self.p.selected.github.repo, "owner/repo")
        self.assertFalse(self.p.selected.github.auto_update)
        self.assertFalse(self.p.selected.enabled)
        self.assertEqual(self.p.store.discover()[0].github.commit, "a" * 40)
        self.network.assert_not_called()

    def test_invalid_input_and_cancelled_fetch_never_install_late_candidate(self):
        with patch.object(self.p.installer, "prepare_github", side_effect=self.candidate) as fetch:
            self.p.open_github_form()
            self.p.set_github_input("https://evil.invalid/a/b")
            self.p.fetch_github()
            self.assertIsNotNone(self.p.github_error)
            self.assertFalse(self.p.executor.pending)
            fetch.assert_not_called()
            self.p.set_github_input("owner/repo")
            self.p.fetch_github()
            self.p.close_github_form()
            self.finish_jobs()
            self.assertIsNone(self.p.candidate)
            self.assertEqual(self.p.plugins, [])
            self.assertFalse(self.p.github_fetching)
            self.assertFalse(self.p.github_form_open)


class PluginPageTests(PluginTestCase):
    def setUp(self):
        super().setUp()
        self.plugin = self.install()
        self.set_model()
        self.p.add_binding()
        self.p.close_plugin()
        self.win = MainWindow(controller=self.c, merge_title_bar=False)
        self.win.resize(1280, 860)
        self.win.show()
        self.addCleanup(self.destroy_window)
        self.win.navigate("plugins")
        self.page = self.win.pages["plugins"]
        self.pump()

    def destroy_window(self):
        self.win._may_close = True
        self.win.close()
        self.win.deleteLater()
        self.pump()

    def pump(self):
        self.qt.processEvents()

    def open_detail(self):
        self.p.open_plugin(PLUGIN)
        self.pump()
        # Qt signal exceptions do not always propagate to unittest. Render directly
        # too, so a missing controller contract fails instead of printing to stderr.
        self.page.render_state()

    def test_list_detail_and_real_trust_warning_replace_preview(self):
        self.page.render_state()
        self.assertTrue(self.page.list_section.isVisible())
        self.assertFalse(self.page.detail_head.isVisible())
        self.assertFalse(self.page.service_running.isChecked())
        self.assertFalse(self.page.gesture_control.isChecked())
        self.page._cards[PLUGIN].manage.click()
        self.pump()
        self.assertFalse(self.page.list_section.isVisible())
        self.assertEqual(self.page.detail_title.text(), "Fixture plugin")
        texts = [item.text() for item in self.page.detail_head.findChildren(QLabel) if item.isVisible()]
        self.assertTrue(any("不是安全沙箱" in text and "账户权限" in text for text in texts))
        self.assertFalse(any("示例数据" in text or "仅预览" in text for text in texts))
        self.assertTrue(self.page.local_box.isVisible())
        self.assertFalse(self.page.github_box.isVisible())
        self.assertEqual(self.page.local_path.text(), str(self.plugin.directory))
        self.p.close_plugin()
        self.pump()
        self.assertTrue(self.page.list_section.isVisible())

    def test_manifest_setting_types_validation_conditional_rows_and_save_button(self):
        self.open_detail()
        controls = {spec.key: (holder, control, error)
                    for spec, holder, control, error in self.page._setting_controls}
        self.assertEqual([spec.kind for spec, *_ in self.page._setting_controls],
                         ["text", "secret", "number", "switch", "choice", "text"])
        self.assertEqual(controls["token"][1].echoMode(), QLineEdit.EchoMode.Password)
        self.assertFalse(controls["extra"][0].isVisible())
        self.p.set_setting("advanced", True)
        self.pump()
        self.assertTrue(controls["extra"][0].isVisible())
        _, timeout, error = controls["timeout"]
        timeout.setText("999")
        timeout.textEdited.emit("999")
        self.pump()
        self.assertEqual(self.plugin.values["timeout"], "999")
        self.assertTrue(error.isVisible())
        self.assertFalse(self.page.save.isEnabled())
        self.assertTrue(self.page.settings_dirty.isVisible())
        timeout.setText("10")
        timeout.textEdited.emit("10")
        self.pump()
        self.assertTrue(self.page.save.isEnabled())
        self.page.save.click()
        self.pump()
        self.assertEqual(self.p.store.load_settings(self.plugin)["timeout"], 10)
        self.assertFalse(self.page.settings_dirty.isVisible())

    def test_binding_parameter_form_edits_real_binding_not_plugin_settings(self):
        self.open_detail()
        parameters = self.page._binding_controls[0][-1]
        self.assertEqual([spec.key for spec, *_ in parameters], ["payload", "count"])
        _, _, payload, _ = parameters[0]
        payload.setText("from UI")
        payload.textEdited.emit("from UI")
        self.assertEqual(self.plugin.bindings[0].parameters["payload"], "default payload")
        payload.editingFinished.emit()
        self.pump()
        self.assertEqual(self.plugin.bindings[0].parameters["payload"], "from UI")
        self.assertNotIn("payload", self.plugin.values)
        self.assertIs(self.page._binding_controls[0][-1][0][2], payload)
        self.p.remove_binding(0)
        self.pump()
        self.assertEqual(self.page._binding_controls, [])

    def test_uncommitted_parameter_text_survives_unrelated_controller_notifications(self):
        self.open_detail()
        payload = self.page._binding_controls[0][-1][0][2]
        self.page.ensureWidgetVisible(payload)
        payload.setFocus()
        payload.setText("typing an action parameter")
        payload.setModified(True)
        payload.textEdited.emit(payload.text())
        self.c.notify()  # Runtime logs/ticks can render while a parameter is edited.
        self.assertEqual(payload.text(), "typing an action parameter")
        self.assertEqual(self.plugin.bindings[0].parameters["payload"], "default payload")
        payload.editingFinished.emit()
        self.assertEqual(self.plugin.bindings[0].parameters["payload"], "typing an action parameter")

    def test_deferred_parameter_signal_cannot_edit_a_different_selected_plugin(self):
        other = self.install(OTHER)
        self.p.add_binding()
        self.open_detail()
        old_payload = self.page._binding_controls[0][-1][0][2]
        old_payload.setText("old plugin unfinished input")
        self.p.open_plugin(OTHER)
        # deleteLater has not run yet: a focus-out signal can still arrive from
        # the old row after the selection changed during a nested event loop.
        old_payload.editingFinished.emit()
        self.assertEqual(other.bindings[0].parameters["payload"], "default payload")
        saved_other = next(plugin for plugin in self.p.store.discover() if plugin.id == OTHER)
        self.assertEqual(saved_other.bindings[0].parameters["payload"], "default payload")

    def test_dependency_button_cannot_install_without_confirmation(self):
        self.open_detail()
        self.dialogs.ask.return_value = False
        self.page.dependencies.click()
        self.assertFalse(self.p.executor.pending)
        self.dependencies.assert_not_called()
        self.dialogs.ask.return_value = True
        self.page.dependencies.click()
        self.pump()
        self.assertTrue(self.page.detail_busy.isVisible())
        self.assertFalse(self.page.dependencies.isEnabled())
        self.dependencies.assert_not_called()
        self.finish_jobs()
        self.pump()
        self.dependencies.assert_called_once()
        self.assertFalse(self.page.detail_busy.isVisible())
        self.network.assert_not_called()

    def test_timeout_event_shows_failure_reason_and_recent_event(self):
        self.open_detail()
        self.event("error", action="send", event={"type": "gesture", "gesture": GESTURE},
                   phase="action", fatal=True, message="Action timed out after 7 seconds")
        self.pump()
        self.assertEqual(self.page.detail_status.text(), "运行失败")
        self.assertTrue(self.page.detail_error.isVisible())
        self.assertIn("timed out", self.page.detail_error.text())
        self.assertEqual(self.page.events_table.item(0, 1).text(), GESTURE)
        self.assertIn("timed out", self.page.events_table.item(0, 3).text())
        self.assertIn("失败", self.page.events_table.item(0, 3).text())

    def test_detail_retranslates_host_controls_but_not_manifest_or_gesture_names(self):
        self.open_detail()
        self.event("finished", action="send", event={"type": "gesture", "gesture": GESTURE})
        language_manager.set_preference("en", persist=False)
        self.addCleanup(language_manager.set_preference, "zh", persist=False)
        self.pump()
        self.assertEqual(self.page.save.text(), "Save settings")
        self.assertEqual(self.page.dependencies.text(), "Install dependencies…")
        self.assertEqual(self.page.detail_title.text(), "Fixture plugin")
        self.assertEqual(self.page._binding_controls[0][1].currentText(), GESTURE)
        self.assertEqual(self.page._binding_controls[0][0].text(), "Matches the current model")
        self.assertEqual(self.page.recording_scope.itemText(0), "Recordings started by this plugin only")
        self.assertEqual(self.page.events_table.item(0, 1).text(), GESTURE)
        self.assertEqual(self.page.events_table.item(0, 3).text(), "Succeeded")
        self.assertEqual(self.page._setting_controls[0][2].accessibleName(), "Fixture target")

    def test_list_and_detail_fit_1100_pixels_in_both_languages(self):
        self.win.resize(1100, 720)
        self.addCleanup(language_manager.set_preference, "zh", persist=False)
        for language in ("zh", "en"):
            language_manager.set_preference(language, persist=False)
            for detail in (False, True):
                with self.subTest(language=language, detail=detail):
                    self.p.open_plugin(PLUGIN) if detail else self.p.close_plugin()
                    self.pump()
                    self.page.render_state()
                    self.pump()
                    self.assertLessEqual(self.page.widget().minimumSizeHint().width(), self.page.viewport().width())
                    self.assertEqual(self.page.horizontalScrollBar().maximum(), 0)


if __name__ == "__main__":
    unittest.main()
