# SPDX-License-Identifier: MPL-2.0
"""Local monitoring tests: fake audio devices only, no Tk or physical output."""
from __future__ import annotations

import builtins
from contextlib import ExitStack
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import openzilo as sdk

from hmm_gesture_studio.playback import PcmPlayback


class FakeStream:
    def __init__(self, **kwargs):
        self.options = kwargs
        self.started = threading.Event()
        self.active = True
        self.start_error = None
        self.abort_error = None
        self.close_error = None
        self.calls = []

    def start(self):
        self.calls.append(("start", threading.current_thread()))
        self.started.set()
        if self.start_error:
            raise self.start_error

    def abort(self):
        self.calls.append(("abort", threading.current_thread()))
        if self.abort_error:
            raise self.abort_error
        self.active = False

    def close(self):
        self.calls.append(("close", threading.current_thread()))
        if self.close_error:
            raise self.close_error

    def render(self, frames=320, status=None):
        out = bytearray(b"\xff" * (frames * self.options["channels"] * 2))
        self.options["callback"](out, frames, None, status)
        return bytes(out)


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stream = None
        self.created = threading.Event()
        self.warning = threading.Event()
        self.events = []
        self.configure = lambda stream: None

        def factory(**kwargs):
            self.stream = FakeStream(**kwargs)
            self.configure(self.stream)
            self.created.set()
            return self.stream

        self.factory = Mock(side_effect=factory)
        self.stack.enter_context(patch.dict(sys.modules, {
            "sounddevice": SimpleNamespace(RawOutputStream=self.factory),
        }))
        self.player = PcmPlayback(self.emit)
        self.stack.callback(self.player.close)

    def emit(self, name, value):
        self.events.append((name, value))
        self.warning.set()

    def start(self, channels=1):
        self.created.clear()
        self.player.start(sdk.PcmConfig(channels=channels))
        self.assertTrue(self.created.wait(2), "device creation did not run")
        self.assertTrue(self.stream.started.wait(2), "device did not start")
        return self.stream

    def assert_warning(self):
        self.assertTrue(self.warning.wait(2), "missing warning")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][0], "warning")
        self.assertIn("实时播放已停止", self.events[0][1])
        self.assertIn("录音接收与保存继续", self.events[0][1])

    def test_mono_and_stereo_default_device_and_unchanged_pcm(self):
        for channels in (1, 2):
            with self.subTest(channels=channels):
                self.player = PcmPlayback(self.emit)
                self.stack.callback(self.player.close)
                stream = self.start(channels)
                options = stream.options.copy()
                self.assertTrue(callable(options.pop("callback")))
                self.assertEqual(options, {
                    "dtype": "int16", "samplerate": 16000,
                    "channels": channels, "blocksize": 320,
                })
                data = b"\x01\x80\xff\x7f" * (160 * channels)
                original = bytes(data)
                self.player.feed(data)
                self.assertEqual(stream.render(), original)
                self.assertEqual(data, original)
                self.player.close()
        self.assertEqual(self.events, [])

    def test_empty_queue_outputs_silence(self):
        stream = self.start()
        self.assertEqual(stream.render(), bytes(640))
        self.assertEqual(self.events, [])

    def test_fragmented_input_and_batched_frames_keep_order(self):
        stream = self.start()
        first, second = b"\x01\x02" * 320, b"\x03\x04" * 320
        self.player.feed(first[:3])
        self.assertEqual(stream.render(), bytes(640))
        self.player.feed(first[3:] + second)
        self.assertEqual(stream.render(), first)
        self.assertEqual(stream.render(), second)
        self.assertEqual(stream.render(), bytes(640))

    def test_short_callback_keeps_remaining_audio(self):
        stream = self.start()
        frame = b"\x01\x02" * 320
        self.player.feed(frame)
        self.assertEqual(stream.render(100), frame[:200])
        self.assertEqual(stream.render(320), frame[200:] + bytes(200))

    def test_capacity_is_25_frames_and_overflow_disables(self):
        stream = self.start(2)
        data = b"\x01\x02" * (320 * 2 * 25)
        self.player.feed(data)
        self.assertEqual(self.player._queue.qsize(), 25)
        self.assertEqual(self.events, [])
        self.player.feed(b"\x03\x04")
        self.assert_warning()
        self.assertEqual(stream.render(), bytes(1280))
        self.player.feed(data)
        self.assertEqual(self.player._queue.qsize(), 25)
        self.player.close()
        self.assertEqual(self.events[0][0], "warning")
        self.assertEqual(len(self.events), 1)
        self.assertEqual(len(data), 32000)

    def test_huge_batch_is_rejected_before_allocating_queue(self):
        self.start()
        self.player.feed(bytes(640 * 26))
        self.assert_warning()
        self.assertEqual(self.player._queue.qsize(), 0)

    def test_fragmented_input_counts_towards_bound(self):
        self.start()
        self.player.feed(bytes(640 * 25 - 1))
        self.assertEqual(self.player._queue.qsize(), 24)
        self.player.feed(b"xx")
        self.assert_warning()

    def test_open_failure_is_warning_not_exception(self):
        self.factory.side_effect = RuntimeError("no default output device")
        self.player.start(sdk.PcmConfig())
        self.assert_warning()
        self.player.feed(bytes(640))
        self.player.close()
        self.assertFalse(self.player._thread.is_alive())

    def test_missing_optional_import_is_warning(self):
        real_import = builtins.__import__

        def missing(name, *args, **kwargs):
            if name == "sounddevice":
                raise ImportError("sounddevice unavailable")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing):
            self.player.start(sdk.PcmConfig())
            self.assert_warning()
            self.player.close()
        self.factory.assert_not_called()

    def test_start_failure_still_aborts_and_closes(self):
        self.configure = lambda stream: setattr(stream, "start_error", RuntimeError("start"))
        self.start()
        self.assert_warning()
        self.player.close()
        self.assertEqual([name for name, _ in self.stream.calls], ["start", "abort", "close"])

    def test_inactive_device_is_detected(self):
        stream = self.start()
        stream.active = False
        self.assert_warning()
        self.player.close()

    def test_callback_status_disables_and_silences(self):
        stream = self.start()
        self.player.feed(b"\x01\x02" * 320)
        self.assertEqual(stream.render(status="output underflow"), bytes(640))
        self.assert_warning()
        self.assertEqual(stream.render(status="again"), bytes(640))
        self.assertEqual(len(self.events), 1)

    def test_callback_exception_is_contained(self):
        stream = self.start()
        with patch.object(self.player._queue, "get_nowait", side_effect=RuntimeError("queue")):
            self.assertEqual(stream.render(), bytes(640))
        self.assert_warning()

    def test_cleanup_failures_warn_once_and_still_attempt_close(self):
        stream = self.start()
        stream.abort_error = RuntimeError("abort")
        stream.close_error = RuntimeError("close")
        self.player.close()
        self.assert_warning()
        self.assertEqual([name for name, _ in stream.calls], ["start", "abort", "close"])
        self.assertFalse(self.player._thread.is_alive())
        self.player.close()
        self.assertEqual(len(stream.calls), 3)
        self.assertEqual(len(self.events), 1)

    def test_close_joins_device_thread_and_disables_future_calls(self):
        stream = self.start()
        self.player.feed(bytes(640))
        self.player.close()
        self.assertFalse(self.player._thread.is_alive())
        self.assertEqual([name for name, _ in stream.calls], ["start", "abort", "close"])
        self.assertTrue(all(thread is self.player._thread for _, thread in stream.calls))
        self.assertEqual(self.player._queue.qsize(), 0)
        self.player.feed(bytes(640))
        self.player.start(sdk.PcmConfig())
        self.player.close()
        self.factory.assert_called_once()
        self.assertEqual(self.player._queue.qsize(), 0)
        self.assertEqual(stream.render(), bytes(640))
        self.assertEqual(self.events, [])

    def test_close_before_start_and_feed_before_start_are_noops(self):
        self.player.feed(bytes(640))
        self.player.close()
        self.player.close()
        self.player.start(sdk.PcmConfig())
        self.player.feed(bytes(640))
        self.factory.assert_not_called()
        self.assertEqual(self.events, [])

    def test_invalid_config_is_warning(self):
        self.player.start(SimpleNamespace(sample_rate=8000, channels=1, bit_depth=16))
        self.assert_warning()
        self.factory.assert_not_called()

    def test_thread_start_failure_is_contained(self):
        with patch("hmm_gesture_studio.playback.threading.Thread.start",
                   side_effect=RuntimeError("cannot start thread")):
            self.player.start(sdk.PcmConfig())
        self.assert_warning()
        self.player.close()
        self.factory.assert_not_called()

    def test_start_returns_while_device_open_is_blocked(self):
        release = threading.Event()
        opening = threading.Event()
        original_factory = self.factory.side_effect

        def blocked(**kwargs):
            opening.set()
            if not release.wait(2):
                raise RuntimeError("test did not release device")
            return original_factory(**kwargs)

        self.factory.side_effect = blocked
        try:
            self.player.start(sdk.PcmConfig())
            self.assertTrue(opening.wait(1))
            self.assertFalse(release.is_set())
            self.player.feed(bytes(640))
            self.player._stop.set()
        finally:
            release.set()
            self.player.close()
        self.assertEqual([name for name, _ in self.stream.calls], ["abort", "close"])
        self.assertEqual(self.events, [])

    def test_warning_emitter_failure_is_contained(self):
        self.player = PcmPlayback(Mock(side_effect=RuntimeError("emitter")))
        self.stack.callback(self.player.close)
        self.player.start(SimpleNamespace(sample_rate=0))
        self.player.feed(bytes(640))
        self.player.close()

    def test_module_import_is_headless_and_does_not_import_sounddevice(self):
        result = subprocess.run([
            sys.executable, "-c",
            "import sys; import hmm_gesture_studio.playback; "
            "assert 'sounddevice' not in sys.modules; "
            "assert 'tkinter' not in sys.modules",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
