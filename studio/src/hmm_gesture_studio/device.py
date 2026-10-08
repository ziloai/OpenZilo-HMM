# SPDX-License-Identifier: MPL-2.0
"""One BLE owner thread; BLE, IMU readiness and audio are separate states."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
import math
import threading
from weakref import WeakSet

import openzilo as sdk

from .audio import save_recording

IMU_TIMEOUT = 1.0
RETRY_DELAY = 1.5
HOUSEKEEPING_INTERVAL = 0.25
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0)
AUDIO_TIMEOUT = 2.0
CLEANUP_TIMEOUT = 1.0


class _StudioClient(sdk.OpenZiloClient):
    """Serialize SDK requests and finish fragmented writes before cancelling.

    Separate locks are intentional: request() calls send_command(). Audio and
    IMU additionally have one business-level owner, including their queue waits.
    No asynchronous SDK packet handlers (which the SDK does not supervise).
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._write_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self.audio_observer = None
        self.audio_extract_index = None

    async def send_command(self, command, body=b""):
        async def write():
            async with self._write_lock:
                try:
                    await super(_StudioClient, self).send_command(command, body)
                except sdk.OpenZiloError:
                    raise
                except Exception as exc:
                    # Bleak's platform transport errors are not always wrapped
                    # by this pinned SDK. Treat failed writes as transport loss.
                    raise sdk.TransportError(f"BLE command write failed: {exc}") from exc
        task = asyncio.create_task(write())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A transport failure during cleanup must not override a user stop
            # and accidentally enter the reconnect loop.
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def request(self, command, response_command, body=b"", *, timeout_s=None):
        async with self._request_lock:
            if command == sdk.AudioCommand.START_EXTRACT:
                self.audio_extract_index = int.from_bytes(body[2:6], "little")
            result = await super().request(
                command, response_command, body, timeout_s=timeout_s,
            )
            if command == sdk.AudioCommand.END_EXTRACT:
                self.audio_extract_index = None
            return result

    async def wait_for_command(self, command, *, timeout_s=None):
        packet = await super().wait_for_command(command, timeout_s=timeout_s)
        if command == sdk.AudioCommand.DATA_FRAME and self.audio_observer:
            self.audio_observer(sdk.parse_audio_data_frame(packet.body))
        return packet


def _drain(ring, command):
    # Pinned SDK has no public drain API. Only call with no consumer of this
    # command: in particular IMU restart happens in its sole consumer task.
    ring._drain_queue(int(command))


class RingWorker:
    """Thread-safe fire-and-forget API. Callbacks run off the Tk thread.

    close() returns the same concurrent Future every time; completion includes
    saving complete recordings, stopping reports, disconnecting and loop exit.
    No caller needs to join the daemon thread.
    """

    def __init__(self, callback: Callable[[str, object], None]):
        self._callback = callback
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._busy: str | None = None
        self._cancel_sent = False
        self._disconnecting = False
        self._closing = False
        self._closed: Future = Future()
        self._cancelled_tasks = WeakSet()
        self._ring = None
        self._wake = None
        self._retry_requested = False
        self._audio_pending = None
        self._audio_task = None
        self._audio_busy = False
        self._stream_state = None
        self._audio_state = None
        self._audio_message = None

    def _cancel_task(self, task):
        # Task.cancelling() is Python 3.11+, but Studio also supports 3.10.
        # Track our own requests so cleanup/save tasks are never cancelled twice.
        if task is not None and not task.done() and task not in self._cancelled_tasks:
            self._cancelled_tasks.add(task)
            task.cancel()

    def _check_interrupted(self):
        # Python 3.10 wait_for can consume cancellation when its child finishes
        # simultaneously. A remembered user stop must still terminate loops.
        if self._closing or self._disconnecting or asyncio.current_task() in self._cancelled_tasks:
            raise asyncio.CancelledError()

    def start(self) -> None:
        with self._lock:
            if self._closing:
                return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="ring-ble", daemon=True,
                )
                self._thread.start()
            self._ready.wait()

    def _run(self) -> None:
        failure = None
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._ready.set()
            self._loop.run_forever()
        except Exception as exc:
            failure = exc
            self._emit("error", str(exc))
        finally:
            self._ready.set()
            try:
                if self._loop is not None:
                    pending = asyncio.all_tasks(self._loop)
                    for task in pending:
                        self._cancel_task(task)
                    if pending:
                        self._loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True)
                        )
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                    self._loop.run_until_complete(self._loop.shutdown_default_executor())
            except Exception as exc:
                failure = exc
                self._emit("error", str(exc))
            finally:
                if self._loop is not None:
                    self._loop.close()
                if failure is None:
                    self._closed.set_result(None)
                else:
                    self._closed.set_exception(failure)

    def _emit(self, event: str, payload: object) -> None:
        if event in {"connected", "samples", "devices"} and (
            self._closing or self._disconnecting
        ):
            return
        try:
            self._callback(event, payload)
        except Exception:
            pass  # A consumer failure must not strand BLE or lose saved audio.

    def _stream(self, state, message, **details):
        if state != self._stream_state:
            self._stream_state = state
            self._emit("stream_state", {"state": state, "message": message, **details})

    def _audio(self, state, message):
        if (state, message) != (self._audio_state, self._audio_message):
            self._audio_state, self._audio_message = state, message
            self._emit("audio_state", {"state": state, "message": message})

    def scan(self) -> None:
        self._request("scan")

    def connect(self, address: str) -> None:
        self._request("connect", address)

    def _request(self, kind: str, address: str = "") -> None:
        self.start()
        with self._lock:
            if self._closing or self._closed.done():
                return
            if self._busy is not None:
                self._loop.call_soon_threadsafe(
                    self._emit, "error", f"Cannot {kind}: {self._busy} already active."
                )
                if kind == "connect" and self._busy != "connect":
                    self._loop.call_soon_threadsafe(self._emit, "disconnected", None)
                return
            self._busy = kind
            self._disconnecting = False
            self._loop.call_soon_threadsafe(self._begin, kind, address)

    def _begin(self, kind: str, address: str) -> None:
        self._cancel_sent = False
        self._task = self._loop.create_task(
            self._scan() if kind == "scan" else self._connect(address)
        )
        self._task.add_done_callback(lambda task: self._finished(task, kind))

    async def _scan(self) -> None:
        devices = await sdk.scan_rings(timeout_s=8.0)
        self._emit("devices", [
            {"name": device.name, "address": device.address, "rssi": device.rssi}
            for device in devices
        ])

    def _control(self, action, *args):
        with self._lock:
            if self._closing or self._disconnecting or self._loop is None:
                return
            self._loop.call_soon_threadsafe(action, *args)

    def retry_imu(self) -> None:
        self._control(self._retry)

    def _retry(self):
        if self._wake is not None:
            self._retry_requested = True
            self._wake.set()

    def start_audio(self, directory) -> None:
        """Listen for files pushed after the user releases the ring button."""
        self._control(self._accept_audio, "listen", None, directory)

    def list_audio(self) -> None:
        self._control(self._accept_audio, "list", None, None)

    def download_audio(self, file_index, directory) -> None:
        self._control(self._accept_audio, "download", file_index, directory)

    def stop_audio(self) -> None:
        """Cancel reception/extraction, never send a recording command."""
        self._control(self._stop_audio)

    def _accept_audio(self, kind, index, directory):
        if self._closing or self._disconnecting:
            return
        if self._ring is None or not self._ring.is_connected:
            self._emit("error", "Connect a ring before using audio.")
            self._audio("idle", "Not connected.")
            return
        if self._audio_busy:
            self._emit("error", "Stop the current audio operation first.")
            return
        if kind == "download" and (type(index) is not int or not 0 <= index <= 0xFFFFFFFF):
            self._emit("error", "Select an unsigned 32-bit audio file index.")
            self._audio("idle", "Invalid audio file index.")
            return
        self._audio_busy = True
        self._audio_pending = (kind, index, directory)
        self._stream("suspended", "IMU paused for audio.")
        self._audio({"listen": "listening", "list": "listing", "download": "downloading"}[kind],
                    "Preparing audio; operate the ring button to record.")
        self._wake.set()

    def _stop_audio(self):
        if not self._audio_busy:
            return
        self._audio("stopping", "Stopping reception; incomplete files remain in ring history.")
        if self._audio_task is not None:
            self._cancel_task(self._audio_task)
        else:
            self._audio_pending = None
            self._audio_busy = False
            self._audio("idle", "Audio reception stopped.")
            self._wake.set()

    async def _connect(self, address: str) -> None:
        if not isinstance(address, str) or not address.strip():
            raise ValueError("A ring address is required.")
        address = address.strip()
        connected_once = False
        attempt = 0
        self._stream_state = None
        while True:
            self._check_interrupted()
            try:
                # Explicit finally also disconnects partially established clients.
                ring = _StudioClient(address=address)
                session_error = None
                try:
                    await ring.connect()
                    info = await sdk.get_system_info(ring)
                    connected_once = True
                    attempt = 0
                    self._emit("connected", {
                        "address": address, "model": info.model,
                        "firmware_version": info.firmware_version,
                        "battery_percent": info.battery_percent,
                    })
                    await self._session(ring)
                except BaseException as exc:
                    session_error = exc
                    raise
                finally:
                    try:
                        await ring.disconnect()
                    except sdk.OpenZiloError as exc:
                        if session_error is None:
                            raise
                        # Cleanup failure must not turn a ProtocolError or user
                        # cancellation into a retryable TransportError.
                        self._emit("warning", f"BLE disconnect cleanup failed: {exc}")
            except sdk.TransportError as exc:
                if not connected_once:
                    raise
                attempt += 1
                delay = RECONNECT_DELAYS[min(attempt - 1, len(RECONNECT_DELAYS) - 1)]
                self._stream("waiting", "BLE disconnected; reconnecting.")
                self._emit("reconnecting", {
                    "address": address, "attempt": attempt,
                    "message": f"{exc}; reconnecting in {delay:g}s.",
                })
                await asyncio.sleep(delay)

    async def _session(self, ring):
        self._ring = ring
        self._wake = asyncio.Event()
        self._retry_requested = False
        self._audio("idle", "Audio reception is off.")
        owner = asyncio.create_task(self._own_stream(ring))
        monitor = asyncio.create_task(self._housekeeping(ring))
        try:
            done, _ = await asyncio.wait((owner, monitor), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            # One cancellation only: a second could interrupt END/STOP or save.
            self._ring = None
            for task in (owner, monitor):
                self._cancel_task(task)
            await asyncio.gather(owner, monitor, return_exceptions=True)
            self._audio_pending = None
            self._audio_task = None
            self._audio_busy = False
            self._wake = None
            self._audio("idle", "Audio reception is off.")
            self._stream("suspended", "BLE session ended.")

    async def _housekeeping(self, ring):
        while True:
            self._check_interrupted()
            if not ring.is_connected:
                raise sdk.TransportError("BLE connection lost")
            # The SDK queues every notification even without a subscription.
            # These fixed-version event queues have no other Studio consumer.
            key_queue = ring._queues.get(0x0704)
            if key_queue is not None and not key_queue.empty():
                self._retry()  # A hint to probe, never evidence of ring mode.
            for command in (0x0701, 0x0702, 0x0703, 0x0704):
                _drain(ring, command)
            if not self._audio_busy:
                _drain(ring, sdk.AudioCommand.DATA_FRAME)
            elif self._audio_task is not None:
                _drain(ring, sdk.SensorCommand.DATA_FRAME)
            await asyncio.sleep(HOUSEKEEPING_INTERVAL)

    async def _pause(self):
        try:
            await asyncio.wait_for(self._wake.wait(), RETRY_DELAY)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()
        self._check_interrupted()

    async def _stop_report(self, ring):
        if ring.is_connected:
            try:
                await sdk.stop_sensor_report(ring, timeout_s=CLEANUP_TIMEOUT)
            except (sdk.TimeoutError, sdk.DeviceError) as exc:
                self._emit("warning", f"Could not stop IMU reports: {exc}")

    async def _own_stream(self, ring):
        started = False
        info = None
        sequence = timestamp = None
        self._stream("waiting", "Waiting for fresh IMU data; select gesture mode on the ring.")
        try:
            while True:
                self._check_interrupted()
                if not ring.is_connected:
                    raise sdk.TransportError("BLE connection lost")
                if self._audio_pending is not None:
                    operation = self._audio_pending
                    if started:
                        await self._stop_report(ring)
                        started = False
                    info = None
                    sequence = timestamp = None
                    # stop_audio can arrive while STOP_REPORT is awaiting ACK.
                    # Do not start a cancelled (or superseded) audio operation.
                    if self._audio_pending is not operation:
                        continue
                    self._audio_pending = None
                    self._audio_task = asyncio.create_task(self._run_audio(ring, *operation))
                    try:
                        # Shield prevents disconnect from cancelling a save/END a
                        # second time when stop_audio has already cancelled it.
                        await asyncio.shield(self._audio_task)
                    except asyncio.CancelledError:
                        if asyncio.current_task() in self._cancelled_tasks:
                            self._cancel_task(self._audio_task)
                            await asyncio.gather(self._audio_task, return_exceptions=True)
                            raise
                    finally:
                        self._audio_task = None
                        self._audio_busy = False
                        self._audio("idle", "Audio reception is off.")
                    self._stream("waiting", "Resuming IMU; select gesture mode on the ring.")
                    continue
                if self._retry_requested:
                    self._retry_requested = False
                    info = None
                if info is None:
                    if self._stream_state == "active":
                        self._stream("waiting", "Restarting IMU; waiting for fresh data.")
                    _drain(ring, sdk.SensorCommand.DATA_FRAME)
                    sequence = timestamp = None
                    try:
                        # Timeout may mean a lost ACK, so cleanup must still stop.
                        started = True
                        info = await sdk.start_sensor_report(ring, timeout_s=IMU_TIMEOUT)
                    except sdk.DeviceError as exc:
                        if exc.error_code != sdk.ErrorCode.DEVICE_BUSY:
                            raise
                        info = None
                        self._stream("suspended", "Ring busy; finish recording and select gesture mode.")
                        await self._pause()
                        continue
                    except sdk.TimeoutError:
                        info = None
                        self._stream("suspended", "IMU start timed out; retrying while BLE stays connected.")
                        await self._pause()
                        continue
                    if not math.isfinite(info.sample_rate_hz) or info.sample_rate_hz <= 0:
                        raise ValueError(f"Invalid device sample rate: {info.sample_rate_hz}")
                    # Also discard batches queued while START was awaiting its
                    # ACK. The next nonempty batch, not buffered pre-ACK data,
                    # establishes readiness (dropping an early fresh batch is safe).
                    _drain(ring, sdk.SensorCommand.DATA_FRAME)
                    if not self._audio_busy:
                        self._stream("waiting", "IMU start acknowledged; waiting for fresh data.")
                    if self._audio_busy:
                        continue
                try:
                    batch = await sdk.wait_sensor_data(ring, timeout_s=IMU_TIMEOUT)
                except sdk.TimeoutError:
                    info = None
                    self._stream("suspended", "IMU stopped reporting; probing again shortly.")
                    await self._pause()
                    continue
                if self._audio_busy or not batch.samples:
                    await asyncio.sleep(0)
                    continue
                samples = batch.samples
                discontinuity = (
                    sequence is not None and batch.sequence_start != sequence
                ) or (timestamp is not None and samples[0].timestamp_ms <= timestamp)
                if discontinuity:
                    self._stream("waiting", "IMU sequence/timestamp discontinuity; starting a new segment.")
                # Split even a malformed/non-monotonic batch at its time boundary.
                rows = []
                for sample in samples:
                    if rows and sample.timestamp_ms <= timestamp:
                        self._emit("samples", rows)
                        rows = []
                        self._stream("waiting", "IMU timestamp restarted; starting a new segment.")
                    self._stream("active", "Receiving fresh IMU data.",
                                 sample_rate_hz=info.sample_rate_hz,
                                 accel_range_g=info.accel_range_g,
                                 gyro_range_dps=info.gyro_range_dps)
                    rows.append([int(getattr(sample, axis)) for axis in (
                        "accel_x", "accel_y", "accel_z", "gyro_x", "gyro_y", "gyro_z",
                    )])
                    timestamp = sample.timestamp_ms
                sequence = (batch.sequence_start + batch.frame_count) & 0xFFFFFFFF
                self._emit("samples", rows)
                await asyncio.sleep(0)  # Buffered batches must not starve controls.
        finally:
            if started:
                try:
                    await self._stop_report(ring)
                except sdk.OpenZiloError as exc:
                    self._emit("warning", f"IMU cleanup failed: {exc}")

    async def _save(self, data, index, directory, metadata):
        self._audio("saving", "Saving complete recording (raw audio is kept if decoding fails).")
        task = asyncio.create_task(asyncio.to_thread(
            save_recording, data, index, directory, metadata=metadata,
        ))
        cancelled = False
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            result = await task
        if result.get("warning"):
            self._emit("warning", result["warning"])
        self._emit("audio_saved", result)
        if cancelled:
            raise asyncio.CancelledError()

    async def _end_extract(self, ring, index):
        if ring.is_connected:
            try:
                await sdk.end_audio_extract(
                    ring, index, timeout_s=CLEANUP_TIMEOUT, ignore_timeout=True,
                )
            except sdk.OpenZiloError as exc:
                self._emit("warning", f"Audio extraction cleanup failed: {exc}")

    async def _run_audio(self, ring, kind, index, directory):
        receiving = False
        received = 0

        def observe(frame):
            nonlocal receiving, received
            receiving = True
            received += len(frame.data)  # Transport bytes, including any retries.
            self._audio("receiving", "Receiving a file from the ring (not recording duration).")
            self._emit("audio_progress", {"bytes": received, "total": None})

        try:
            if kind == "list":
                count = await sdk.get_audio_file_count(ring, timeout_s=AUDIO_TIMEOUT)
                if type(count) is not int or not 0 <= count <= 10000:
                    raise ValueError(f"Unexpected recording count: {count}; refusing to build an unbounded list")
                self._emit("audio_files", [{"file_index": i} for i in range(count)])
            elif kind == "download":
                complete = False
                try:
                    info, data = await sdk.download_audio_file(
                        ring, index, quick=False, timeout_s=AUDIO_TIMEOUT,
                        progress=lambda size, total: self._emit(
                            "audio_progress", {"bytes": size, "total": total},
                        ),
                    )
                    complete = True  # SDK normal success already sent END.
                finally:
                    if not complete:
                        await self._end_extract(ring, index)
                await self._save(data, index, directory, {
                    "record_time": info.record_time, "data_size": info.data_size,
                })
            else:
                ring.audio_observer = observe
                while True:
                    self._check_interrupted()
                    receiving = False
                    received = 0
                    self._audio("listening", "Hold/release the ring button to record; stop reception before gestures.")
                    try:
                        index, data = await sdk.receive_auto_audio_file(ring, timeout_s=AUDIO_TIMEOUT)
                    except sdk.TimeoutError:
                        if receiving:
                            raise
                        continue  # Waiting for the first frame, not a broken file.
                    await self._save(data, index, directory, {})
                    # Auto-stream gap recovery starts a normal extraction, but
                    # the SDK auto receiver does not end that extraction itself.
                    if ring.audio_extract_index is not None:
                        await self._end_extract(ring, ring.audio_extract_index)
                        ring.audio_extract_index = None
        except (sdk.TimeoutError, sdk.ProtocolError, sdk.DeviceError, OSError, ValueError, TypeError) as exc:
            # File/decoder/configuration errors end only the audio operation;
            # they must not tear down a healthy BLE connection.
            self._emit("error", f"Audio failed: {exc}. Recover incomplete files by index from ring history.")
        except asyncio.CancelledError:
            self._emit("warning", "Audio reception stopped; recover any incomplete file from ring history.")
            raise
        finally:
            ring.audio_observer = None
            if kind == "listen" and ring.audio_extract_index is not None:
                await self._end_extract(ring, ring.audio_extract_index)
                ring.audio_extract_index = None

    def _finished(self, task: asyncio.Task, kind: str) -> None:
        with self._lock:
            self._task = None
            self._busy = None
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    self._emit("warning" if isinstance(error, sdk.TimeoutError) else "error", str(error))
            if kind == "connect":
                self._emit("disconnected", None)
            if self._closing:
                self._loop.stop()

    def disconnect(self) -> None:
        with self._lock:
            if not self._closing and self._busy == "connect":
                self._disconnecting = True
                self._loop.call_soon_threadsafe(self._cancel)

    def _cancel(self) -> None:
        if self._task is not None:
            if not self._cancel_sent:
                self._cancel_sent = True
                self._cancel_task(self._task)
        elif self._closing:
            self._loop.stop()

    def close(self) -> Future:
        with self._lock:
            if not self._closing:
                self._closing = True
                if self._thread is None:
                    self._closed.set_result(None)
                elif not self._closed.done():
                    self._loop.call_soon_threadsafe(self._cancel)
            return self._closed
