# SPDX-License-Identifier: MPL-2.0
"""信号滤波：中值滤波 + Butterworth 低通。"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, medfilt, sosfilt


class SignalFilter:
    def __init__(
        self,
        sample_rate: float = 25.0,
        cutoff_hz: float = 10.0,
        order: int = 2,
        median_kernel: int = 5,
    ):
        self.sample_rate = sample_rate
        self.cutoff_hz = cutoff_hz
        self.order = order
        self.median_kernel = median_kernel
        nyq = sample_rate / 2.0
        self._sos = butter(order, cutoff_hz / nyq, btype="low", output="sos")

    def apply(self, raw: np.ndarray) -> np.ndarray:
        """滤波 6-axis IMU 数据。输入/输出 shape=(N, 6)。"""
        data = raw.astype(np.float64)
        n_axes = data.shape[1]
        for ax in range(n_axes):
            if self.median_kernel > 1:
                data[:, ax] = medfilt(data[:, ax], kernel_size=self.median_kernel)
            data[:, ax] = sosfilt(self._sos, data[:, ax])
        return data
