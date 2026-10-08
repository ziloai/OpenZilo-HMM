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
    filter_initialization: str = "zero"  # v1 compatibility; new Studio uses steady state.

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
        if not isinstance(self.filter_initialization, str) or self.filter_initialization not in {"zero", "steady"}:
            raise ValueError("filter_initialization must be zero or steady")

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
    mode: str = "motion"
    post_roll: int = 0

    def __post_init__(self) -> None:
        positive_number("energy_threshold", self.energy_threshold)
        for name in ("min_onset_frames", "min_offset_frames", "min_gesture_len", "max_gesture_len"):
            integer(name, getattr(self, name))
        integer("pre_roll", self.pre_roll, 0)
        integer("cooldown_frames", self.cooldown_frames, 0)
        if self.min_gesture_len > self.max_gesture_len:
            raise ValueError("min_gesture_len must not exceed max_gesture_len")
        if not isinstance(self.mode, str) or self.mode not in {"motion", "impulse"}:
            raise ValueError("segmentation mode must be motion or impulse")
        integer("post_roll", self.post_roll, 0)
        if self.mode == "impulse":
            if self.post_roll < 1:
                raise ValueError("impulse segmentation requires post_roll >= 1")
            if not self.min_gesture_len <= self.pre_roll + 1 + self.post_roll <= self.max_gesture_len:
                raise ValueError("impulse context length must be within gesture length limits")

    @classmethod
    def for_sample_rate(cls, sample_rate_hz: float, *, mode: str = "motion",
                        energy_threshold: float = 1500.0, min_samples: int = 12):
        """Explicit time-based Studio presets; legacy constructors keep v1 frame counts."""
        positive_number("sample_rate_hz", sample_rate_hz)
        integer("min_samples", min_samples)
        frames = lambda seconds: max(1, round(seconds * sample_rate_hz))
        pre = frames(0.12)
        post = max(frames(0.28), min_samples - pre - 1) if mode == "impulse" else 0
        return cls(energy_threshold=energy_threshold,
                   min_onset_frames=1 if mode == "impulse" else frames(0.12),
                   min_offset_frames=frames(0.20),
                   min_gesture_len=min_samples if mode == "impulse" else max(min_samples, frames(0.48)),
                   max_gesture_len=max(frames(5.0), min_samples, pre + post + 1),
                   pre_roll=pre, cooldown_frames=frames(0.60), mode=mode, post_roll=post)


@dataclass(frozen=True)
class RejectionCalibration:
    """Positive-example acceptance limits, NOT a calibrated probability of a gesture."""
    score_floor: float
    peak_floor: float
    min_samples: int
    max_samples: int

    def __post_init__(self):
        if isinstance(self.score_floor, bool) or not isinstance(self.score_floor, (int, float)) or not math.isfinite(self.score_floor):
            raise ValueError("score_floor must be finite")
        if isinstance(self.peak_floor, bool) or not isinstance(self.peak_floor, (int, float)) or not math.isfinite(self.peak_floor) or self.peak_floor < 0:
            raise ValueError("peak_floor must be finite and nonnegative")
        integer("min_samples", self.min_samples)
        integer("max_samples", self.max_samples)
        if self.min_samples > self.max_samples:
            raise ValueError("calibration min_samples must not exceed max_samples")
