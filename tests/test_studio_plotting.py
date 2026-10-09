# SPDX-License-Identifier: MPL-2.0
"""Headless plotting tests: neither Tk roots nor Bluetooth are needed."""
from collections import deque
import math
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hmm_gesture_studio.plotting import (
    IMUPlot, MAX_SAMPLES, MAX_REGIONS, RollingIMUBuffer, axis_limits, curve_points,
    RecordingPlot, RecognitionRegion, region_bounds,
)


def row(value=0):
    return [value] * 6


class FakeVariable:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def make_recording_plot(callback=None):
    plot = RecordingPlot.__new__(RecordingPlot)
    plot.canvas = Mock()
    plot.canvas.winfo_ismapped.return_value = True
    plot.canvas.winfo_width.return_value = 720
    plot.canvas.winfo_height.return_value = 400
    plot._overlay = FakeVariable(True)
    plot._selection_mode = FakeVariable(False)
    plot._selection_button = Mock()
    plot._reset_button = Mock()
    plot._status = Mock()
    plot._raw = plot._filtered = ()
    plot._regions = ()
    plot._training_region = None
    plot._rate = 25.0
    plot._min_samples = 1
    plot._on_training_region = callback
    plot._editable = True
    plot._drag_anchor = plot._drag_bounds = plot._drag_region = None
    plot._dirty = True
    plot._sync_editing()
    return plot


class FakeRecordingController:
    """Persist accepted proposals, then reload the authoritative preview."""
    def __init__(self, samples, pipeline, segmentation=None):
        self.samples = samples
        self.pipeline = pipeline
        self.segmentation = segmentation
        self.region = None
        self.rejected = []
        self.callback = Mock(side_effect=self.select)
        self.plot = make_recording_plot(self.callback)
        self.show()

    def show(self):
        self.plot.set_editable(False)
        self.plot.show_recording(self.samples, self.pipeline, segmentation=self.segmentation,
                                 training_region=self.region)
        self.plot.set_editable(True)

    def select(self, region):
        if region is not None and region[1] - region[0] < self.pipeline.min_samples:
            self.rejected.append(region)
            self.show()
            return
        self.region = region
        self.show()


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
from hmm_gesture_studio.plotting import RollingIMUBuffer, IMUPlot, RecordingPlot
assert len(RollingIMUBuffer()) == 0
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class RecordingPreviewTests(unittest.TestCase):
    def make_plot(self):
        return make_recording_plot()

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

    def test_manual_crop_overrides_automatic_filtering_and_preserves_raw(self):
        import numpy as np
        from hmm_gesture import PipelineConfig, SegmentationConfig
        from hmm_gesture.preprocessing import make_pipeline
        plot = self.make_plot()
        data = np.zeros((100, 6), dtype=np.int16)
        data[:, 2] = 2000
        data[:, 3] = np.arange(100) * 200
        data[50, 0] = 18000
        original = data.copy()
        pipeline = PipelineConfig(sample_rate_hz=100, cutoff_hz=30, median_kernel=1,
                                  filter_initialization="steady")
        segmentation = SegmentationConfig.for_sample_rate(100, mode="impulse", energy_threshold=8000)
        plot.show_recording(data, pipeline, segmentation=segmentation)
        self.assertEqual([(r.start_sample, r.end_sample) for r in plot._regions], [(38, 79)])
        automatic = np.asarray(plot._filtered)
        plot.show_recording(data, pipeline, segmentation=segmentation, training_region=[45, 65])
        signal_filter, _ = make_pipeline(pipeline)
        expected = signal_filter.apply(data)
        expected[45:65] = signal_filter.apply(data[45:65])
        np.testing.assert_array_equal(plot._filtered, expected)
        self.assertFalse(np.array_equal(automatic[45:65], expected[45:65]))
        np.testing.assert_array_equal(plot._raw, original)
        np.testing.assert_array_equal(data, original)
        self.assertEqual(plot._training_region, (45, 65))
        self.assertEqual(plot._regions, (RecognitionRegion(45, 65, "手动训练窗口"),))
        status = plot._status.set.call_args.args[0]
        for text in ("手动训练窗口", "[45, 65)", "0.45–0.65 秒", "20 帧", "不改变实时触发"):
            self.assertIn(text, status)
        self.assertNotIn("无法训练", status)
        plot.redraw()
        self.assertEqual(plot.canvas.create_rectangle.call_count, 2)
        # Restoring automatic mode must restore the original segment-local result.
        plot.show_recording(data, pipeline, segmentation=segmentation)
        np.testing.assert_array_equal(plot._filtered, automatic)
        self.assertIsNone(plot._training_region)

    def test_manual_crop_works_with_zero_windows_and_without_impulse_mode(self):
        from hmm_gesture import PipelineConfig, SegmentationConfig
        pipeline = PipelineConfig(median_kernel=1)
        data = [row()] * 100
        for segmentation in (None, SegmentationConfig.for_sample_rate(25, mode="impulse")):
            with self.subTest(segmentation=segmentation):
                plot = self.make_plot()
                plot.show_recording(data, pipeline, segmentation=segmentation, training_region=(10, 40))
                self.assertEqual(plot._regions, (RecognitionRegion(10, 40, "手动训练窗口"),))
                status = plot._status.set.call_args.args[0]
                self.assertIn("0.40–1.60 秒", status)
                self.assertIn("30 帧", status)
                self.assertNotIn("无法训练", status)

    def test_short_manual_crop_is_shown_as_invalid_not_trainable(self):
        from hmm_gesture import PipelineConfig
        plot = self.make_plot()
        pipeline = PipelineConfig(median_kernel=1)
        plot.show_recording([row()] * 50, pipeline, training_region=(10, 13))
        status = plot._status.set.call_args.args[0]
        self.assertIn("3 帧", status)
        self.assertIn(f"无法训练：手动窗口过短，至少需要 {pipeline.min_samples} 帧", status)
        self.assertIn("不改变实时触发", status)
        self.assertNotIn("只用框内训练", status)
        plot.redraw()
        self.assertEqual(plot.canvas.create_rectangle.call_count, 2)
        self.assertTrue(all(call.kwargs["outline"] == "#c0392b"
                            for call in plot.canvas.create_rectangle.call_args_list))
        self.assertTrue(any("无法训练" in call.kwargs.get("text", "")
                            for call in plot.canvas.create_text.call_args_list))

    def test_manual_crop_uses_shared_strict_interval_validation(self):
        from hmm_gesture import PipelineConfig
        plot = self.make_plot()
        pipeline = PipelineConfig()
        for region in ((-1, 12), (1, 1), (0, 101), (True, 12), (1.0, 12), [1], "1,12"):
            with self.subTest(region=region), self.assertRaises(ValueError):
                plot.show_recording([row()] * 100, pipeline, training_region=region)

    def test_hidden_preview_redraw_is_deferred_and_invalid_input_rejected(self):
        from hmm_gesture import PipelineConfig
        plot = self.make_plot()
        with self.assertRaises(ValueError):
            plot.show_recording([[40000] * 6] * 12, PipelineConfig())
        plot.canvas.winfo_ismapped.return_value = False
        plot.redraw()
        plot.canvas.delete.assert_not_called()


class RecordingSelectionTests(unittest.TestCase):
    @staticmethod
    def enable(plot):
        plot._selection_mode.set(True)
        plot._toggle_selection()

    @staticmethod
    def event(x, y=90):
        return SimpleNamespace(x=x, y=y)

    @staticmethod
    def edge(index, count=101):
        # Sample-cell edges are the same half-sample edges as the visible boxes.
        return 72 + (index - 0.5) * 630 / (count - 1)

    def make_plot(self):
        from hmm_gesture import PipelineConfig
        callback = Mock()
        plot = make_recording_plot(callback)
        plot.show_recording([row()] * 101, PipelineConfig(median_kernel=1))
        self.enable(plot)
        return plot, callback

    def drag(self, plot, start, end, y=90):
        plot._start_drag(self.event(start, y))
        plot._move_drag(self.event(end, y))
        plot._finish_drag(self.event(end, y))

    def test_constructor_controls_and_bindings_without_tk_root(self):
        ttk = SimpleNamespace(Frame=Mock(), Checkbutton=Mock(), Button=Mock(), Label=Mock())
        tk = SimpleNamespace(ttk=ttk, BooleanVar=FakeVariable, StringVar=FakeVariable, Canvas=Mock())
        callback = Mock()
        with patch.dict(sys.modules, {"tkinter": tk, "tkinter.ttk": ttk}):
            plot = RecordingPlot(None, on_training_region=callback)
        self.assertIs(plot._on_training_region, callback)
        self.assertEqual(ttk.Checkbutton.call_args.kwargs["text"], "手动框选")
        self.assertEqual(ttk.Button.call_args.kwargs["text"], "恢复自动裁剪")
        bindings = dict(call.args for call in plot.canvas.bind.call_args_list)
        self.assertEqual(bindings["<ButtonPress-1>"], plot._start_drag)
        self.assertEqual(bindings["<B1-Motion>"], plot._move_drag)
        self.assertEqual(bindings["<ButtonRelease-1>"], plot._finish_drag)
        plot._selection_button.configure.assert_called_with(state="disabled")
        plot._reset_button.configure.assert_called_with(state="disabled")

    def test_drag_mapping_full_take_reverse_exact_edges_and_clamping_on_both_charts(self):
        cases = ((72, 702, (0, 101)), (702, 72, (0, 101)),
                 (-100, 900, (0, 101)), (900, -100, (0, 101)),
                 (self.edge(10), self.edge(50), (10, 50)),
                 (self.edge(50), self.edge(10), (10, 50)),
                 (self.edge(80), 702, (80, 101)),
                 (701.9, 900, (100, 101)), (-100, 72.1, (0, 1)),
                 (72 + 10 * 6.3, 72 + 49 * 6.3, (10, 50)))
        for y in (90, 275):
            for start, end, expected in cases:
                with self.subTest(y=y, start=start, end=end):
                    plot, callback = self.make_plot()
                    self.drag(plot, start, end, y)
                    callback.assert_called_once_with(expected)
                    self.assertTrue(all(type(i) is int for i in callback.call_args.args[0]))
                    self.assertIsNone(plot._drag_region)
                    # Callback-only proposals do not optimistically change the view.
                    self.assertIsNone(plot._training_region)

    def test_drag_previews_one_shared_box_then_controller_replaces_and_resets(self):
        import numpy as np
        from hmm_gesture import PipelineConfig, SegmentationConfig
        data = np.zeros((101, 6), dtype=np.int16)
        original = data.copy()
        controller = FakeRecordingController(
            data, PipelineConfig(median_kernel=1), SegmentationConfig.for_sample_rate(25, mode="impulse"))
        plot = controller.plot
        self.assertIn("当前 0 个完整窗口", plot._status.set.call_args.args[0])
        self.enable(plot)
        plot._start_drag(self.event(self.edge(10)))
        plot._move_drag(self.event(self.edge(50)))
        self.assertEqual(plot._drag_region, (10, 50))
        controller.callback.assert_not_called()
        plot.redraw()
        boxes = plot.canvas.create_rectangle.call_args_list
        self.assertEqual(len(boxes), 2)
        for call, (top, bottom) in zip(boxes, ((32, 160), (217, 345))):
            self.assertAlmostEqual(call.args[0], self.edge(10))
            self.assertAlmostEqual(call.args[2], self.edge(50))
            self.assertEqual((call.args[1], call.args[3]), (top, bottom))
        self.assertTrue(any("手动训练窗口（预览）" in call.kwargs.get("text", "")
                            for call in plot.canvas.create_text.call_args_list))
        plot._finish_drag(self.event(self.edge(50)))
        controller.callback.assert_called_once_with((10, 50))
        self.assertEqual(controller.region, (10, 50))
        self.assertEqual(plot._training_region, (10, 50))
        plot._reset_button.configure.assert_called_with(state="normal")
        # Controller reloads keep the toggle active so a re-drag replaces the crop.
        self.assertTrue(plot._selection_mode.get())
        plot.canvas.create_rectangle.reset_mock()
        plot._start_drag(self.event(self.edge(80), 275))
        plot._move_drag(self.event(self.edge(40), 275))
        plot.redraw()
        self.assertEqual(plot.canvas.create_rectangle.call_count, 2)
        plot._finish_drag(self.event(self.edge(40), 275))
        self.assertEqual(controller.region, (40, 80))
        self.assertEqual(plot._regions, (RecognitionRegion(40, 80, "手动训练窗口"),))
        plot._restore_automatic()
        controller.callback.assert_called_with(None)
        self.assertIsNone(controller.region)
        self.assertEqual(plot._regions, ())
        self.assertIn("当前 0 个完整窗口", plot._status.set.call_args.args[0])
        self.assertFalse(plot._selection_mode.get())
        plot._reset_button.configure.assert_called_with(state="disabled")
        plot._restore_automatic()  # Already automatic: no redundant callback.
        self.assertEqual(controller.callback.call_count, 3)
        np.testing.assert_array_equal(data, original)
        np.testing.assert_array_equal(plot._raw, original)

    def test_clicks_no_horizontal_motion_and_vertical_margins_are_ignored(self):
        plot, callback = self.make_plot()
        plot._start_drag(self.event(200))
        plot._finish_drag(self.event(200, 110))
        plot._start_drag(self.event(200))
        plot._move_drag(self.event(400))
        plot._finish_drag(self.event(200))  # Moving back to the anchor is also empty.
        for y in (0, 31, 161, 200, 216, 346, 400):
            plot._start_drag(self.event(100, y))
            plot._finish_drag(self.event(600))
            plot._start_drag(self.event(100))
            plot._move_drag(self.event(600, y))
            self.assertIsNone(plot._drag_region)
            plot._finish_drag(self.event(600, y))
        self.drag(plot, 702, 900)  # Both positions clamp to the same edge.
        self.drag(plot, -100, 72)
        callback.assert_not_called()

    def test_disabled_read_only_short_and_empty_previews_do_not_edit(self):
        from hmm_gesture import PipelineConfig
        pipeline = PipelineConfig(median_kernel=1)
        for size in (0, pipeline.min_samples - 1, 101):
            for editable, has_callback in ((False, True), (True, False), (True, True)):
                if size == 101 and editable and has_callback:
                    continue
                with self.subTest(size=size, editable=editable, callback=has_callback):
                    callback = Mock()
                    plot = make_recording_plot(callback if has_callback else None)
                    if size:
                        plot.show_recording([row()] * size, pipeline, training_region=(0, size))
                    plot.set_editable(editable)
                    self.enable(plot)
                    self.assertFalse(plot._selection_mode.get())
                    plot._selection_button.configure.assert_called_with(state="disabled")
                    plot._reset_button.configure.assert_called_with(state="disabled")
                    self.drag(plot, 72, 702)
                    plot._restore_automatic()
                    callback.assert_not_called()
                    self.assertIsNone(plot._drag_anchor)
        # The exact minimum-length take remains editable, including its last sample.
        callback = Mock()
        plot = make_recording_plot(callback)
        plot.show_recording([row()] * pipeline.min_samples, pipeline)
        self.enable(plot)
        self.drag(plot, 72, 702)
        callback.assert_called_once_with((0, pipeline.min_samples))

    def test_temporary_disable_and_recording_switch_preserve_authoritative_regions(self):
        from hmm_gesture import PipelineConfig
        plot, callback = self.make_plot()
        pipeline = PipelineConfig(median_kernel=1)
        plot.show_recording([row()] * 101, pipeline, training_region=(10, 50))
        raw, filtered, regions = plot._raw, plot._filtered, plot._regions
        plot._start_drag(self.event(100))
        plot._move_drag(self.event(500))
        plot.set_editable(False)
        self.assertIsNone(plot._drag_region)
        self.assertTrue(plot._selection_mode.get())
        self.assertEqual((plot._raw, plot._filtered, plot._regions), (raw, filtered, regions))
        self.assertEqual(plot._training_region, (10, 50))
        plot._selection_button.configure.assert_called_with(state="disabled")
        plot._reset_button.configure.assert_called_with(state="disabled")
        plot._finish_drag(self.event(600))
        plot._restore_automatic()
        callback.assert_not_called()
        # Mimic GUI refreshes and switching repetitions with separate saved crops.
        for count, region in ((101, (10, 50)), (200, (70, 150)), (101, (10, 50))):
            plot.set_editable(False)
            plot.show_recording([row()] * count, pipeline, training_region=region)
            plot.set_editable(True)
            self.assertEqual(plot._training_region, region)
            self.assertEqual(plot._regions, (RecognitionRegion(*region, "手动训练窗口"),))
            self.assertTrue(plot._selection_mode.get())
            plot._reset_button.configure.assert_called_with(state="normal")
        callback.assert_not_called()
        self.drag(plot, self.edge(40), self.edge(80))
        callback.assert_called_once_with((40, 80))

    def test_short_drag_is_invalid_preview_and_controller_rejects_without_commit(self):
        from hmm_gesture import PipelineConfig
        controller = FakeRecordingController([row()] * 101, PipelineConfig(median_kernel=1))
        plot = controller.plot
        self.enable(plot)
        self.drag(plot, self.edge(10), self.edge(50))
        plot.canvas.create_rectangle.reset_mock()
        plot.canvas.create_text.reset_mock()
        plot._start_drag(self.event(self.edge(20)))
        plot._move_drag(self.event(self.edge(23)))
        plot.redraw()
        self.assertEqual(plot.canvas.create_rectangle.call_count, 2)
        self.assertTrue(all(call.kwargs["outline"] == "#c0392b"
                            for call in plot.canvas.create_rectangle.call_args_list))
        self.assertTrue(any("无法训练，至少 12 帧" in call.kwargs.get("text", "")
                            for call in plot.canvas.create_text.call_args_list))
        plot._finish_drag(self.event(self.edge(23)))
        self.assertEqual(controller.rejected, [(20, 23)])
        self.assertEqual(controller.region, (10, 50))
        self.assertEqual(plot._training_region, (10, 50))
        self.assertIsNone(plot._drag_region)

    def test_switch_clear_disable_toggle_resize_and_cancel_discard_unfinished_drag(self):
        from hmm_gesture import PipelineConfig
        for action in ("switch", "clear", "disable", "toggle", "resize", "cancel", "restore"):
            with self.subTest(action=action):
                plot, callback = self.make_plot()
                plot.show_recording([row()] * 101, PipelineConfig(median_kernel=1), training_region=(5, 40))
                plot._start_drag(self.event(100))
                plot._move_drag(self.event(500))
                self.assertIsNotNone(plot._drag_region)
                if action == "switch":
                    plot.show_recording([row(5)] * 200, PipelineConfig(median_kernel=1))
                elif action == "clear":
                    plot.clear()
                elif action == "disable":
                    plot.set_editable(False)
                    plot.set_editable(True)
                    self.enable(plot)
                elif action == "toggle":
                    plot._selection_mode.set(False)
                    plot._toggle_selection()
                    self.enable(plot)
                elif action == "resize":
                    plot._invalidate(self.event(0))
                elif action == "cancel":
                    plot._cancel_drag()
                else:
                    plot._restore_automatic()
                    callback.assert_called_once_with(None)
                    callback.reset_mock()
                self.assertIsNone(plot._drag_anchor)
                self.assertIsNone(plot._drag_region)
                plot._finish_drag(self.event(600))
                callback.assert_not_called()

    def test_hidden_or_tiny_plot_does_not_start_drag(self):
        for attribute, value in (("winfo_ismapped", False), ("winfo_width", 100), ("winfo_height", 100)):
            with self.subTest(attribute=attribute):
                plot, callback = self.make_plot()
                getattr(plot.canvas, attribute).return_value = value
                self.drag(plot, 72, 702)
                callback.assert_not_called()
                self.assertIsNone(plot._drag_anchor)


if __name__ == "__main__":
    unittest.main()
