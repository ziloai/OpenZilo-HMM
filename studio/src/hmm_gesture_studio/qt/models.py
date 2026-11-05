# SPDX-License-Identifier: MPL-2.0
"""Chart data and recording analysis, independent of Qt widgets and Bluetooth."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import math

import numpy as np

from ..acceleration import acceleration_scale, acceleration_vector
from ..plotting import MAX_REGIONS, RecognitionRegion, RollingIMUBuffer


def seconds_axis(count, rate):
    """Seconds relative to the newest sample, estimated from the sample rate."""
    return (np.arange(count) - (count - 1)) / rate


def visible_region_span(region, *, next_sample, count, rate, seconds):
    """Clip a half-open stream interval to the visible samples, in relative seconds."""
    first = next_sample - count
    if not count or region.end_sample <= first or region.start_sample >= next_sample:
        return None

    def x(index):
        return -(next_sample - 1 - index) / rate
    start = max(-seconds, x(max(first, region.start_sample) - 0.5))
    stop = min(0.0, x(min(next_sample, region.end_sample) - 0.5))
    return (start, stop) if stop > start else None


class LiveStreamModel:
    """Rolling six-axis data shared by chart views. Freezing affects display only."""

    def __init__(self, *, sample_rate_hz=25.0, seconds=10.0):
        self.buffer = RollingIMUBuffer(sample_rate_hz=sample_rate_hz, seconds=seconds)
        self.revision = 0
        self._regions = deque(maxlen=MAX_REGIONS)
        self._frozen = None
        self._frozen_regions = ()
        self._frozen_end = 0

    @property
    def next_sample(self):
        return self.buffer.next_sample

    @property
    def paused(self):
        return self._frozen is not None

    def append_samples(self, samples):
        self.buffer.append_samples(samples)
        first = self.next_sample - len(self.buffer)
        self._regions = deque((r for r in self._regions if r.end_sample > first), maxlen=MAX_REGIONS)
        if samples:
            self.revision += 1

    def add_recognition(self, start_sample, end_sample, label):
        if (type(start_sample) is not int or type(end_sample) is not int
                or not 0 <= start_sample < end_sample <= self.next_sample):
            raise ValueError("recognition interval must be within the received stream")
        if end_sample > self.next_sample - len(self.buffer):
            self._regions.append(RecognitionRegion(start_sample, end_sample, str(label)))
            self.revision += 1

    def clear(self):
        # Keep the stream clock: clearing a view must not shift recognition boxes.
        self.buffer.clear()
        self._regions.clear()
        self._frozen_regions = ()
        if self._frozen is not None:
            self._frozen = ()
            self._frozen_end = self.next_sample
        self.revision += 1

    def set_sample_rate(self, rate):
        self.buffer.set_sample_rate(rate)
        self.clear()

    def toggle_pause(self):
        if self._frozen is None:
            self._frozen = self.buffer.samples
            self._frozen_end = self.next_sample
            self._frozen_regions = tuple(self._regions)
        else:
            self._frozen = None
        self.revision += 1

    def snapshot(self):
        """Return (x seconds, Nx6 samples, [(start_s, stop_s, label)]) for drawing."""
        samples = self.buffer.samples if self._frozen is None else self._frozen
        end = self.next_sample if self._frozen is None else self._frozen_end
        regions = self._regions if self._frozen is None else self._frozen_regions
        rate, seconds = self.buffer.sample_rate_hz, self.buffer.seconds
        data = np.asarray(samples, dtype=float).reshape(-1, 6)
        spans = []
        for region in regions:
            span = visible_region_span(region, next_sample=end, count=len(data), rate=rate, seconds=seconds)
            if span is not None:
                spans.append((*span, region.label))
        return seconds_axis(len(data), rate), data, spans


class AccelerationModel:
    """Bounded magnitude history; stale values are cleared when the stream changes."""

    IDLE_STATUS = "等待 IMU；量程未知"

    def __init__(self):
        self.buffer = RollingIMUBuffer()
        self.revision = 0
        self.range_g = None
        self.status = self.IDLE_STATUS
        self._active = False
        self._frozen = None

    @property
    def paused(self):
        return self._frozen is not None

    def set_stream_state(self, payload):
        active = payload["state"] == "active"
        if active:
            rate = float(payload["sample_rate_hz"])
            scale = acceleration_scale(payload.get("accel_range_g"))
            new_range = None if scale is None else scale * 32768.0
            if not self._active or self.buffer.sample_rate_hz != rate or self.range_g != new_range:
                self.buffer.set_sample_rate(rate)
                self.clear()
            self.range_g = new_range
            units = f"量程 ±{new_range:g} g" if new_range is not None else "量程未知，仅显示 raw"
            self.status = f"IMU 正常上报；{rate:g} Hz；{units}；时间轴按采样率估算，非设备时间戳"
        else:
            self.range_g = None
            self.clear()
            self.status = payload.get("message", "无新 IMU 数据；旧值已清除")
        self._active = active
        self.revision += 1

    def append_samples(self, samples):
        if not self._active:
            return
        self.buffer.append_samples(samples)
        if samples:
            self.revision += 1

    def clear(self):
        self.buffer.clear()
        if self._frozen is not None:
            self._frozen = ()
        self.revision += 1

    def toggle_pause(self):
        self._frozen = self.buffer.samples if self._frozen is None else None
        self.revision += 1

    def snapshot(self):
        """Return (x seconds, magnitudes in g or raw, latest vector or None, scale)."""
        samples = self.buffer.samples if self._frozen is None else self._frozen
        scale = acceleration_scale(self.range_g)
        data = np.asarray(samples, dtype=float).reshape(-1, 6)
        magnitudes = np.sqrt((data[:, :3] ** 2).sum(axis=1)) * (scale if scale is not None else 1.0)
        vector = acceleration_vector(samples[-1][:3], accel_range_g=self.range_g) if samples else None
        return seconds_axis(len(data), self.buffer.sample_rate_hz), magnitudes, vector, scale


@dataclass(frozen=True)
class RecordingView:
    raw: np.ndarray
    filtered: np.ndarray
    regions: tuple
    training_region: tuple | None
    rate: float
    min_samples: int
    status: str

    @property
    def seconds(self):
        return max(1, len(self.raw) - 1) / self.rate


def analyze_recording(samples, pipeline, *, segmentation=None, training_region=None) -> RecordingView:
    """Show exactly what training sees; raw data stays untouched and floating-point
    filter output may overshoot int16."""
    from hmm_gesture.preprocessing import make_pipeline, validate_samples
    from hmm_gesture.segmentation import MotionSegmenter, impulse_peak, prepare_recording
    from ..datasets import validate_training_region

    data = validate_samples(samples)
    training_region = validate_training_region(training_region, len(data))
    signal_filter, _ = make_pipeline(pipeline)
    filtered = signal_filter.apply(data)
    regions = ()
    detail = "整段参与训练"
    if training_region is not None:
        start, end = training_region
        regions = (RecognitionRegion(start, end, "手动训练窗口"),)
        filtered[start:end] = signal_filter.apply(data[start:end])
        detail = (f"手动训练窗口：样本 [{start}, {end})，"
                  f"{start / pipeline.sample_rate_hz:.2f}–{end / pipeline.sample_rate_hz:.2f} 秒，{end - start} 帧")
        if end - start < pipeline.min_samples:
            detail += f"；无法训练：手动窗口过短，至少需要 {pipeline.min_samples} 帧"
        else:
            detail += "；只用框内训练，框外不参与"
        detail += "；手动裁剪不改变实时触发条件"
    elif segmentation is not None and segmentation.mode == "impulse":
        segments = MotionSegmenter(**asdict(segmentation)).feed_segments(data)
        regions = tuple(RecognitionRegion(s.start_sample, s.end_sample, "训练窗口")
                        for s in segments[:MAX_REGIONS])
        # Inside each box show the segment-local filtering the model actually sees.
        for segment in segments:
            filtered[segment.start_sample:segment.end_sample] = signal_filter.apply(segment.samples)
        if prepare_recording(data, segmentation) is not None:
            detail = f"只用紫框内 {len(segments[0].samples)} 帧训练，框外等待不参与"
        else:
            detail = f"无法训练：需恰好一个完整冲击及前后文，当前 {len(segments)} 个完整窗口"
        if len(segments) > MAX_REGIONS:
            detail += f"（仅显示前 {MAX_REGIONS} 框）"
    rate = pipeline.sample_rate_hz
    peak = impulse_peak(data)
    attenuation = ""
    if peak > 1000:
        ratio = float(np.linalg.norm(np.diff(filtered[:, :3], axis=0), axis=1).max()) / peak
        attenuation = f"；相邻加速度变化峰值保留 {ratio:.0%}"
        if ratio < 0.3:
            attenuation += "（短促动作可能被过度平滑）"
    median = "关闭" if pipeline.median_kernel == 1 else pipeline.median_kernel
    status = (f"{len(data)} 样本 / {len(data) / rate:.2f} 秒；{rate:g} Hz；中值 {median} → "
              f"{pipeline.filter_order} 阶低通 {pipeline.cutoff_hz:g} Hz（当前训练参数，原始数据不变）"
              f"{attenuation}；{detail}")
    return RecordingView(data, filtered, regions, training_region, rate, pipeline.min_samples, status)


def recording_region_span(region, view):
    """A half-open sample interval in seconds, with half-sample cell edges."""
    start = max(0.0, (region.start_sample - 0.5) / view.rate)
    stop = min(view.seconds, (region.end_sample - 0.5) / view.rate)
    return (start, stop) if stop > start else None


def drag_to_region(x0, x1, sample_count, seconds):
    """Select the sample cells a horizontal drag touches; None if nothing selected.
    The tiny tolerance keeps exact cell-edge drags stable under float math."""
    low, high = sorted((max(0.0, min(seconds, x0)), max(0.0, min(seconds, x1))))
    if low == high or sample_count < 1:
        return None
    scale = (sample_count - 1) / seconds
    start = max(0, math.floor(low * scale + 0.5 + 1e-9))
    end = min(sample_count, math.ceil(high * scale + 0.5 - 1e-9))
    return (start, end) if start < end else None
