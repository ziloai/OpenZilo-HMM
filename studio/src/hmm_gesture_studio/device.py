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
from .wire import AudioCommand, SensorCommand, ErrorCode

IMU_TIMEOUT = 1.0
RETRY_DELAY = 1.5
HOUSEKEEPING_INTERVAL = 0.25
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0)
AUDIO_TIMEOUT = 2.0
CLEANUP_TIMEOUT = 1.0
BATTERY_REFRESH_INTERVAL = 60.0
BATTERY_TIMEOUT = 2.0


class _StudioClient(sdk.OpenZiloClient):
    """Observe transfers and finish writes before cancelling.

    SDK 0.6.2 owns write and per-response request locks. Never acquire its write
    lock here: super().send_command() acquires it internally.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.audio_observer = None
        self.audio_extract_index = None

    async def send_command(self, command, body=b""):
        async def write():
            try:
                await super(_StudioClient, self).send_command(command, body)
            except sdk.OpenZiloError:
                raise
            except Exception as exc:
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
        if command == AudioCommand.START_EXTRACT:
            self.audio_extract_index = int.from_bytes(body[2:6], "big")
        result = await super().request(
            command, response_command, body, timeout_s=timeout_s,
        )
        if command == AudioCommand.END_EXTRACT:
            self.audio_extract_index = None
        return result

    async def wait_for_command(self, command, *, timeout_s=None):
        packet = await super().wait_for_command(command, timeout_s=timeout_s)
        if command == AudioCommand.DATA_FRAME and self.audio_observer:
            self.audio_observer(sdk.parse_audio_data_frame(packet.body))
        return packet


def _drain(ring, command):
    # Pinned SDK has no public drain API. Only call with no consumer of this
    # command: in particular IMU restart happens in its sole consumer task.
    ring._drain_queue(int(command))


def _battery_status(info):
    return {"battery_percent": getattr(info, "battery_percent", None),
            "battery_charging": getattr(info, "battery_charging", None)}


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
        self._audio_armed = False
        self._audio_directory = None
        self._audio_pending = None
        self._audio_task = None
        self._audio_busy = False  # Pending/active operation, not passive arming.
        self._stream_state = None
        self._audio_state = None
        self._audio_message = None
        self._address = None
        self._tool_kind = None
        self._tool_pending = None
        self._tool_task = None
        self._calibration_continue = None
        self._recording_stop = None
        self._firmware_reserved = False
        self._telemetry_lock = None

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
        if event in {"connected", "samples", "devices", "battery"} and (
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
        """Arm auto reception; pause IMU only while receiving a pushed file."""
        self._control(self._accept_audio, "listen", None, directory)

    def list_audio(self) -> None:
        self._control(self._accept_audio, "list", None, None)

    def download_audio(self, file_index, directory) -> None:
        self._control(self._accept_audio, "download", file_index, directory)

    def stop_audio(self) -> None:
        """Disarm and cancel reception/extraction; never command recording."""
        self._control(self._stop_audio)

    def _accept_audio(self, kind, index, directory):
        if self._closing or self._disconnecting:
            return
        if self._ring is None or not self._ring.is_connected:
            self._emit("error", "Connect a ring before using audio.")
            self._audio("idle", "Not connected.")
            return
        if self._audio_busy or self._audio_armed or self._tool_kind or self._firmware_reserved:
            # Manual extraction shares (and can drain) the auto-push queue.
            self._emit("error", "Stop the current audio operation first.")
            return
        if kind == "download" and (type(index) is not int or not 0 <= index <= 0xFFFFFFFF):
            self._emit("error", "Select an unsigned 32-bit audio file index.")
            self._audio("idle", "Invalid audio file index.")
            return
        if kind == "listen":
            # Arming is passive: only queued audio may interrupt the IMU owner.
            self._audio_armed = True
            self._audio_directory = directory
            self._audio("listening", "Listening for saved recordings; IMU remains available.")
        else:
            self._queue_audio(kind, index, directory)

    def read_recording_config(self):
        self._control(self._accept_audio, "config", None, None)

    def start_remote_recording(self, options, directory, *, live=False):
        self._control(self._accept_audio, "live" if live else "record", dict(options), directory)

    def finish_remote_recording(self):
        self._control(self._finish_recording)

    def _finish_recording(self):
        if self._recording_stop is not None:
            self._recording_stop.set()

    def start_calibration(self, mode):
        self._control(self._accept_tool, "calibrating", mode)

    def continue_calibration(self):
        self._control(self._continue_calibration)

    def _continue_calibration(self):
        if self._calibration_continue is not None:
            self._calibration_continue.set()

    def start_diagnostics(self):
        self._control(self._accept_tool, "diagnostics", None)

    def show_calibration(self):
        self._control(self._accept_tool, "profile", None)

    def reset_calibration(self):
        self._control(self._accept_tool, "reset", None)

    def install_firmware(self, verified):
        with self._lock:
            if self._closing or self._disconnecting or self._loop is None:
                raise RuntimeError("Connect before installing firmware")
            if self._firmware_reserved:
                raise RuntimeError("Firmware update is already pending")
            self._firmware_reserved = True
            self._loop.call_soon_threadsafe(self._accept_tool, "firmware", verified)

    def stop_tool(self):
        self._control(self._stop_tool)

    def _accept_tool(self, kind, value):
        if (self._ring is None or not self._ring.is_connected or self._closing
                or self._disconnecting or self._audio_busy or self._audio_armed
                or self._tool_kind or (self._firmware_reserved and kind != "firmware")):
            if kind == "firmware":
                self._firmware_reserved = False
            self._emit("error", "Connect and stop other audio/device operations first.")
            if not self._tool_kind and not self._firmware_reserved:
                self._emit("tool_state", {"state": "idle", "message": "设备操作未启动"})
            return
        self._tool_kind = kind
        self._tool_pending = (kind, value)
        self._stream("suspended", "IMU reserved for device tools.")
        self._emit("tool_state", {"state": kind, "message": "正在准备设备操作"})
        self._wake.set()

    def _stop_tool(self):
        if self._tool_kind == "firmware" or self._firmware_reserved:
            self._emit("warning", "Firmware update must finish or time out before disconnecting.")
            return
        if self._tool_task is not None:
            self._cancel_task(self._tool_task)
        else:
            self._tool_pending = None
            self._tool_kind = None
            self._emit("tool_state", {"state": "idle", "message": "设备操作已取消"})

    async def _run_tool(self, ring, kind, value):
        from . import calibration, firmware

        async def prompt(message):
            self._calibration_continue = asyncio.Event()
            self._emit("calibration_prompt", message)
            try:
                await self._calibration_continue.wait()
                self._check_interrupted()
            finally:
                self._calibration_continue = None

        if kind == "calibrating":
            await calibration.run_calibration(
                ring, value, device_id=self._address, prompt=prompt, emit=self._emit,
            )
        elif kind == "diagnostics":
            await calibration.run_diagnostics(ring, device_id=self._address, emit=self._emit)
        elif kind == "profile":
            self._emit("calibration_profile", await asyncio.to_thread(calibration.read_profile, self._address))
        elif kind == "reset":
            await asyncio.to_thread(calibration.reset_profile, self._address)
            self._emit("calibration_profile", "本机校准配置已重置；手势数据未改变。")
        elif kind == "firmware":
            if ring.capabilities.supports("recording_control"):
                status = await sdk.get_audio_recording_status(ring)
                if status.state not in (sdk.AudioRecordingState.IDLE,
                                        sdk.AudioRecordingState.COMPLETE,
                                        sdk.AudioRecordingState.FAILED,
                                        sdk.AudioRecordingState.CANCELLED):
                    raise ValueError("Stop recording and confirm completion before OTA")
            await firmware.install_firmware(ring, value, emit=self._emit)

    def _queue_audio(self, kind, index, directory):
        self._audio_busy = True
        self._audio_pending = (kind, index, directory)
        if kind in {"record", "live"}:
            self._recording_stop = asyncio.Event()
        self._stream("suspended", "IMU paused for audio.")
        self._audio({"listen": "receiving", "list": "listing", "download": "downloading",
                     "config": "listing", "record": "recording", "live": "live"}[kind],
                    "Preparing audio transfer.")
        self._wake.set()

    def _stop_audio(self):
        if not self._audio_armed and not self._audio_busy:
            return
        self._audio_armed = False
        self._audio_directory = None
        self._audio("stopping", "Stopping reception; incomplete files remain in ring history.")
        if self._audio_task is not None:
            self._cancel_task(self._audio_task)
        else:
            self._audio_pending = None
            self._audio_busy = False
            self._recording_stop = None
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
                    capabilities = await sdk.get_device_capabilities(ring)
                    self._address = address
                    self._emit("capabilities", {name: capabilities.supports(name) for name in (
                        "audio_files", "recording_control", "live_audio", "imu", "ota",
                    )})
                    connected_once = True
                    attempt = 0
                    self._emit("connected", {
                        "address": address, "model": info.model,
                        "firmware_version": info.firmware_version,
                        **_battery_status(info),
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
        self._telemetry_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._retry_requested = False
        self._audio("idle", "Audio reception is off.")
        owner = asyncio.create_task(self._own_stream(ring))
        monitor = asyncio.create_task(self._housekeeping(ring))
        battery = asyncio.create_task(self._monitor_battery(ring))
        tasks = (owner, monitor, battery)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            # One cancellation only: a second could interrupt END/STOP or save.
            self._ring = None
            self._audio_armed = False
            self._audio_directory = None
            for task in tasks:
                self._cancel_task(task)
            await asyncio.gather(*tasks, return_exceptions=True)
            self._audio_pending = None
            self._audio_task = None
            self._audio_busy = False
            self._tool_pending = None
            self._tool_task = None
            self._tool_kind = None
            self._recording_stop = None
            self._firmware_reserved = False
            self._calibration_continue = None
            self._emit("tool_state", {"state": "idle", "message": "设备操作已结束"})
            self._wake = None
            self._audio("idle", "Audio reception is off.")
            self._stream("suspended", "BLE session ended.")

    async def _monitor_battery(self, ring):
        """Low-rate telemetry; never block IMU consumption or restart reports.

        GET_INFO shares the client's request/write locks. Skip ongoing audio
        operations; cancellation belongs to the BLE session, not the Tk thread.
        Optional telemetry failures must not tear down an otherwise usable link.
        """
        while True:
            # Initial battery info was already included in the connected event.
            await asyncio.sleep(BATTERY_REFRESH_INTERVAL)
            self._check_interrupted()
            if not ring.is_connected or self._audio_busy or self._tool_kind:
                continue
            try:
                async with self._telemetry_lock:
                    if self._tool_kind:
                        continue
                    info = await sdk.get_system_info(ring, timeout_s=BATTERY_TIMEOUT)
            except sdk.OpenZiloError as exc:
                self._emit("battery", _battery_status(None))
                self._emit("warning", f"电量读取失败，将于下次刷新重试：{exc}")
            else:
                if ring.is_connected:
                    self._emit("battery", _battery_status(info))

    async def _housekeeping(self, ring):
        while True:
            self._check_interrupted()
            if self._tool_kind == "firmware":
                # OTA owns reconnect/version verification as well as transmission.
                await asyncio.sleep(HOUSEKEEPING_INTERVAL)
                continue
            if not ring.is_connected:
                raise sdk.TransportError("BLE connection lost")
            if self._tool_kind:
                await asyncio.sleep(HOUSEKEEPING_INTERVAL)
                continue
            # The SDK queues every notification even without a subscription.
            # These fixed-version event queues have no other Studio consumer.
            for command in (0x0701, 0x0702, 0x0703, 0x0704):
                _drain(ring, command)
            if not self._audio_busy:
                if self._audio_armed:
                    # Peek only. The owner must stop IMU before the SDK receiver
                    # runs: gap recovery can send extraction commands.
                    pending = any(
                        (q := ring._queues.get(int(command))) is not None and not q.empty()
                        for command in (AudioCommand.RECORD_RESULT, AudioCommand.DATA_FRAME)
                    )
                    if pending:
                        self._queue_audio("listen", None, self._audio_directory)
                else:
                    _drain(ring, AudioCommand.DATA_FRAME)
                    _drain(ring, AudioCommand.RECORD_RESULT)
            if self._audio_task is not None:
                _drain(ring, SensorCommand.DATA_FRAME)
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
        self._stream("waiting", "Subscribing to IMU; waiting for fresh data.")
        try:
            while True:
                self._check_interrupted()
                if not ring.is_connected:
                    raise sdk.TransportError("BLE connection lost")
                if self._tool_pending is not None:
                    operation = self._tool_pending
                    kind, value = operation
                    try:
                        # Wait for any already-started telemetry before exclusive use.
                        async with self._telemetry_lock:
                            if started:
                                await sdk.stop_sensor_report(ring, timeout_s=CLEANUP_TIMEOUT)
                                started = False
                            info = None
                            sequence = timestamp = None
                            if self._tool_pending is not operation:
                                continue
                            self._tool_pending = None
                            self._tool_task = asyncio.create_task(self._run_tool(ring, kind, value))
                            try:
                                await asyncio.shield(self._tool_task)
                            except asyncio.CancelledError:
                                if asyncio.current_task() in self._cancelled_tasks:
                                    self._cancel_task(self._tool_task)
                                    await asyncio.gather(self._tool_task, return_exceptions=True)
                                    raise
                    except Exception as exc:
                        self._emit("error", f"Device operation failed: {exc}")
                    finally:
                        self._tool_task = None
                        self._tool_pending = None
                        # Keep OTA reservation through final disconnect.
                        if kind != "firmware":
                            self._tool_kind = None
                            self._emit("tool_state", {"state": "idle", "message": "设备操作已结束"})
                    if kind == "firmware":
                        return  # No blind retries after an OTA attempt; reconnect explicitly.
                    continue
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
                        if self._audio_armed:
                            self._audio("listening", "Listening for saved recordings; IMU remains available.")
                        else:
                            self._audio("idle", "Audio reception is off.")
                    self._stream("waiting", "Resuming IMU subscription.")
                    continue
                if self._retry_requested:
                    self._retry_requested = False
                    info = None
                if info is None:
                    if self._stream_state == "active":
                        self._stream("waiting", "Restarting IMU; waiting for fresh data.")
                    _drain(ring, SensorCommand.DATA_FRAME)
                    sequence = timestamp = None
                    try:
                        # Timeout may mean a lost ACK, so cleanup must still stop.
                        started = True
                        info = await sdk.start_sensor_report(ring, timeout_s=IMU_TIMEOUT)
                    except sdk.DeviceError as exc:
                        if exc.error_code != ErrorCode.DEVICE_BUSY:
                            raise
                        info = None
                        self._stream("suspended", "Ring busy; waiting to retry IMU subscription.")
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
                    _drain(ring, SensorCommand.DATA_FRAME)
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
                if self._audio_busy or self._tool_kind or not batch.samples:
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
            if kind in {"record", "live", "config"}:
                from . import remote_audio
                if kind == "config":
                    await remote_audio.read_config(ring, self._emit)
                else:
                    if self._recording_stop is None:
                        self._recording_stop = asyncio.Event()
                    try:
                        await remote_audio.record(
                            ring, index, live=kind == "live", stop=self._recording_stop,
                            emit=self._emit,
                            save=lambda data, file_index, metadata: self._save(
                                data, file_index, directory, metadata,
                            ),
                        )
                    finally:
                        self._recording_stop = None
            elif kind == "list":
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
                self._check_interrupted()
                saved_queue = ring._queues.get(int(AudioCommand.RECORD_RESULT))
                saved_notification = saved_queue is not None and not saved_queue.empty()
                try:
                    index, data = await sdk.receive_auto_audio_file(ring, timeout_s=AUDIO_TIMEOUT)
                except sdk.TimeoutError:
                    if receiving or saved_notification:
                        raise
                    return  # No first frame: yield back to IMU, leaving arming intact.
                await self._save(data, index, directory, {})
                # Save first. If cancellation interrupts normal END, the finally
                # below must finish cleanup before the owner resumes IMU.
                if ring.audio_extract_index is not None:
                    await self._end_extract(ring, ring.audio_extract_index)
                    ring.audio_extract_index = None
        except (sdk.TimeoutError, sdk.ProtocolError, sdk.DeviceError, sdk.CompatibilityError,
                sdk.AudioDecodeError, OSError, ValueError, TypeError) as exc:
            # File/decoder/configuration errors end only the audio operation;
            # they must not tear down a healthy BLE connection.
            self._emit("error", f"Audio failed: {exc}. Recover incomplete files by index from ring history.")
        except asyncio.CancelledError:
            self._emit("warning", "Audio reception stopped; recover any incomplete file from ring history.")
            raise
        finally:
            ring.audio_observer = None
            # Auto-stream gap recovery starts extraction but the SDK does not
            # end it. Save a complete file before END, even during cancellation.
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
            if self._firmware_reserved or self._tool_kind == "firmware":
                raise RuntimeError("Wait for firmware update completion before disconnecting")
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
            if self._firmware_reserved or self._tool_kind == "firmware":
                raise RuntimeError("Wait for firmware update completion before closing")
            if not self._closing:
                self._closing = True
                if self._thread is None:
                    self._closed.set_result(None)
                elif not self._closed.done():
                    self._loop.call_soon_threadsafe(self._cancel)
            return self._closed
