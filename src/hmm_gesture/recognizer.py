# SPDX-License-Identifier: MPL-2.0
"""Public inference API: no GUI, BLE SDK, or training-platform imports."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
import math

import numpy as np

from .bundle import GestureBundle
from .preprocessing import make_pipeline, validate_samples
from .segmentation import MotionSegmenter, impulse_peak, prepare_recording


@dataclass(frozen=True)
class Prediction:
    name: str
    confidence: float | None  # No comparative confidence exists with only one label.
    score: float
    scores: dict[str, float]
    # Half-open indices in the feed() stream since reset(); None for predict().
    start_sample: int | None = None
    end_sample: int | None = None


class GestureRecognizer:
    """Classify recorded segments or consume continuous raw six-axis batches.

    Confidence is a relative score-gap heuristic, NOT a calibrated probability
    or unknown-gesture detector. With one model it is None, never a fixed 0.8.
    Calibrated bundles additionally reject implausible scores/strengths/lengths.
    A single uncalibrated model cannot be used for automatic stream detection.
    Instances maintain stream state and should not be shared across threads.
    """
    def __init__(self, bundle: GestureBundle, min_confidence: float = 0.0):
        if not math.isfinite(min_confidence) or not 0 <= min_confidence <= 1:
            raise ValueError("min_confidence must be between 0 and 1")
        self.bundle = bundle
        self.min_confidence = min_confidence
        self.filter, self.extractor = make_pipeline(bundle.pipeline)
        self.segmenter = MotionSegmenter(**asdict(bundle.segmentation))

    @classmethod
    def load(cls, path: str | Path, *, min_confidence: float = 0.0) -> GestureRecognizer:
        return cls(GestureBundle.load(path), min_confidence=min_confidence)

    @property
    def gesture_names(self) -> tuple[str, ...]:
        return self.bundle.gesture_names

    @property
    def sample_rate_hz(self) -> float:
        return self.bundle.pipeline.sample_rate_hz

    @property
    def has_rejection(self) -> bool:
        return self.bundle.rejection is not None

    def reset(self) -> None:
        """Reset stream state and baseline, e.g. after disconnect/reconnect."""
        self.segmenter.reset()

    def _check_rate(self, sample_rate_hz: float | None) -> None:
        if sample_rate_hz is not None and sample_rate_hz != self.sample_rate_hz:
            raise ValueError(f"Expected {self.sample_rate_hz:g} Hz, received {sample_rate_hz} Hz; no automatic resampling")

    def predict(self, samples: object, *, sample_rate_hz: float | None = None) -> Prediction | None:
        """Classify a complete (N, 6) gesture; return None if too short/rejected."""
        self._check_rate(sample_rate_hz)
        data = validate_samples(samples, allow_empty=True)
        if len(data) < self.bundle.pipeline.min_samples:
            return None
        data = prepare_recording(data, self.bundle.segmentation)
        return self._predict_segment(data) if data is not None else None

    def _predict_segment(self, data: np.ndarray) -> Prediction | None:
        if len(data) < self.bundle.pipeline.min_samples:
            return None
        features = self.extractor.extract(self.filter.apply(data))
        scores = {}
        for name, model in self.bundle.models.items():
            score = float(model.score(features))
            if np.isfinite(score):
                scores[name] = score
        if not scores:
            return None
        ranked = sorted(scores, key=scores.get, reverse=True)
        best = ranked[0]
        # Scores are per feature frame for comparability across segment lengths.
        normalized = {name: score / len(features) for name, score in scores.items()}
        if self.bundle.rejection is not None:
            limits = self.bundle.rejection[best]
            if (not limits.min_samples <= len(data) <= limits.max_samples
                    or normalized[best] < limits.score_floor
                    or impulse_peak(data) < limits.peak_floor):
                return None
        confidence = None if len(ranked) == 1 else float(-np.expm1(-(scores[best] - scores[ranked[1]]) / 10.0))
        if ((confidence is None and self.min_confidence > 0)
                or (confidence is not None and confidence < self.min_confidence)):
            return None
        return Prediction(best, confidence, normalized[best], normalized)

    def feed(self, samples: object, *, sample_rate_hz: float | None = None) -> list[Prediction]:
        """Consume a batch, returning ALL completed predictions (possibly none).

        Feed some resting samples first to establish the motion baseline. A
        trailing rest ends a gesture; incomplete segments are not auto-flushed.
        """
        self._check_rate(sample_rate_hz)
        data = validate_samples(samples, allow_empty=True)
        if not len(data):
            return []
        if len(self.bundle.models) == 1 and not self.has_rejection:
            raise ValueError("单类模型缺少拒识标定，无法区分普通运动；请用至少 3 次录制重新训练并开启拒识标定")
        results = []
        for segment in self.segmenter.feed_segments(data):
            prediction = self._predict_segment(segment.samples)
            if prediction is not None:
                results.append(replace(prediction, start_sample=segment.start_sample,
                                       end_sample=segment.end_sample))
        return results
