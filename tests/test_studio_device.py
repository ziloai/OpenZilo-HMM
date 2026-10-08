# SPDX-License-Identifier: MPL-2.0
"""Deterministic worker/SDK-adapter tests; no BLE adapter or Tk required."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
import queue
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import openzilo as sdk

from hmm_gesture_studio import device
from hmm_gesture_studio.device import RingWorker, _StudioClient
from hmm_gesture_studio.ring_stream import open_ring_stream


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events = queue.Queue()
        self.history = []
        self.order = []
        self.waiting = threading.Event()
        self.connecting = threading.Event()
        self.stopping = threading.Event()
        self.source = asyncio.Queue()
        self.audio_gate = None
        self.gates = []
        self.active = self.max_active = 0
        self.rings = []
        self.start_info = sdk.SensorStartInfo(50, 8, 2000)
        self.connect_error = None
        self.connect_gate = self.stop_gate = None

        class FakeRing:
            def __init__(ring, **kwargs):
                ring.address = kwargs["address"]
                ring.is_connected = False
                ring._queues = defaultdict(asyncio.Queue)
                ring.audio_observer = None
                ring.audio_extract_index = None
                self.rings.append(ring)

            async def connect(ring):
                self.connecting.set()
                if self.connect_gate is not None:
                    await self.connect_gate.wait()
                if self.connect_error:
                    raise self.connect_error
                ring.is_connected = True
                self.order.append("connect")

            async def disconnect(ring):
                ring.is_connected = False
                self.order.append("disconnect")

            def _drain_queue(ring, command):
                q = ring._queues[command]
                while not q.empty():
                    q.get_nowait()
                if command == int(sdk.SensorCommand.DATA_FRAME):
                    self.order.append("drain")
                    while not self.source.empty():
                        self.source.get_nowait()

        self.client = self.stack.enter_context(patch.object(device, "_StudioClient", FakeRing))
        self.stack.enter_context(patch.object(device, "IMU_TIMEOUT", 0.2))
        self.stack.enter_context(patch.object(device, "RETRY_DELAY", 0.05))
        self.stack.enter_context(patch.object(device, "HOUSEKEEPING_INTERVAL", 0.01))
        self.stack.enter_context(patch.object(device, "RECONNECT_DELAYS", (0.01, 0.02, 0.04)))
        self.system = self.stack.enter_context(patch.object(
            sdk, "get_system_info", new_callable=AsyncMock,
            return_value=SimpleNamespace(model="ring", firmware_version="1", battery_percent=90),
        ))
        self.start_report = self.stack.enter_context(patch.object(
            sdk, "start_sensor_report", new_callable=AsyncMock, return_value=self.start_info,
        ))

        async def stop(*args, **kwargs):
            self.order.append("stop")
            self.stopping.set()
            if self.stop_gate is not None:
                await self.stop_gate.wait()
        self.stop_report = self.stack.enter_context(patch.object(sdk, "stop_sensor_report", side_effect=stop))

        async def receive(*args, timeout_s, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.waiting.set()
            try:
                try:
                    item = await asyncio.wait_for(self.source.get(), timeout_s)
                except asyncio.TimeoutError:
                    raise sdk.TimeoutError("no IMU frames")
                if isinstance(item, Exception):
                    raise item
                return item
            finally:
                self.active -= 1
        self.receive = self.stack.enter_context(patch.object(sdk, "wait_sensor_data", side_effect=receive))

        async def auto(ring, **kwargs):
            self.assertEqual(self.active, 0)
            item = await ring._queues[int(sdk.AudioCommand.DATA_FRAME)].get()
            if isinstance(item, Exception):
                raise item
            ring.audio_observer(SimpleNamespace(frame_offset=0, data=item[1]))
            if self.audio_gate is not None:
                await self.audio_gate.wait()
            return item
        self.auto = self.stack.enter_context(patch.object(sdk, "receive_auto_audio_file", side_effect=auto))
        self.count = self.stack.enter_context(patch.object(sdk, "get_audio_file_count", new_callable=AsyncMock, return_value=3))
        self.file_info = self.stack.enter_context(patch.object(sdk, "get_audio_file_info", new_callable=AsyncMock))
        self.download = self.stack.enter_context(patch.object(sdk, "download_audio_file", new_callable=AsyncMock))
        self.end = self.stack.enter_context(patch.object(sdk, "end_audio_extract", new_callable=AsyncMock))
        self.scan = self.stack.enter_context(patch.object(sdk, "scan_rings", new_callable=AsyncMock))
        self.saved = {"path": "x.bin", "raw_path": "x.bin", "duration_s": None,
                      "bytes": 4, "file_index": 2, "warning": None}
        self.save = self.stack.enter_context(patch.object(device, "save_recording", return_value=self.saved))

        def callback(event, payload):
            item = (event, payload, threading.get_ident())
            self.history.append(item)
            self.events.put(item)
        self.worker = RingWorker(callback)
        self.worker.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        if not self.worker._closed.done():
            for gate in self.gates:
                self.worker._loop.call_soon_threadsafe(gate.set)
        self.worker.close().result(timeout=3)
        self.worker._thread.join(timeout=3)
        self.assertFalse(self.worker._thread.is_alive())
        self.assertTrue(self.worker._loop.is_closed())

    def gate(self):
        gate = asyncio.Event()
        self.gates.append(gate)
        return gate

    def event(self, expected, state=None):
        deadline = time.monotonic() + 3
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"Timed out waiting for {expected}/{state}")
            event, payload, ident = self.events.get(timeout=remaining)
            self.assertEqual(ident, self.worker._thread.ident)
            self.assertNotEqual(ident, threading.get_ident())
            if event == expected and (state is None or payload["state"] == state):
                return payload
            if event == "error" and expected != "error":
                self.fail(str(payload))

    def feed(self, value):
        self.worker._loop.call_soon_threadsafe(self.source.put_nowait, value)

    def feed_audio(self, *values):
        def push():
            for value in values:
                self.rings[-1]._queues[int(sdk.AudioCommand.DATA_FRAME)].put_nowait(value)
        self.worker._loop.call_soon_threadsafe(push)

    def housekeeping_pass(self):
        async def check():
            task = asyncio.create_task(self.worker._housekeeping(self.rings[-1]))
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        asyncio.run_coroutine_threadsafe(check(), self.worker._loop).result(timeout=3)

    def batch(self, sequence=0, timestamp=100):
        return sdk.SensorDataBatch(sequence, 1, 16, (
            sdk.SensorDataSample(timestamp, -32768, 32767, 3, 4, -5, 6),
        ))

    def connect(self):
        self.worker.connect("manual-address")
        self.assertEqual(self.event("connected"), {
            "address": "manual-address", "model": "ring",
            "firmware_version": "1", "battery_percent": 90,
        })

    def streaming(self):
        self.connect()
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch())
        state = self.event("stream_state", "active")
        self.assertEqual(state["sample_rate_hz"], 50)
        self.assertEqual(self.event("samples"), [[-32768, 32767, 3, 4, -5, 6]])

    def listen(self, directory="out"):
        self.worker.start_audio(directory)
        self.event("audio_state", "listening")
        self.assertTrue(self.worker._audio_armed)
        self.assertEqual(self.worker._audio_directory, directory)
        self.assertFalse(self.worker._audio_busy)
        self.assertIsNone(self.worker._audio_task)

    def test_scan_and_duplicate_request(self):
        self.scan.return_value = [SimpleNamespace(name="Ring", address="A", rssi=-61)]
        self.worker.scan()
        self.assertEqual(self.event("devices"), [{"name": "Ring", "address": "A", "rssi": -61}])
        self.scan.assert_awaited_once_with(timeout_s=8.0)
        self.assertTrue(self.worker._thread.daemon)

    def test_single_consumer_and_stop_before_disconnect(self):
        self.streaming()
        self.worker.connect("other")
        self.assertIn("already active", self.event("error"))
        self.worker.scan()
        self.event("error")
        self.worker.disconnect()
        self.event("disconnected")
        self.assertEqual(self.max_active, 1)
        self.assertEqual(self.active, 0)
        self.assertLess(self.order.index("stop"), self.order.index("disconnect"))
        self.assertEqual(len(self.rings), 1)

    def test_busy_initial_connection_survives_and_retries(self):
        self.start_report.side_effect = [sdk.DeviceError(sdk.ErrorCode.DEVICE_BUSY), self.start_info]
        self.connect()
        self.event("stream_state", "suspended")
        self.assertTrue(self.rings[0].is_connected)
        self.worker.retry_imu()
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch())
        self.event("stream_state", "active")
        self.event("samples")
        self.assertEqual(len(self.rings), 1)
        self.assertEqual(self.start_report.await_count, 2)

    def test_busy_recovers_automatically_without_manual_retry(self):
        attempts = 0
        async def start(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sdk.DeviceError(sdk.ErrorCode.DEVICE_BUSY)
            return self.start_info
        self.start_report.side_effect = start
        self.connect()
        self.event("stream_state", "suspended")
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch())
        self.event("stream_state", "active")
        self.event("samples")
        self.assertEqual(len(self.rings), 1)

    def test_armed_listener_recovers_from_recording_busy_to_gesture_mode(self):
        gesture_mode = threading.Event()
        async def start(*args, **kwargs):
            if not gesture_mode.is_set():
                raise sdk.DeviceError(sdk.ErrorCode.DEVICE_BUSY)
            return self.start_info
        self.start_report.side_effect = start
        self.connect()
        self.event("stream_state", "suspended")
        self.listen()
        gesture_mode.set()
        # No audio, key event or manual retry is needed to recover gesture mode.
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch())
        self.event("stream_state", "active")
        self.event("samples")
        self.assertTrue(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)
        self.assertEqual(self.worker._audio_state, "listening")
        self.assertEqual(self.max_active, 1)
        self.assertEqual(len(self.rings), 1)
        self.auto.assert_not_awaited()
        self.stop_report.assert_not_awaited()

    def test_batches_arriving_before_start_ack_are_discarded(self):
        async def start(*args, **kwargs):
            self.source.put_nowait(self.batch(88, 8888))
            return self.start_info
        self.start_report.side_effect = start
        self.connect()
        self.assertTrue(self.waiting.wait(3))
        self.assertFalse(any(e == "samples" for e, _, _ in self.history))
        self.feed(self.batch())
        self.event("stream_state", "active")
        self.event("samples")
        self.assertEqual(sum(e == "samples" for e, _, _ in self.history), 1)

    def test_ack_and_empty_batch_are_not_active(self):
        self.connect()
        self.assertTrue(self.waiting.wait(3))
        self.feed(sdk.SensorDataBatch(0, 0, 16, ()))
        self.worker.disconnect()
        self.event("disconnected")
        self.assertFalse(any(e == "stream_state" and p["state"] == "active" for e, p, _ in self.history))
        self.assertFalse(any(e == "samples" for e, _, _ in self.history))

    def test_timeout_busy_then_new_stream_has_fresh_boundary(self):
        self.streaming()
        self.start_report.side_effect = [sdk.DeviceError(sdk.ErrorCode.DEVICE_BUSY), self.start_info]
        self.feed(sdk.TimeoutError("mode changed"))
        self.event("stream_state", "suspended")
        self.waiting.clear()
        # Two explicit retry hints are harmless; only one consumer exists.
        self.worker.retry_imu()
        self.worker.retry_imu()
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch(0, 1))
        self.event("stream_state", "active")
        self.event("samples")
        self.assertEqual(self.max_active, 1)
        self.assertEqual(len(self.rings), 1)

    def test_gap_and_timestamp_rollback_reset_before_samples(self):
        self.streaming()
        self.feed(self.batch(9, 50))
        self.event("stream_state", "waiting")
        self.event("stream_state", "active")
        self.event("samples")

    def test_restart_drains_stale_frames(self):
        self.connect()
        self.assertTrue(self.waiting.wait(3))
        self.feed(sdk.TimeoutError("pause"))
        self.event("stream_state", "suspended")
        def stale_and_retry():
            self.source.put_nowait(self.batch(80, 8000))
            self.worker._retry()
        self.waiting.clear()
        self.worker._loop.call_soon_threadsafe(stale_and_retry)
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch(0, 1))
        self.event("samples")
        self.assertGreaterEqual(self.order.count("drain"), 2)
        self.assertEqual(sum(e == "samples" for e, _, _ in self.history), 1)

    def test_disconnect_reconnects_same_address(self):
        self.streaming()
        self.worker._loop.call_soon_threadsafe(setattr, self.rings[0], "is_connected", False)
        self.assertEqual(self.event("reconnecting")["attempt"], 1)
        self.event("connected")
        self.assertEqual([r.address for r in self.rings], ["manual-address"] * 2)

    def test_reconnect_and_close_clear_passive_audio_subscription(self):
        self.streaming()
        self.listen()
        self.worker._loop.call_soon_threadsafe(setattr, self.rings[0], "is_connected", False)
        self.event("reconnecting")
        self.event("connected")
        self.assertFalse(self.worker._audio_armed)
        self.assertIsNone(self.worker._audio_directory)
        self.assertFalse(self.worker._audio_busy)
        self.assertIsNone(self.worker._audio_task)
        self.assertIsNone(self.worker._audio_pending)
        self.listen("new-output")
        self.worker.close().result(timeout=3)
        self.assertFalse(self.worker._audio_armed)
        self.assertIsNone(self.worker._audio_directory)
        self.assertIsNone(self.worker._ring)
        self.assertIsNone(self.worker._wake)
        self.auto.assert_not_awaited()

    def test_cancel_reconnect_backoff(self):
        self.stack.enter_context(patch.object(device, "RECONNECT_DELAYS", (30,)))
        self.streaming()
        self.feed(sdk.TransportError("lost"))
        self.event("reconnecting")
        self.worker.disconnect()
        self.event("disconnected")
        self.assertEqual(len(self.rings), 1)

    def test_reconnect_backoff_increases_and_caps(self):
        self.streaming()
        self.connect_error = sdk.TransportError("still offline")
        self.feed(sdk.TransportError("lost"))
        retries = [self.event("reconnecting") for _ in range(5)]
        self.assertEqual([p["attempt"] for p in retries], [1, 2, 3, 4, 5])
        self.assertEqual([p["address"] for p in retries], ["manual-address"] * 5)
        for payload, delay in zip(retries, (0.01, 0.02, 0.04, 0.04, 0.04)):
            self.assertIn(f"in {delay:g}s", payload["message"])
        self.worker.close().result(timeout=3)

    def test_initial_failure_and_protocol_error_do_not_retry(self):
        self.connect_error = sdk.TransportError("connect failed")
        self.worker.connect("A")
        self.assertIn("connect failed", self.event("error"))
        self.event("disconnected")
        self.assertEqual(len(self.rings), 1)
        self.connect_error = None
        self.connect()
        self.assertTrue(self.waiting.wait(3))
        self.feed(sdk.ProtocolError("bad packet"))
        self.assertIn("bad packet", self.event("error"))
        self.event("disconnected")
        self.assertEqual(len(self.rings), 2)

    def test_invalid_sample_rate_cleans_up(self):
        self.start_report.return_value = sdk.SensorStartInfo(float("nan"), 8, 2000)
        self.connect()
        self.assertIn("sample rate", self.event("error"))
        self.event("disconnected")
        self.assertLess(self.order.index("stop"), self.order.index("disconnect"))

    def test_close_idempotent_and_does_not_recancel_cleanup(self):
        self.streaming()
        self.stop_gate = self.gate()
        self.worker.disconnect()
        self.assertTrue(self.stopping.wait(3))
        result = self.worker.close()
        self.assertIs(result, self.worker.close())
        self.assertFalse(result.done())
        self.worker._loop.call_soon_threadsafe(self.stop_gate.set)
        result.result(timeout=3)
        self.assertEqual(self.order.count("stop"), 1)
        self.assertEqual(self.order[-1], "disconnect")

    def test_close_while_connecting(self):
        self.connect_gate = self.gate()
        self.worker.connect("A")
        self.assertTrue(self.connecting.wait(3))
        self.worker.close().result(timeout=3)
        self.assertEqual(self.order, ["disconnect"])
        self.start_report.assert_not_awaited()

    def test_cancel_audio_while_imu_stop_is_pending(self):
        self.streaming()
        self.stop_gate = self.gate()
        self.listen()
        self.feed_audio((2, b"data"))
        self.assertTrue(self.stopping.wait(3))
        self.feed(self.batch(88, 8888))
        self.housekeeping_pass()
        self.assertEqual(self.source.qsize(), 1)  # Pending is not an active audio consumer.
        self.assertEqual(self.rings[0]._queues[int(sdk.AudioCommand.DATA_FRAME)].qsize(), 1)
        self.worker.stop_audio()
        self.event("audio_state", "idle")
        self.waiting.clear()
        self.worker._loop.call_soon_threadsafe(self.stop_gate.set)
        self.assertTrue(self.waiting.wait(3))
        self.auto.assert_not_awaited()
        self.assertFalse(self.worker._audio_busy)
        self.assertFalse(self.worker._audio_armed)
        self.assertIsNone(self.worker._audio_directory)

    def test_audio_list_uses_count_not_metadata_or_extraction(self):
        self.streaming()
        self.worker.list_audio()
        self.assertEqual(self.event("audio_files"), [{"file_index": i} for i in range(3)])
        self.file_info.assert_not_awaited()
        self.download.assert_not_awaited()

    def test_armed_without_frames_keeps_imu_and_excludes_manual_audio(self):
        self.streaming()
        self.listen()
        self.worker.download_audio(2, "out")
        self.assertIn("Stop", self.event("error"))
        self.worker.list_audio()
        self.event("error")
        for i in range(1, 4):
            self.housekeeping_pass()
            self.feed(self.batch(i, 100 + i))
            self.event("samples")
        # Listening/housekeeping are not START_REPORT heartbeats.
        self.start_report.assert_awaited_once()
        self.stop_report.assert_not_awaited()
        self.auto.assert_not_awaited()
        self.download.assert_not_awaited()
        self.count.assert_not_awaited()
        self.assertTrue(self.worker._audio_armed)
        self.assertEqual(self.worker._stream_state, "active")
        self.assertEqual(self.max_active, 1)

    def test_auto_file_returns_to_listening_and_fresh_imu(self):
        self.streaming()
        self.listen()
        self.waiting.clear()
        self.feed_audio((2, b"data"))
        self.assertIs(self.event("audio_saved"), self.saved)
        self.event("audio_state", "listening")
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch(0, 1))
        self.event("stream_state", "active")
        self.event("samples")
        self.assertTrue(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)
        self.assertEqual(self.worker._audio_directory, "out")
        self.assertEqual(self.start_report.await_count, 2)
        self.stop_report.assert_awaited_once()
        self.auto.assert_awaited_once_with(self.rings[0], timeout_s=device.AUDIO_TIMEOUT)
        self.save.assert_called_once_with(b"data", 2, "out", metadata={})
        self.assertEqual(self.max_active, 1)

    def test_stop_disarms_listener_without_interrupting_imu(self):
        self.streaming()
        self.listen()
        self.worker.stop_audio()
        self.event("audio_state", "idle")
        self.assertFalse(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)
        self.assertIsNone(self.worker._audio_directory)
        self.feed_audio((2, b"late"))
        self.housekeeping_pass()
        self.assertTrue(self.rings[0]._queues[int(sdk.AudioCommand.DATA_FRAME)].empty())
        self.feed(self.batch(1, 101))
        self.event("samples")
        self.start_report.assert_awaited_once()
        self.stop_report.assert_not_awaited()
        self.auto.assert_not_awaited()

    def test_stop_cancels_auto_transfer_and_finishes_end_before_resuming(self):
        self.audio_gate = self.gate()
        async def end(*args, **kwargs):
            self.assertEqual(self.active, 0)
            self.order.append("end")
        self.end.side_effect = end
        self.streaming()
        self.listen()
        self.feed_audio((2, b"part"))
        self.event("audio_progress")
        self.assertEqual(self.active, 0)
        self.worker._loop.call_soon_threadsafe(setattr, self.rings[0], "audio_extract_index", 2)
        self.worker.retry_imu()
        self.feed(self.batch(88, 8888))
        self.housekeeping_pass()
        self.assertTrue(self.source.empty())
        # Even retry hints cannot interleave commands with an audio transfer.
        self.start_report.assert_awaited_once()
        self.stop_report.assert_awaited_once()
        self.waiting.clear()
        self.worker.stop_audio()
        self.event("audio_state", "idle")
        self.assertTrue(self.waiting.wait(3))
        self.end.assert_awaited_once_with(self.rings[0], 2, timeout_s=device.CLEANUP_TIMEOUT, ignore_timeout=True)
        self.assertIsNone(self.rings[0].audio_extract_index)
        self.assertIsNone(self.rings[0].audio_observer)
        self.assertFalse(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)
        self.assertIsNone(self.worker._audio_directory)
        self.assertIsNone(self.worker._audio_task)
        self.save.assert_not_called()
        self.feed(self.batch(0, 1))
        self.event("stream_state", "active")
        self.event("samples")
        self.assertEqual(self.max_active, 1)

    def test_stop_during_normal_auto_end_finishes_cleanup_after_saving(self):
        ending = threading.Event()
        async def end(*args, **kwargs):
            self.save.assert_called_once()
            self.assertEqual(self.active, 0)
            if self.end.await_count == 1:
                ending.set()
                await asyncio.Event().wait()
        self.end.side_effect = end
        self.streaming()
        self.listen()
        self.worker._loop.call_soon_threadsafe(setattr, self.rings[0], "audio_extract_index", 2)
        self.feed_audio((2, b"data"))
        self.event("audio_saved")
        self.assertTrue(ending.wait(3))
        self.waiting.clear()
        self.worker.stop_audio()
        self.event("audio_state", "idle")
        self.assertTrue(self.waiting.wait(3))
        self.assertEqual(self.end.await_count, 2)
        self.assertIsNone(self.rings[0].audio_extract_index)
        self.assertIsNone(self.rings[0].audio_observer)
        self.assertFalse(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)

    def test_disconnect_does_not_recancel_auto_extraction_cleanup(self):
        self.audio_gate = self.gate()
        end_gate = self.gate()
        ending = threading.Event()
        async def end(*args, **kwargs):
            ending.set()
            await end_gate.wait()
            self.order.append("end")
        self.end.side_effect = end
        self.streaming()
        self.listen()
        self.feed_audio((2, b"part"))
        self.event("audio_progress")
        self.worker._loop.call_soon_threadsafe(setattr, self.rings[0], "audio_extract_index", 2)
        self.worker.stop_audio()
        self.assertTrue(ending.wait(3))
        self.worker.stop_audio()
        self.worker.disconnect()
        self.worker._loop.call_soon_threadsafe(end_gate.set)
        self.event("disconnected")
        self.end.assert_awaited_once()
        self.assertLess(self.order.index("end"), self.order.index("disconnect"))
        self.assertFalse(self.worker._audio_armed)
        self.assertIsNone(self.worker._audio_directory)
        self.assertIsNone(self.worker._audio_task)
        self.assertIsNone(self.rings[0].audio_observer)
        self.assertIsNone(self.rings[0].audio_extract_index)
        self.save.assert_not_called()

    def test_consecutive_auto_files_are_not_drained_while_saving_or_rearming(self):
        saving = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        def save(data, index, directory, *, metadata):
            if index == 2:
                saving.set()
                self.assertTrue(release.wait(3))
            return {**self.saved, "file_index": index}
        self.save.side_effect = save
        self.streaming()
        self.listen()
        self.waiting.clear()
        self.feed_audio((2, b"first"))
        self.assertTrue(saving.wait(3))
        self.feed_audio((3, b"second"), (4, b"third"))
        self.housekeeping_pass()
        self.assertEqual(self.rings[0]._queues[int(sdk.AudioCommand.DATA_FRAME)].qsize(), 2)
        self.assertTrue(self.worker._audio_busy)
        self.assertEqual(self.active, 0)
        release.set()
        self.assertEqual([self.event("audio_saved")["file_index"] for _ in range(3)], [2, 3, 4])
        self.event("audio_state", "listening")
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch(0, 1))
        self.event("stream_state", "active")
        self.event("samples")
        self.assertEqual(self.auto.await_count, 3)
        self.assertEqual([call.args for call in self.save.call_args_list], [
            (b"first", 2, "out"), (b"second", 3, "out"), (b"third", 4, "out"),
        ])
        self.assertTrue(self.rings[0]._queues[int(sdk.AudioCommand.DATA_FRAME)].empty())
        self.assertTrue(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)
        self.assertEqual(self.max_active, 1)

    def test_download_explicit_index_progress_and_metadata(self):
        async def download(ring, index, *, quick, progress, timeout_s):
            self.assertEqual(index, 2)
            self.assertFalse(quick)
            self.assertEqual(self.active, 0)
            progress(4, 4)
            return SimpleNamespace(record_time=123, data_size=4), b"data"
        self.download.side_effect = download
        self.streaming()
        self.worker.download_audio(2, "out")
        self.assertEqual(self.event("audio_progress"), {"bytes": 4, "total": 4})
        self.event("audio_saved")
        self.save.assert_called_once_with(b"data", 2, "out", metadata={"record_time": 123, "data_size": 4})
        self.end.assert_not_awaited()
        self.event("audio_state", "idle")
        self.worker.download_audio(-1, "out")
        self.assertIn("index", self.event("error"))

    def test_cancel_download_sends_end_before_resuming(self):
        extracting = threading.Event()
        async def download(*args, **kwargs):
            extracting.set()
            await asyncio.Event().wait()
        self.download.side_effect = download
        self.streaming()
        self.worker.download_audio(7, "out")
        self.assertTrue(extracting.wait(3))
        self.worker.stop_audio()
        self.event("audio_state", "idle")
        self.end.assert_awaited_once_with(self.rings[0], 7, timeout_s=device.CLEANUP_TIMEOUT, ignore_timeout=True)
        self.save.assert_not_called()

    def test_auto_first_frame_timeout_releases_imu_and_stays_armed(self):
        self.streaming()
        self.listen()
        self.waiting.clear()
        self.feed_audio(sdk.TimeoutError("no first frame"))
        self.event("audio_state", "receiving")
        self.event("audio_state", "listening")
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch(0, 1))
        self.event("stream_state", "active")
        self.event("samples")
        self.assertTrue(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)
        self.auto.assert_awaited_once()
        self.save.assert_not_called()
        self.assertFalse(any(e == "error" for e, _, _ in self.history))

    def test_auto_partial_failure_does_not_save_and_releases_imu(self):
        receive = self.auto.side_effect
        async def broken(ring, **kwargs):
            await receive(ring, **kwargs)
            raise sdk.TimeoutError("missing frame")
        self.auto.side_effect = broken
        self.streaming()
        self.listen()
        self.waiting.clear()
        self.feed_audio((2, b"part"))
        self.assertIn("ring history", self.event("error"))
        self.event("audio_state", "listening")
        self.assertTrue(self.waiting.wait(3))
        self.feed(self.batch(0, 1))
        self.event("samples")
        self.assertTrue(self.worker._audio_armed)
        self.assertFalse(self.worker._audio_busy)
        self.save.assert_not_called()
        self.assertFalse(any(e == "audio_saved" for e, _, _ in self.history))

    def test_close_waits_for_complete_audio_save_and_emits_saved(self):
        saving = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        def save(*args, **kwargs):
            self.assertNotEqual(threading.get_ident(), self.worker._thread.ident)
            saving.set()
            self.assertTrue(release.wait(3))
            self.order.append("saved")
            return self.saved
        self.save.side_effect = save
        self.streaming()
        self.listen()
        self.feed_audio((2, b"data"))
        self.assertTrue(saving.wait(3))
        self.worker.stop_audio()
        self.event("audio_state", "stopping")
        result = self.worker.close()
        self.assertFalse(result.done())
        release.set()
        result.result(timeout=3)
        self.assertIs(self.event("audio_saved"), self.saved)
        self.assertLess(self.order.index("saved"), self.order.index("disconnect"))
        self.assertFalse(self.worker._audio_armed)
        self.assertIsNone(self.worker._audio_directory)
        self.assertIsNone(self.worker._audio_task)
        self.assertIsNone(self.worker._audio_pending)

    def test_decoder_failure_preserves_raw_and_connection(self):
        from hmm_gesture_studio.audio import save_recording
        self.save.side_effect = save_recording
        self.stack.enter_context(patch.object(sdk, "decode_audio_to_wav", side_effect=sdk.SpeexDecoderUnavailable("ffmpeg missing")))
        with tempfile.TemporaryDirectory() as directory:
            self.streaming()
            self.listen(directory)
            self.feed_audio((2, b"data"))
            self.assertIn("ffmpeg", self.event("warning"))
            result = self.event("audio_saved")
            self.assertEqual(Path(result["raw_path"]).read_bytes(), b"data")
            self.assertIsNone(result["duration_s"])
            self.assertTrue(self.rings[0].is_connected)

    def test_audio_save_error_keeps_ble_connection(self):
        self.save.side_effect = OSError("disk full")
        self.streaming()
        self.listen()
        self.feed_audio((2, b"data"))
        self.assertIn("disk full", self.event("error"))
        self.event("audio_state", "listening")
        self.assertTrue(self.rings[0].is_connected)
        self.assertEqual(len(self.rings), 1)
        self.assertFalse(any(e == "audio_saved" for e, _, _ in self.history))

    def test_invalid_history_count_is_bounded_and_preserves_connection(self):
        self.count.return_value = 0xFFFFFFFF
        self.streaming()
        self.worker.list_audio()
        self.assertIn("recording count", self.event("error"))
        self.event("audio_state", "idle")
        self.assertTrue(self.rings[0].is_connected)
        self.assertFalse(any(e == "audio_files" for e, _, _ in self.history))

    def test_close_cancels_scan_and_rejects_overlapping_requests(self):
        scanning = threading.Event()
        cancelled = threading.Event()
        async def scan(**kwargs):
            scanning.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.scan.side_effect = scan
        self.worker.scan()
        self.assertTrue(scanning.wait(3))
        self.worker.scan()
        self.event("error")
        self.worker.connect("manual")
        self.event("error")
        self.event("disconnected")
        self.worker.close().result(timeout=3)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.rings)

    def test_cancel_before_connect_coroutine_starts(self):
        blocked = threading.Event()
        release = threading.Event()
        def block():
            blocked.set()
            release.wait(3)
        self.worker._loop.call_soon_threadsafe(block)
        self.assertTrue(blocked.wait(3))
        try:
            self.worker.connect("manual")
            self.worker.disconnect()
        finally:
            release.set()
        self.event("disconnected")
        self.assertFalse(self.rings)

    def test_housekeeping_drains_unsubscribed_audio_and_keys(self):
        self.streaming()
        done = threading.Event()
        async def check():
            ring = self.rings[0]
            ring._queues[int(sdk.AudioCommand.DATA_FRAME)].put_nowait(object())
            ring._queues[0x0704].put_nowait(object())
            task = asyncio.create_task(self.worker._housekeeping(ring))
            await asyncio.sleep(0)  # Run precisely one housekeeping pass.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertTrue(ring._queues[int(sdk.AudioCommand.DATA_FRAME)].empty())
            self.assertTrue(ring._queues[0x0704].empty())
            done.set()
        future = asyncio.run_coroutine_threadsafe(check(), self.worker._loop)
        future.result(timeout=3)
        self.assertTrue(done.is_set())

    def test_close_before_start_and_requests_after_close_are_safe(self):
        worker = RingWorker(lambda *args: self.fail("Unexpected callback"))
        result = worker.close()
        self.assertIs(result, worker.close())
        self.assertIsNone(result.result(timeout=3))
        worker.start()
        worker.connect("address")
        worker.scan()
        worker.retry_imu()
        worker.start_audio("out")
        worker.stop_audio()
        worker.disconnect()
        self.assertIsNone(worker._thread)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_finishes_fragmented_write_and_serializes_next(self):
        started = asyncio.Event()
        release = asyncio.Event()
        writes = []
        async def write(data):
            writes.append("begin")
            started.set()
            await release.wait()
            writes.append("end")
        transport = SimpleNamespace(write=write)
        ring = _StudioClient(transport=transport)
        first = asyncio.create_task(ring.send_command(1))
        await started.wait()
        first.cancel()
        second = asyncio.create_task(ring.send_command(2))
        await asyncio.sleep(0)
        self.assertEqual(writes, ["begin"])
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertEqual(writes, ["begin", "end", "begin", "end"])

    async def test_transport_failure_cannot_override_user_cancellation(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def write(data):
            started.set()
            await release.wait()
            raise OSError("link failed during write cleanup")
        ring = _StudioClient(transport=SimpleNamespace(write=write))
        task = asyncio.create_task(ring.send_command(1))
        await started.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_platform_write_error_is_exposed_as_transport_loss(self):
        ring = _StudioClient(transport=SimpleNamespace(write=AsyncMock(side_effect=OSError("lost link"))))
        with self.assertRaisesRegex(sdk.TransportError, "lost link"):
            await ring.send_command(1)

    async def test_complete_requests_are_serialized(self):
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        async def request(*args, **kwargs):
            calls.append("begin")
            started.set()
            await release.wait()
            calls.append("end")
        ring = _StudioClient(transport=SimpleNamespace())
        with patch.object(sdk.OpenZiloClient, "request", side_effect=request):
            first = asyncio.create_task(ring.request(1, 2))
            await started.wait()
            second = asyncio.create_task(ring.request(3, 4))
            await asyncio.sleep(0)
            self.assertEqual(calls, ["begin"])
            release.set()
            await asyncio.gather(first, second)
        self.assertEqual(calls, ["begin", "end", "begin", "end"])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.order = []
        self.ring = SimpleNamespace(is_connected=True)
        @asynccontextmanager
        async def client(**kwargs):
            try:
                yield self.ring
            finally:
                self.order.append("disconnect")
                self.ring.is_connected = False
        self.stack.enter_context(patch(
            "hmm_gesture_studio.ring_stream.sdk.OpenZiloClient", side_effect=client,
        ))
        self.stack.enter_context(patch(
            "hmm_gesture_studio.ring_stream.sdk.get_system_info", new_callable=AsyncMock,
            return_value=SimpleNamespace(model="ring", firmware_version="1", battery_percent=90),
        ))
        self.start = self.stack.enter_context(patch(
            "hmm_gesture_studio.ring_stream.sdk.start_sensor_report", new_callable=AsyncMock,
            return_value=sdk.SensorStartInfo(25, 8, 2000),
        ))
        async def stop(ring):
            self.order.append("stop")
        self.stop = self.stack.enter_context(patch(
            "hmm_gesture_studio.ring_stream.sdk.stop_sensor_report", side_effect=stop,
        ))
        self.status = []

    async def test_stop_precedes_disconnect_on_cancel(self):
        with self.assertRaises(asyncio.CancelledError):
            async with open_ring_stream("A", on_status=self.status.append):
                raise asyncio.CancelledError()
        self.assertEqual(self.order, ["stop", "disconnect"])

    async def test_stop_failure_does_not_mask_receive_failure(self):
        self.stop.side_effect = sdk.TimeoutError("stop failed")
        original = sdk.ProtocolError("original receive failure")
        with self.assertRaises(sdk.ProtocolError) as caught:
            async with open_ring_stream("A", on_status=self.status.append):
                raise original
        self.assertIs(caught.exception, original)
        self.assertTrue(any("stop failed" in text for text in self.status))
        self.assertFalse(self.ring.is_connected)
        self.stop.assert_awaited_once_with(self.ring)

    async def test_busy_guidance_and_no_automatic_mode_switch(self):
        self.start.side_effect = sdk.DeviceError(sdk.ErrorCode.DEVICE_BUSY)
        with self.assertRaisesRegex(sdk.DeviceError, "key event alone"):
            async with open_ring_stream("A", on_status=self.status.append):
                self.fail("busy must not yield")
        self.stop.assert_not_awaited()
        self.assertFalse(self.ring.is_connected)

    async def test_invalid_rate_still_cleans_up(self):
        self.start.return_value = sdk.SensorStartInfo(0, 8, 2000)
        with self.assertRaisesRegex(ValueError, "sample rate"):
            async with open_ring_stream("A", on_status=self.status.append):
                self.fail("invalid sample rate")
        self.assertEqual(self.order, ["stop", "disconnect"])


if __name__ == "__main__":
    unittest.main()
