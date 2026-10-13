# SPDX-License-Identifier: MPL-2.0
"""Acceleration diagnostics in the sensor frame, independent of Tk and BLE.

Raw accelerometer readings include gravity. Their direction is neither a world
heading nor the direction of velocity. Physical units use the reported signed
int16 full-scale range; no range is guessed when metadata is unavailable.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from numbers import Real

STANDARD_GRAVITY = 9.80665
# Display-only reference bands around 1 g, not firmware detection thresholds.
NARROW_MAGNITUDE_BAND = (0.97, 1.03)
WIDE_MAGNITUDE_BAND = (0.90, 1.10)


def acceleration_scale(accel_range_g) -> float | None:
    """g per raw count, or None if full-scale metadata is unavailable/invalid."""
    if isinstance(accel_range_g, bool) or not isinstance(accel_range_g, Real):
        return None
    try:
        value = float(accel_range_g)
    except (OverflowError, ValueError):
        return None
    return value / 32768.0 if math.isfinite(value) and value > 0 else None


@dataclass(frozen=True)
class AccelerationVector:
    xyz_raw: tuple[float, float, float]
    magnitude_raw: float
    unit_direction: tuple[float, float, float] | None
    magnitude_g: float | None
    xyz_g: tuple[float, float, float] | None

    @property
    def magnitude_m_s2(self) -> float | None:
        return None if self.magnitude_g is None else self.magnitude_g * STANDARD_GRAVITY

    @property
    def axis_angles_deg(self) -> tuple[float, float, float] | None:
        """Angles to sensor +X/+Y/+Z, not Euler attitude angles."""
        if self.unit_direction is None:
            return None
        return tuple(math.degrees(math.acos(max(-1.0, min(1.0, v))))
                     for v in self.unit_direction)


def acceleration_vector(xyz, *, accel_range_g=None) -> AccelerationVector:
    """Describe a three-axis vector without int16 squaring/overflow.

    ``xyz`` may also be a floating-point residual after subtracting a baseline.
    A zero vector has no direction. Invalid/missing range metadata leaves only
    raw units available, rather than silently assuming (for example) +/-8 g.
    """
    values = tuple(xyz)
    if len(values) != 3 or any(isinstance(v, bool) or not isinstance(v, Real)
                               or not math.isfinite(v) for v in values):
        raise ValueError("acceleration must contain three finite numbers")
    values = tuple(float(v) for v in values)
    magnitude = math.hypot(*values)
    direction = tuple(v / magnitude for v in values) if magnitude else None
    scale = acceleration_scale(accel_range_g)
    magnitude_g = magnitude * scale if scale is not None else None
    xyz_g = tuple(v * scale for v in values) if scale is not None else None
    return AccelerationVector(values, magnitude, direction, magnitude_g, xyz_g)
