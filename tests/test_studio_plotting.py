# SPDX-License-Identifier: MPL-2.0
"""Headless plotting tests: neither Tk roots nor Bluetooth are needed."""
import math
import subprocess
import sys
import unittest
from unittest.mock import Mock

from hmm_gesture_studio.plotting import (
    IMUPlot, MAX_SAMPLES, RollingIMUBuffer, axis_limits, curve_points,
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


if __name__ == "__main__":
    unittest.main()
