# SPDX-License-Identifier: MPL-2.0
"""Native calibration orchestration, using SDK types without BLE hardware."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import openzilo as sdk

from hmm_gesture_studio import calibration


DEVICE = "synthetic-ring"


def window(axis=0, sign=1, gyro=(0.2, -0.1, 0.05)):
    accel = [0.0, 0.0, 0.0]
    accel[axis] = float(sign)
    return sdk.ImuStaticWindow(tuple(accel), gyro, 0.001, 0.01, 500)


def accel_windows():
    return [window(axis, sign) for axis in range(3) for sign in (1, -1)] + [
        window(axis) for axis in range(3)
    ]


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "imu.json"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.config = sdk.SensorStartInfo(100, 16, 2000)
        self.info = sdk.SystemInfo("fw", 0, 0, 0, 80, False, "sn", "cpu", "model")
        self.ring = Mock(spec=sdk.OpenZiloClient)
        self.prompt = AsyncMock()
        self.emit = Mock()
        self.caps = sdk.DeviceCapabilities("device", "fw", "model", features=(
            sdk.FeatureVersion("imu", 2, 0),
        ))
        self.patch("imu_calibration_path", return_value=self.path)
        self.patch("get_device_capabilities", new=AsyncMock(return_value=self.caps))
        self.patch("get_system_info", new=AsyncMock(return_value=self.info))
        self.start = self.patch("start_sensor_report", new=AsyncMock(return_value=self.config))
        self.stop = self.patch("stop_sensor_report", new=AsyncMock())
        self.collect = self.patch("collect_imu_static_window", new=AsyncMock())
        # Prompt draining blocks until cancelled, like a quiet BLE receiver.
        async def wait(*args, **kwargs):
            await asyncio.Event().wait()
        self.wait = self.patch("wait_sensor_data", new=AsyncMock(side_effect=wait))

    def patch(self, name, **kwargs):
        return self.stack.enter_context(patch.object(sdk, name, **kwargs))

    def old_profile(self):
        profile = sdk.ImuCalibrationProfile(
            sdk.imu_device_key(DEVICE), "fw", "model",
            sdk.ImuCalibration(0, 16, 2000, accel_bias_ug=(1000, 0, 0)), True,
        )
        sdk.save_imu_calibration_profile(profile, self.path)
        return profile

    async def run_mode(self, mode):
        return await calibration.run_calibration(
            self.ring, mode, device_id=DEVICE, prompt=self.prompt, emit=self.emit,
        )

    async def test_accel_six_faces_and_fresh_three_axis_validation(self):
        self.collect.side_effect = accel_windows()
        validate = self.patch("validate_imu_calibration", wraps=sdk.validate_imu_calibration)
        profile = await self.run_mode("accel")
        self.assertEqual(sdk.load_imu_calibration_profile(self.path), profile)
        self.assertTrue(profile.accel_validated)
        self.assertFalse(profile.gyro_validated)
        self.assertEqual(self.collect.await_count, 9)
        for call in self.collect.await_args_list:
            self.assertEqual(call.kwargs, {"seconds": 5.0, "settle_seconds": 2.0, "check_gyro": False})
        validate.assert_called_once()
        self.assertEqual(validate.call_args.kwargs, {"full": True, "component": "accel"})
        self.stop.assert_awaited_once()
        events = [call.args[0] for call in self.emit.call_args_list]
        self.assertIn("calibration_profile", events)
        self.assertNotIn("samples", events)

    async def test_accel_retries_bad_duplicate_and_ambiguous_faces(self):
        ambiguous = sdk.ImuStaticWindow((0.7, 0.7, 0), (0, 0, 0), 0, 0, 500)
        self.collect.side_effect = [ValueError("movement"), ambiguous, window(), *accel_windows()]
        profile = await self.run_mode("accel")
        self.assertTrue(profile.accel_validated)
        self.assertEqual(self.collect.await_count, 12)

    async def test_accel_validates_corrected_new_means_and_retains_gyro(self):
        previous = replace(self.old_profile(), gyro_validated=True,
                           calibration=sdk.ImuCalibration(0, 16, 2000, gyro_bias_udps=(200000, 0, 0)))
        sdk.save_imu_calibration_profile(previous, self.path)
        self.collect.side_effect = [
            replace(sample, accel_g=tuple(v / 1.05 + 0.03 for v in sample.accel_g))
            for sample in accel_windows()
        ]
        profile = await self.run_mode("accel")
        self.assertEqual(profile.calibration.accel_bias_ug, (30000,) * 3)
        self.assertEqual(profile.calibration.accel_scale_ppm, (1050000,) * 3)
        self.assertEqual(profile.calibration.gyro_bias_udps, previous.calibration.gyro_bias_udps)
        self.assertTrue(profile.gyro_validated)

    async def test_gyro_drift_and_bad_window_reset_consecutive_stability(self):
        self.collect.side_effect = [
            window(), window(), ValueError("movement"),
            window(gyro=(0.5, 0, 0)), window(), window(), window(),
            window(), window(),
        ]
        profile = await self.run_mode("quick")
        self.assertTrue(profile.gyro_validated)
        self.assertEqual([call.kwargs["seconds"] for call in self.collect.await_args_list],
                         [5] * 7 + [20, 5])

    async def test_gyro_stability_has_three_minute_deadline(self):
        self.old_profile()
        before = self.path.read_bytes()
        self.collect.return_value = window()
        with patch.object(calibration.time, "monotonic", side_effect=[0.0, 0.0, 181.0]):
            with self.assertRaisesRegex(ValueError, "stabilize"):
                await self.run_mode("quick")
        self.assertEqual(self.collect.await_count, 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.stop.assert_awaited_once()

    async def test_gyro_retry_limit_leaves_previous_file(self):
        self.old_profile()
        before = self.path.read_bytes()
        self.collect.side_effect = [window()] * 3 + [ValueError("drifting")] * 3
        with self.assertRaisesRegex(ValueError, "stable gyroscope"):
            await self.run_mode("quick")
        self.assertEqual(self.collect.await_count, 6)
        self.assertEqual(self.path.read_bytes(), before)
        self.stop.assert_awaited_once()

    async def test_invalid_mode_is_rejected_without_io_or_ble(self):
        with self.assertRaises(ValueError):
            await self.run_mode("invalid")
        sdk.get_device_capabilities.assert_not_awaited()
        self.start.assert_not_awaited()

    async def test_disconnect_during_prompt_cancels_prompt_without_saving(self):
        entered = asyncio.Event()
        prompt_closed = Mock()
        async def pending_prompt(message):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                prompt_closed()
        self.prompt.side_effect = pending_prompt
        async def disconnected(*args, **kwargs):
            await entered.wait()
            raise sdk.TransportError("disconnected during prompt")
        self.wait.side_effect = disconnected
        with self.assertRaisesRegex(sdk.TransportError, "during prompt"):
            await self.run_mode("accel")
        prompt_closed.assert_called_once()
        self.stop.assert_awaited_once()
        self.assertFalse(self.path.exists())

    async def test_task_cancellation_joins_prompt_and_idle_consumer(self):
        entered = asyncio.Event()
        prompt_closed = Mock()
        async def pending_prompt(message):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                prompt_closed()
        self.prompt.side_effect = pending_prompt
        operation = asyncio.create_task(self.run_mode("accel"))
        await entered.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        prompt_closed.assert_called_once()
        self.stop.assert_awaited_once()
        self.assertFalse(self.path.exists())

    async def test_quick_requires_three_stable_windows_twenty_seconds_and_new_validation(self):
        previous = self.old_profile()
        self.collect.side_effect = [window()] * 5
        profile = await self.run_mode("quick")
        self.assertEqual(profile.calibration.accel_bias_ug, previous.calibration.accel_bias_ug)
        self.assertEqual(profile.calibration.gyro_bias_udps, (200000, -100000, 50000))
        self.assertTrue(profile.accel_validated and profile.gyro_validated)
        self.assertEqual([call.kwargs["seconds"] for call in self.collect.await_args_list], [5, 5, 5, 20, 5])
        self.assertTrue(all(call.kwargs["check_gyro"] for call in self.collect.await_args_list))

    async def test_full_saves_only_after_both_stages(self):
        self.collect.side_effect = accel_windows() + [window()] * 5
        save = self.patch("save_imu_calibration_profile", wraps=sdk.save_imu_calibration_profile)
        profile = await self.run_mode("full")
        save.assert_called_once_with(profile, self.path)
        self.assertTrue(profile.accel_validated and profile.gyro_validated)
        self.assertEqual(self.stop.await_count, 2)

    async def test_failed_full_gyro_validation_preserves_old_profile(self):
        self.old_profile()
        before = self.path.read_bytes()
        self.collect.side_effect = accel_windows() + [window()] * 4 + [window(gyro=(1, 0, 0))]
        with self.assertRaisesRegex(ValueError, "Validation failed"):
            await self.run_mode("full")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call.args[0] == "calibration_profile" for call in self.emit.call_args_list))
        self.stop.assert_awaited()

    async def test_cancellation_during_prompt_or_collection_preserves_profile(self):
        for during_prompt in (True, False):
            with self.subTest(during_prompt=during_prompt):
                self.old_profile()
                before = self.path.read_bytes()
                self.prompt.side_effect = asyncio.CancelledError if during_prompt else None
                self.collect.side_effect = asyncio.CancelledError
                with self.assertRaises(asyncio.CancelledError):
                    await self.run_mode("accel")
                self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.stop.await_count, 2)

    async def test_failed_start_still_attempts_stop(self):
        self.start.side_effect = sdk.TimeoutError("start acknowledgement lost")
        with self.assertRaises(sdk.TimeoutError):
            await self.run_mode("accel")
        self.stop.assert_awaited_once()
        self.assertFalse(self.path.exists())

    async def test_cleanup_stop_failure_preserves_original_cancellation(self):
        self.old_profile()
        before = self.path.read_bytes()
        cancelled = asyncio.CancelledError("user cancelled")
        self.collect.side_effect = cancelled
        self.stop.side_effect = sdk.TransportError("cleanup disconnected")
        with self.assertLogs(calibration.__name__, level="WARNING"), self.assertRaises(asyncio.CancelledError) as caught:
            await self.run_mode("accel")
        self.assertIs(caught.exception, cancelled)
        self.stop.assert_awaited_once()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call.args[0] == "calibration_profile" for call in self.emit.call_args_list))

    async def test_cleanup_stop_failure_preserves_original_validation_error(self):
        self.old_profile()
        before = self.path.read_bytes()
        self.collect.side_effect = accel_windows()
        invalid = ValueError("independent validation failed")
        validate = self.patch("validate_imu_calibration", side_effect=invalid)
        self.stop.side_effect = sdk.TimeoutError("cleanup stop timeout")
        with self.assertLogs(calibration.__name__, level="WARNING"), self.assertRaises(ValueError) as caught:
            await self.run_mode("accel")
        self.assertIs(caught.exception, invalid)
        validate.assert_called_once()
        self.stop.assert_awaited_once()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call.args[0] == "calibration_profile" for call in self.emit.call_args_list))

    async def test_normal_stop_failure_never_saves_even_if_cleanup_retry_succeeds(self):
        self.old_profile()
        before = self.path.read_bytes()
        failed_stop = sdk.TimeoutError("normal stop failed")
        save = self.patch("save_imu_calibration_profile", wraps=sdk.save_imu_calibration_profile)
        for retry in (None, sdk.TransportError("cleanup stop failed")):
            with self.subTest(retry=retry):
                self.collect.side_effect = accel_windows()
                self.stop.side_effect = [failed_stop, retry]
                with patch.object(calibration._logger, "warning") as warning:
                    with self.assertRaises(sdk.TimeoutError) as caught:
                        await self.run_mode("accel")
                self.assertEqual(warning.call_count, int(retry is not None))
                self.assertIs(caught.exception, failed_stop)
                self.assertEqual(self.path.read_bytes(), before)
                save.assert_not_called()
                self.assertFalse(any(call.args[0] == "calibration_profile" for call in self.emit.call_args_list))

    async def test_save_follows_successful_validation_and_stop(self):
        self.collect.side_effect = accel_windows()
        validate = self.patch("validate_imu_calibration", wraps=sdk.validate_imu_calibration)
        save_original = sdk.save_imu_calibration_profile
        def save(profile, path):
            validate.assert_called_once()
            self.stop.assert_awaited_once()
            save_original(profile, path)
        self.patch("save_imu_calibration_profile", side_effect=save)
        await self.run_mode("accel")
        self.assertTrue(self.path.exists())

    async def test_bad_profiles_rejected_before_start(self):
        original = self.old_profile()
        for changes in ({"device_key": sdk.imu_device_key("other")},
                        {"firmware_version": "old"}, {"model": "other"}):
            with self.subTest(changes=changes):
                sdk.save_imu_calibration_profile(replace(original, **changes), self.path)
                with self.assertRaises(sdk.CompatibilityError):
                    await self.run_mode("quick")
        self.path.write_text("corrupt", encoding="utf-8")
        with self.assertRaises(ValueError):
            await self.run_mode("quick")
        self.start.assert_not_awaited()

    async def test_range_mismatch_stops_and_does_not_save(self):
        self.old_profile()
        before = self.path.read_bytes()
        self.start.return_value = replace(self.config, accel_range_g=8)
        with self.assertRaises((ValueError, sdk.CompatibilityError)):
            await self.run_mode("quick")
        self.stop.assert_awaited_once()
        self.assertEqual(self.path.read_bytes(), before)

    async def test_capability_rejection(self):
        for features in ((), (sdk.FeatureVersion("imu", 1, 0),),
                         (*self.caps.features, sdk.FeatureVersion("imu_calibration", 1, 0))):
            with self.subTest(features=features):
                sdk.get_device_capabilities.return_value = replace(self.caps, features=features)
                with self.assertRaises(sdk.CompatibilityError):
                    await self.run_mode("accel")
        self.start.assert_not_awaited()

    async def test_gyro_absent_full_skips_quick_rejects(self):
        self.start.return_value = sdk.SensorStartInfo(100, 16, 0)
        self.collect.side_effect = accel_windows()
        profile = await self.run_mode("full")
        self.assertFalse(profile.gyro_validated)
        with self.assertRaisesRegex(ValueError, "gyroscope"):
            await self.run_mode("quick")

    async def test_save_failure_keeps_old_file(self):
        self.old_profile()
        before = self.path.read_bytes()
        self.collect.side_effect = [window()] * 5
        self.patch("save_imu_calibration_profile", side_effect=OSError("disk full"))
        with self.assertRaisesRegex(OSError, "disk full"):
            await self.run_mode("quick")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(call.args[0] == "calibration_profile" for call in self.emit.call_args_list))

    def test_read_and_reset_are_local_and_missing_is_explicit(self):
        with self.assertRaises(FileNotFoundError):
            calibration.read_profile(DEVICE)
        with self.assertRaises(FileNotFoundError):
            calibration.reset_profile(DEVICE)
        self.old_profile()
        summary = json.loads(calibration.read_profile(DEVICE))
        self.assertTrue(summary["accel_validated"])
        calibration.reset_profile(DEVICE)
        self.assertFalse(self.path.exists())
        self.start.assert_not_called()

    def test_reset_removes_corrupt_or_wrong_device_profile_but_read_stays_strict(self):
        original = self.old_profile()
        sdk.save_imu_calibration_profile(replace(original, device_key=sdk.imu_device_key("other")), self.path)
        with self.assertRaises(sdk.CompatibilityError):
            calibration.read_profile(DEVICE)
        calibration.reset_profile(DEVICE)
        self.assertFalse(self.path.exists())
        self.path.write_text("corrupt", encoding="utf-8")
        with self.assertRaises(ValueError):
            calibration.read_profile(DEVICE)
        calibration.reset_profile(DEVICE)
        self.assertFalse(self.path.exists())

    def test_reset_only_deletes_sdk_generated_path_not_identifier_as_path(self):
        unrelated = self.path.parent / "keep.json"
        unrelated.write_text("keep", encoding="utf-8")
        self.path.write_text("corrupt", encoding="utf-8")
        sdk.imu_calibration_path.reset_mock()
        calibration.reset_profile(str(unrelated))
        sdk.imu_calibration_path.assert_called_once_with(str(unrelated))
        self.assertFalse(self.path.exists())
        self.assertEqual(unrelated.read_text(), "keep")
        with self.assertRaises(TypeError):
            calibration.reset_profile(DEVICE, path=unrelated)

    async def test_diagnostics_only_emit_physical_units_throttled_and_close_on_cancel(self):
        sample = sdk.CalibratedImuSample(123, (0.0, 0.0, 1.0), (0.1, 0.2, 0.3))
        batch = sdk.CalibratedImuBatch(0, 1, (sample,), ("accel",))
        iterator = Mock()
        iterator.__aiter__ = Mock(return_value=iterator)
        iterator.__anext__ = AsyncMock(side_effect=[batch, batch, batch, asyncio.CancelledError])
        closed = Mock()
        @asynccontextmanager
        async def stream(*args, **kwargs):
            try:
                yield iterator
            finally:
                closed()
        opened = self.patch("open_calibrated_imu_stream", side_effect=stream)
        with patch.object(calibration.time, "monotonic", side_effect=[0.0, 0.01, 0.11]):
            with self.assertRaises(asyncio.CancelledError):
                await calibration.run_diagnostics(self.ring, device_id=DEVICE, emit=self.emit)
        opened.assert_called_once_with(self.ring, device_id=DEVICE)
        closed.assert_called_once()
        self.assertEqual(self.emit.call_count, 2)
        for call in self.emit.call_args_list:
            self.assertEqual(call.args, ("calibration_sample", {
                "accel_g": sample.accel_g, "gyro_dps": sample.gyro_dps,
                "applied_components": ("accel",),
            }))


if __name__ == "__main__":
    unittest.main()
