# SPDX-License-Identifier: MPL-2.0
"""SDK 0.6.2 transport-level audio/IMU demultiplexing regression tests.

Only the BLE transport is fake: Studio's client, SDK framing/CRC, command
queues, live-audio inbox, parsers and unsubscribe cleanup are real. Private
protocol imports are deliberate fixtures for the pinned SDK, not app APIs.
These tests establish multiplexing support, not RingWorker scheduling or
hardware behavior. Opus output avoids decoders, microphones and playback.
"""
from __future__ import annotations

import asyncio
from importlib.metadata import version
import struct
import unittest

import openzilo as sdk
from openzilo._protocol import AudioCommand, SensorCommand, decode_packet, encode_packet

from hmm_gesture_studio.device import _StudioClient


TIMEOUT = 2.0
SESSION = 41
RECORD_TIME = 123
# Synthetic 20-ms stereo TOC (one frame); only the SDK's Opus wire validator
# runs. This fixture makes no claim about decoded audio quality.
OPUS = b"\xfc\xff\xfe"


def imu_packet(sequence):
    # Successful batch: sequence, count=1, sample size=16, timestamp + six i16.
    return (SensorCommand.DATA_FRAME,
            struct.pack(">HIHHI6h", 0, sequence, 1, 16, 1000 + sequence * 10,
                        sequence, -2, 3, -4, 5, -6))


def live_start():
    return (AudioCommand.LIVE_START,
            struct.pack(">IIIIHBB", SESSION, 0, RECORD_TIME, 0, 16000, 2, 3))


def live_data(sequence):
    return (AudioCommand.LIVE_DATA,
            struct.pack(">IIIH", SESSION, sequence, sequence * len(OPUS), len(OPUS))
            + OPUS)


def live_end(frame_count):
    size = frame_count * len(OPUS)
    return (AudioCommand.LIVE_END, struct.pack(">HIIIII", 0, SESSION, 0, size, size, 0))


class SimulatedTransport:
    """Strict, in-memory NUS boundary; unexpected outbound commands fail."""

    def __init__(self):
        self.is_connected = False
        self.rx = None
        self.writes = []
        self.imu_on_unsubscribe = None

    def set_rx_callback(self, callback):
        self.rx = callback

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    def notify(self, *packets):
        if not self.is_connected or self.rx is None:
            raise AssertionError("Notifications require a connected fake transport")
        wire = b"".join(encode_packet(command, body) for command, body in packets)
        # Fragment headers and bodies; also place multiple commands in a chunk.
        # No direct queue insertion or calls to the SDK's private dispatcher.
        for chunk in (wire[:3], wire[3:17], wire[17:]):
            if chunk:
                self.rx(chunk)

    async def write(self, data):
        packet = decode_packet(data)
        self.writes.append((packet.command, packet.body))
        if packet.command == SensorCommand.START_REPORT and not packet.body:
            self.notify((SensorCommand.START_REPORT_RESP,
                         struct.pack(">HHHH", 0, 100, 8, 2000)))
        elif packet.command == SensorCommand.STOP_REPORT and not packet.body:
            self.notify((SensorCommand.STOP_REPORT_RESP, struct.pack(">H", 0)))
        elif (packet.command == AudioCommand.LIVE_SUBSCRIBE
              and packet.body in (b"\x00", b"\x01")):
            if packet.body == b"\x00" and self.imu_on_unsubscribe is not None:
                # A real IMU notification arriving during audio cleanup, before
                # its response: request() must drain only the audio ACK queue.
                self.notify(imu_packet(self.imu_on_unsubscribe))
            self.notify((AudioCommand.LIVE_SUBSCRIBE_RESP, b"\x00\x00" + packet.body))
        else:
            raise AssertionError(f"Unexpected device command: {packet!r}")


class AudioImuMultiplexTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertEqual(version("openzilo"), "0.6.2",
                         "Review wire fixtures when changing the pinned SDK")
        self.transport = SimulatedTransport()
        self.ring = _StudioClient(transport=self.transport, command_timeout_s=TIMEOUT)
        await self.ring.connect()
        self.addAsyncCleanup(self.ring.disconnect)
        info = await sdk.start_sensor_report(self.ring, timeout_s=TIMEOUT)
        self.assertEqual(info, sdk.SensorStartInfo(100, 8, 2000))

    def task(self, coroutine):
        task = asyncio.create_task(coroutine)

        async def cleanup():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        # LIFO cleanup joins consumers before disconnecting their transport.
        self.addAsyncCleanup(cleanup)
        return task

    async def bounded(self, awaitable):
        return await asyncio.wait_for(awaitable, TIMEOUT)

    async def assert_imu(self, sequence, pending=None):
        batch = await self.bounded(
            pending if pending is not None
            else sdk.wait_sensor_data(self.ring, timeout_s=TIMEOUT))
        self.assertEqual(batch, sdk.SensorDataBatch(
            sequence, 1, 16,
            (sdk.SensorDataSample(1000 + sequence * 10, sequence, -2, 3, -4, 5, -6),),
        ))

    async def assert_audio_only_cleanup(self):
        # Exact traffic rules out hidden IMU STOP/restart, recording commands,
        # and duplicate audio subscribe/unsubscribe writes.
        self.assertEqual(self.transport.writes, [
            (SensorCommand.START_REPORT, b""),
            (AudioCommand.LIVE_SUBSCRIBE, b"\x01"),
            (AudioCommand.LIVE_SUBSCRIBE, b"\x00"),
        ])
        self.assertTrue(self.ring.is_connected)
        await sdk.stop_sensor_report(self.ring, timeout_s=TIMEOUT)
        self.assertEqual(self.transport.writes[-1], (SensorCommand.STOP_REPORT, b""))

    async def test_interleaved_live_end_preserves_imu_during_and_after_cleanup(self):
        ready = asyncio.Event()
        delivered = asyncio.Queue()
        streams = []

        async def receive_audio():
            async with sdk.open_live_audio_stream(
                self.ring, output="opus", timeout_s=TIMEOUT,
            ) as stream:
                streams.append(stream)
                ready.set()
                async for event in stream:
                    delivered.put_nowait(event)

        audio = self.task(receive_audio())
        await self.bounded(ready.wait())
        for sequence, audio_packet, expected in (
            (0, live_start(), sdk.LiveAudioStart(SESSION, RECORD_TIME, 16000, 2, 3)),
            (1, live_data(0), sdk.LiveAudioData(SESSION, 0, 0, OPUS)),
            (2, live_data(1), sdk.LiveAudioData(SESSION, 1, len(OPUS), OPUS)),
        ):
            imu = self.task(sdk.wait_sensor_data(self.ring, timeout_s=TIMEOUT))
            self.transport.notify(imu_packet(sequence), audio_packet)
            await self.assert_imu(sequence, imu)
            self.assertEqual(await self.bounded(delivered.get()), expected)
            self.assertFalse(audio.done(), "IMU must be delivered while audio is active")

        # Leave IMU buffered across END and unsubscribe, with no IMU consumer
        # available to hide an erroneous global drain during audio cleanup.
        self.transport.imu_on_unsubscribe = 4
        self.transport.notify(imu_packet(3), live_end(2))
        await self.bounded(audio)
        self.assertEqual(await self.bounded(delivered.get()),
                         sdk.LiveAudioEnd(0, SESSION, 6, 6, 0))
        self.assertTrue(delivered.empty())
        self.assertTrue(streams[0].complete)
        self.transport.notify(imu_packet(5))
        for sequence in (3, 4, 5):
            await self.assert_imu(sequence)
        await self.assert_audio_only_cleanup()

    async def test_cancelling_live_consumer_does_not_drain_or_cancel_imu(self):
        ready = asyncio.Event()
        delivered = asyncio.Queue()
        streams = []

        async def receive_audio():
            async with sdk.open_live_audio_stream(
                self.ring, output="opus", timeout_s=TIMEOUT,
            ) as stream:
                streams.append(stream)
                ready.set()
                async for event in stream:
                    delivered.put_nowait(event)

        audio = self.task(receive_audio())
        await self.bounded(ready.wait())
        imu = self.task(sdk.wait_sensor_data(self.ring, timeout_s=TIMEOUT))
        self.transport.notify(live_start(), imu_packet(0), live_data(0))
        await self.assert_imu(0, imu)
        self.assertEqual(await self.bounded(delivered.get()),
                         sdk.LiveAudioStart(SESSION, RECORD_TIME, 16000, 2, 3))
        self.assertEqual(await self.bounded(delivered.get()),
                         sdk.LiveAudioData(SESSION, 0, 0, OPUS))

        # No END: cancel the consumer while an independent IMU wait is pending.
        # Its next packet arrives inside the unsubscribe write, not beforehand.
        imu = self.task(sdk.wait_sensor_data(self.ring, timeout_s=TIMEOUT))
        self.transport.imu_on_unsubscribe = 1
        audio.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.bounded(audio)
        await self.assert_imu(1, imu)
        self.assertFalse(streams[0].complete)
        self.assertTrue(delivered.empty())
        self.transport.notify(imu_packet(2), imu_packet(3))
        await self.assert_imu(2)
        await self.assert_imu(3)
        await self.assert_audio_only_cleanup()


if __name__ == "__main__":
    unittest.main()
