# SPDX-License-Identifier: MPL-2.0
"""Regressions against the installed SDK, without BLE hardware."""
import asyncio
from collections import defaultdict
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from hmm_gesture_studio import device
from hmm_gesture_studio.wire import AudioCommand


class UpgradeTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_write_uses_sdk_lock_without_reentrant_deadlock(self):
        transport = SimpleNamespace(write=AsyncMock())
        ring = device._StudioClient(transport=transport)
        await asyncio.wait_for(ring.send_command(1), 0.2)
        transport.write.assert_awaited_once()

    async def test_saved_notification_wakes_auto_receiver(self):
        worker = device.RingWorker(lambda *_: None)
        worker._wake = asyncio.Event()
        worker._audio_armed = True
        worker._audio_directory = 'recordings'
        ring = SimpleNamespace(is_connected=True, _queues=defaultdict(asyncio.Queue),
                               _drain_queue=Mock())
        ring._queues[int(AudioCommand.RECORD_RESULT)].put_nowait(object())
        task = asyncio.create_task(worker._housekeeping(ring))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(worker._audio_pending, ('listen', None, 'recordings'))

    async def test_firmware_exclusive_session_does_not_treat_reboot_as_transport_loss(self):
        worker = device.RingWorker(lambda *_: None)
        worker._tool_kind = 'firmware'
        ring = SimpleNamespace(is_connected=False, _queues=defaultdict(asyncio.Queue),
                               _drain_queue=Mock())
        task = asyncio.create_task(worker._housekeeping(ring))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ring._drain_queue.assert_not_called()

    async def test_tool_reservation_rejects_audio_and_cancellation_does_not_run_it(self):
        events = []
        worker = device.RingWorker(lambda *item: events.append(item))
        worker._ring = SimpleNamespace(is_connected=True)
        worker._wake = asyncio.Event()
        worker._accept_tool('calibrating', 'quick')
        worker._accept_audio('listen', None, 'out')
        self.assertFalse(worker._audio_armed)
        self.assertTrue(any(event == 'error' for event, _ in events))
        worker._stop_tool()
        self.assertIsNone(worker._tool_pending)
        self.assertIsNone(worker._tool_kind)

    async def test_profile_operation_requires_connected_session(self):
        events = []
        worker = device.RingWorker(lambda *item: events.append(item))
        worker._accept_tool('profile', None)
        self.assertIsNone(worker._tool_pending)
        self.assertTrue(any(event == 'error' for event, _ in events))
