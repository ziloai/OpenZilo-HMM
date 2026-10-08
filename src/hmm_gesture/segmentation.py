# SPDX-License-Identifier: MPL-2.0
"""Motion segmentation, independent of a device or GUI event loop."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import numpy as np

from .config import SegmentationConfig
from .preprocessing import validate_samples


@dataclass(frozen=True)
class MotionSegment:
    """A completed segment and its half-open sample interval since reset()."""
    samples: np.ndarray
    start_sample: int
    end_sample: int


class MotionSegmenter:
    """Detect start/stop using distance to an idle baseline in raw IMU units.

    ``feed_all`` preserves every completed gesture, regardless of batch size.
    ``feed`` is the old single-result API (last result in a batch).
    """
    IDLE, ACTIVE, TAIL = 0, 1, 2

    def __init__(self, energy_threshold: float = 1500.0, min_onset_frames: int = 3,
                 min_offset_frames: int = 5, min_gesture_len: int = 12,
                 max_gesture_len: int = 125, pre_roll: int = 3,
                 cooldown_frames: int = 15, mode: str = "motion", post_roll: int = 0):
        self.config = SegmentationConfig(energy_threshold, min_onset_frames,
                                         min_offset_frames, min_gesture_len,
                                         max_gesture_len, pre_roll, cooldown_frames, mode, post_roll)
        for name in self.config.__dataclass_fields__:
            setattr(self, name, getattr(self.config, name))
        self.reset()

    def reset(self) -> None:
        self._state = self.IDLE
        self._baseline = np.zeros(6, dtype=np.float64)
        self._baseline_initialized = False
        self._onset_count = 0
        self._offset_count = 0
        self._buffer: list[list[float]] = []
        self._pre_buffer: deque[list[float]] = deque(
            maxlen=self.pre_roll + (1 if self.mode == "impulse" else self.min_onset_frames))
        self._cooldown_remaining = 0
        self.samples_seen = 0
        self._previous_accel = None
        self._post_remaining = 0
        self._next_trigger = 0
        self.incomplete_impulses = 0

    def feed_segments(self, samples: object) -> list[MotionSegment]:
        """Return segments with exact positions, including pre-roll, excluding rest tails."""
        segments = []
        for sample in validate_samples(samples, allow_empty=True).tolist():
            self.samples_seen += 1
            segment = self._feed_one(sample)
            if segment is not None:
                segments.append(segment)
        return segments

    def feed_all(self, samples: object) -> list[np.ndarray]:
        return [segment.samples for segment in self.feed_segments(samples)]

    def feed(self, samples: object) -> np.ndarray | None:
        segments = self.feed_all(samples)
        return segments[-1] if segments else None

    def _feed_one(self, sample: list[float]) -> MotionSegment | None:
        vec = np.asarray(sample, dtype=np.float64)
        if self.mode == "impulse":
            return self._feed_impulse(sample, vec)
        if not self._baseline_initialized:
            self._update_baseline(vec)
        if self._cooldown_remaining:
            self._cooldown_remaining -= 1
            self._update_baseline(vec)
            return None

        energy = float(np.linalg.norm(vec - self._baseline))
        if self._state == self.IDLE:
            self._pre_buffer.append(list(sample))
            if energy > self.energy_threshold:
                self._onset_count += 1
                if self._onset_count >= self.min_onset_frames:
                    self._state = self.ACTIVE
                    self._buffer = list(self._pre_buffer)
                    self._pre_buffer.clear()
                    self._onset_count = 0
            else:
                self._onset_count = 0
                self._update_baseline(vec)
            return None

        self._buffer.append(list(sample))
        self._offset_count = self._offset_count + 1 if energy < self.energy_threshold else 0
        if self._offset_count >= self.min_offset_frames:
            segment = self._buffer[:-self._offset_count]
            end_sample = self.samples_seen - self._offset_count
            self._finish()
            if self.min_gesture_len <= len(segment) <= self.max_gesture_len:
                return MotionSegment(np.asarray(segment, dtype=np.float64),
                                     end_sample - len(segment), end_sample)
        elif len(self._buffer) >= self.max_gesture_len + self.min_offset_frames:
            # Reject sustained movement rather than classifying an arbitrary truncation.
            self._finish()
        return None

    def _feed_impulse(self, sample: list[float], vec: np.ndarray) -> MotionSegment | None:
        # A snap can be a single acceleration impulse; requiring 3 consecutive
        # frames or return to the old gravity vector loses it. Use raw accel
        # first differences for triggering, and retain an exact context window.
        energy = 0.0 if self._previous_accel is None else float(np.linalg.norm(vec[:3] - self._previous_accel))
        self._previous_accel = vec[:3].copy()
        self._pre_buffer.append(list(sample))
        if self._state == self.ACTIVE:
            self._buffer.append(list(sample))
            self._post_remaining -= 1
            if self._post_remaining == 0:
                data = np.asarray(self._buffer, dtype=np.float64)
                self._buffer = []
                self._state = self.IDLE
                return MotionSegment(data, self.samples_seen - len(data), self.samples_seen)
            return None
        index = self.samples_seen - 1
        if index < self._next_trigger or energy < self.energy_threshold:
            return None
        # Cooldown is measured from the trigger, not added again after post-roll.
        self._next_trigger = index + self.cooldown_frames
        if len(self._pre_buffer) < self.pre_roll + 1:
            self.incomplete_impulses += 1
            return None
        self._buffer = list(self._pre_buffer)
        self._post_remaining = self.post_roll
        self._state = self.ACTIVE
        return None

    def _finish(self) -> None:
        self._state = self.IDLE
        self._buffer = []
        self._offset_count = self._onset_count = 0
        self._pre_buffer.clear()
        self._cooldown_remaining = self.cooldown_frames

    def _update_baseline(self, vec: np.ndarray) -> None:
        if not self._baseline_initialized:
            self._baseline = vec.copy()
            self._baseline_initialized = True
        else:
            self._baseline = 0.95 * self._baseline + 0.05 * vec


def impulse_peak(samples: object) -> float:
    """Largest adjacent-sample acceleration change in raw sensor units."""
    data = validate_samples(samples, allow_empty=True)
    return float(np.linalg.norm(np.diff(data[:, :3], axis=0), axis=1).max()) if len(data) > 1 else 0.0


def prepare_recording(samples: object, config: SegmentationConfig) -> np.ndarray | None:
    """Use the SAME impulse window for manual takes, validation and streaming.

    A manual impulse take must contain exactly one complete event with sufficient
    pre/post context. Never silently train on waiting time or choose one of several
    gestures. Legacy motion recordings remain whole segments.
    """
    data = validate_samples(samples, allow_empty=True)
    if config.mode == "motion":
        return data
    segmenter = MotionSegmenter(**asdict(config))
    segments = segmenter.feed_segments(data)
    if len(segments) != 1 or segmenter.incomplete_impulses or segmenter._state == segmenter.ACTIVE:
        return None
    return segments[0].samples
