# SPDX-License-Identifier: MPL-2.0
"""Shared, deterministic preprocessing for both training and inference."""
from __future__ import annotations

import numpy as np
from scipy.signal import butter, medfilt, sosfilt, sosfilt_zi

from .config import PipelineConfig, integer


def validate_samples(samples: object, *, allow_empty: bool = False) -> np.ndarray:
    """Validate raw IMU rows without silently wrapping out-of-range int16 data."""
    try:
        data = np.asarray(samples, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("Samples must be numeric rows in ax,ay,az,gx,gy,gz order") from exc
    if allow_empty and data.shape == (0,):
        return np.empty((0, 6), dtype=np.float64)
    if data.ndim != 2 or data.shape[1] != 6:
        raise ValueError("Samples must have shape (N, 6): ax,ay,az,gx,gy,gz")
    if not allow_empty and not len(data):
        raise ValueError("Samples must not be empty")
    if not np.isfinite(data).all():
        raise ValueError("Samples must contain only finite numbers")
    if np.any(data < -32768) or np.any(data > 32767):
        raise ValueError("Samples must use raw signed 16-bit IMU units")
    return data


class SignalFilter:
    def __init__(self, sample_rate: float = 25.0, cutoff_hz: float = 10.0,
                 order: int = 2, median_kernel: int = 5, initialization: str = "zero"):
        PipelineConfig(sample_rate_hz=sample_rate, cutoff_hz=cutoff_hz,
                       filter_order=order, median_kernel=median_kernel,
                       filter_initialization=initialization)
        self.sample_rate = sample_rate
        self.cutoff_hz = cutoff_hz
        self.order = order
        self.median_kernel = median_kernel
        self.initialization = initialization
        self._sos = butter(order, cutoff_hz / (sample_rate / 2), btype="low", output="sos")

    def apply(self, raw: np.ndarray) -> np.ndarray:
        data = validate_samples(raw).copy()
        for ax in range(6):
            if self.median_kernel > 1:
                data[:, ax] = medfilt(data[:, ax], kernel_size=self.median_kernel)
            if self.initialization == "steady":
                data[:, ax], _ = sosfilt(self._sos, data[:, ax], zi=sosfilt_zi(self._sos) * data[0, ax])
            else:
                data[:, ax] = sosfilt(self._sos, data[:, ax])
        return data


class FeatureExtractor:
    def __init__(self, window_size: int = 8, overlap: int = 4):
        integer("window_size", window_size, 2)
        integer("overlap", overlap, 0)
        if overlap >= window_size:
            raise ValueError("overlap must be smaller than window_size")
        self.window_size = window_size
        self.overlap = overlap

    def extract(self, filtered: np.ndarray) -> np.ndarray:
        data = np.asarray(filtered, dtype=np.float64)
        if data.ndim != 2 or data.shape[1] != 6 or not np.isfinite(data).all():
            raise ValueError("Filtered data must have shape (N, 6) and be finite")
        frames = []
        for start in range(0, len(data) - self.window_size + 1, self.window_size - self.overlap):
            window = data[start:start + self.window_size]
            mean = window.mean(axis=0)
            var = window.var(axis=0)
            rms = np.sqrt(np.mean(window ** 2, axis=0))
            zcr = np.count_nonzero(np.diff(np.sign(window), axis=0), axis=0) / self.window_size
            frames.append(np.concatenate([mean, var, rms, zcr]))
        return np.asarray(frames) if frames else np.empty((0, 24))


def make_pipeline(config: PipelineConfig) -> tuple[SignalFilter, FeatureExtractor]:
    return (SignalFilter(config.sample_rate_hz, config.cutoff_hz,
                         config.filter_order, config.median_kernel, config.filter_initialization),
            FeatureExtractor(config.window_size, config.window_overlap))
