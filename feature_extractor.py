# SPDX-License-Identifier: MPL-2.0
"""HMM 特征提取：滑窗统计特征（均值/方差/RMS/过零率）。"""

from __future__ import annotations

import numpy as np


class FeatureExtractor:
    def __init__(self, window_size: int = 8, overlap: int = 4):
        self.window_size = window_size
        self.overlap = overlap

    def extract(self, filtered: np.ndarray) -> np.ndarray:
        """从滤波后 IMU 数据提取特征序列。

        输入 shape=(N, 6)，输出 shape=(n_frames, 24)。
        每帧 24 维：均值(6) + 方差(6) + RMS(6) + 过零率(6)。
        """
        step = self.window_size - self.overlap
        n_samples = filtered.shape[0]
        n_axes = filtered.shape[1]
        frames = []

        for start in range(0, n_samples - self.window_size + 1, step):
            window = filtered[start : start + self.window_size]
            mean = window.mean(axis=0)
            var = window.var(axis=0)
            rms = np.sqrt(np.mean(window**2, axis=0))
            zcr = np.zeros(n_axes)
            for ax in range(n_axes):
                sign_changes = np.diff(np.sign(window[:, ax]))
                zcr[ax] = np.count_nonzero(sign_changes) / self.window_size
            frames.append(np.concatenate([mean, var, rms, zcr]))

        if not frames:
            return np.empty((0, n_axes * 4))
        return np.array(frames)
