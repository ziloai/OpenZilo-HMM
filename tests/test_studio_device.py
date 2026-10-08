# SPDX-License-Identifier: MPL-2.0
"""Deterministic BLE worker tests: no adapter, Tk, or polling sleeps."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
import queue
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import openzilo as sdk

from hmm_gesture_studio.device import RingWorker
from hmm_gesture_studio.ring_stream import open_ring_stream


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events = queue.Queue()
        self.started = threading.Event()
        self.exited = threading.Event()
        self.waiting = threading.Event()
        self.source = asyncio.Queue()
        self.ring = SimpleNamespace(is_connected=True)
        self.info = sdk.SensorStartInfo(50, 8, 2000)
        self.connect_gate = None
        self.stop_gate = None
        self.stopping = threading.Event()
        self.active = 0
        self.max_active = 0
        self.enter_count = 0

        @asynccontextmanager
        async def stream(address, *, on_status):
            self.enter_count += 1
            self.started.set()
            try:
                if self.connect_gate is not None:
                    await self.connect_gate.wait()
                yield self.ring, self.info
            finally:
                self.stopping.set()
                if self.stop_gate is not None:
                    await self.stop_gate.wait()
                self.ring.is_connected = False
                self.exited.set()

        async def receive(*args, **kwargs):
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.waiting.set()
            try:
                item = await self.source.get()
                if isinstance(item, Exception):
                    raise item
                return item
            finally:
                self.active -= 1

        self.stream = self.stack.enter_context(patch(
            "hmm_gesture_studio.device.open_ring_stream", side_effect=stream,
        ))
        self.receive = self.stack.enter_context(patch(
            "hmm_gesture_studio.device.sdk.wait_sensor_data", side_effect=receive,
        ))
        self.scan = self.stack.enter_context(patch(
            "hmm_gesture_studio.device.sdk.scan_rings", new_callable=AsyncMock,
        ))
        self.worker = RingWorker(lambda event, payload: self.events.put(
            (event, payload, threading.get_ident())
        ))
        self.worker.start()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for gate in (self.connect_gate, self.stop_gate):
            if gate is not None and not self.worker._closed.done():
                self.worker._loop.call_soon_threadsafe(gate.set)
        self.worker.close().result(timeout=3)
        # Only the test joins to assert termination; no GUI caller needs to.
        self.worker._thread.join(timeout=3)
        self.assertFalse(self.worker._thread.is_alive())
        self.assertTrue(self.worker._loop.is_closed())

    def event(self, expected):
        event, payload, ident = self.events.get(timeout=3)
        self.assertEqual(event, expected)
        self.assertEqual(ident, self.worker._thread.ident)
        self.assertNotEqual(ident, threading.get_ident())
        return payload

    def feed(self, value):
        self.worker._loop.call_soon_threadsafe(self.source.put_nowait, value)

    def connect(self):
        self.worker.connect("manual-address")
        self.assertEqual(self.event("connected"), {
            "address": "manual-address", "sample_rate_hz": 50,
            "accel_range_g": 8, "gyro_range_dps": 2000,
        })
        self.assertTrue(self.waiting.wait(3))

    def test_scan_results_and_immediate_request_after_start(self):
        self.scan.return_value = [
            SimpleNamespace(name="Ring", address="A", rssi=-61),
            SimpleNamespace(name=None, address="B", rssi=None),
        ]
        self.worker.scan()
        self.assertEqual(self.event("devices"), [
            {"name": "Ring", "address": "A", "rssi": -61},
            {"name": None, "address": "B", "rssi": None},
        ])
        self.scan.assert_awaited_once_with(timeout_s=8.0)
        self.assertTrue(self.worker._thread.daemon)

    def test_samples_single_consumer_and_duplicate_connection_rejected(self):
        self.connect()
        self.worker.connect("other-address")
        self.assertIn("already active", self.event("error"))
        self.worker.scan()
        self.assertIn("already active", self.event("error"))
        self.feed(SimpleNamespace(samples=[SimpleNamespace(
            accel_x=-32768, accel_y=32767, accel_z=3,
            gyro_x=4, gyro_y=-5, gyro_z=6,
        )]))
        self.assertEqual(self.event("samples"), [[-32768, 32767, 3, 4, -5, 6]])
        self.worker.disconnect()
        self.assertIsNone(self.event("disconnected"))
        self.assertEqual(self.max_active, 1)
        self.assertEqual(self.active, 0)
        self.assertEqual(self.enter_count, 1)
        self.scan.assert_not_awaited()
        self.assertTrue(self.exited.is_set())
        self.receive.assert_awaited_with(self.ring, timeout_s=5.0)

    def test_sdk_timeout_warns_and_receive_retries(self):
        self.connect()
        self.feed(sdk.TimeoutError("temporary timeout"))
        self.assertIn("temporary timeout", self.event("warning"))
        self.feed(SimpleNamespace(samples=[]))
        self.assertEqual(self.event("samples"), [])
        self.assertGreaterEqual(self.receive.await_count, 2)

    def test_timeout_after_disconnect_exits_instead_of_retrying(self):
        self.connect()
        def lose_connection():
            self.ring.is_connected = False
            self.source.put_nowait(sdk.TimeoutError("offline"))
        self.worker._loop.call_soon_threadsafe(lose_connection)
        self.event("warning")
        self.event("disconnected")
        self.assertEqual(self.receive.await_count, 1)

    def test_receive_failure_is_fatal_and_cleans_up(self):
        self.connect()
        self.feed(sdk.ProtocolError("bad packet"))
        self.assertIn("bad packet", self.event("error"))
        self.event("disconnected")
        self.assertTrue(self.exited.is_set())
        self.assertEqual(self.receive.await_count, 1)

    def test_builtin_timeout_is_not_sdk_retry_timeout(self):
        self.connect()
        self.feed(TimeoutError("not SDK timeout"))
        self.assertIn("not SDK timeout", self.event("error"))
        self.event("disconnected")
        self.assertEqual(self.receive.await_count, 1)

    def test_disconnect_cancels_in_progress_connection(self):
        self.connect_gate = asyncio.Event()
        self.worker.connect("manual")
        self.assertTrue(self.started.wait(3))
        self.worker.connect("duplicate")
        self.event("error")
        self.worker.disconnect()
        self.event("disconnected")
        self.assertTrue(self.exited.is_set())
        self.receive.assert_not_awaited()

    def test_close_cancels_connection_and_waits_for_cleanup(self):
        self.connect_gate = asyncio.Event()
        self.stop_gate = asyncio.Event()
        self.worker.connect("manual")
        self.assertTrue(self.started.wait(3))
        self.worker.disconnect()
        self.assertTrue(self.stopping.wait(3))
        future = self.worker.close()
        self.assertIs(future, self.worker.close())
        self.assertFalse(future.done())
        self.worker._loop.call_soon_threadsafe(self.stop_gate.set)
        future.result(timeout=3)
        self.assertTrue(self.exited.is_set())
        self.assertTrue(self.worker._loop.is_closed())
        self.event("disconnected")

    def test_close_active_receiver(self):
        self.connect()
        self.worker.close().result(timeout=3)
        self.assertTrue(self.exited.is_set())
        self.assertEqual(self.active, 0)
        self.event("disconnected")

    def test_close_cancels_scan_and_requests_do_not_accumulate(self):
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
        self.scan.assert_awaited_once()
        self.stream.assert_not_called()

    def test_scan_failure_and_invalid_address_are_events(self):
        self.scan.side_effect = sdk.TransportError("adapter unavailable")
        self.worker.scan()
        self.assertIn("adapter unavailable", self.event("error"))
        self.worker.connect("  ")
        self.assertIn("address", self.event("error"))
        self.event("disconnected")
        self.stream.assert_not_called()

    def test_cancel_before_connection_coroutine_starts_still_resets_ui(self):
        blocked = threading.Event()
        release = threading.Event()
        def block_loop():
            blocked.set()
            release.wait(3)
        self.worker._loop.call_soon_threadsafe(block_loop)
        self.assertTrue(blocked.wait(3))
        try:
            self.worker.connect("manual")
            self.worker.disconnect()
        finally:
            release.set()
        self.event("disconnected")
        self.stream.assert_not_called()
        self.receive.assert_not_awaited()

    def test_connection_failure_resets_ui(self):
        @asynccontextmanager
        async def failed_stream(*args, **kwargs):
            raise sdk.TransportError("connect failed")
            yield  # Make this a context manager that fails on entry.
        self.stream.side_effect = failed_stream
        self.worker.connect("manual")
        self.assertIn("connect failed", self.event("error"))
        self.event("disconnected")
        self.receive.assert_not_awaited()

    def test_scan_sdk_timeout_is_warning(self):
        self.scan.side_effect = sdk.TimeoutError("scan timeout")
        self.worker.scan()
        self.assertIn("scan timeout", self.event("warning"))
        self.scan.assert_awaited_once()

    def test_close_before_start_and_requests_after_close_are_safe(self):
        worker = RingWorker(lambda *args: self.fail("Unexpected callback"))
        result = worker.close()
        self.assertIs(result, worker.close())
        self.assertIsNone(result.result(timeout=3))
        worker.start()
        worker.connect("address")
        worker.scan()
        worker.disconnect()
        self.assertIsNone(worker._thread)


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
