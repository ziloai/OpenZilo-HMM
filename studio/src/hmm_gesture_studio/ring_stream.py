# SPDX-License-Identifier: MPL-2.0
"""Shared IMU stream lifecycle using the official OpenZilo SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import openzilo as sdk

from hmm_gesture_studio.wire import ErrorCode


@asynccontextmanager
async def open_ring_stream(
    address: str,
    *,
    on_status: Callable[[str], None] = print,
) -> AsyncIterator[tuple[sdk.OpenZiloClient, sdk.SensorStartInfo]]:
    """Connect, enable IMU reports, and stop reports before disconnecting.

    Discover capabilities before directly subscribing to IMU reports.
    A busy device can be retried later; no manual mode switch is required.
    """
    on_status(f"Connecting to OpenZilo ring {address} ...")
    async with sdk.OpenZiloClient(address=address) as ring:
        info = await sdk.get_system_info(ring)
        on_status(f"Connected: {info.model} (firmware {info.firmware_version}, "
                  f"battery {info.battery_percent}%)")
        await sdk.get_device_capabilities(ring)
        try:
            start = await sdk.start_sensor_report(ring)
        except sdk.DeviceError as exc:
            if exc.error_code == ErrorCode.DEVICE_BUSY:
                raise sdk.DeviceError(
                    exc.error_code,
                    "Cannot start IMU reports: device busy. Retry later after "
                    "the active operation finishes.",
                ) from exc
            raise

        try:
            if start.sample_rate_hz <= 0:
                raise ValueError(f"Invalid device sample rate: {start.sample_rate_hz}")
            on_status(f"IMU: {start.sample_rate_hz} Hz, "
                      f"accel ±{start.accel_range_g} g, gyro ±{start.gyro_range_dps} dps")
            yield ring, start
        finally:
            if ring.is_connected:
                try:
                    await sdk.stop_sensor_report(ring)
                except sdk.OpenZiloError as exc:
                    # Do not mask the original capture/recognition error.
                    on_status(f"Warning: could not stop IMU reports: {exc}")
