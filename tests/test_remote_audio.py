# SPDX-License-Identifier: MPL-2.0
"""Remote recording contracts, using the installed SDK's public value types."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import wave

import openzilo as sdk

from hmm_gesture_studio import remote_audio


S = sdk.AudioRecordingState


def status(session=41, state=S.COMPLETE, saved=True, index=7):
    return sdk.AudioRecordingStatus(session, state, saved, 1 if state == S.FAILED else 0,
                                    index if saved else None)


IDLE = status(0, S.IDLE, False)
ACTIVE = status(41, S.CAPTURING, False)


class Stream:
    def __init__(self, events, trace):
        self.events = events
        self.trace = trace
        self.complete = False
        self.entered = False
        self.closed = False

    async def __aenter__(self):
        self.trace.append("subscribe")
        self.entered = True
        return self

    async def __aexit__(self, *args):
        self.closed = True
        self.trace.append("unsubscribe")

    async def __aiter__(self):
        for event in self.events:
            await asyncio.sleep(0)
            if isinstance(event, BaseException):
                raise event
            if isinstance(event, asyncio.Event):
                await event.wait()
                continue
            if isinstance(event, sdk.LiveAudioEnd):
                self.complete = True
                self.trace.append("live_end")
            yield event


class RemoteAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.trace = []
        self.events = []
        self.emit = lambda event, payload: self.events.append((event, payload))
        self.stop = asyncio.Event()
        self.started = asyncio.Event()
        self.waiting = asyncio.Event()
        self.stop_ack = asyncio.Event()
        self.finished = asyncio.Event()
        self.save = AsyncMock()
        self.ring = SimpleNamespace(is_connected=True, capabilities=sdk.DeviceCapabilities(
            "device", "test", "ring", features=tuple(
                sdk.FeatureVersion(name, 1, 0)
                for name in ("recording_control", "live_audio", "audio_files"))))

        def mock(name, **kwargs):
            result = AsyncMock(**kwargs)
            self.stack.enter_context(patch.object(sdk, name, result))
            return result

        self.query = mock("get_audio_recording_status", return_value=IDLE)
        self.get_config = mock("get_audio_recording_config", return_value=sdk.AudioRecordingConfig())
        self.set_config = mock("set_audio_recording_config", side_effect=lambda ring, config: config)

        async def start(*args, **kwargs):
            self.trace.append("start")
            self.started.set()
            return 41

        async def stop(*args, **kwargs):
            self.trace.append("stop")
            self.stop_ack.set()
            return 41

        async def finish(*args, **kwargs):
            self.waiting.set()
            self.trace.append("complete")
            return status()

        self.start = mock("start_audio_recording", side_effect=start)
        self.stop_recording = mock("stop_audio_recording", side_effect=stop)
        self.wait_finished = mock("wait_audio_recording_finished", side_effect=finish)
        self.download = mock("download_audio_file", return_value=(sdk.AudioFileInfo(7, 123, 3), b"raw"))
        self.end_extract = mock("end_audio_extract")
        self.open_stream = self.stack.enter_context(patch.object(sdk, "open_live_audio_stream"))
        self.wav_builder = self.stack.enter_context(patch.object(
            sdk, "build_wav_from_pcm", wraps=sdk.build_wav_from_pcm))

    async def run_record(self, *, live=False, options=None):
        await remote_audio.record(self.ring, options or {}, live=live, stop=self.stop,
                                  emit=self.emit, save=self.save)

    async def held_finish(self, *args, **kwargs):
        self.waiting.set()
        await self.finished.wait()
        self.trace.append("complete")
        return status()

    def stream(self, channels=2, *, end=True, frames=1):
        pcm = b"\x01\x00" * (320 * channels)
        events = [sdk.LiveAudioStart(41, 123, 16000, channels, 3)]
        events += [sdk.LivePcmFrame(41, i * 320, 320, pcm, sdk.PcmConfig(16000, channels, 16))
                   for i in range(frames)]
        if end:
            events.append(sdk.LiveAudioEnd(0, 41, frames * 10, frames * 10, 0))
        stream = Stream(events, self.trace)
        self.open_stream.return_value = stream
        return stream, pcm * frames

    async def test_flash_waits_then_downloads_original_and_saves(self):
        await self.run_record(options={"channels": 1, "max_duration_ms": 1000})
        config = self.set_config.call_args.args[1]
        self.assertEqual(config.output, sdk.AudioOutput.FLASH)
        self.assertEqual(config.disconnect_policy, sdk.AudioDisconnectPolicy.STOP)
        self.assertEqual(config.channels, 1)
        self.assertEqual(self.download.call_args.args, (self.ring, 7))
        self.end_extract.assert_awaited_once()
        self.save.assert_awaited_once()
        data, index, metadata = self.save.call_args.args
        self.assertEqual((data, index, metadata["record_time"]), (b"raw", 7, 123))
        self.assertEqual(self.start.await_count, 1)
        self.stop_recording.assert_not_awaited()
        self.assertIn(("recording_config", dict(config.__dict__)), self.events)
        self.assertTrue(any(e == "recording_status" and isinstance(v, str) for e, v in self.events))
        self.assertTrue(any(e == "audio_state" and v["state"] == "recording" for e, v in self.events))

    async def test_start_timeout_recovers_with_one_query_and_never_retries(self):
        self.start.side_effect = sdk.TimeoutError("lost ACK")
        self.query.side_effect = [IDLE, ACTIVE]
        await self.run_record()
        self.assertEqual(self.start.await_count, 1)
        self.assertEqual(self.query.await_count, 2)  # preflight + exactly one recovery
        self.assertEqual(self.wait_finished.call_args.args, (self.ring, 41))
        self.save.assert_awaited_once()

    async def test_start_timeout_can_recover_already_complete_new_session(self):
        self.start.side_effect = asyncio.TimeoutError()
        self.query.side_effect = [status(40), status(41)]
        await self.run_record()
        self.assertEqual(self.query.await_count, 2)
        self.save.assert_awaited_once()

    async def test_timeout_rejects_idle_or_unchanged_session(self):
        for previous, recovered in ((IDLE, IDLE), (status(), status())):
            with self.subTest(recovered=recovered):
                self.query.side_effect = [previous, recovered]
                self.start.side_effect = sdk.TimeoutError("lost ACK")
                with self.assertRaises(sdk.ProtocolError):
                    await self.run_record()
                self.stop_recording.assert_not_awaited()
                self.save.assert_not_awaited()
        self.assertEqual(self.start.await_count, 2)
        self.assertEqual(self.query.await_count, 4)

    async def test_busy_preflight_never_sets_or_starts(self):
        self.query.return_value = ACTIVE
        with self.assertRaises(sdk.ProtocolError):
            await self.run_record()
        self.set_config.assert_not_awaited()
        self.start.assert_not_awaited()
        self.stop_recording.assert_not_awaited()

    async def test_stop_ack_does_not_save_until_terminal_confirmation(self):
        self.wait_finished.side_effect = self.held_finish
        task = asyncio.create_task(self.run_record())
        await self.waiting.wait()
        self.stop.set()
        await self.stop_ack.wait()
        self.save.assert_not_awaited()
        self.download.assert_not_awaited()
        self.assertFalse(task.done())
        self.finished.set()
        await task
        self.stop_recording.assert_awaited_once()
        self.assertEqual(self.stop_recording.call_args.args, (self.ring, 41))
        self.save.assert_awaited_once()

    async def test_live_subscribes_before_start_and_builds_real_wav_after_end(self):
        for channels in (1, 2):
            with self.subTest(channels=channels):
                stream, pcm = self.stream(channels)
                self.trace.clear()
                self.save.reset_mock()
                await self.run_record(live=True, options={"channels": channels})
                self.assertLess(self.trace.index("subscribe"), self.trace.index("start"))
                self.assertTrue(stream.closed)
                self.assertEqual(self.open_stream.call_args.kwargs["output"], "pcm")
                config = self.set_config.call_args.args[1]
                self.assertEqual(config.output, sdk.AudioOutput.LIVE_AND_FLASH)
                self.assertEqual(config.disconnect_policy, sdk.AudioDisconnectPolicy.STOP)
                data, index, metadata = self.save.call_args.args
                self.assertEqual((index, metadata["record_time"]), (7, 123))
                with wave.open(io.BytesIO(data)) as wav:
                    self.assertEqual((wav.getnchannels(), wav.getframerate(), wav.getsampwidth()),
                                     (channels, 16000, 2))
                    self.assertEqual(wav.readframes(wav.getnframes()), pcm)
        self.download.assert_not_awaited()
        self.assertEqual(self.wav_builder.call_count, 2)

    async def test_live_stop_still_requires_end_and_saved_status(self):
        stream, _ = self.stream()
        gate = asyncio.Event()
        stream.events.insert(-1, gate)
        self.wait_finished.side_effect = self.held_finish
        task = asyncio.create_task(self.run_record(live=True))
        await self.waiting.wait()
        self.stop.set()
        await self.stop_ack.wait()
        self.finished.set()
        await asyncio.sleep(0)
        self.save.assert_not_awaited()
        self.wav_builder.assert_not_called()
        gate.set()
        await task
        self.stop_recording.assert_awaited_once()
        self.save.assert_awaited_once()

    async def test_live_progress_is_throttled_and_final_count_is_emitted(self):
        _, pcm = self.stream(frames=200)
        await self.run_record(live=True)
        progress = [v for e, v in self.events if e == "audio_progress"]
        self.assertLess(len(progress), 20)
        self.assertEqual(progress[-1], {"bytes": len(pcm), "total": None})

    async def test_missing_end_never_builds_or_saves_wav(self):
        stream, _ = self.stream(end=False)
        with self.assertRaises(sdk.ProtocolError):
            await self.run_record(live=True)
        self.assertTrue(stream.closed)
        self.wav_builder.assert_not_called()
        self.save.assert_not_awaited()

    async def test_subscription_failure_never_starts(self):
        self.open_stream.return_value = SimpleNamespace()  # invalid async context
        with self.assertRaises(TypeError):
            await self.run_record(live=True)
        self.start.assert_not_awaited()
        self.save.assert_not_awaited()

    async def test_failed_cancelled_unsaved_or_replaced_status_never_saves(self):
        for result in (status(state=S.FAILED), status(state=S.CANCELLED, saved=False),
                       status(saved=False), status(session=99)):
            with self.subTest(result=result):
                self.wait_finished.side_effect = None
                self.wait_finished.return_value = result
                with self.assertRaises(sdk.ProtocolError):
                    await self.run_record()
                self.save.assert_not_awaited()
                self.download.assert_not_awaited()

    async def test_live_other_session_never_saves(self):
        stream, _ = self.stream()
        stream.events[0] = sdk.LiveAudioStart(99, 123, 16000, 2, 3)
        with self.assertRaises(sdk.ProtocolError):
            await self.run_record(live=True)
        self.save.assert_not_awaited()

    async def test_cancel_stops_owned_session_and_joins_children(self):
        self.query.side_effect = [IDLE, ACTIVE]
        self.wait_finished.side_effect = self.held_finish
        task = asyncio.create_task(self.run_record())
        await self.waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.stop_recording.assert_awaited_once()
        self.save.assert_not_awaited()
        self.assertFalse([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()])

    async def test_cancel_is_preserved_when_sdk_completion_swallows_cancel(self):
        async def swallow(*args, **kwargs):
            self.waiting.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return status()

        self.wait_finished.side_effect = swallow
        self.query.side_effect = [IDLE, ACTIVE]
        task = asyncio.create_task(self.run_record())
        await self.waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.save.assert_not_awaited()
        self.download.assert_not_awaited()

    async def test_cancel_during_start_retains_ack_for_cleanup(self):
        gate = asyncio.Event()

        async def delayed_start(*args, **kwargs):
            self.started.set()
            await gate.wait()
            return 41

        self.start.side_effect = delayed_start
        self.query.side_effect = [IDLE, ACTIVE]
        task = asyncio.create_task(self.run_record())
        await self.started.wait()
        task.cancel()
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.stop_recording.assert_awaited_once()
        self.save.assert_not_awaited()

    async def test_cancel_live_closes_stream_without_saving(self):
        stream, _ = self.stream(end=False)
        stream.events.append(asyncio.Event())
        self.query.side_effect = [IDLE, ACTIVE]
        self.wait_finished.side_effect = self.held_finish
        task = asyncio.create_task(self.run_record(live=True))
        await self.waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stream.closed)
        self.stop_recording.assert_awaited_once()
        self.wav_builder.assert_not_called()
        self.save.assert_not_awaited()

    async def test_cleanup_does_not_stop_replaced_session(self):
        self.query.side_effect = [IDLE, status(99, S.CAPTURING, False)]
        self.wait_finished.side_effect = sdk.TransportError("lost connection")
        with self.assertRaises(sdk.TransportError):
            await self.run_record()
        self.stop_recording.assert_not_awaited()

    async def test_stop_failure_is_not_retried_in_cleanup(self):
        self.stop.set()
        self.wait_finished.side_effect = self.held_finish
        self.stop_recording.side_effect = sdk.TimeoutError("stop ACK lost")
        self.query.side_effect = [IDLE, ACTIVE]
        with self.assertRaises(sdk.TimeoutError):
            await self.run_record()
        self.stop_recording.assert_awaited_once()
        self.save.assert_not_awaited()

    async def test_download_failure_still_sends_end(self):
        self.download.side_effect = sdk.TransportError("download failed")
        with self.assertRaisesRegex(sdk.TransportError, "download failed"):
            await self.run_record()
        self.end_extract.assert_awaited_once()
        self.assertEqual(self.end_extract.call_args.args, (self.ring, 7))
        self.save.assert_not_awaited()

    async def test_download_cancellation_sends_end_without_swallowing_cancel(self):
        entered = asyncio.Event()

        async def download(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.download.side_effect = download
        task = asyncio.create_task(self.run_record())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.end_extract.assert_awaited_once()
        self.save.assert_not_awaited()

    async def test_download_swallowing_cancel_cannot_turn_it_into_a_save(self):
        entered = asyncio.Event()

        async def swallow(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return sdk.AudioFileInfo(7, 123, 3), b"raw"

        self.download.side_effect = swallow
        task = asyncio.create_task(self.run_record())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.end_extract.assert_awaited_once()
        self.save.assert_not_awaited()

    async def test_invalid_options_rejected_before_device_mutation(self):
        for options in ({"output": 2}, {"disconnect_policy": 1}, {"sample_rate": 8000},
                        {"unknown": 1}, {"channels": True}, {"channels": 3},
                        {"bitrate": 7999}, {"gain_left_q8": -1}, {"gain_right_q8": 1025},
                        {"max_duration_ms": 21}):
            with self.subTest(options=options), self.assertRaises((ValueError, TypeError)):
                await self.run_record(options=options)
        self.set_config.assert_not_awaited()
        self.start.assert_not_awaited()

    async def test_device_capabilities_are_required(self):
        self.ring.capabilities = sdk.DeviceCapabilities("unknown", "test", "ring")
        with self.assertRaises(sdk.CompatibilityError):
            await self.run_record()
        self.start.assert_not_awaited()

    async def test_missing_sdk_api_is_actionable_and_precedes_start(self):
        with patch.object(sdk, "open_live_audio_stream", None):
            with self.assertRaisesRegex(RuntimeError, "SDK|sdk|openzilo"):
                await self.run_record(live=True)
        self.start.assert_not_awaited()

    async def test_recovery_query_failure_is_not_retried(self):
        self.start.side_effect = sdk.TimeoutError("start timeout")
        self.query.side_effect = [IDLE, sdk.TimeoutError("status timeout")]
        with self.assertRaisesRegex(sdk.TimeoutError, "status timeout"):
            await self.run_record()
        self.assertEqual(self.start.await_count, 1)
        self.assertEqual(self.query.await_count, 2)
        self.stop_recording.assert_not_awaited()
        self.save.assert_not_awaited()

    async def test_corrupt_end_and_stream_failure_cannot_save(self):
        for end in (sdk.LiveAudioEnd(1, 41, 10, 10, 0),
                    sdk.LiveAudioEnd(0, 41, 10, 9, 0),
                    sdk.LiveAudioEnd(0, 41, 10, 10, 1),
                    sdk.TransportError("disconnected")):
            with self.subTest(end=end):
                stream, _ = self.stream()
                stream.events[-1] = end
                with self.assertRaises((sdk.ProtocolError, sdk.TransportError)):
                    await self.run_record(live=True)
                self.assertTrue(stream.closed)
                self.wav_builder.assert_not_called()
                self.save.assert_not_awaited()

    async def test_live_failure_stops_owned_session_before_unsubscribe(self):
        stream, _ = self.stream(end=False)
        self.query.side_effect = [IDLE, ACTIVE]
        self.wait_finished.side_effect = self.held_finish
        with self.assertRaises(sdk.ProtocolError):
            await self.run_record(live=True)
        self.stop_recording.assert_awaited_once()
        self.assertLess(self.trace.index("stop"), self.trace.index("unsubscribe"))
        self.save.assert_not_awaited()

    async def test_repeated_cancel_waits_for_cleanup_and_leaves_no_children(self):
        cleaning = asyncio.Event()
        release = asyncio.Event()

        async def query(*args, **kwargs):
            if not self.started.is_set():
                return IDLE
            cleaning.set()
            await release.wait()
            return ACTIVE

        self.query.side_effect = query
        self.wait_finished.side_effect = self.held_finish
        task = asyncio.create_task(self.run_record())
        await self.waiting.wait()
        task.cancel()
        await cleaning.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.stop_recording.assert_awaited_once()
        self.save.assert_not_awaited()
        self.assertFalse([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()])

    async def test_cleanup_error_does_not_mask_original_error(self):
        self.query.side_effect = [IDLE, sdk.TimeoutError("cleanup timeout")]
        self.wait_finished.side_effect = sdk.ProtocolError("original failure")
        with self.assertRaisesRegex(sdk.ProtocolError, "original failure"):
            await self.run_record()
        self.save.assert_not_awaited()

    async def test_cancel_during_config_does_not_proceed_to_start(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        async def configure(ring, config):
            entered.set()
            await release.wait()
            return config

        self.set_config.side_effect = configure
        task = asyncio.create_task(self.run_record())
        await entered.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.start.assert_not_awaited()
        self.save.assert_not_awaited()

    async def test_zero_is_a_valid_saved_file_index(self):
        self.wait_finished.side_effect = None
        self.wait_finished.return_value = status(index=0)
        self.download.return_value = sdk.AudioFileInfo(0, 123, 3), b"raw"
        await self.run_record()
        self.assertEqual(self.save.call_args.args[1], 0)

    async def test_wrong_download_metadata_still_ends_without_saving(self):
        self.download.return_value = sdk.AudioFileInfo(99, 123, 3), b"raw"
        with self.assertRaises(sdk.ProtocolError):
            await self.run_record()
        self.end_extract.assert_awaited_once()
        self.save.assert_not_awaited()

    async def test_live_requires_live_capability_before_config_or_start(self):
        self.ring.capabilities = sdk.DeviceCapabilities("device", "test", "ring", features=(
            sdk.FeatureVersion("recording_control", 1, 0),))
        with self.assertRaises(sdk.CompatibilityError):
            await self.run_record(live=True)
        self.set_config.assert_not_awaited()
        self.start.assert_not_awaited()

    async def test_save_failure_is_propagated_without_second_save_or_stop(self):
        self.save.side_effect = OSError("disk full")
        with self.assertRaisesRegex(OSError, "disk full"):
            await self.run_record()
        self.save.assert_awaited_once()
        self.stop_recording.assert_not_awaited()

    async def test_read_config_reports_active_live_state(self):
        self.get_config.return_value = sdk.AudioRecordingConfig(output=sdk.AudioOutput.LIVE_AND_FLASH)
        self.query.return_value = ACTIVE
        await remote_audio.read_config(self.ring, self.emit)
        self.assertTrue(any(e == "audio_state" and p["state"] == "live" for e, p in self.events))
        self.start.assert_not_awaited()

    async def test_read_config_emits_dict_and_text_without_mutating(self):
        await remote_audio.read_config(self.ring, self.emit)
        self.assertIn(("recording_config", dict(sdk.AudioRecordingConfig().__dict__)), self.events)
        statuses = [v for e, v in self.events if e == "recording_status"]
        self.assertEqual(len(statuses), 1)
        self.assertIsInstance(statuses[0], str)
        self.set_config.assert_not_awaited()
        self.start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
