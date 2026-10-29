# SPDX-License-Identifier: MPL-2.0
"""One remote recording on an already connected ring; no GUI, playback or files.

``emit(name, payload)`` is synchronous and must return promptly. ``save`` is an
async callback accepting (complete_bytes, file_index, metadata); the worker owns
its destination and shields/joins any file-writing thread. Exceptions propagate.
Setting ``stop`` finishes and saves; cancelling the task discards provisional PCM.
The caller must serialize recording operations on this connection.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

import openzilo as sdk


_OPTIONS = {"channels", "bitrate", "gain_left_q8", "gain_right_q8", "max_duration_ms"}
_PROGRESS_INTERVAL = 0.1
_CLEANUP_TIMEOUT = 2.0
Emit = Callable[[str, Any], None]
Save = Callable[[bytes, int, dict], Awaitable[Any]]


async def _capabilities(ring: Any, *features: str) -> None:
    capabilities = getattr(ring, "capabilities", None)
    if capabilities is None:
        capabilities = await _command(sdk.get_device_capabilities(ring))
    for feature in features:
        capabilities.require(feature)


def _report_status(emit: Emit, status: Any) -> None:
    emit("recording_status", (
        f"{status.state.name.lower()}: session={status.session_id}, "
        f"saved={status.saved}, file_index={status.file_index}, error={status.error_code}"
    ))


def _report_active(emit: Emit, *, live: bool) -> None:
    emit("audio_state", {"state": "live" if live else "recording",
                         "message": "实时录音中，可提前停止并保存" if live else "远程录音中，可提前停止并保存"})


async def read_config(ring: Any, emit: Emit) -> None:
    """Report settings/status only; observing a session does not claim ownership."""
    await _capabilities(ring, "recording_control")
    config = await _command(sdk.get_audio_recording_config(ring))
    status = await _command(sdk.get_audio_recording_status(ring))
    emit("recording_config", asdict(config))
    _report_status(emit, status)


async def _settle(task: asyncio.Future) -> bool:
    """Join without cancelling the child; remember repeated parent cancellations.

    asyncio.wait avoids Python 3.10 wait_for's cancellation/completion race. The
    caller always retrieves the result and propagates cancellation separately.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


async def _command(operation: Awaitable[Any]) -> Any:
    # Do not interrupt a protocol write halfway through. The SDK bounds commands.
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _settle(task)
        try:
            task.result()
        except (Exception, asyncio.CancelledError):
            pass
        raise


async def _best_effort(operation: Awaitable[Any]) -> None:
    task = asyncio.create_task(operation)
    cancelled = await _settle(task)
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        pass
    if cancelled:
        raise asyncio.CancelledError


async def _cancel_join(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        joined = asyncio.gather(*tasks, return_exceptions=True)
        cancelled = await _settle(joined)
        joined.result()
        if cancelled:
            raise asyncio.CancelledError


class _Session:
    def __init__(self, ring: Any, previous: Any, emit: Emit) -> None:
        self.ring = ring
        self.previous = previous
        self.emit = emit
        self.id: int | None = None
        self.stop_sent = False
        self.terminal = False
        self.recovered: Any = None

    async def start(self) -> None:
        try:
            session_id = await sdk.start_audio_recording(self.ring)
        except (sdk.TimeoutError, asyncio.TimeoutError):
            # Exactly one recovery query, never a second START. An unchanged
            # terminal session is not proof that this request was accepted.
            recovered = await sdk.get_audio_recording_status(self.ring)
            if (not recovered.session_id or recovered.session_id == self.previous.session_id
                    or recovered.state == sdk.AudioRecordingState.IDLE):
                raise sdk.ProtocolError("START outcome is ambiguous; no new recording session")
            self.recovered = recovered
            session_id = recovered.session_id
        if (isinstance(session_id, bool) or not isinstance(session_id, int)
                or not 1 <= session_id <= 0xFFFFFFFF or session_id == self.previous.session_id):
            raise sdk.ProtocolError("START did not identify a new recording session")
        self.id = session_id

    async def stop_once(self) -> None:
        if self.id is not None and not self.stop_sent and not self.terminal:
            self.stop_sent = True  # Lost ACK must not cause another STOP.
            await _command(sdk.stop_audio_recording(self.ring, self.id, timeout_s=_CLEANUP_TIMEOUT))

    async def cleanup(self) -> None:
        if self.id is None or self.terminal or self.stop_sent or not self.ring.is_connected:
            return
        status = await sdk.get_audio_recording_status(self.ring, timeout_s=_CLEANUP_TIMEOUT)
        if status.session_id == self.id and not status.finished and status.state != sdk.AudioRecordingState.IDLE:
            await self.stop_once()

    def confirmed(self, status: Any) -> Any:
        if status.session_id != self.id:
            raise sdk.ProtocolError("Recording status belongs to another session")
        self.terminal = status.finished
        _report_status(self.emit, status)
        if (status.state != sdk.AudioRecordingState.COMPLETE or status.error_code
                or not status.saved or status.file_index is None):
            raise sdk.ProtocolError("Recording has no confirmed complete saved file")
        return status

    async def finish(self, stop: asyncio.Event, duration_ms: int) -> Any:
        if self.recovered is not None and self.recovered.finished:
            return self.confirmed(self.recovered)
        finished = asyncio.create_task(sdk.wait_audio_recording_finished(
            self.ring, self.id, timeout_s=duration_ms / 1000 + 15))
        requested = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait({finished, requested}, return_when=asyncio.FIRST_COMPLETED)
            if finished not in done:
                await self.stop_once()
                self.emit("recording_status", "停止已请求，等待录音结束及文件保存确认")
            return self.confirmed(await asyncio.shield(finished))
        finally:
            await _cancel_join([finished, requested])


async def _collect_pcm(stream: Any, emit: Emit) -> tuple[bytes, Any, Any]:
    start = None
    end = None
    pcm = bytearray()
    config = None
    samples = 0
    last_progress = float("-inf")
    last_bytes = -1
    loop = asyncio.get_running_loop()
    async for event in stream:
        if end is not None:
            raise sdk.ProtocolError("Live audio continued after END")
        if isinstance(event, sdk.LiveAudioStart):
            if start is not None or event.sample_rate != 16000 or event.channels not in (1, 2) or event.codec != 3:
                raise sdk.ProtocolError("Invalid live START format")
            start = event
            config = sdk.PcmConfig(event.sample_rate, event.channels, 16)
        elif isinstance(event, sdk.LivePcmFrame):
            if (start is None or event.session_id != start.session_id or event.pcm_config != config
                    or event.sample_offset != samples or event.sample_count != 320
                    or len(event.data) != event.sample_count * config.channels * 2):
                raise sdk.ProtocolError("Live PCM session, format or sample offset mismatch")
            pcm.extend(event.data)
            samples += event.sample_count
            if samples > 16000 * 60:
                raise sdk.ProtocolError("Live PCM exceeds the recording limit")
            if loop.time() - last_progress >= _PROGRESS_INTERVAL:
                emit("audio_progress", {"bytes": len(pcm), "total": None})
                last_progress = loop.time()
                last_bytes = len(pcm)
        elif isinstance(event, sdk.LiveAudioEnd):
            if (start is None or event.session_id != start.session_id or event.error_code
                    or event.dropped_frames or event.produced_bytes != event.sent_bytes):
                raise sdk.ProtocolError("Live END does not confirm complete audio")
            end = event
        else:
            raise sdk.ProtocolError("Unexpected live PCM event")
    if start is None or end is None or not stream.complete:
        raise sdk.ProtocolError("Live recording is incomplete: missing verified END")
    if last_bytes != len(pcm):
        emit("audio_progress", {"bytes": len(pcm), "total": None})
    return bytes(pcm), config, start


async def _capture(session: _Session, config: Any, stop: asyncio.Event, emit: Emit,
                   stream: Any = None) -> tuple[Any, Any]:
    tasks = []
    collector = None
    try:
        if stream is not None:
            # Drain while START ACK is pending, including lost-ACK recovery.
            collector = asyncio.create_task(_collect_pcm(stream, emit))
            tasks.append(collector)
        await _command(session.start())
        _report_active(emit, live=stream is not None)
        finished = asyncio.create_task(session.finish(stop, config.max_duration_ms))
        tasks.append(finished)
        if collector is None:
            return await asyncio.shield(finished), None
        status, recording = await asyncio.gather(finished, collector)
        if recording[2].session_id != session.id:
            raise sdk.ProtocolError("Live recording belongs to another session")
        return status, recording
    finally:
        try:
            await _cancel_join(tasks)
        finally:
            await _best_effort(session.cleanup())


async def _download(ring: Any, index: int, emit: Emit) -> tuple[Any, bytes]:
    # SDK quick download leaves END to its caller, on both success and failure.
    try:
        transfer = asyncio.create_task(sdk.download_audio_file(
            ring, index, quick=True,
            progress=lambda current, total: emit("audio_progress", {"bytes": current, "total": total})))
        try:
            info, data = await asyncio.shield(transfer)
        finally:
            await _cancel_join([transfer])
        if info.file_index != index or info.data_size != len(data):
            raise sdk.ProtocolError("Downloaded file metadata does not match recording")
    except BaseException:
        await _best_effort(sdk.end_audio_extract(ring, index, timeout_s=_CLEANUP_TIMEOUT,
                                                 ignore_timeout=False))
        raise
    await _command(sdk.end_audio_extract(ring, index, timeout_s=_CLEANUP_TIMEOUT, ignore_timeout=False))
    return info, data


async def record(ring: Any, options: dict, *, live: bool, stop: asyncio.Event,
                 emit: Emit, save: Save) -> None:
    """Record FLASH or LIVE_AND_FLASH, then hand only confirmed audio to save.

    Options are the five mutable SDK recording fields. Output and disconnect
    policy are fixed here. A START timeout is recovered once against the idle /
    terminal baseline; if ownership remains unknown, no session is stopped.
    """
    if not isinstance(options, dict):
        raise TypeError("recording options must be a dict")
    if options.keys() - _OPTIONS:
        raise ValueError("Unsupported recording options: " + ", ".join(map(str, options.keys() - _OPTIONS)))
    config = sdk.AudioRecordingConfig(
        **options, output=sdk.AudioOutput.LIVE_AND_FLASH if live else sdk.AudioOutput.FLASH,
        disconnect_policy=sdk.AudioDisconnectPolicy.STOP)
    await _capabilities(ring, "recording_control", "live_audio" if live else "audio_files")
    previous = await _command(sdk.get_audio_recording_status(ring))
    _report_status(emit, previous)
    if previous.state != sdk.AudioRecordingState.IDLE and not previous.finished:
        raise sdk.ProtocolError("Another recording is already active")
    config = await _command(sdk.set_audio_recording_config(ring, config))
    emit("recording_config", asdict(config))
    session = _Session(ring, previous, emit)
    if live:
        async with sdk.open_live_audio_stream(ring, output="pcm") as stream:
            status, recording = await _capture(session, config, stop, emit, stream)
        pcm, pcm_config, start = recording
        data = sdk.build_wav_from_pcm(pcm, pcm_config)
        metadata = {"record_time": start.record_time, "session_id": session.id,
                    "pcm_config": asdict(pcm_config)}
    else:
        status, _ = await _capture(session, config, stop, emit)
        info, data = await _download(ring, status.file_index, emit)
        metadata = {"record_time": info.record_time, "session_id": session.id}
    await save(data, status.file_index, metadata)
