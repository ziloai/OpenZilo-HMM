# SPDX-License-Identifier: MPL-2.0
"""Motion segmentation, independent of a device or GUI event loop."""
from __future__ import annotations

from collections import deque
import numpy as np

from .config import SegmentationConfig
from .preprocessing import validate_samples


class MotionSegmenter:
    """Detect start/stop using distance to an idle baseline in raw IMU units.

    ``feed_all`` preserves every completed gesture, regardless of batch size.
    ``feed`` is the old single-result API (last result in a batch).
    """
    IDLE, ACTIVE, TAIL = 0, 1, 2

    def __init__(self, energy_threshold: float = 1500.0, min_onset_frames: int = 3,
                 min_offset_frames: int = 5, min_gesture_len: int = 12,
                 max_gesture_len: int = 125, pre_roll: int = 3,
                 cooldown_frames: int = 15):
        self.config = SegmentationConfig(energy_threshold, min_onset_frames,
                                         min_offset_frames, min_gesture_len,
                                         max_gesture_len, pre_roll, cooldown_frames)
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
            maxlen=self.pre_roll + self.min_onset_frames)
        self._cooldown_remaining = 0

    def feed_all(self, samples: object) -> list[np.ndarray]:
        segments = []
        for sample in validate_samples(samples, allow_empty=True).tolist():
            segment = self._feed_one(sample)
            if segment is not None:
                segments.append(segment)
        return segments

    def feed(self, samples: object) -> np.ndarray | None:
        segments = self.feed_all(samples)
        return segments[-1] if segments else None

    def _feed_one(self, sample: list[float]) -> np.ndarray | None:
        vec = np.asarray(sample, dtype=np.float64)
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
            self._finish()
            if self.min_gesture_len <= len(segment) <= self.max_gesture_len:
                return np.asarray(segment, dtype=np.float64)
        elif len(self._buffer) >= self.max_gesture_len + self.min_offset_frames:
            # Reject sustained movement rather than classifying an arbitrary truncation.
            self._finish()
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
