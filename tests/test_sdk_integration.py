# SPDX-License-Identifier: MPL-2.0
"""Exercise the installed SDK's public types without opening a BLE adapter."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import openzilo as sdk

import recognize
import record_gesture
from ring_stream import open_ring_stream


def batch(value: int, count: int = 12) -> sdk.SensorDataBatch:
    samples = tuple(
        sdk.SensorDataSample(i * 40, value, value + 1, value + 2,
                             value + 3, value + 4, value + 5)
        for i in range(count)
    )
    return sdk.SensorDataBatch(0, count, 16, samples)


class FakeRing:
    is_connected = False

    async def __aenter__(self):
        self.is_connected = True
        return self

    async def __aexit__(self, *args):
        self.is_connected = False


class StreamTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.ring = FakeRing()
        self.client = self.stack.enter_context(
            patch("ring_stream.sdk.OpenZiloClient", return_value=self.ring)
        )
        self.info = self.stack.enter_context(patch(
            "ring_stream.sdk.get_system_info", new_callable=AsyncMock,
            return_value=sdk.SystemInfo("test-firmware", 0, 0, 0, 80, False,
                                       "test-sn", "test-cpuid", "test-ring"),
        ))
        self.start = self.stack.enter_context(patch(
            "ring_stream.sdk.start_sensor_report", new_callable=AsyncMock,
            return_value=sdk.SensorStartInfo(25, 8, 2000),
        ))
        self.stop = self.stack.enter_context(patch(
            "ring_stream.sdk.stop_sensor_report", new_callable=AsyncMock,
            return_value=sdk.SensorStopInfo(),
        ))


class TestStreamLifecycle(StreamTestCase):
    async def test_official_client_and_cleanup(self):
        async with open_ring_stream("test-uuid") as (ring, start):
            self.assertIs(ring, self.ring)
            self.assertEqual(start.sample_rate_hz, 25)
            self.assertTrue(ring.is_connected)
        self.client.assert_called_once_with(address="test-uuid")
        self.start.assert_awaited_once_with(self.ring)
        self.stop.assert_awaited_once_with(self.ring)
        self.assertFalse(self.ring.is_connected)

    async def test_busy_is_not_treated_as_a_successful_mode_switch(self):
        self.start.side_effect = sdk.DeviceError(sdk.ErrorCode.DEVICE_BUSY)
        with self.assertRaisesRegex(sdk.DeviceError, "gesture mode"):
            async with open_ring_stream("test-uuid"):
                self.fail("A failed start must not yield a stream")
        self.stop.assert_not_awaited()
        self.assertFalse(self.ring.is_connected)

    async def test_other_device_errors_are_preserved(self):
        error = sdk.DeviceError(sdk.ErrorCode.CMD_NOT_EXIST)
        self.start.side_effect = error
        with self.assertRaises(sdk.DeviceError) as caught:
            async with open_ring_stream("test-uuid"):
                self.fail("A failed start must not yield a stream")
        self.assertIs(caught.exception, error)
        self.stop.assert_not_awaited()

    async def test_original_failure_survives_stop_failure(self):
        self.stop.side_effect = sdk.TimeoutError("stop timeout")
        with self.assertRaisesRegex(sdk.ProtocolError, "broken packet"):
            async with open_ring_stream("test-uuid"):
                raise sdk.ProtocolError("broken packet")
        self.stop.assert_awaited_once()
        self.assertFalse(self.ring.is_connected)

    async def test_cancel_stops_reporting(self):
        with self.assertRaises(asyncio.CancelledError):
            async with open_ring_stream("test-uuid"):
                raise asyncio.CancelledError()
        self.stop.assert_awaited_once()
        self.assertFalse(self.ring.is_connected)

    async def test_disconnected_ring_does_not_receive_stop(self):
        with self.assertRaises(sdk.TransportError):
            async with open_ring_stream("test-uuid") as (ring, _):
                ring.is_connected = False
                raise sdk.TransportError("disconnected")
        self.stop.assert_not_awaited()

    async def test_invalid_rate_still_stops_reporting(self):
        self.start.return_value = sdk.SensorStartInfo(0, 8, 2000)
        with self.assertRaisesRegex(ValueError, "sample rate"):
            async with open_ring_stream("test-uuid"):
                self.fail("Invalid rates must be rejected")
        self.stop.assert_awaited_once()


class BatchSource:
    """Queue whose join confirms the application's receiver consumed a batch."""

    def __init__(self):
        self.queue = asyncio.Queue()
        self.pending = False

    async def receive(self, *args, **kwargs):
        # A new read means the previous batch has been processed by the caller.
        if self.pending:
            self.queue.task_done()
            self.pending = False
        value = await self.queue.get()
        if isinstance(value, Exception):
            self.queue.task_done()
            raise value
        self.pending = True
        return value

    async def emit(self, value):
        await self.queue.put(value)
        await asyncio.wait_for(self.queue.join(), timeout=2)


class TestRecording(StreamTestCase):
    async def test_idle_data_discard_retry_and_device_sample_rate(self):
        self.start.return_value = sdk.SensorStartInfo(50, 8, 2000)
        source = BatchSource()
        takes = iter([batch(1, 4), batch(2, 12), batch(3, 16)])
        prompt_count = 0

        async def prompt(text, receiver):
            nonlocal prompt_count
            prompt_count += 1
            if "Enter to start" in text:
                await source.emit(sdk.TimeoutError("temporary timeout"))
                await source.emit(batch(999, 20))  # Idle data must not be saved.
            else:
                await source.emit(next(takes))
            return ""

        with tempfile.TemporaryDirectory() as directory:
            with patch("record_gesture.sdk.wait_sensor_data", side_effect=source.receive), \
                 patch("record_gesture._prompt_while_receiving", side_effect=prompt):
                path = await asyncio.wait_for(record_gesture.record_from_ring(
                    "test-uuid", "snap", 2, Path(directory)
                ), timeout=5)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(prompt_count, 6)  # Short take is retried, not counted.
        self.assertEqual(saved["sample_rate_hz"], 50)
        self.assertEqual(saved["num_repetitions"], 2)
        self.assertEqual([r["num_samples"] for r in saved["repetitions"]], [12, 16])
        self.assertEqual(saved["repetitions"][0]["data"][0], [2, 3, 4, 5, 6, 7])
        self.assertEqual(saved["repetitions"][1]["data"][0], [3, 4, 5, 6, 7, 8])
        self.stop.assert_awaited_once()

    async def test_fatal_stream_error_surfaces_while_waiting_for_input(self):
        release_input = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            try:
                with patch("builtins.input", side_effect=lambda _: release_input.wait(5)), \
                     patch("record_gesture.sdk.wait_sensor_data", new_callable=AsyncMock,
                           side_effect=sdk.TransportError("lost connection")):
                    with self.assertRaisesRegex(sdk.TransportError, "lost connection"):
                        await asyncio.wait_for(record_gesture.record_from_ring(
                            "test-uuid", "snap", 2, Path(directory)
                        ), timeout=2)
            finally:
                release_input.set()
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.stop.assert_awaited_once()

    async def test_repetition_validation_happens_before_connection(self):
        with self.assertRaisesRegex(ValueError, "two repetitions"):
            await record_gesture.record_from_ring("test-uuid", "snap", 1, Path("unused"))
        self.client.assert_not_called()


class TestAsyncPrompt(unittest.IsolatedAsyncioTestCase):
    async def test_input_does_not_block_event_loop(self):
        release_input = threading.Event()
        receiver = asyncio.create_task(asyncio.sleep(60))

        def read_input(_):
            if not release_input.wait(2):
                raise OSError("Event loop was blocked")
            return "answer"

        try:
            with patch("builtins.input", side_effect=read_input):
                prompt = asyncio.create_task(record_gesture._prompt_while_receiving(
                    "prompt", receiver
                ))
                await asyncio.sleep(0.01)
                release_input.set()
                self.assertEqual(await asyncio.wait_for(prompt, 2), "answer")
        finally:
            release_input.set()
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

    async def test_eof_is_reported(self):
        receiver = asyncio.create_task(asyncio.sleep(60))
        try:
            with patch("builtins.input", side_effect=EOFError):
                with self.assertRaises(EOFError):
                    await record_gesture._prompt_while_receiving("prompt", receiver)
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)


class TestRealtimeRecognition(StreamTestCase):
    def setUp(self):
        super().setUp()
        self.recognizer = Mock()
        self.recognizer._models = {"test": object()}
        self.recognizer.feed.return_value = ("test", 0.8)
        self.stack.enter_context(patch("recognize.HMMRecognizer", return_value=self.recognizer))

    async def test_timeout_retry_axis_order_and_transport_failure(self):
        with patch("recognize.sdk.wait_sensor_data", new_callable=AsyncMock,
                   side_effect=[sdk.TimeoutError("temporary"), batch(10, 1),
                                sdk.TransportError("disconnected")]):
            with self.assertRaises(sdk.TransportError):
                await recognize.run_realtime(Path("unused"), "test-uuid", 25, 10, 8, 4)
        self.recognizer.feed.assert_called_once_with([[10, 11, 12, 13, 14, 15]])
        self.stop.assert_awaited_once()

    async def test_device_rate_mismatch_is_rejected_and_stream_stopped(self):
        self.start.return_value = sdk.SensorStartInfo(50, 8, 2000)
        with patch("recognize.sdk.wait_sensor_data", new_callable=AsyncMock) as wait:
            with self.assertRaisesRegex(ValueError, "Device streams at 50 Hz"):
                await recognize.run_realtime(Path("unused"), "test-uuid", 25, 10, 8, 4)
        wait.assert_not_awaited()
        self.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
