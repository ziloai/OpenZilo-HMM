# SPDX-License-Identifier: MPL-2.0
"""Host-only IMU calibration and diagnostics, never gesture-model input.

The worker owns the connected ring exclusively for the duration of either async
operation, including prompts. It must not reconnect underneath an operation.
Callbacks marshal events to the UI; they must not access Tk from the BLE thread.
Synchronous profile helpers perform file I/O: UI callers must use to_thread.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict
import json
import logging
import time

import openzilo as sdk


_logger = logging.getLogger(__name__)


Emit = Callable[[str, object], None]
Prompt = Callable[[str], Awaitable[object]]


def _load_profile(device_id: str) -> sdk.ImuCalibrationProfile:
    path = sdk.imu_calibration_path(device_id)
    try:
        profile = sdk.load_imu_calibration_profile(path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"No local IMU calibration profile: {path}") from exc
    if profile.device_key != sdk.imu_device_key(device_id):
        raise sdk.CompatibilityError("IMU profile belongs to another device")
    return profile


def _summary(profile: sdk.ImuCalibrationProfile) -> str:
    return json.dumps({"application": "software", **asdict(profile)}, ensure_ascii=False, indent=2)


def read_profile(device_id: str) -> str:
    """Read a local profile, rejecting missing/corrupt/wrong-device files."""
    return _summary(_load_profile(device_id))


def reset_profile(device_id: str) -> None:
    """Delete this device's SDK-selected local file after caller confirmation.

    Deliberately do not parse or validate the file: reset is also the recovery
    path for corrupt or incorrectly bound profiles. No path override is allowed.
    """
    path = sdk.imu_calibration_path(device_id)
    try:
        path.unlink()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"No local IMU calibration profile: {path}") from exc


async def _prompt_while_streaming(ring: sdk.OpenZiloClient, prompt: Prompt, message: str) -> None:
    """Consume idle reports using public APIs so a long prompt cannot overflow BLE."""
    async def discard() -> None:
        while True:
            try:
                await sdk.wait_sensor_data(ring, timeout_s=5.0)
            except sdk.TimeoutError:
                continue

    asking = asyncio.ensure_future(prompt(message))
    draining = asyncio.create_task(discard())
    try:
        done, _ = await asyncio.wait((asking, draining), return_when=asyncio.FIRST_COMPLETED)
        if draining in done:
            draining.result()  # A disconnect/protocol failure must terminate the prompt.
        await asking
    finally:
        for task in (asking, draining):
            if not task.done():
                task.cancel()
        await asyncio.gather(asking, draining, return_exceptions=True)


def _correct_window(window: sdk.ImuStaticWindow, parameters: sdk.ImuCalibration, *,
                    accel: bool = False, gyro: bool = False) -> sdk.ImuStaticWindow:
    # The collector returns physical-unit means, not raw batches accepted by
    # apply_imu_calibration. Match SDK CLI's correction without i16 requantization.
    return sdk.ImuStaticWindow(
        tuple((value - parameters.accel_bias_ug[i] / 1e6) * parameters.accel_scale_ppm[i] / 1e6
              if accel else value for i, value in enumerate(window.accel_g)),
        tuple(value - parameters.gyro_bias_udps[i] / 1e6 if gyro else value
              for i, value in enumerate(window.gyro_dps)),
        window.accel_std_g, window.gyro_std_dps, window.samples,
    )


async def run_calibration(
    ring: sdk.OpenZiloClient, mode: str, *, device_id: str, prompt: Prompt, emit: Emit,
) -> sdk.ImuCalibrationProfile:
    """Run accel/quick/full with SDK CLI quality gates; commit only after validation.

    Cancellation or validation failure leaves the old file untouched. Existing
    profiles must match this device, exact firmware, model and active ranges;
    incompatible profiles require an explicit reset, never automatic migration.
    """
    if mode not in ("accel", "quick", "full"):
        raise ValueError("Calibration mode must be accel, quick or full")
    caps = await sdk.get_device_capabilities(ring)
    caps.require("imu")
    if any(feature.name in ("imu_calibration", "unknown_11") for feature in caps.features):
        raise sdk.CompatibilityError("Firmware IMU calibration advertised; software correction is unsafe")
    info = await sdk.get_system_info(ring)
    path = sdk.imu_calibration_path(device_id)
    previous = _load_profile(device_id) if path.exists() else None
    if previous:
        previous.check_device(device_id, info.firmware_version, info.model)
    candidate = previous.calibration if previous else None
    accel_validated = previous.accel_validated if previous else False
    gyro_validated = previous.gyro_validated if previous else False
    streaming = False

    def progress(message: str) -> None:
        emit("calibration_progress", message)

    async def ask(message: str) -> None:
        progress(message)
        if streaming:
            await _prompt_while_streaming(ring, prompt, message)
        else:
            await prompt(message)

    async def start() -> sdk.SensorStartInfo:
        nonlocal candidate, streaming
        streaming = True  # A timed-out start may already have enabled capture.
        config = await sdk.start_sensor_report(ring)
        if candidate is None:
            candidate = sdk.ImuCalibration.identity(config)
        if (config.accel_range_g != candidate.accel_range_g or
                config.gyro_range_dps != candidate.gyro_range_dps):
            raise sdk.CompatibilityError("IMU ranges differ from profile; reset and recalibrate")
        return config

    async def stop() -> None:
        nonlocal streaming
        if streaming:
            await sdk.stop_sensor_report(ring)
            streaming = False

    async def collect(config: sdk.SensorStartInfo, seconds: float = 5.0, *, gyro: bool = True):
        try:
            return await sdk.collect_imu_static_window(
                ring, config, seconds=seconds, settle_seconds=2.0, check_gyro=gyro,
            )
        except ValueError as exc:
            progress(str(exc))
            return None

    async def stable_gyro(config: sdk.SensorStartInfo) -> None:
        progress("Keep still: waiting for three stable windows (up to 3 minutes).")
        deadline = asyncio.get_running_loop().time() + 180
        history = []
        while asyncio.get_running_loop().time() < deadline:
            sample = await collect(config)
            if sample is None:
                history.clear()
                continue
            history = (history + [sample])[-3:]
            progress("Stationary angular velocity (deg/s): " + ", ".join(f"{v:.3f}" for v in sample.gyro_dps))
            if len(history) == 3 and all(
                max(item.gyro_dps[i] for item in history) - min(item.gyro_dps[i] for item in history) <= 0.08
                for i in range(3)
            ):
                return
        raise ValueError("Gyroscope did not stabilize; profile was not updated")

    async def retry_window(config: sdk.SensorStartInfo, seconds: float) -> sdk.ImuStaticWindow:
        for _ in range(3):
            result = await collect(config, seconds)
            if result is not None:
                return result
        raise ValueError("Could not collect stable gyroscope data; profile was not updated")

    try:
        progress("Calibration is local software only; gesture model input is unchanged.")
        if mode in ("accel", "full"):
            progress("Secure the ring in the charging case; do not detach it during the six faces.")
            config = await start()
            faces = {}
            while len(faces) < 6:
                await ask(f"Collected {list(faces)}. Place case on an unused face, release it, then continue.")
                sample = await collect(config, gyro=False)
                if sample is None:
                    continue
                try:
                    face = sample.face
                except ValueError as exc:
                    progress(str(exc))
                    continue
                if face in faces:
                    progress(f"{face} already collected; choose another face.")
                    continue
                faces[face] = sample
                progress(f"Accepted {face}.")
            candidate = sdk.fit_imu_six_face_calibration(candidate, list(faces.values()))
            validations = []
            while len(validations) < 3:
                await ask(f"Fresh accelerometer validation {len(validations) + 1}/3: choose another axis, release case, then continue.")
                sample = await collect(config, gyro=False)
                if sample is None:
                    continue
                corrected = _correct_window(sample, candidate, accel=True)
                try:
                    axis = corrected.face[0]
                except ValueError as exc:
                    progress(str(exc))
                    continue
                if axis in {item.face[0] for item in validations}:
                    progress("This axis is already validated; choose another axis.")
                    continue
                validations.append(corrected)
            sdk.validate_imu_calibration(validations, full=True, component="accel")
            accel_validated = True
            await stop()
            progress("Accelerometer independent validation passed.")

        if mode == "quick" or (mode == "full" and candidate.gyro_range_dps):
            await ask("Remove ring from case, keep away from magnets, lay it still with paper support if needed, release it, then continue.")
            config = await start()
            if not config.gyro_range_dps:
                raise ValueError("This sensor has no gyroscope; use accel")
            await stable_gyro(config)
            sample = await retry_window(config, 20.0)
            candidate = sdk.fit_imu_gyro_bias(candidate, sample)
            await ask("Keep ring still outside the case; continue to collect fresh gyroscope validation data.")
            verification = await retry_window(config, 5.0)
            sdk.validate_imu_calibration(
                [_correct_window(verification, candidate, gyro=True)], full=False, component="gyro",
            )
            gyro_validated = True
            await stop()
            progress("Gyroscope independent validation passed.")

        profile = sdk.ImuCalibrationProfile(
            sdk.imu_device_key(device_id), info.firmware_version, info.model,
            candidate, accel_validated, gyro_validated,
        )
        # No await between committing the file and reporting its new identity.
        sdk.save_imu_calibration_profile(profile, path)
        emit("calibration_profile", _summary(profile))
        return profile
    except BaseException:
        # Successful stages must stop before saving. On failure, cleanup is
        # best-effort and must not replace the original error or cancellation.
        try:
            await stop()
        except (Exception, asyncio.CancelledError):
            _logger.warning("Could not stop IMU during failed calibration cleanup", exc_info=True)
        raise


async def run_diagnostics(ring: sdk.OpenZiloClient, *, device_id: str, emit: Emit) -> None:
    """Emit newest physical-unit sample at most 10 Hz; never emit model samples.

    The SDK context validates profile/device/ranges and stops reports on cancel
    or error. Reconnection requires opening a new diagnostics context.
    """
    last_emitted = float("-inf")
    async with sdk.open_calibrated_imu_stream(ring, device_id=device_id) as stream:
        async for batch in stream:
            if not batch.samples:
                continue
            now = time.monotonic()
            if now - last_emitted < 0.1:
                continue
            last_emitted = now
            sample = batch.samples[-1]
            emit("calibration_sample", {
                "accel_g": sample.accel_g,
                "gyro_dps": sample.gyro_dps,
                "applied_components": batch.applied_components,
            })
