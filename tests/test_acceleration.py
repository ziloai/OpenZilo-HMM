# SPDX-License-Identifier: MPL-2.0
"""Numerical and UI-independent coverage for acceleration diagnostics."""
import math
import unittest

import numpy as np

from hmm_gesture_studio.acceleration import acceleration_vector


class AccelerationVectorTests(unittest.TestCase):
    def test_vector_norm_and_direction(self):
        vector = acceleration_vector([3, 4, 0])
        self.assertEqual(vector.magnitude_raw, 5)
        self.assertEqual(vector.unit_direction, (0.6, 0.8, 0.0))
        self.assertAlmostEqual(vector.axis_angles_deg[0], 53.1301023542)
        self.assertEqual(vector.axis_angles_deg[2], 90)

    def test_signed_axes_and_axis_angles(self):
        for axis in range(3):
            for sign in (-1, 1):
                with self.subTest(axis=axis, sign=sign):
                    xyz = [0, 0, 0]
                    xyz[axis] = sign * 4096
                    vector = acceleration_vector(xyz, accel_range_g=8)
                    expected = [0, 0, 0]
                    expected[axis] = sign
                    self.assertEqual(vector.unit_direction, tuple(expected))
                    self.assertEqual(vector.axis_angles_deg[axis], 0 if sign == 1 else 180)
                    self.assertEqual(vector.magnitude_g, 1)
                    self.assertEqual(vector.xyz_g, tuple(expected))
                    self.assertAlmostEqual(vector.magnitude_m_s2, 9.80665)

    def test_all_components_contribute_to_physical_magnitude(self):
        vector = acceleration_vector([8192, -8192, 8192], accel_range_g=4)
        self.assertAlmostEqual(vector.magnitude_g, math.sqrt(3))
        self.assertAlmostEqual(sum(v * v for v in vector.unit_direction), 1)

    def test_int16_extremes_do_not_overflow(self):
        vector = acceleration_vector(np.array([-32768, 32767, -32768], dtype=np.int16),
                                     accel_range_g=16)
        self.assertAlmostEqual(vector.magnitude_raw, math.sqrt(32768**2 * 2 + 32767**2))
        self.assertGreater(vector.magnitude_g, 16)
        self.assertTrue(all(math.isfinite(v) for v in vector.unit_direction))

    def test_zero_has_no_direction(self):
        vector = acceleration_vector([0, 0, 0], accel_range_g=2)
        self.assertEqual(vector.magnitude_raw, 0)
        self.assertEqual(vector.magnitude_g, 0)
        self.assertEqual(vector.magnitude_m_s2, 0)
        self.assertIsNone(vector.unit_direction)
        self.assertIsNone(vector.axis_angles_deg)

    def test_missing_or_invalid_range_never_assumes_physical_units(self):
        for scale in (None, 0, -8, True, "8", float("nan"), float("inf"), 10**1000):
            with self.subTest(scale=scale):
                vector = acceleration_vector([0, 0, 4096], accel_range_g=scale)
                self.assertEqual(vector.magnitude_raw, 4096)
                self.assertEqual(vector.unit_direction, (0, 0, 1))
                self.assertIsNone(vector.magnitude_g)
                self.assertIsNone(vector.magnitude_m_s2)
                self.assertIsNone(vector.xyz_g)

    def test_invalid_vector_rejected(self):
        for xyz in ([], [1, 2], [1, 2, 3, 4], [True, 0, 0], ["1", 0, 0],
                    [math.nan, 0, 0], [math.inf, 0, 0]):
            with self.subTest(xyz=xyz):
                with self.assertRaises(ValueError):
                    acceleration_vector(xyz)


if __name__ == "__main__":
    unittest.main()
