# SPDX-License-Identifier: MPL-2.0
"""Headless plotting tests: neither Tk roots nor Bluetooth are needed."""
from collections import deque
import math
import subprocess
import sys
import unittest
from unittest.mock import Mock

from hmm_gesture_studio.plotting import (
    IMUPlot, MAX_SAMPLES, MAX_REGIONS, RollingIMUBuffer, axis_limits, curve_points,
    RecordingPlot, RecognitionRegion, region_bounds,
)


def row(value=0):
    return [value] * 6


class RollingIMUBufferTests(unittest.TestCase):
    def test_long_batch_is_bounded_and_retains_latest(self):
        buffer = RollingIMUBuffer(sample_rate_hz=10000, seconds=10)
        buffer.append_samples([row(i) for i in range(12000)])
        self.assertEqual(buffer.capacity, MAX_SAMPLES)
        self.assertEqual(len(buffer), MAX_SAMPLES)
        self.assertEqual(buffer.samples[0], tuple(row(7000)))
        self.assertEqual(buffer.samples[-1], tuple(row(11999)))
        buffer.append_samples([row(-1)])
        self.assertEqual(len(buffer), MAX_SAMPLES)
        self.assertEqual(buffer.samples[0], tuple(row(7001)))

    def test_windows_and_sample_relative_time(self):
        for rate, seconds, expected in ((25, 10, 250), (50, 2, 100),
                                        (12.5, 1, 13), (0.1, 1, 1)):
            with self.subTest(rate=rate, seconds=seconds):
                buffer = RollingIMUBuffer(sample_rate_hz=rate, seconds=seconds)
                buffer.append_samples([row()] * 1000)
                self.assertEqual(len(buffer), expected)
                self.assertEqual(buffer.times()[-1], 0)
                self.assertAlmostEqual(buffer.times()[0], -(expected - 1) / rate)
                self.assertGreater(buffer.times()[0], -seconds)

    def test_clear_and_rate_change(self):
        buffer = RollingIMUBuffer()
        buffer.append_samples([row(1)])
        buffer.clear()
        self.assertEqual(buffer.samples, ())
        self.assertEqual(buffer.times(), ())
        buffer.append_samples([row(2)])
        buffer.set_sample_rate(50)
        self.assertEqual(buffer.capacity, 500)
        self.assertEqual(buffer.sample_rate_hz, 50)
        self.assertEqual(len(buffer), 0)
        buffer.append_samples([row(3)])
        with self.assertRaises(ValueError):
            buffer.set_sample_rate(0)
        self.assertEqual(buffer.sample_rate_hz, 50)
        self.assertEqual(buffer.samples, (tuple(row(3)),))

    def test_bad_configuration(self):
        invalid = (0, -1, float("nan"), float("inf"), -float("inf"),
                   True, "25", None, 10 ** 1000)
        for value in invalid:
            for name in ("seconds", "sample_rate_hz"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError):
                        RollingIMUBuffer(**{name: value})
        self.assertEqual(RollingIMUBuffer(sample_rate_hz=1e308,
                                         seconds=1e308).capacity, MAX_SAMPLES)
        self.assertEqual(RollingIMUBuffer(sample_rate_hz=1e-308,
                                         seconds=1e-308).capacity, 1)

    def test_bad_batches_are_atomic_including_discarded_prefix(self):
        buffer = RollingIMUBuffer(sample_rate_hz=1, seconds=1)
        buffer.append_samples([row(7)])
        invalid = (None, "bad", [None], [row()[:5]], [row() + [0]],
                   [row(1.0)], [row(True)], [row(float("nan"))],
                   [row(float("inf"))], [row(-32769)], [row(32768)], [row("0")])
        for batch in invalid:
            with self.subTest(batch=batch):
                with self.assertRaises(ValueError):
                    buffer.append_samples(batch)
                self.assertEqual(buffer.samples, (tuple(row(7)),))
        with self.assertRaises(ValueError):
            buffer.append_samples([row(2), row(float("nan"))] + [row(1)] * 100)
        self.assertEqual(buffer.samples, (tuple(row(7)),))

    def test_copies_input_and_accepts_int16_endpoints(self):
        buffer = RollingIMUBuffer()
        batch = [row(-32768), row(32767)]
        buffer.append_samples(batch)
        batch[0][0] = 123
        self.assertEqual(buffer.samples[0][0], -32768)
        self.assertEqual(buffer.samples[-1][-1], 32767)
        buffer.append_samples([])
        self.assertEqual(len(buffer), 2)


class CoordinateTests(unittest.TestCase):
    def points(self, samples, limits, bounds=(0, 0, 100, 100), rate=25):
        return curve_points(samples, 0, sample_rate_hz=rate, seconds=10,
                            bounds=bounds, limits=limits)

    def test_empty_zero_and_constant_scales_are_finite(self):
        for samples in ((), [row()] * 250, [row(32767)] * 250,
                        [row(-32768)] * 250):
            limits = axis_limits(samples, range(3))
            self.assertLess(limits[0], limits[1])
            points = self.points(samples, limits)
            self.assertTrue(all(math.isfinite(value) for value in points))
            self.assertTrue(all(0 <= y <= 100 for y in points[1::2]))
        self.assertEqual(axis_limits([row()] * 10, range(3)), (-1, 1))
        self.assertEqual(self.points([], (-1, 1)), ())
        self.assertEqual(self.points([row()], (-1, 1)), (100, 50))

    def test_scale_covers_all_axes_and_full_range(self):
        samples = [[-32768, 0, 32767, 1, 2, 3]]
        low, high = axis_limits(samples, range(3))
        self.assertLess(low, -32768)
        self.assertGreater(high, 32767)
        self.assertEqual(axis_limits(samples, range(3, 6)), (0, 4))

    def test_pixel_budget_keeps_spikes_and_sample_time_positions(self):
        samples = [row()] * 5000
        samples[2401] = row(100)
        samples[2402] = row(-100)
        points = self.points(samples, (-100, 100), rate=1000)
        self.assertLessEqual(len(points) // 2, 100)
        self.assertIn(0, points[1::2])
        self.assertIn(100, points[1::2])
        self.assertAlmostEqual(points[0], 50.01)
        self.assertEqual(points[-2], 100)
        self.assertEqual(sorted(points[::2]), list(points[::2]))


class PlotLifecycleTests(unittest.TestCase):
    def make_plot(self):
        plot = IMUPlot.__new__(IMUPlot)
        plot.buffer = RollingIMUBuffer()
        plot._dirty = plot._layout_dirty = True
        plot._frozen = None
        plot._regions = deque(maxlen=MAX_REGIONS)
        plot._frozen_regions = ()
        plot._frozen_end = 0
        plot.canvas = Mock()
        plot.canvas.winfo_ismapped.return_value = True
        plot.canvas.winfo_width.return_value = 720
        plot.canvas.winfo_height.return_value = 400
        plot._status = Mock()
        plot._view_status = Mock()
        plot._pause_button = Mock()
        return plot

    def test_batching_hidden_redraw_and_bounded_canvas_items(self):
        plot = self.make_plot()
        plot.append_samples([row()] * 1000)
        plot.append_samples([row(1)])
        plot.canvas.delete.assert_not_called()
        plot.canvas.winfo_ismapped.return_value = False
        plot.redraw()
        plot.canvas.delete.assert_not_called()
        plot.canvas.winfo_ismapped.return_value = True
        plot.redraw()
        plot.canvas.delete.assert_called_once_with("all")
        self.assertEqual(plot.canvas.create_line.call_count, 16)
        plot.redraw()
        self.assertEqual(plot.canvas.delete.call_count, 1)
        plot._invalidate_layout()
        plot.redraw()
        self.assertEqual(plot.canvas.delete.call_count, 2)
        plot.set_status("IMU已暂停 / 等待恢复")
        plot._status.set.assert_called_once_with("IMU已暂停 / 等待恢复")

    def test_freeze_receives_data_resize_preserves_view_and_resume_is_latest(self):
        plot = self.make_plot()
        plot.append_samples([row(1)])
        plot.redraw()
        plot._toggle_pause()
        plot.redraw()
        frozen = plot._frozen
        plot.append_samples([row(2)] * 300)
        plot.redraw()
        self.assertEqual(plot.canvas.delete.call_count, 2)
        self.assertEqual(plot.buffer.samples[-1], tuple(row(2)))
        plot._invalidate_layout()
        plot.redraw()
        self.assertEqual(plot._frozen, frozen)
        self.assertEqual(plot.canvas.delete.call_count, 3)
        plot._toggle_pause()
        plot.redraw()
        self.assertIsNone(plot._frozen)
        self.assertEqual(plot.canvas.delete.call_count, 4)
        plot._toggle_pause()
        plot.clear()
        self.assertEqual(plot._frozen, ())
        self.assertEqual(len(plot.buffer), 0)
        plot.append_samples([row(3)])
        plot.set_sample_rate(50)
        self.assertEqual(len(plot.buffer), 0)
        self.assertEqual(plot._frozen, ())

    def test_recognition_regions_roll_clip_freeze_and_clear(self):
        plot = self.make_plot()
        plot.append_samples([row()] * 100)
        plot.add_recognition(27, 50, "向上 0.95")
        plot.redraw()
        self.assertEqual(plot.canvas.create_rectangle.call_count, 2)  # Both axis groups.
        box = plot.canvas.create_rectangle.call_args.args
        self.assertTrue(72 <= box[0] < box[2] <= 702)
        plot._toggle_pause()
        plot.redraw()
        frozen_boxes = plot.canvas.create_rectangle.call_args_list[-2:]
        plot.append_samples([row(5)] * 300)
        self.assertEqual(len(plot._regions), 0)  # Expired, but frozen view retains it.
        plot._invalidate_layout()
        plot.redraw()
        self.assertEqual(plot.canvas.create_rectangle.call_args_list[-2:], frozen_boxes)
        plot._toggle_pause()
        plot.canvas.create_rectangle.reset_mock()
        plot.redraw()
        plot.canvas.create_rectangle.assert_not_called()
        before_clear = plot.next_sample
        plot.clear()
        self.assertEqual(plot.next_sample, before_clear)
        plot.append_samples([row()] * 30)
        plot.add_recognition(before_clear + 3, before_clear + 20, "new")
        self.assertEqual(len(plot._regions), 1)
        plot.set_sample_rate(50)
        self.assertEqual(plot.next_sample, 0)
        self.assertEqual(len(plot._regions), 0)

    def test_regions_are_bounded_and_reject_invalid_ranges(self):
        plot = self.make_plot()
        plot.append_samples([row()] * 250)
        for _ in range(MAX_REGIONS + 5):
            plot.add_recognition(1, 10, "same")
        self.assertEqual(len(plot._regions), MAX_REGIONS)
        for start, end in ((-1, 10), (5, 5), (1, 251), (True, 8), (1.5, 10)):
            with self.assertRaises(ValueError):
                plot.add_recognition(start, end, "bad")
        args = dict(next_sample=100, sample_count=50, sample_rate_hz=10,
                    seconds=5, bounds=(0, 0, 100, 100))
        self.assertIsNone(region_bounds(RecognitionRegion(1, 50, "old"), **args))
        self.assertIsNone(region_bounds(RecognitionRegion(100, 120, "future"), **args))
        box = region_bounds(RecognitionRegion(40, 70, "clipped"), **args)
        self.assertAlmostEqual(box[0], 1)  # First visible sample edge at index 49.5.
        self.assertAlmostEqual(box[2], 41)

    def test_import_does_not_need_tk(self):
        code = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'tkinter' or name.startswith('tkinter.'):
        raise ImportError('Tk intentionally unavailable')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from hmm_gesture_studio.plotting import RollingIMUBuffer, IMUPlot
assert len(RollingIMUBuffer()) == 0
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class RecordingPreviewTests(unittest.TestCase):
    def make_plot(self):
        plot = RecordingPlot.__new__(RecordingPlot)
        plot.canvas = Mock()
        plot.canvas.winfo_ismapped.return_value = True
        plot.canvas.winfo_width.return_value = 720
        plot.canvas.winfo_height.return_value = 400
        plot._overlay = Mock()
        plot._overlay.get.return_value = True
        plot._status = Mock()
        plot._raw = plot._filtered = ()
        plot._regions = ()
        plot._rate = 25.0
        plot._dirty = True
        return plot

    def test_preview_matches_training_filter_without_modifying_raw(self):
        import numpy as np
        from hmm_gesture import PipelineConfig
        from hmm_gesture.preprocessing import make_pipeline
        plot = self.make_plot()
        data = np.random.default_rng(3).integers(-10000, 10000, (600, 6))
        original = data.copy()
        config = PipelineConfig(sample_rate_hz=50, cutoff_hz=8)
        plot.show_recording(data, config)
        expected = make_pipeline(config)[0].apply(data)
        np.testing.assert_array_equal(plot._filtered, expected)
        np.testing.assert_array_equal(plot._raw, original)
        np.testing.assert_array_equal(data, original)
        self.assertFalse(np.array_equal(expected, expected.astype(int)))
        plot.redraw()
        # All 600 samples, not just a 10-second rolling tail, remain available.
        self.assertEqual(len(plot._filtered), 600)
        self.assertEqual(plot.canvas.create_line.call_count, 22)
        for call in plot.canvas.create_line.call_args_list:
            self.assertLessEqual(len(call.args), 2 * 630)
        plot.clear("参数无效")
        self.assertEqual(plot._filtered, ())
        plot._status.set.assert_called_with("参数无效")

    def test_impulse_preview_marks_training_context_and_filters_it_identically(self):
        import numpy as np
        from hmm_gesture import PipelineConfig, SegmentationConfig
        from hmm_gesture.preprocessing import make_pipeline
        plot = self.make_plot()
        data = np.zeros((100, 6))
        data[:, 2] = 2000
        data[50, 0] = 18000
        pipeline = PipelineConfig(sample_rate_hz=100, cutoff_hz=30, median_kernel=1,
                                  filter_initialization="steady")
        segmentation = SegmentationConfig.for_sample_rate(100, mode="impulse", energy_threshold=8000)
        plot.show_recording(data, pipeline, segmentation=segmentation)
        self.assertEqual([(r.start_sample, r.end_sample) for r in plot._regions], [(38, 79)])
        expected = make_pipeline(pipeline)[0].apply(data[38:79])
        np.testing.assert_array_equal(plot._filtered[38:79], expected)
        self.assertIn("41 帧", plot._status.set.call_args.args[0])
        self.assertIn("中值 关闭", plot._status.set.call_args.args[0])
        plot.redraw()
        self.assertEqual(plot.canvas.create_rectangle.call_count, 2)
        plot.show_recording(np.zeros((100, 6)), pipeline, segmentation=segmentation)
        self.assertIn("无法训练", plot._status.set.call_args.args[0])
        self.assertEqual(plot._regions, ())

    def test_hidden_preview_redraw_is_deferred_and_invalid_input_rejected(self):
        from hmm_gesture import PipelineConfig
        plot = self.make_plot()
        with self.assertRaises(ValueError):
            plot.show_recording([[40000] * 6] * 12, PipelineConfig())
        plot.canvas.winfo_ismapped.return_value = False
        plot.redraw()
        plot.canvas.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
