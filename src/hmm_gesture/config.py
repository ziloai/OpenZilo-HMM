# SPDX-License-Identifier: MPL-2.0
"""Versioned preprocessing and stream segmentation settings."""
from __future__ import annotations

from dataclasses import dataclass
import math

AXES = ("ax", "ay", "az", "gx", "gy", "gz")
N_FEATURES = 24


def positive_number(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


def integer(name: str, value: int, minimum: int = 1) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")


@dataclass(frozen=True)
class PipelineConfig:
    sample_rate_hz: float = 25.0
    cutoff_hz: float = 10.0
    filter_order: int = 2
    median_kernel: int = 5
    window_size: int = 8
    window_overlap: int = 4

    def __post_init__(self) -> None:
        positive_number("sample_rate_hz", self.sample_rate_hz)
        positive_number("cutoff_hz", self.cutoff_hz)
        if self.cutoff_hz >= self.sample_rate_hz / 2:
            raise ValueError("cutoff_hz must be below half the sample rate")
        integer("filter_order", self.filter_order)
        integer("median_kernel", self.median_kernel)
        if self.median_kernel % 2 != 1:
            raise ValueError("median_kernel must be odd")
        integer("window_size", self.window_size, 2)
        integer("window_overlap", self.window_overlap, 0)
        if self.window_overlap >= self.window_size:
            raise ValueError("window_overlap must be smaller than window_size")

    @property
    def min_samples(self) -> int:
        """At least two complete feature windows."""
        return self.window_size * 2 - self.window_overlap


@dataclass(frozen=True)
class SegmentationConfig:
    energy_threshold: float = 1500.0
    min_onset_frames: int = 3
    min_offset_frames: int = 5
    min_gesture_len: int = 12
    max_gesture_len: int = 125
    pre_roll: int = 3
    cooldown_frames: int = 15

    def __post_init__(self) -> None:
        positive_number("energy_threshold", self.energy_threshold)
        for name in ("min_onset_frames", "min_offset_frames", "min_gesture_len", "max_gesture_len"):
            integer(name, getattr(self, name))
        integer("pre_roll", self.pre_roll, 0)
        integer("cooldown_frames", self.cooldown_frames, 0)
        if self.min_gesture_len > self.max_gesture_len:
            raise ValueError("min_gesture_len must not exceed max_gesture_len")
