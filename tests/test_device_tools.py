# SPDX-License-Identifier: MPL-2.0
"""Worker-level ownership tests for maintenance and new audio services."""
import asyncio
from collections import defaultdict
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import openzilo as sdk
from hmm_gesture_studio.device import RingWorker, _StudioClient
from hmm_gesture_studio.wire import AudioCommand


class DeviceToolsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.events = []
        self.worker = RingWorker(lambda *item: self.events.append(item))
        self.worker._wake = asyncio.Event()
        self.worker._telemetry_lock = asyncio.Lock()
        self.worker._address = 'test-device'
        self.ring = SimpleNamespace(is_connected=True, _drain_queue=Mock(),
                                    _queues=defaultdict(asyncio.Queue),
                                    capabilities=SimpleNamespace(supports=lambda _: True))
        self.worker._ring = self.ring
        self.started = asyncio.Event()
        self.start = self.stack.enter_context(patch.object(sdk, 'start_sensor_report',
            new_callable=AsyncMock, return_value=sdk.SensorStartInfo(100, 8, 2000)))
        self.stop = self.stack.enter_context(patch.object(sdk, 'stop_sensor_report', new_callable=AsyncMock))

    async def finish(self, task):
        self.worker._cancel_task(task)
        await asyncio.gather(task, return_exceptions=True)

    async def test_maintenance_stops_raw_stream_and_cancels_child_before_resume(self):
        release_sample = asyncio.Event()
        cleaned = asyncio.Event()
        entered = asyncio.Event()
        async def receive(*args, **kwargs):
            self.started.set()
            await release_sample.wait()
            return sdk.SensorDataBatch(0, 1, 16, (sdk.SensorDataSample(1, 1, 2, 3, 4, 5, 6),))
        self.stack.enter_context(patch.object(sdk, 'wait_sensor_data', side_effect=receive))
        async def tool(*args):
            self.stop.assert_awaited_once()
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        self.stack.enter_context(patch.object(self.worker, '_run_tool', side_effect=tool))
        owner = asyncio.create_task(self.worker._own_stream(self.ring))
        self.addAsyncCleanup(self.finish, owner)
        await asyncio.wait_for(self.started.wait(), 1)
        self.worker._accept_tool('calibrating', 'quick')
        release_sample.set()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(any(name == 'samples' for name, _ in self.events))
        self.worker._stop_tool()
        await asyncio.wait_for(cleaned.wait(), 1)
        for _ in range(20):
            if self.start.await_count > 1:
                break
            await asyncio.sleep(0)
        self.assertGreater(self.start.await_count, 1)
        self.assertIsNone(self.worker._tool_kind)

    async def test_ota_waits_for_inflight_telemetry_and_ends_owner_without_restarting(self):
        await self.worker._telemetry_lock.acquire()
        run = self.stack.enter_context(patch.object(self.worker, '_run_tool', new_callable=AsyncMock))
        self.worker._accept_tool('firmware', 'verified-snapshot')
        owner = asyncio.create_task(self.worker._own_stream(self.ring))
        self.addAsyncCleanup(self.finish, owner)
        await asyncio.sleep(0)
        run.assert_not_awaited()
        self.worker._telemetry_lock.release()
        await asyncio.wait_for(owner, 1)
        run.assert_awaited_once_with(self.ring, 'firmware', 'verified-snapshot')
        self.start.assert_not_awaited()
        # Reservation remains until session disconnect cleanup, not just upload.
        self.assertEqual(self.worker._tool_kind, 'firmware')
        with self.assertRaises(RuntimeError):
            self.worker.disconnect()
        with self.assertRaises(RuntimeError):
            self.worker.close()

    async def test_ota_rejects_external_recording_before_installing(self):
        from hmm_gesture_studio import firmware
        with patch.object(sdk, 'get_audio_recording_status', new_callable=AsyncMock,
                          return_value=SimpleNamespace(state=sdk.AudioRecordingState.CAPTURING)), \
             patch.object(firmware, 'install_firmware', new_callable=AsyncMock) as install:
            with self.assertRaisesRegex(ValueError, 'Stop recording'):
                await self.worker._run_tool(self.ring, 'firmware', 'bundle')
            install.assert_not_awaited()

    async def test_remote_recording_stop_event_and_save_callback_are_wired(self):
        from hmm_gesture_studio import remote_audio
        saved = self.stack.enter_context(patch.object(self.worker, '_save', new_callable=AsyncMock))
        async def record(ring, options, *, live, stop, emit, save):
            self.assertTrue(live)
            self.assertEqual(options, {'channels': 2})
            self.worker._finish_recording()
            self.assertTrue(stop.is_set())
            await save(b'wav-data', 5, {'record_time': 123})
        with patch.object(remote_audio, 'record', side_effect=record):
            await self.worker._run_audio(self.ring, 'live', {'channels': 2}, 'audio-dir')
        saved.assert_awaited_once_with(b'wav-data', 5, 'audio-dir', {'record_time': 123})
        self.assertIsNone(self.worker._recording_stop)

    async def test_extraction_observer_uses_network_byte_order(self):
        ring = _StudioClient(transport=SimpleNamespace())
        body = sdk.BinaryWriter().u16(0).u32(0x01020304).build()
        with patch.object(sdk.OpenZiloClient, 'request', new_callable=AsyncMock):
            await ring.request(AudioCommand.START_EXTRACT, 0x0504, body)
            self.assertEqual(ring.audio_extract_index, 0x01020304)
            await ring.request(AudioCommand.END_EXTRACT, 0x0508)
            self.assertIsNone(ring.audio_extract_index)
