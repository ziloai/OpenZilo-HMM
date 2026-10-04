# SPDX-License-Identifier: MPL-2.0
"""Shared IMU stream lifecycle using the official OpenZilo SDK."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import openzilo as sdk


@asynccontextmanager
async def open_ring_stream(
    address: str,
) -> AsyncIterator[tuple[sdk.OpenZiloClient, sdk.SensorStartInfo]]:
    """Connect, enable IMU reports, and stop reports before disconnecting.

    The user must put the ring in gesture mode. A key event is not proof of
    the current mode; only a successful start_sensor_report enables streaming.
    """
    print(f"Connecting to OpenZilo ring {address} ...")
    async with sdk.OpenZiloClient(address=address) as ring:
        info = await sdk.get_system_info(ring)
        print(f"Connected: {info.model} (firmware {info.firmware_version}, "
              f"battery {info.battery_percent}%)")
        print("The ring must be in gesture mode before starting IMU reports.")
        try:
            start = await sdk.start_sensor_report(ring)
        except sdk.DeviceError as exc:
            if exc.error_code == sdk.ErrorCode.DEVICE_BUSY:
                raise sdk.DeviceError(
                    exc.error_code,
                    "Cannot start IMU reports: device busy. Finish any active "
                    "operation, switch the ring to gesture mode with a single "
                    "button press if needed, then retry. A key event alone "
                    "does not confirm the mode.",
                ) from exc
            raise

        try:
            if start.sample_rate_hz <= 0:
                raise ValueError(f"Invalid device sample rate: {start.sample_rate_hz}")
            print(f"IMU: {start.sample_rate_hz} Hz, "
                  f"accel ±{start.accel_range_g} g, gyro ±{start.gyro_range_dps} dps")
            yield ring, start
        finally:
            if ring.is_connected:
                try:
                    await sdk.stop_sensor_report(ring)
                except sdk.OpenZiloError as exc:
                    # Do not mask the original capture/recognition error.
                    print(f"Warning: could not stop IMU reports: {exc}")
