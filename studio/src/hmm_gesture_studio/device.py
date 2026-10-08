# SPDX-License-Identifier: MPL-2.0
"""One BLE owner thread. Callbacks run there, never on the Tk thread."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future
import threading

import openzilo as sdk

from .ring_stream import open_ring_stream


class RingWorker:
    """A single scan or connection at a time; requests never queue BLE tasks.

    Call ``start`` before requesting work. The callback must be thread-safe
    (for example, Queue.put), must not access Tk, and should not block.
    ``close`` is idempotent; requests after closing are harmless no-ops.
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

    def start(self) -> None:
        with self._lock:
            if self._closing:
                return
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="ring-ble", daemon=True,
                )
                self._thread.start()
            # The thread publishes its loop before releasing this barrier.
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
                        task.cancel()
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
            # A consumer failure must not strand a live BLE connection.
            pass

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

    def _status(self, message: str) -> None:
        if message.startswith("Warning:"):
            self._emit("warning", message)

    async def _connect(self, address: str) -> None:
        if not isinstance(address, str) or not address.strip():
            raise ValueError("A ring address is required.")
        address = address.strip()
        async with open_ring_stream(address, on_status=self._status) as (ring, info):
            self._emit("connected", {
                "address": address,
                "sample_rate_hz": info.sample_rate_hz,
                "accel_range_g": info.accel_range_g,
                "gyro_range_dps": info.gyro_range_dps,
            })
            while ring.is_connected:
                try:
                    batch = await sdk.wait_sensor_data(ring, timeout_s=5.0)
                except sdk.TimeoutError as exc:
                    self._emit("warning", str(exc))
                    if not ring.is_connected:
                        break
                else:
                    self._emit("samples", [
                        [int(sample.accel_x), int(sample.accel_y), int(sample.accel_z),
                         int(sample.gyro_x), int(sample.gyro_y), int(sample.gyro_z)]
                        for sample in batch.samples
                    ])
                # Buffered SDK batches must not starve cancellation requests.
                await asyncio.sleep(0)

    def _finished(self, task: asyncio.Task, kind: str) -> None:
        with self._lock:
            self._task = None
            self._busy = None
            if not task.cancelled():
                error = task.exception()
                if error is not None:
                    self._emit("warning" if isinstance(error, sdk.TimeoutError) else "error",
                               str(error))
            if kind == "connect":
                # Also covers cancellation before the coroutine's first instruction.
                self._emit("disconnected", None)
            if self._closing and self._busy is None:
                self._loop.stop()

    def disconnect(self) -> None:
        with self._lock:
            if not self._closing and self._busy == "connect":
                self._disconnecting = True
                self._loop.call_soon_threadsafe(self._cancel)

    def _cancel(self) -> None:
        if self._task is not None:
            # A second cancel could interrupt stop_sensor_report / __aexit__.
            if not self._cancel_sent:
                self._cancel_sent = True
                self._task.cancel()
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
