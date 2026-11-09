# SPDX-License-Identifier: MPL-2.0
"""Real subprocess integration tests; standard library only, no device/network."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest

from hmm_gesture_studio.plugins.runtime import Runtime


class PluginRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.events = queue.Queue()
        self.runtime = Runtime(self.events, startup_timeout=3)
        self.addCleanup(self.runtime.close)
        self.seen = []

    def drain(self):
        self.runtime.poll()
        while True:
            try:
                channel, payload = self.events.get_nowait()
            except queue.Empty:
                break
            self.assertEqual(channel, "plugin")
            self.assertIn("plugin_id", payload)
            self.assertIn(payload["kind"], {"started", "finished", "error", "log", "rpc", "stopped"})
            self.seen.append(payload)

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain()
            if predicate():
                return
            time.sleep(0.005)
        self.fail(f"Timed out waiting; events: {self.seen!r}")

    def kinds(self, kind, plugin_id="test"):
        return [e for e in self.seen if e["kind"] == kind and e["plugin_id"] == plugin_id]

    def start(self, source, *, plugin_id="test", wait=True, entry="main.py"):
        code = self.root / plugin_id / "code"
        code.mkdir(parents=True, exist_ok=True)
        (code / "main.py").write_text(textwrap.dedent(source), encoding="utf-8")
        self.runtime.start(plugin_id, Path(sys.executable), code, entry,
                           self.root / plugin_id / "data", self.root / plugin_id / "cache")
        if wait:
            self.wait(lambda: bool(self.kinds("started", plugin_id)))

    def submit(self, action="run", event=None, parameters=None, settings=None, timeout=30):
        return self.runtime.submit("test", action, event or {}, parameters or {}, settings or {}, timeout)

    def test_settings_event_and_parameters_are_submission_snapshots(self):
        self.start('''
            from zilo_plugin import Context
            def handle_action(context: Context, action, event, parameters):
                if action == 'block':
                    return context.recording.status()
                return {'settings': context.settings, 'event': event, 'params': parameters,
                        'data': str(context.data_dir), 'cache': str(context.cache_dir)}
        ''')
        self.assertTrue(self.submit("block"))
        self.wait(lambda: bool(self.kinds("rpc")))
        settings, event, params = {"nested": [1]}, {"type": "gesture", "values": [2]}, {"list": [3]}
        self.assertTrue(self.submit("snapshot", event, params, settings))
        settings["nested"].append(9)
        event["values"].append(9)
        params["list"].append(9)
        self.runtime.reply("test", self.kinds("rpc")[0]["request_id"], result={"active": False})
        self.wait(lambda: len(self.kinds("finished")) == 2)
        first, second = self.kinds("finished")
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(second["action"], "snapshot")
        self.assertEqual(second["event"], {"type": "gesture", "values": [2]})
        result = second["result"]
        self.assertEqual(result["settings"], {"nested": [1]})
        self.assertEqual(result["params"], {"list": [3]})
        self.assertTrue(Path(result["data"]).is_dir())
        self.assertTrue(Path(result["cache"]).is_dir())
        self.assertFalse(self.runtime.busy("test"))

    def test_print_native_output_and_sdk_import_do_not_corrupt_rpc(self):
        self.start('''
            from zilo_plugin import Context, RPCError
            import os, sys
            print('import print')
            def handle_action(context, action, event, parameters):
                print('before rpc')
                print('stderr print', file=sys.stderr)
                os.write(1, b'native stdout\\n')
                os.write(2, b'native stderr\\n')
                context.log('explicit log', 'warning')
                started = context.recording.start()
                stopped = context.recording.stop(started['session_id'])
                status = context.recording.status()
                try:
                    context.call('custom.fail', value=1)
                except RPCError as exc:
                    return [stopped, status, exc.error]
        ''')
        self.assertTrue(self.submit())
        replies = [({"session_id": "s1"}, None), ({"stopped": True}, None),
                   ({"active": False}, None), (None, {"message": "denied"})]
        for i, (result, error) in enumerate(replies):
            self.wait(lambda: len(self.kinds("rpc")) > i)
            request = self.kinds("rpc")[i]
            self.assertTrue(request["task_id"])
            self.assertEqual(request["action"], "run")
            self.assertEqual(request["event"], {})
            self.runtime.reply("test", request["request_id"], result=result, error=error)
        self.wait(lambda: bool(self.kinds("finished")))
        requests = self.kinds("rpc")
        self.assertEqual([r["method"] for r in requests],
                         ["recording.start", "recording.stop", "recording.status", "custom.fail"])
        self.assertEqual(requests[0]["params"], {"max_seconds": 60, "live": False})
        self.assertEqual(requests[1]["params"], {"session_id": "s1"})
        self.assertEqual(self.kinds("finished")[0]["result"],
                         [{"stopped": True}, {"active": False}, {"message": "denied"}])
        self.assertFalse(self.kinds("error"))
        logs = " ".join(e["message"] for e in self.kinds("log"))
        self.assertIn("before rpc", logs)
        self.assertIn("import print", logs)

    def test_failed_action_is_not_retried_and_next_action_runs(self):
        self.start('''
            count = 0
            def handle_action(context, action, event, parameters):
                global count
                count += 1
                if action == 'fail':
                    raise ValueError('deliberate failure')
                return count
        ''')
        self.assertTrue(self.submit("fail"))
        self.assertTrue(self.submit("next"))
        self.wait(lambda: bool(self.kinds("finished")))
        self.assertEqual(len(self.kinds("error")), 1)
        self.assertFalse(self.kinds("error")[0]["fatal"])
        self.assertEqual(self.kinds("error")[0]["action"], "fail")
        self.assertEqual(self.kinds("finished")[0]["result"], 2)
        self.assertTrue(self.runtime.running("test"))

    def test_event_handler_cannot_start_recording_even_with_generic_call(self):
        self.start('''
            from zilo_plugin import RPCError
            def handle_event(context, event):
                errors = []
                for call in (context.recording.start, lambda: context.call('recording.start')):
                    try:
                        call()
                    except RPCError as exc:
                        errors.append(str(exc))
                return errors
        ''')
        self.assertTrue(self.submit("@event", {"type": "recording.completed"}))
        self.wait(lambda: bool(self.kinds("finished")))
        self.assertEqual(len(self.kinds("finished")[0]["result"]), 2)
        self.assertFalse(self.kinds("rpc"))

    def test_queue_has_twenty_waiting_slots_and_one_active(self):
        self.start('''
            def handle_action(context, action, event, parameters):
                if action == 'block':
                    context.call('wait')
                return action
        ''')
        self.assertTrue(self.submit("block"))
        self.wait(lambda: bool(self.kinds("rpc")))
        for i in range(20):
            self.assertTrue(self.submit(str(i)))
        self.assertFalse(self.submit("overflow"))
        self.drain()
        self.assertTrue(any("queue is full" in e["message"] for e in self.kinds("log")))
        self.runtime.reply("test", self.kinds("rpc")[0]["request_id"])
        self.wait(lambda: len(self.kinds("finished")) == 21)
        self.assertEqual([e["action"] for e in self.kinds("finished")], ["block"] + list(map(str, range(20))))

    def test_task_timeout_terminates_process_and_discards_waiting_tasks(self):
        self.start('''
            import time
            def handle_action(context, action, event, parameters):
                time.sleep(60)
        ''')
        self.assertTrue(self.submit(timeout=0.1))
        self.assertTrue(self.submit("never"))
        self.wait(lambda: bool(self.kinds("stopped")))
        self.assertEqual(self.kinds("error")[0]["phase"], "timeout")
        self.assertFalse(self.runtime.running("test"))
        self.assertFalse(self.runtime.busy("test"))
        self.assertFalse(self.submit())
        self.assertFalse(self.kinds("finished"))
        self.runtime.reply("test", "late", result={})
        self.runtime.poll()
        self.assertEqual(len(self.kinds("started")), 1)

    def test_late_poll_does_not_accept_overdue_finished_frame(self):
        self.start('''
            import time
            def handle_action(context, action, event, parameters):
                time.sleep(0.15)
                return 'too late'
        ''')
        self.assertTrue(self.submit(timeout=0.05))
        time.sleep(0.3)
        self.wait(lambda: bool(self.kinds("stopped")))
        self.assertFalse(self.kinds("finished"))
        self.assertEqual(self.kinds("error")[0]["phase"], "timeout")

    def test_top_level_import_has_deadline(self):
        self.runtime.startup_timeout = 0.15
        self.start('''
            while True:
                pass
        ''', wait=False)
        self.assertTrue(self.submit("queued"))
        self.wait(lambda: bool(self.kinds("stopped")))
        self.assertFalse(self.kinds("started"))
        self.assertIn("startup timed out", self.kinds("error")[0]["message"])

    def test_import_failure_is_reported_once_without_restart(self):
        self.start("raise RuntimeError('import failure')", wait=False)
        self.wait(lambda: bool(self.kinds("stopped")))
        self.assertEqual(len(self.kinds("error")), 1)
        self.assertIn("import failure", self.kinds("error")[0]["message"])
        self.assertFalse(self.runtime.running("test"))

    def test_process_exit_is_detected_and_pending_tasks_are_discarded(self):
        self.start('''
            import os
            def handle_action(context, action, event, parameters):
                os._exit(7)
        ''')
        self.assertTrue(self.submit())
        self.assertTrue(self.submit("not retried"))
        self.wait(lambda: bool(self.kinds("stopped")))
        self.assertIn("(7)", self.kinds("error")[0]["message"])
        self.assertEqual(len(self.kinds("error")), 1)
        self.assertFalse(self.kinds("finished"))

    def test_results_and_submissions_must_be_bounded_json(self):
        self.start('''
            def handle_action(context, action, event, parameters):
                if action == 'invalid':
                    return {1, 2}
                if action == 'huge':
                    return 'x' * 100000
                if action == 'nan':
                    return float('nan')
                return 'ok'
        ''')
        self.assertFalse(self.submit(settings={"x": object()}))
        self.assertFalse(self.submit(settings={"x": "x" * 100000}))
        self.assertFalse(self.submit(timeout=float("nan")))
        for action in ("invalid", "huge", "nan", "valid"):
            self.assertTrue(self.submit(action))
        self.wait(lambda: bool(self.kinds("finished")))
        self.assertEqual(len(self.kinds("error")), 3)
        self.assertEqual(self.kinds("finished")[0]["result"], "ok")

    def test_output_flood_is_bounded_and_does_not_block_completion(self):
        self.start('''
            import os
            def handle_action(context, action, event, parameters):
                for i in range(2000):
                    print('x' * 5000)
                os.write(2, b'y' * 200000)
                return True
        ''')
        self.assertTrue(self.submit())
        self.wait(lambda: bool(self.kinds("finished")))
        self.assertTrue(self.kinds("finished")[0]["result"])
        self.assertLessEqual(len(self.kinds("log")), 80)
        self.assertTrue(all(len(e["message"]) <= 4096 for e in self.kinds("log")))

    def test_worker_threads_do_not_publish_host_events_before_poll(self):
        self.start('''
            print('startup log')
            def handle_action(context, action, event, parameters):
                return None
        ''', wait=False)
        time.sleep(0.15)
        self.assertTrue(self.events.empty())
        self.wait(lambda: bool(self.kinds("started")))

    def test_module_and_explicit_callable_entries(self):
        self.start('''
            def custom(context, action, event, parameters):
                return 42
        ''', entry="main:custom")
        self.assertTrue(self.submit())
        self.wait(lambda: bool(self.kinds("finished")))
        self.assertEqual(self.kinds("finished")[0]["result"], 42)

    def test_close_is_nonblocking_and_reaps_process(self):
        self.start('''
            import time
            def handle_action(context, action, event, parameters):
                time.sleep(60)
        ''')
        pid = self.kinds("started")[0]["pid"]
        self.assertTrue(self.submit())
        start = time.monotonic()
        self.runtime.close()
        self.assertLess(time.monotonic() - start, 0.5)
        self.runtime.close()
        self.assertFalse(self.runtime.running("test"))
        if os.name == "posix":
            self.wait(lambda: not self.process_alive(pid))
        self.wait(lambda: not any(t.name.startswith("plugin-test-") for t in threading.enumerate()))
        self.assertEqual(len(self.kinds("stopped")), 1)

    @staticmethod
    def process_alive(pid):
        result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=2)
        status = result.stdout.strip()
        return bool(status) and not status.startswith("Z")

    @unittest.skipUnless(os.name == "posix", "POSIX process group cleanup")
    def test_close_kills_descendants_in_process_group(self):
        self.start('''
            import subprocess, sys
            def handle_action(context, action, event, parameters):
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
                return child.pid
        ''')
        self.assertTrue(self.submit())
        self.wait(lambda: bool(self.kinds("finished")))
        child_pid = self.kinds("finished")[0]["result"]
        self.assertTrue(self.process_alive(child_pid))
        self.runtime.close()
        self.wait(lambda: not self.process_alive(child_pid))

    def test_cancel_pending_filters_event_type_without_touching_inflight(self):
        self.start('''
            def handle_action(context, action, event, parameters):
                if action == 'block':
                    context.call('wait')
                return action
            def handle_event(context, event):
                return context.recording.status()
        ''')
        self.assertTrue(self.submit('block', {'type': 'gesture'}))
        self.wait(lambda: bool(self.kinds('rpc')))
        self.assertTrue(self.submit('cancel me', {'type': 'gesture'}))
        self.assertTrue(self.submit('@event', {'type': 'recording.completed'}))
        self.assertTrue(self.submit('manual'))
        self.assertEqual(self.runtime.cancel_pending('test', event_type='gesture'), 1)
        self.assertTrue(self.runtime.busy('test'))
        self.runtime.reply('test', self.kinds('rpc')[0]['request_id'])
        self.wait(lambda: len(self.kinds('rpc')) == 2)
        request = self.kinds('rpc')[1]
        self.assertEqual(request['action'], '@event')
        self.assertEqual(request['event']['type'], 'recording.completed')
        self.runtime.reply('test', request['request_id'], result={'active': False})
        self.wait(lambda: len(self.kinds('finished')) == 3)
        self.assertEqual([e['action'] for e in self.kinds('finished')], ['block', '@event', 'manual'])

    def test_before_ready_tasks_queue_and_global_cancel_does_not_cancel_startup(self):
        source = '''
            import time
            time.sleep(0.1)
            def handle_action(context, action, event, parameters):
                return action
        '''
        for plugin_id in ('test', 'other'):
            self.start(source, plugin_id=plugin_id, wait=False)
            self.assertTrue(self.runtime.running(plugin_id))
            self.assertFalse(self.runtime.busy(plugin_id))
            self.assertTrue(self.runtime.submit(plugin_id, 'gesture', {'type': 'gesture'}, {}, {}))
            self.assertTrue(self.runtime.submit(plugin_id, 'manual', {}, {}, {}))
        self.assertEqual(self.runtime.cancel_pending(event_type='gesture'), 2)
        self.assertEqual(self.runtime.cancel_pending('missing'), 0)
        self.assertEqual(self.runtime.cancel_pending('other'), 1)
        self.wait(lambda: bool(self.kinds('finished')) and bool(self.kinds('started', 'other')))
        self.assertEqual(self.kinds('finished')[0]['result'], 'manual')
        self.assertFalse(self.kinds('finished', 'other'))

    def test_missing_python_is_a_ui_error_not_an_exception(self):
        self.runtime.start('test', self.root / 'missing-python', self.root, 'main.py',
                           self.root / 'data', self.root / 'cache')
        self.drain()
        self.assertEqual(len(self.kinds('error')), 1)
        self.assertEqual(self.kinds('error')[0]['phase'], 'startup')
        self.assertEqual(len(self.kinds('stopped')), 1)
        self.assertFalse(self.runtime.running('test'))

    def test_rpc_wait_obeys_task_deadline(self):
        self.start('''
            def handle_action(context, action, event, parameters):
                return context.call('unanswered')
        ''')
        self.assertTrue(self.submit(timeout=0.15))
        self.wait(lambda: bool(self.kinds("rpc")))
        self.wait(lambda: bool(self.kinds("stopped")))
        self.assertEqual(self.kinds("error")[0]["phase"], "timeout")


if __name__ == "__main__":
    unittest.main()
