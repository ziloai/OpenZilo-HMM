# SPDX-License-Identifier: MPL-2.0
"""Headless acceleration view tests; no BLE consumer or display required."""
import math
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hmm_gesture_studio.acceleration import acceleration_vector
from hmm_gesture_studio.acceleration_view import AccelerationView, reading_text
from hmm_gesture_studio.plotting import RollingIMUBuffer


class Var:
    def __init__(self):
        self.value = None

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


def sample(x=0, y=0, z=4096):
    return [x, y, z, 0, 0, 0]


def make_view():
    view = AccelerationView.__new__(AccelerationView)
    view.buffer = RollingIMUBuffer()
    view._active = False
    view._range_g = None
    view._frozen = None
    view._dirty = view._layout_dirty = True
    view.canvas = Mock()
    view.canvas.winfo_ismapped.return_value = True
    view.canvas.winfo_width.return_value = 720
    view.canvas.winfo_height.return_value = 330
    for name in ("_magnitude", "_direction", "_gates", "_status", "_view_status"):
        setattr(view, name, Var())
    view._pause_button = Mock()
    return view


def activate(view, rate=25, scale=8):
    view.set_stream_state(dict(state="active", sample_rate_hz=rate, accel_range_g=scale))


class ReadingTextTests(unittest.TestCase):
    def test_waiting_zero_and_unknown_scale_are_explicit(self):
        self.assertIn("等待", reading_text(None)[0])
        magnitude, direction, gates = reading_text(acceleration_vector([0, 0, 0], accel_range_g=8))
        self.assertIn("0.0000 g", magnitude)
        self.assertIn("未定义", direction)
        self.assertEqual(gates.count("范围外"), 2)
        magnitude, direction, gates = reading_text(acceleration_vector([4096, 0, 0]))
        self.assertIn("量程未知", magnitude)
        self.assertIn("+1.0000", direction)
        self.assertNotIn("范围内", gates)
        self.assertNotIn("范围外", gates)

    def test_reference_bands_include_both_edges(self):
        for magnitude_g, narrow, wide in ((0.899, False, False), (0.9, False, True),
                                          (0.969, False, True), (0.97, True, True),
                                          (1.0, True, True), (1.03, True, True),
                                          (1.031, False, True), (1.1, False, True),
                                          (1.101, False, False)):
            with self.subTest(magnitude_g=magnitude_g):
                vector = acceleration_vector([0, 0, magnitude_g * 4096], accel_range_g=8)
                gates = reading_text(vector)[2]
                self.assertIn(f"0.97–1.03 g [{'范围内' if narrow else '范围外'}", gates)
                self.assertIn(f"0.90–1.10 g [{'范围内' if wide else '范围外'}", gates)
                self.assertIn("仅供观察", gates)
                self.assertNotIn("校准成功", gates)

    def test_ui_uses_generic_reference_copy(self):
        ttk = SimpleNamespace(Frame=Mock(), Button=Mock(), Label=Mock())
        tk = SimpleNamespace(ttk=ttk, Canvas=Mock(), StringVar=Mock())
        with patch.dict(sys.modules, {"tkinter": tk}):
            AccelerationView(Mock())
        texts = [call.kwargs.get("text", "") for call in ttk.Label.call_args_list]
        for vector in (None, acceleration_vector([0, 0, 4096]),
                       acceleration_vector([0, 0, 0], accel_range_g=8),
                       acceleration_vector([0, 0, 4096], accel_range_g=8)):
            texts.extend(reading_text(vector))
        text = "\n".join(texts)
        self.assertNotRegex(text, r"PR\s*#?\s*\d+|ring_sound")
        self.assertIn("1g ±3%", text)
        self.assertIn("1g ±10%", text)
        self.assertIn("不代表设备的静止、姿态融合或校准判定", text)

    def test_gravity_and_angle_wording_does_not_claim_linear_acceleration_or_attitude(self):
        magnitude, direction, _ = reading_text(acceleration_vector([0, 0, 4096], accel_range_g=8))
        self.assertIn("1.0000 g", magnitude)
        self.assertIn(f"{9.80665:.4f} m/s²", magnitude)
        self.assertIn("不是去重力", magnitude)
        self.assertIn("不是欧拉角", direction)
        self.assertIn("90.0° / 90.0° / 0.0°", direction)


class AccelerationViewTests(unittest.TestCase):
    def test_each_sample_contributes_to_curve_and_latest_controls_direction(self):
        view = make_view()
        activate(view)
        view.append_samples([sample(), sample(z=8192), sample(x=-4096, z=0)])
        view.redraw()
        self.assertIn("1.0000 g", view._magnitude.get())
        self.assertIn("(-1.0000, +0.0000, +0.0000)", view._direction.get())
        curves = [call for call in view.canvas.create_line.call_args_list if call.kwargs.get("tags") == "magnitude"]
        self.assertEqual(len(curves), 1)
        self.assertEqual(len(curves[0].args), 6)
        self.assertLess(curves[0].args[3], curves[0].args[1])  # Interior 2g peak is higher.
        thresholds = [call for call in view.canvas.create_line.call_args_list if call.kwargs.get("tags") == "threshold"]
        self.assertEqual(len(thresholds), 4)
        self.assertTrue(all(math.isfinite(x) for call in curves + thresholds for x in call.args))
        view.redraw()
        view.canvas.delete.assert_called_once_with("all")

    def test_zero_vector_draws_no_direction_arrow(self):
        view = make_view()
        activate(view)
        view.append_samples([sample(z=0)])
        view.redraw()
        self.assertIn("未定义", view._direction.get())
        self.assertFalse(any(call.kwargs.get("tags") == "direction"
                             for call in view.canvas.create_line.call_args_list))

    def test_missing_range_keeps_raw_curve_without_thresholds(self):
        view = make_view()
        activate(view, scale=None)
        view.append_samples([sample(), sample(z=8192)])
        view.redraw()
        self.assertIn("8192.00 raw", view._magnitude.get())
        self.assertIn("无法换算", view._magnitude.get())
        self.assertFalse(any(call.kwargs.get("tags") == "threshold"
                             for call in view.canvas.create_line.call_args_list))

    def test_hidden_view_defers_drawing_and_keeps_bounded_latest_samples(self):
        view = make_view()
        activate(view)
        view.canvas.winfo_ismapped.return_value = False
        view.append_samples([sample()] * 6000 + [sample(z=-4096)])
        self.assertEqual(len(view.buffer), 250)
        view.redraw()
        view.canvas.delete.assert_not_called()
        view.canvas.winfo_ismapped.return_value = True
        view.redraw()
        self.assertIn("-1.0000)", view._direction.get())

    def test_freeze_includes_numbers_directions_and_curve_and_resume_catches_up(self):
        view = make_view()
        activate(view)
        view.append_samples([sample()])
        view.redraw()
        view._toggle_pause()
        view.redraw()
        before = view._magnitude.get(), view._direction.get(), view._gates.get()
        view.append_samples([sample(z=-8192)])
        view.redraw()
        self.assertEqual(view.canvas.delete.call_count, 2)
        self.assertEqual((view._magnitude.get(), view._direction.get(), view._gates.get()), before)
        view._invalidate_layout()
        view.redraw()
        self.assertEqual(view._direction.get(), before[1])
        view._toggle_pause()
        view.redraw()
        self.assertIn("2.0000 g", view._magnitude.get())
        self.assertIn("-1.0000)", view._direction.get())

    def test_gap_and_metadata_change_clear_stale_values_even_when_frozen(self):
        view = make_view()
        activate(view)
        view.append_samples([sample()])
        view._toggle_pause()
        view.redraw()
        view.set_stream_state({"state": "waiting", "message": "IMU 暂停"})
        self.assertIsNone(view._range_g)
        self.assertEqual(view._frozen, ())
        self.assertEqual(view._direction.get(), "方向：—")
        view.append_samples([sample()])
        self.assertEqual(len(view.buffer), 0)
        activate(view, rate=100, scale=16)
        view._toggle_pause()
        view.append_samples([sample()])
        view.redraw()
        self.assertIn("2.0000 g", view._magnitude.get())
        activate(view, rate=100, scale=8)
        self.assertEqual(len(view.buffer), 0)
        self.assertEqual(view._direction.get(), "方向：—")
        view.append_samples([sample()])
        activate(view, rate=50, scale=8)
        self.assertEqual(len(view.buffer), 0)
        self.assertEqual(view.buffer.sample_rate_hz, 50)
        view.append_samples([sample()])
        activate(view, rate=50, scale=None)
        self.assertEqual(len(view.buffer), 0)
        self.assertIsNone(view._range_g)

    def test_tiny_canvas_still_clears_old_arrows_on_gap(self):
        view = make_view()
        activate(view)
        view.append_samples([sample()])
        view.redraw()
        view.canvas.reset_mock()
        view.canvas.winfo_height.return_value = 150
        view.set_stream_state({"state": "waiting"})
        view.redraw()
        view.canvas.delete.assert_called_once_with("all")
        view.canvas.create_line.assert_not_called()
        self.assertEqual(view._direction.get(), "方向：—")

    def test_clear_does_not_stop_capture(self):
        view = make_view()
        activate(view)
        view.append_samples([sample()])
        view._toggle_pause()
        view.clear()
        self.assertEqual(view._frozen, ())
        self.assertEqual(len(view.buffer), 0)
        view.append_samples([sample(z=8192)])
        self.assertEqual(len(view.buffer), 1)
        view._toggle_pause()
        view.redraw()
        self.assertIn("2.0000 g", view._magnitude.get())

    def test_import_has_no_tk_or_sdk_side_effects(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import hmm_gesture_studio.acceleration_view; "
                                 "assert 'tkinter' not in sys.modules; assert 'openzilo' not in sys.modules"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
