# SPDX-License-Identifier: MPL-2.0
"""Application state and rules for the new interface; owns no widgets.

Main-thread only. Worker and executor callbacks only put events on a queue, which
is drained on a timer and at recording boundaries. Pages read state from here and
re-render on stateChanged, so enablement has a single source of truth. Dialogs are
injected; every modal runs a nested event loop, so state is rechecked afterwards.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import queue
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from hmm_gesture import GestureBundle, GestureRecognizer, PipelineConfig, SegmentationConfig

from ..datasets import GestureDataset, dataset_path, load_dataset, save_dataset
from .i18n import Text, resolve, tr
from .models import AccelerationModel, LiveStreamModel, analyze_recording
from .plugins_controller import PluginsController
from .tools_controller import ToolsController

MIN_TAKE_SAMPLES = 12
PARAM_DEFAULTS = dict(n_states="6", cutoff_hz="10", window_size="8", window_overlap="4",
                      sample_rate_hz="25", energy_threshold="1500", median_kernel="5",
                      segmentation_mode="motion")
_STARTUP_HINTS = (
    tr("每次只录制一个完整动作；拒识标定每类至少 3 次，留出评估至少 4 次，建议采集更多变化。"),
    tr("原始六轴顺序：ax, ay, az, gx, gy, gz；录制/识别保留原始单位，合加速度与方向请看“诊断”页。"),
    tr("响指/敲击请先应用短促动作预设；单类不显示虚构置信度，正样本标定仍不能保证消除误触。"),
    tr("训练、录制和识别必须使用相同采样率。"),
    tr("连接后订阅 IMU，无需切换模式；录音页支持远程录音、实时 PCM 保存与保存通知监听。"),
)


class _JoinedText(Text):
    """A flat, lazy sequence: large dataset summaries must not nest tr() calls."""

    def __new__(cls, parts, separator):
        parts = tuple(parts)
        rendered = separator.join(resolve(part) for part in parts)
        text = str.__new__(cls, rendered)
        text.source = "{parts}"
        text.values = {"parts": parts, "separator": separator}
        return text

    def translated(self):
        return self.values["separator"].join(resolve(part) for part in self.values["parts"])


def _join_text(parts, separator="\n"):
    """Keep translations intact with linear formatting and constant nesting."""
    return _JoinedText(parts, separator)


def _exception_text(exc):
    # ValueError(tr(...)) retains its lazy message in args, unlike str(exc).
    return exc.args[0] if len(exc.args) == 1 and isinstance(exc.args[0], str) else str(exc)


class StudioController(QObject):
    stateChanged = Signal()
    logged = Signal(str, str, object)  # time, level (info/warning/error), text
    paramsReplaced = Signal()
    previewChanged = Signal()
    previewRevealed = Signal()
    datasetsChanged = Signal()
    devicesChanged = Signal()
    audioFieldsChanged = Signal()
    audioFilesChanged = Signal()
    remoteAudioChanged = Signal()
    tick = Signal()
    closeFinished = Signal()

    def __init__(self, dialogs, *, worker=None, history=None, directory=None, audio_directory=None, plugin_root=None):
        super().__init__()
        self.dialogs = dialogs
        self.events = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1)
        if worker is None:
            from ..device import RingWorker
            worker = RingWorker(lambda event, payload: self.events.put((event, payload)))
        self.worker = worker
        if history is None:
            from ..device_history import DeviceHistory
            history = DeviceHistory()
        self.device_history = history
        self.directory = Path(directory) if directory is not None else Path.cwd() / "gestures"
        self.audio_directory = Path(audio_directory) if audio_directory is not None else Path.cwd() / "audio"
        self.tools = ToolsController(self)
        self.stream = LiveStreamModel()
        self.acceleration = AccelerationModel()
        self.params = dict(PARAM_DEFAULTS)
        self._setting_preset = False
        # Datasets; row ids change on each refresh so stale selections are detectable.
        self.records = {}
        self.invalid_files = []
        self.selected_ids = []
        self._next_record_id = 0
        # Device.
        self.address = ""
        self.device_entries = []
        self.scanned_devices = []
        self.connected = False
        self.connecting = False
        self.scanning = False
        self.device_name = ""
        self.actual_rate = None
        self.imu_active = False
        self.count = 0
        self.latest_sample = None
        self.status = tr("未连接；可扫描或直接输入地址")
        self.stream_text = tr("IMU 未就绪；连接后自动订阅，无需切换模式")
        self.raw_note = ""
        self.battery_text = tr("电量：—")
        self.battery_tone = "muted"
        # Capture.
        self.name = ""
        self.take = None
        self.take_rate = None
        self.pending = []
        self.pending_regions = []
        self.pending_rate = None
        # Waveform preview.
        self.preview_repetitions = []
        self.preview_rate = None
        self.preview_is_pending = False
        self.preview_training_regions = []
        self.preview_path = None
        self.preview_editable = False
        self.preview_index = -1
        self.preview_title = tr("尚无录制")
        self.preview_view = None
        self.preview_message = tr("停止一次录制后显示滤波波形；也可在数据集中选中已保存手势。")
        # Model.
        self.training = False
        self.closing = False
        self.bundle = None
        self.origin = None
        self.stale = False
        self.revision = 0
        self.evaluation_revision = None
        self.mode_text = tr("当前分段：连续动作（帧数按采样率换算）")
        self.model_text = tr("尚未训练或加载模型")
        self.evaluation_text = tr("拒识标定训练至少 3 次；留出评估至少 4 次。单类只能评估正样本通过率，不能测误触率。")
        self.recognizer = None
        self.live = False
        self.recognition_origin = 0
        self.prediction = None
        self.prediction_serial = 0
        self.result_text = "—"
        # Audio.
        self.audio_state = "idle"
        self.audio_text = tr("未开启接收")
        self.audio_rows = []
        self.remote_audio_files = {}
        self.remote_audio_selected = ""
        self._tick_dirty = False
        self._timer = None
        self._close_future = None
        self._close_deadline = 0.0
        # Read metadata only after controller state exists; never import plugins here.
        if plugin_root is None and history is not None:
            plugin_root = Path(history.path).parent / "plugins" if hasattr(history, "path") else None
        self.plugins = PluginsController(self, root=plugin_root)

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.refresh_device_choices()
        if self.device_history.warning:
            warning = self.device_history.warning
            # This is DeviceHistory's emitted display prefix, not a parser
            # for arbitrary exceptions or user-provided strings.
            prefix = "无法读取设备历史（不影响连接）："
            self.log(tr("无法读取设备历史（不影响连接）：{error}", error=warning[len(prefix):])
                     if warning.startswith(prefix) else tr(warning))
        self.worker.start()
        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.poll)
        self._timer.start()
        self.plugins.start()
        for hint in _STARTUP_HINTS:
            self.log(hint)
        self.notify()

    def notify(self):
        self.stateChanged.emit()

    def poll(self):
        if self.closing:
            return
        self.drain_events()  # Apply disconnect/model/maintenance gates before dispatch.
        self.plugins.poll()
        self.drain_events()
        if self._tick_dirty:
            self._tick_dirty = False
            self.notify()
        self.tick.emit()

    def drain_events(self):
        for _ in range(self.events.qsize()):
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self.handle(event, payload)
            except Exception as exc:
                self.log(tr("处理 {event} 事件失败：{error}", event=event, error=_exception_text(exc)))

    def log(self, text, level="info"):
        self.logged.emit(time.strftime("%H:%M:%S"), level, tr(text if isinstance(text, str) else str(text)))

    def error(self, text):
        text = tr(text if isinstance(text, str) else str(text))
        self.log(text, "error")
        self.dialogs.error(tr("操作失败"), text)

    def request(self, action):
        if self.closing:
            return False
        try:
            action()
            return True
        except Exception as exc:
            self.error(_exception_text(exc))
            return False

    def device_request(self, action, *, imu=False):
        if self.tools.busy or (imu and (not self.connected or self.audio_state != "idle"
                                        or not self.tools.supports("imu"))):
            self.error(tr("请等待当前操作结束；IMU 需要已连接且支持此能力的设备。"))
            return False
        return self.request(action)

    # ------------------------------------------------------------- enablement
    @property
    def locked(self):
        return self.training or self.closing

    @property
    def can_export(self):
        return self.bundle is not None and not self.stale and not self.locked

    @property
    def name_editable(self):
        return not (self.pending or self.take is not None or self.locked)

    @property
    def _imu_ready(self):
        return (self.connected and self.imu_active and self.tools.supports("imu")
                and self.audio_state in {"idle", "listening"})

    @property
    def can_record(self):
        return (not self.closing and not self.tools.busy and not self.training
                and (self.take is not None or self._imu_ready))

    @property
    def can_toggle_live(self):
        return not self.closing and not self.tools.busy and not self.training and self._recognition_ready

    @property
    def _recognition_ready(self):
        # Audio has its own consumer; capture restrictions are intentionally stricter.
        return self.connected and self.imu_active and self.tools.supports("imu") and self.take is None

    @property
    def can_retry_imu(self):
        return (not self.closing and not self.tools.busy and self.connected
                and self.audio_state == "idle" and self.tools.supports("imu"))

    @property
    def can_scan(self):
        # The worker runs one scan or connection session at a time.
        return (not self.closing and not self.tools.busy and not self.scanning
                and not self.connected and not self.connecting)

    @property
    def can_connect(self):
        return (not self.closing and not self.tools.busy and not self.scanning
                and not self.connected and not self.connecting)

    @property
    def can_disconnect(self):
        return not self.closing and not self.tools.firmware_locked and (self.connected or self.connecting)

    @property
    def can_undo(self):
        return not self.locked and self.take is None and bool(self.pending)

    @property
    def can_save(self):
        return not self.locked and self.take is None and bool(self.pending)

    @property
    def can_discard(self):
        return not self.locked and (self.take is not None or bool(self.pending))

    @property
    def can_delete(self):
        return not self.locked and bool(self.selected_ids)

    @property
    def can_download_audio(self):
        return self.tools.available("audio") and bool(self.remote_audio_files)

    @property
    def can_change_audio_dir(self):
        return not self.closing and not self.tools.busy and self.audio_state == "idle"

    @property
    def can_stop_audio(self):
        return (not self.closing and not self.tools.busy and not self.tools.remote_session
                and self.audio_state not in {"idle", "recording", "live", "saving", "stopping"})

    @property
    def preview_can_edit(self):
        view = self.preview_view
        return (self.preview_editable and not self.locked and view is not None
                and len(view.raw) >= view.min_samples)

    @property
    def take_text(self):
        lengths = [len(rep) for rep in self.pending]
        if self.take is not None:
            return tr("未保存：{count} 次，长度 {lengths}；正在录制：{samples} 样本",
                      count=len(lengths), lengths=lengths, samples=len(self.take))
        return tr("未保存：{count} 次，长度 {lengths}", count=len(lengths), lengths=lengths)

    @property
    def device_summary(self):
        if self.connected:
            return self.device_name or self.address
        return tr("正在连接…") if self.connecting else tr("未连接")

    # ----------------------------------------------------- parameters & model
    def invalidate(self, reason):
        self.revision += 1
        if self.evaluation_revision is not None:
            self.evaluation_text = tr("数据或参数已改变；请重新进行留出评估。")
            self.evaluation_revision = None
        if self.bundle is not None and self.origin == "trained" and not self.stale:
            self.stale = True
            self.stop_live()
            self.model_text = tr("{reason}；旧模型已失效，请重新训练。", reason=reason)
            self.log(tr("{reason}；旧模型不可导出或试识别。", reason=reason))

    def pipeline_settings(self, sample_rate=None):
        return PipelineConfig(
            sample_rate_hz=float(self.params["sample_rate_hz"]) if sample_rate is None else sample_rate,
            cutoff_hz=float(self.params["cutoff_hz"]),
            median_kernel=int(self.params["median_kernel"]),
            window_size=int(self.params["window_size"]),
            window_overlap=int(self.params["window_overlap"]),
            filter_initialization="steady",
        )

    def segmentation_settings(self, pipeline):
        return SegmentationConfig.for_sample_rate(
            pipeline.sample_rate_hz, mode=self.params["segmentation_mode"],
            energy_threshold=float(self.params["energy_threshold"]), min_samples=pipeline.min_samples,
        )

    def set_param(self, key, value):
        if self.params.get(key) == value:
            return
        self.params[key] = value
        self.parameters_changed()

    def apply_preset(self, mode):
        try:
            rates = {dataset.sample_rate_hz for _, dataset in self.records.values()}
            rate = (self.actual_rate or self.preview_rate or
                    (next(iter(rates)) if len(rates) == 1 else float(self.params["sample_rate_hz"])))
            # Validate before writing anything to the fields.
            PipelineConfig(sample_rate_hz=rate, cutoff_hz=min(40.0, 0.3 * rate))
            window = max(4, round(rate * (0.08 if mode == "impulse" else 0.32)))
            self.params.update(
                sample_rate_hz=f"{rate:g}",
                cutoff_hz=f"{min(40.0, rate * 0.3) if mode == 'impulse' else min(10.0, rate * 0.4):g}",
                median_kernel="1" if mode == "impulse" else "5",
                window_size=str(window), window_overlap=str(window // 2),
                energy_threshold="8000" if mode == "impulse" else "1500",
                segmentation_mode=mode)
            self.paramsReplaced.emit()
            self.parameters_changed()
            self.log(tr("已应用预设；请重新训练。原始录制不变，短促模式只使用冲击前后窗口。"))
        except (ValueError, TypeError) as exc:
            self.error(_exception_text(exc))

    def parameters_changed(self):
        self.invalidate(tr("训练参数已改变"))
        try:
            pipeline = self.pipeline_settings()
            config = self.segmentation_settings(pipeline)
            if config.mode == "impulse":
                self.mode_text = tr("短促动作：加速度相邻差触发，前 {pre:.2f}s + 后 {post:.2f}s；"
                                    "训练默认自动裁剪，可在波形中手动框选",
                                    pre=config.pre_roll / pipeline.sample_rate_hz,
                                    post=config.post_roll / pipeline.sample_rate_hz)
            else:
                self.mode_text = tr("连续动作：结束静止 {offset:.2f}s，最长 {maximum:.1f}s（按采样率换算）",
                                    offset=config.min_offset_frames / pipeline.sample_rate_hz,
                                    maximum=config.max_gesture_len / pipeline.sample_rate_hz)
        except (ValueError, TypeError):
            self.mode_text = tr("参数尚未完整或无效；请检查采样率、低通、中值窗口和触发阈值。")
        self.show_preview()
        self.notify()

    # ------------------------------------------------------------------ preview
    def preview_recordings(self, repetitions, rate, title, *, pending=False, reveal=False,
                           training_regions=None, path=None):
        self.preview_repetitions = list(repetitions)
        self.preview_rate = rate
        self.preview_is_pending = pending
        self.preview_training_regions = (list(training_regions) if training_regions is not None
                                         else [None] * len(repetitions))
        self.preview_path = path
        # Short rejected takes can still be viewed, but have no editable target.
        self.preview_editable = path is not None or (pending and training_regions is not None)
        self.preview_title = title
        self.preview_index = len(self.preview_repetitions) - 1
        self.show_preview()
        if reveal:
            self.previewRevealed.emit()

    @property
    def preview_choices(self):
        return [tr("第 {number} 次 · {samples} 样本", number=i + 1, samples=len(rep))
                for i, rep in enumerate(self.preview_repetitions)]

    def set_preview_index(self, index):
        if index != self.preview_index:
            self.preview_index = index
            self.show_preview()

    def show_preview(self):
        index = self.preview_index
        self.preview_view = None
        if not self.preview_repetitions or not 0 <= index < len(self.preview_repetitions):
            self.preview_message = tr("暂无录制波形")
        else:
            try:
                # Use the recording's actual rate, not a potentially different training rate.
                pipeline = self.pipeline_settings(self.preview_rate)
                self.preview_view = analyze_recording(
                    self.preview_repetitions[index], pipeline,
                    segmentation=self.segmentation_settings(pipeline),
                    training_region=self.preview_training_regions[index])
                self.preview_message = tr(self.preview_view.status)
            except (ValueError, TypeError) as exc:
                self.preview_message = tr("无法预览，请检查滤波参数：{error}", error=_exception_text(exc))
        self.previewChanged.emit()

    def set_preview_training_region(self, region):
        """Commit an annotation, never destructively crop a recording or append it."""
        if self.locked or not self.preview_editable:
            return
        index = self.preview_index
        if not 0 <= index < len(self.preview_repetitions):
            return
        try:
            from ..datasets import save_dataset_file, validate_training_region
            region = validate_training_region(region, len(self.preview_repetitions[index]))
            if region is not None:
                minimum = self.pipeline_settings(self.preview_rate).min_samples
                if region[1] - region[0] < minimum:
                    raise ValueError(tr("手动训练框过短，至少需要 {minimum} 个采样点，请扩大选框。", minimum=minimum))
            if region == self.preview_training_regions[index]:
                return
            if self.preview_is_pending:
                self.pending_regions[index] = region
                self.preview_training_regions[index] = region
                saved_text = tr("随录制一起保存")
            elif self.preview_path is not None:
                # Re-read before editing; never overwrite externally changed raw
                # recordings or unrelated annotations with a stale preview copy.
                dataset = load_dataset(self.preview_path)
                if (not self.same_rate(dataset.sample_rate_hz, self.preview_rate)
                        or len(dataset.repetitions) != len(self.preview_repetitions)
                        or any(not np.array_equal(a, b) for a, b in
                               zip(dataset.repetitions, self.preview_repetitions))):
                    raise ValueError(tr("录制文件已变化，请刷新数据集后重新选择。"))
                regions = list(dataset.training_regions or [None] * len(dataset.repetitions))
                regions[index] = region
                dataset.training_regions = regions
                save_dataset_file(dataset, self.preview_path)
                self.preview_training_regions = list(regions)
                for key, (path, _) in list(self.records.items()):
                    if path == self.preview_path:
                        self.records[key] = (path, dataset)
                self.invalidate(tr("训练裁剪框已改变"))
                saved_text = tr("已保存到原文件；需重新训练")
            else:
                return
            detail = (tr("手动训练框 [{start}, {end})，{samples} 样本",
                         start=region[0], end=region[1], samples=region[1] - region[0])
                      if region is not None else tr("已恢复自动裁剪"))
            self.log(tr("第 {number} 次：{detail}；{saved}。原始录制不变。",
                        number=index + 1, detail=detail, saved=saved_text))
        except (ValueError, OSError, TypeError) as exc:
            self.error(_exception_text(exc))
        finally:
            self.show_preview()
            self.notify()

    # ------------------------------------------------------------------ capture
    def set_name(self, name):
        if name == self.name:
            return
        self.name = name
        if self.pending or self.take is not None:
            self.log(tr("手势名称改变，已丢弃未保存数据，避免混入新手势。"))
            self.clear_takes()
        self.notify()

    def cancel_take(self):
        if self.take is not None:
            self.log(tr("未完成的本次录制已取消。"))
        self.take = None
        self.notify()

    def clear_takes(self):
        self.cancel_take()
        self.pending.clear()
        self.pending_regions.clear()
        self.pending_rate = None
        self.notify()
        if self.preview_is_pending:
            self.preview_recordings([], None, tr("暂无未保存录制"))

    def discard_takes(self):
        if self.confirm_discard():
            self.clear_takes()

    def confirm_discard(self):
        return not (self.pending or self.take is not None) or self.dialogs.ask(
            tr("丢弃未保存录制？"), tr("继续将丢弃未保存的重复和正在录制的数据。"))

    def toggle_record(self):
        # Process already-received batches at the boundary so idle samples do
        # not leak into a new take, and the last batch is retained on stop.
        self.drain_events()
        if self.tools.busy:
            self.error(tr("工具操作期间不能录制手势。"))
            return
        if self.take is not None:
            samples = self.take
            self.take = None
            if len(samples) < MIN_TAKE_SAMPLES:
                self.log(tr("本次仅 {samples} 个样本，少于 {minimum}，已拒绝。",
                            samples=len(samples), minimum=MIN_TAKE_SAMPLES))
                if samples:
                    self.preview_recordings([samples], self.take_rate, tr("本次过短（未保留）"),
                                            pending=True, reveal=True)
            else:
                try:
                    dataset = GestureDataset(self.name.strip(), [np.asarray(samples)], self.take_rate)
                    self.pending.extend(dataset.repetitions)
                    self.pending_regions.append(None)
                    self.pending_rate = self.take_rate
                    self.preview_recordings(self.pending, self.pending_rate, tr("未保存：{name}", name=dataset.name),
                                            pending=True, reveal=True, training_regions=self.pending_regions)
                except Exception as exc:
                    self.error(_exception_text(exc))
            self.notify()
            return
        if not self._imu_ready or not self.name.strip():
            self.error(tr("请输入手势名称，等待 IMU 恢复上报；录音期间不能录制手势。"))
            return
        if self.pending and not self.same_rate(self.pending_rate, self.actual_rate):
            self.error(tr("未保存录制与当前设备采样率不一致，请先保存或撤销。"))
            return
        self.plugins.disarm()
        self.take_rate = self.actual_rate
        self.take = []
        self.notify()

    def undo(self):
        if self.take is not None:
            self.error(tr("请先停止本次录制。"))
        elif self.pending:
            self.pending.pop()
            self.pending_regions.pop()
            self.preview_recordings(self.pending, self.pending_rate, tr("未保存：{name}", name=self.name), pending=True,
                                    training_regions=self.pending_regions)
            self.notify()

    @staticmethod
    def same_rate(a, b):
        return a is not None and b is not None and np.isclose(a, b, rtol=1e-9, atol=1e-9)

    def persist(self, dataset, minimum=1):
        target = dataset_path(dataset.name, self.directory)
        if target.exists():
            existing = load_dataset(target)
            if existing.name != dataset.name:
                raise ValueError(tr("文件名冲突：{file} 属于“{name}”，不会覆盖。请换一个名称。",
                                    file=target.name, name=existing.name))
            if not self.same_rate(existing.sample_rate_hz, dataset.sample_rate_hz):
                raise ValueError(tr("同名数据的采样率不同，不能追加；请选择其他名称或目录。"))
            if not self.dialogs.ask(tr("追加重复？"), tr("“{name}”已有 {existing} 次。\n追加本次 {added} 次？原数据将保留。",
                                    name=dataset.name, existing=len(existing.repetitions), added=len(dataset.repetitions))):
                return False
            regions = ((existing.training_regions or [None] * len(existing.repetitions))
                       + (dataset.training_regions or [None] * len(dataset.repetitions)))
            dataset = GestureDataset(dataset.name, existing.repetitions + dataset.repetitions,
                                     dataset.sample_rate_hz, regions)
        if len(dataset.repetitions) < minimum:
            raise ValueError(tr("保存录制需要至少 {minimum} 次重复（可以包含已有数据）。", minimum=minimum))
        self.directory.mkdir(parents=True, exist_ok=True)
        saved = save_dataset(dataset, self.directory)
        self.log(tr("已保存 {path}；共 {count} 次，{rate:g} Hz。",
                    path=saved, count=len(dataset.repetitions), rate=dataset.sample_rate_hz))
        self.refresh()
        return True

    def save_takes(self):
        if self.take is not None or not self.pending:
            self.error(tr("请先停止录制，并至少保留一次有效重复。"))
            return
        try:
            dataset = GestureDataset(self.name.strip(), list(self.pending), self.pending_rate,
                                     list(self.pending_regions))
            if self.persist(dataset, minimum=2):
                path = dataset_path(dataset.name, self.directory)
                saved = load_dataset(path)
                self.preview_recordings(saved.repetitions, saved.sample_rate_hz, tr("刚保存：{name}", name=saved.name),
                                        path=path, training_regions=saved.training_regions)
                self.clear_takes()
        except Exception as exc:
            self.error(_exception_text(exc))

    # ----------------------------------------------------------------- datasets
    def choose_directory(self):
        directory = self.dialogs.directory(str(self.directory.parent))
        if not directory or Path(directory) == self.directory or not self.confirm_discard():
            return
        self.clear_takes()
        self.directory = Path(directory)
        self.refresh()

    def select_datasets(self, ids):
        self.selected_ids = list(ids)
        if self.selected_ids and self.selected_ids[0] in self.records:
            path, dataset = self.records[self.selected_ids[0]]
            self.preview_recordings(dataset.repetitions, dataset.sample_rate_hz, tr("已保存：{name}", name=dataset.name),
                                    path=path, training_regions=dataset.training_regions, reveal=True)
        self.notify()

    def _replace_records(self, entries):
        self.records = {}
        for entry in entries:
            self.records[self._next_record_id] = entry
            self._next_record_id += 1
        self.selected_ids = []
        self.datasetsChanged.emit()

    def refresh(self, *, preserve_on_failure=False):
        """Return scan success; deletion can retain remaining rows on failure."""
        self.invalidate(tr("数据目录或数据集已刷新"))
        if self.preview_path is not None:
            self.preview_recordings([], None, tr("请重新选择已保存手势"))
        try:
            # Stage the scan so a directory-level failure does not erase rows after
            # a partially successful deletion. glob can hide permission errors.
            records, invalid_files = [], []
            try:
                paths = sorted(path for path in self.directory.iterdir() if path.match("*.json"))
            except FileNotFoundError:
                paths = []
            for path in paths:
                try:
                    records.append((path, load_dataset(path)))
                except Exception as exc:
                    invalid_files.append(path.name)
                    self.log(tr("无效数据 {file}：{error}；修正或移出目录后才能训练。",
                                file=path.name, error=_exception_text(exc)))
            self.invalid_files = invalid_files
            self._replace_records(records)
            self.notify()
            return True
        except Exception as exc:
            if not preserve_on_failure:
                # Normal refreshes must never fall back to cached data.
                self.invalid_files = []
                self._replace_records([])
            self.notify()
            self.error(_exception_text(exc))
            return False

    def delete_datasets(self):
        if self.locked:
            return
        selected = list(self.selected_ids)
        if not selected:
            self.error(tr("请先在数据集列表中选择要删除的手势。"))
            return
        if any(item not in self.records for item in selected):
            self.error(tr("所选手势已变化，请刷新数据集后重新选择。"))
            return
        directory = self.directory
        targets = [(item, *self.records[item]) for item in selected]
        details = _join_text(tr("“{name}”（{count} 次录制）\n  {path}",
                                name=dataset.name, count=len(dataset.repetitions), path=path)
                             for _, path, dataset in targets)
        if not self.dialogs.ask(
                tr("永久删除选中手势？"), tr("将永久删除选中的 {count} 个手势：\n\n{details}\n\n"
                "所列 JSON 中的全部录制和训练框标注都会删除，无法撤销。\n"
                "未保存录制、正在采集的数据和已导出的模型文件均保留。\n"
                "当前训练出的模型需要重新训练；独立加载的模型包不受影响。",
                count=len(targets), details=details), danger=True):
            return
        # Recheck after the modal, and never expand to a newly selected row or path.
        if self.locked:
            return
        if self.directory != directory or any(
                item not in self.records or self.records[item][0] != path for item, path, _ in targets):
            self.error(tr("所选手势已变化，请刷新数据集后重新选择。"))
            return
        deleted, missing, failures, removed = [], [], [], []
        for item, path, _ in targets:
            try:
                path.unlink()
            except FileNotFoundError:
                missing.append(str(path))
            except OSError as exc:
                failures.append(tr("{path}：{error}", path=path, error=_exception_text(exc)))
                continue
            else:
                deleted.append(str(path))
            removed.append(item)
        refreshed = True
        if removed:
            # Deleted paths must no longer be selectable even if the rescan fails.
            for item in removed:
                self.records.pop(item)
            self.selected_ids = [item for item in self.selected_ids if item in self.records]
            self.datasetsChanged.emit()
            refreshed = self.refresh(preserve_on_failure=True)
        result = [tr("删除结果：成功删除 {deleted} 个；已不存在 {missing} 个；失败 {failed} 个。",
                     deleted=len(deleted), missing=len(missing), failed=len(failures))]
        for label, paths in ((tr("已删除"), deleted), (tr("文件已不存在（非本次删除）"), missing), (tr("删除失败"), failures)):
            if paths:
                result.append(tr("{label}：\n{paths}", label=label, paths=_join_text(paths)))
        if not refreshed:
            result.append(tr("列表刷新失败，请手动刷新；已删除的文件不会恢复。"))
        if missing or failures or not refreshed:
            self.error(_join_text(result))
        else:
            self.log(_join_text(result))
        self.notify()

    def import_json(self):
        for path in self.dialogs.open_files(tr("数据 JSON (*.json)")):
            try:
                if Path(path).resolve().parent == self.directory.resolve():
                    self.log(tr("{file} 已在当前目录，仅刷新。", file=Path(path).name))
                    self.refresh()
                else:
                    self.persist(load_dataset(path))
            except Exception as exc:
                self.error(tr("{path}：{error}", path=Path(path).name, error=_exception_text(exc)))

    def import_csv(self):
        name = self.name.strip()
        if not name:
            self.error(tr("导入 CSV 前请输入手势名称，并设置训练参数中的采样率。"))
            return
        paths = self.dialogs.open_files(tr("六轴 CSV (*.csv)"))
        if not paths:
            return
        try:
            rate = float(self.params["sample_rate_hz"])
            repetitions = [np.loadtxt(path, delimiter=",", ndmin=2) for path in paths]
            self.persist(GestureDataset(name, repetitions, rate))
        except Exception as exc:
            self.error(_exception_text(exc))

    # ----------------------------------------------------------------- training
    def _begin_background(self):
        self.stop_live()
        self.training = True
        self.notify()

    def _finish_background(self):
        self.training = False
        self.show_preview()
        self.notify()

    def train(self):
        if self.training:
            return
        if self.take is not None:
            self.error(tr("请先停止录制并保存数据。"))
            return
        if self.pending and not self.dialogs.ask(tr("仍有未保存录制"), tr("训练只使用已保存数据。继续？")):
            return
        try:
            # Read files again: a dataset might have changed outside this process.
            self.refresh()
            if self.invalid_files:
                raise ValueError(tr("目录中有无效数据：{files}。请先修正或移出目录。", files=', '.join(self.invalid_files)))
            datasets = [record[1] for record in self.records.values()]
            if not datasets:
                raise ValueError(tr("当前目录没有有效数据。"))
            pipeline = self.pipeline_settings()
            segmentation = self.segmentation_settings(pipeline)
            n_states = int(self.params["n_states"])
            if n_states < 1:
                raise ValueError(tr("状态数必须为正整数。"))
        except Exception as exc:
            self.error(_exception_text(exc))
            return
        # A failed new run must never leave a previous model looking current.
        self.bundle = None
        self.origin = None
        revision = self.revision
        summary = _join_text((tr("{name}: {count} 次/{samples} 样本", name=d.name,
                                 count=len(d.repetitions), samples=sum(map(len, d.repetitions)))
                              for d in datasets), "; ")
        self.model_text = tr("正在后台训练，请查看日志…")
        self.log(tr("训练开始：{summary}", summary=summary))
        self._begin_background()
        events = self.events

        def run():
            try:
                from ..training import train_datasets
                bundle = train_datasets(datasets, pipeline, n_states=n_states, segmentation=segmentation,
                                        calibrate_rejection=True,
                                        progress=lambda text: events.put(("progress", text)))
                events.put(("trained", (bundle, summary, revision)))
            except Exception as exc:
                events.put(("train_failed", _exception_text(exc)))
        self.executor.submit(run)

    def evaluate(self):
        if self.training:
            return
        if self.take is not None:
            self.error(tr("请先停止本次录制；评估只使用已保存数据。"))
            return
        if self.pending and not self.dialogs.ask(tr("仍有未保存录制"), tr("评估只使用已保存数据。继续？")):
            return
        try:
            # Do not refresh/invalidate the current model for a read-only evaluation.
            datasets = [load_dataset(path) for path in sorted(self.directory.glob("*.json"))]
            if not datasets:
                raise ValueError(tr("当前目录没有评估数据。"))
            pipeline = self.pipeline_settings()
            segmentation = self.segmentation_settings(pipeline)
            n_states = int(self.params["n_states"])
        except Exception as exc:
            self.error(_exception_text(exc))
            return
        self.evaluation_text = tr("正在后台逐次留出评估；不会替换当前模型…")
        revision = self.revision
        self._begin_background()
        events = self.events

        def run():
            try:
                from ..evaluation import evaluate_leave_one_out
                result = evaluate_leave_one_out(datasets, pipeline, n_states, segmentation=segmentation,
                                                calibrate_rejection=True,
                                                progress=lambda text: events.put(("progress", text)))
                events.put(("evaluated", (result, revision)))
            except Exception as exc:
                events.put(("evaluation_failed", _exception_text(exc)))
        self.executor.submit(run)

    def set_bundle(self, bundle, origin, summary):
        self.stop_live()
        self.bundle = bundle
        self.plugins.set_model(bundle)
        self.origin = origin
        self.stale = False
        mode = tr("短促动作") if bundle.segmentation.mode == "impulse" else tr("连续动作")
        rejection = tr("带拒识标定（非概率）") if bundle.rejection else tr("无拒识标定；单类不能开启自动识别，请重新训练")
        self.model_text = tr("{summary}；手势：{names}；{rate:g} Hz；{mode}，中值 {median} / 低通 {cutoff:g} Hz；{rejection}",
                             summary=summary, names=', '.join(bundle.gesture_names), rate=bundle.pipeline.sample_rate_hz,
                             mode=mode, median=bundle.pipeline.median_kernel, cutoff=bundle.pipeline.cutoff_hz,
                             rejection=rejection)
        self.log(self.model_text)
        self.notify()

    def load_bundle(self):
        try:
            path = self.dialogs.open_file(tr("手势模型包（JSON） (*.json)"))
            if path:
                self.set_bundle(GestureBundle.load(path), "imported", tr("已加载 {file}（独立于当前训练数据）", file=Path(path).name))
        except Exception as exc:
            self.error(_exception_text(exc))

    def export_bundle(self):
        if not self.can_export:
            return
        try:
            path = self.dialogs.save_file("model.gesture.json", tr("手势模型包（JSON） (*.json)"))
            if path:
                self.bundle.save(path)
                self.log(tr("模型包已导出：{path}", path=path))
        except Exception as exc:
            self.error(_exception_text(exc))

    # -------------------------------------------------------- live recognition
    def stop_live(self):
        self.plugins.disarm()
        self.live = False
        if self.recognizer is not None:
            self.recognizer.reset()
        self.recognizer = None
        self.prediction = None
        self.result_text = "—"

    def set_live(self, enabled):
        self.live = bool(enabled)
        self._sync_live()
        self.notify()

    def _sync_live(self):
        if not self.live:
            self.stop_live()
            return
        try:
            if self.training or self.bundle is None or self.stale:
                raise ValueError(tr("请先训练有效模型或加载模型包。"))
            if self.tools.busy or not self._recognition_ready:
                raise ValueError(tr("请先连接设备并等待 IMU 恢复上报；维护或手势采集期间不能试识别。"))
            if not self.same_rate(self.actual_rate, self.bundle.pipeline.sample_rate_hz):
                raise ValueError(tr("采样率不匹配：设备 {device:g} Hz，模型 {model:g} Hz。",
                                    device=self.actual_rate, model=self.bundle.pipeline.sample_rate_hz))
            self.recognizer = GestureRecognizer(self.bundle)
            if len(self.bundle.gesture_names) == 1 and not self.recognizer.has_rejection:
                raise ValueError(tr("这个单类旧模型没有拒识标定，普通运动也会被当成手势。请至少录制 3 次，选择合适预设后重新训练。"))
            self.recognition_origin = self.stream.next_sample
            if not self.recognizer.has_rejection:
                self.log(tr("警告：旧模型未配置未知动作拒识，建议重新训练。"))
            self.log(tr("实时试识别已开启；仅通过检查的片段画紫框。单类没有相对置信度，拒识标定仍需用负样本检验。"))
        except Exception as exc:
            self.stop_live()
            self.error(_exception_text(exc))

    def samples(self, payload):
        if self.tools.busy or not self.connected or not self.imu_active or not payload:
            return
        self.stream.append_samples(payload)
        self.acceleration.append_samples(payload)
        self.count += len(payload)
        self.latest_sample = tuple(payload[-1])
        self.raw_note = ""
        self._tick_dirty = True
        if self.take is not None:
            self.take.extend([list(sample) for sample in payload])
        if self.recognizer is not None:
            try:
                for start in range(0, len(payload), 32):
                    for prediction in self.recognizer.feed(payload[start:start + 32]):
                        self._record_prediction(prediction)
            except Exception as exc:
                self.stop_live()
                self.log(tr("试识别已停止：{error}", error=_exception_text(exc)))

    def _record_prediction(self, prediction):
        assessment = (tr("单类匹配，通过拒识检查（无相对置信度）") if prediction.confidence is None else
                      tr("相对置信度 {confidence:.3f}（非概率）", confidence=prediction.confidence))
        self.prediction = (prediction.name, tr("{assessment}；分数 {score:.3f}", assessment=assessment, score=prediction.score))
        self.prediction_serial += 1
        self.plugins.prediction(prediction)
        self.result_text = tr("{name}；{assessment}", name=prediction.name, assessment=self.prediction[1])
        self.log(self.result_text)
        if prediction.start_sample is not None and prediction.end_sample is not None:
            self.stream.add_recognition(
                self.recognition_origin + prediction.start_sample,
                self.recognition_origin + prediction.end_sample,
                prediction.name if prediction.confidence is None else f"{prediction.name} {prediction.confidence:.2f}")

    def set_stream_state(self, payload):
        active = payload["state"] == "active"
        message = tr(payload.get("message", "IMU 正常上报" if active else "IMU 暂停，等待恢复订阅（无需切换模式）"))
        if active:
            rate = float(payload["sample_rate_hz"])
            if not self.imu_active or not self.same_rate(self.actual_rate, rate):
                self.stream.set_sample_rate(rate)
                if self.recognizer is not None:
                    self.recognizer.reset()
                    self.recognizer = None
            self.actual_rate = rate
            if payload.get("accel_range_g") is not None:
                message = tr("{message}；{rate:g} Hz；加速度 ±{accel} g，陀螺仪 ±{gyro} °/s",
                             message=message, rate=rate, accel=payload["accel_range_g"], gyro=payload["gyro_range_dps"])
            else:
                message = tr("{message}；{rate:g} Hz", message=message, rate=rate)
        else:
            self.plugins.disarm()
            self.actual_rate = None
            self.cancel_take()
            if self.connected and self.live:
                if self.recognizer is not None:
                    self.recognizer.reset()
                self.recognizer = None
                self.prediction = None
                self.result_text = tr("识别已暂停；IMU 恢复后自动继续（请先静止）")
            else:
                self.stop_live()
            self.latest_sample = None
            self.raw_note = tr("无新数据，未断言 BLE 断开")
        self.imu_active = active
        self.acceleration.set_stream_state(payload)
        self.stream_text = message
        if active and self.live and self.recognizer is None and self._recognition_ready:
            self._sync_live()
        self.notify()

    def set_battery_status(self, payload=None):
        """Render only reported values; 0% is valid, absent/out-of-range is not."""
        text, tone = tr("电量：—"), "muted"
        if payload is not None:
            percent = payload.get("battery_percent")
            charging = payload.get("battery_charging") is True
            valid = type(percent) is int and 0 <= percent <= 100
            text = tr("电量：{percent}%", percent=percent) if valid else tr("电量：未知")
            if charging:
                text = tr("{battery} · 充电中", battery=text)
                tone = "good"
            elif valid and percent <= 20:
                text = tr("{battery} · 低电量", battery=text)
                tone = "critical"
            elif valid:
                tone = "normal"
        self.battery_text, self.battery_tone = text, tone

    # ------------------------------------------------------------------- device
    def set_address(self, address):
        self.address = address
        self.notify()

    def refresh_device_choices(self):
        scanned = {d["address"].casefold(): d for d in self.scanned_devices}
        entries, seen = [], set()
        for entry in self.device_history.entries:
            key = entry["address"].casefold()
            name = scanned.get(key, {}).get("name") or entry["name"] or tr("未命名")
            rssi = scanned.get(key, {}).get("rssi")
            entries.append(dict(name=name, address=entry["address"], source="历史", rssi=rssi))
            seen.add(key)
        for entry in self.scanned_devices:
            key = entry["address"].casefold()
            if key not in seen:
                entries.append(dict(name=entry.get("name") or tr("未命名"), address=entry["address"],
                                    source="扫描", rssi=entry.get("rssi")))
                seen.add(key)
        self.device_entries = entries
        if not self.address.strip() and entries:
            self.address = entries[0]["address"]
        self.devicesChanged.emit()

    def remember_device(self, payload):
        address = payload["address"]
        name = next((d.get("name") for d in self.scanned_devices
                     if d["address"].casefold() == address.casefold()), None)
        if not name and not any(e["address"].casefold() == address.casefold() for e in self.device_history.entries):
            name = payload.get("model", "")
        try:
            self.device_history.remember(address, name or "")
        except (OSError, ValueError) as exc:
            self.log(tr("设备已连接，但无法保存历史：{error}", error=_exception_text(exc)))
        known = next((e["name"] for e in self.device_history.entries
                      if e["address"].casefold() == address.casefold()), "")
        self.device_name = known or name or payload.get("model", "")
        self.address = address
        self.refresh_device_choices()

    def clear_device_history(self):
        if not self.dialogs.ask(tr("清空设备历史？"), tr("仅删除本机保存的设备历史，不会断开当前设备。")):
            return
        try:
            self.device_history.clear()
            self.refresh_device_choices()
        except OSError as exc:
            self.error(_exception_text(exc))

    def scan(self):
        if not self.can_scan:
            self.error(tr("请等待当前操作结束；连接设备期间不能扫描，请先断开。"))
            return
        self.scanning = True
        self.notify()
        if not self.device_request(self.worker.scan):
            self.scanning = False
            self.notify()

    def retry_imu(self):
        self.device_request(self.worker.retry_imu, imu=True)

    def connect_device(self):
        if self.tools.busy:
            self.error(tr("请等待工具操作完成后再连接。"))
            return
        address = self.address.strip()
        if not address:
            self.error(tr("请先选择设备或输入地址。"))
            return
        if self.connected or self.connecting:
            self.error(tr("请先断开当前设备 / 取消正在进行的连接。"))
            return
        if self.scanning:
            self.error(tr("请等待扫描结束后再连接。"))
            return
        if not self.confirm_discard():
            return
        self.clear_takes()
        self.stop_live()
        self.connecting = True
        self.set_battery_status()
        self.status = tr("正在连接；可以点击断开取消")
        if not self.request(lambda: self.worker.connect(address)):
            self.connecting = False
            self.status = tr("连接失败")
        self.notify()

    def disconnect_device(self):
        if self.tools.firmware_locked:
            self.error(tr("固件升级进行中，不能断开；请等待完成或超时。"))
            return
        if self.audio_state != "idle" and not self.dialogs.ask(
                tr("音频尚未结束"), tr("断开将停止接收/下载；远程录音采用断线停止策略。"
                "未完整接收的音频不会标为完整文件，设备副本可之后查询。继续？")):
            return
        if self.tools.firmware_locked:
            return
        self.tools.capabilities = {}
        self.tools.clear_calibration_sample()
        self.tools.connection_epoch += 1
        self.set_stream_state({"state": "waiting", "message": tr("IMU 已停止")})
        self.cancel_take()
        self.stop_live()
        self.connected = False
        self.connecting = False
        self.actual_rate = None
        self.set_battery_status()
        self.status = tr("正在断开")
        self.request(self.worker.disconnect)
        self.notify()

    # -------------------------------------------------------------------- audio
    def choose_audio_directory(self):
        if self.tools.busy or self.audio_state != "idle":
            self.error(tr("工具运行、录音、接收、下载或保存期间不能更改目录。"))
            return
        path = self.dialogs.directory(str(self.audio_directory.parent))
        if path:
            self.audio_directory = Path(path)
            self.notify()

    def start_audio(self):
        if not self.tools.available("audio"):
            self.error(tr("请连接支持录音的戒指，并等待手势录制、音频或工具结束。"))
            return
        self.set_audio_state({"state": "starting", "message": tr("正在开启保存通知监听（不占用手势采集）…")})
        if not self.request(lambda: self.worker.start_audio(self.audio_directory)):
            self.set_audio_state({"state": "idle", "message": tr("开启接收失败")})

    def stop_audio(self):
        if self.tools.busy:
            return
        if self.tools.remote_session or self.audio_state in {"recording", "live"}:
            self.tools.finish_recording()
            return
        if self.audio_state not in {"idle", "saving", "stopping"}:
            if self.audio_state in {"receiving", "downloading"} and not self.dialogs.ask(
                    tr("中断音频传输？"), tr("未接收完整的文件不会保存。之后可从戒指历史录音重新下载。")):
                return
            if self.tools.busy:
                return
            self.set_audio_state({"state": "stopping", "message": tr("正在停止电脑端接收（不控制戒指录音）…")})
            self.request(self.worker.stop_audio)

    def list_audio(self):
        if not self.tools.available("audio"):
            self.error(tr("请连接支持录音的戒指，停止手势录制、音频和工具后再查询。"))
            return
        self.set_audio_state({"state": "listing", "message": tr("正在查询历史录音…")})
        if not self.request(self.worker.list_audio):
            self.set_audio_state({"state": "idle", "message": tr("查询请求失败")})

    @property
    def remote_audio_choices(self):
        # Keep the original keys stable; only the combo-box labels are translated.
        return [(label, tr("录音索引 {index}", index=index))
                for label, index in self.remote_audio_files.items()]

    def set_remote_audio_selected(self, label):
        self.remote_audio_selected = label

    def download_audio(self):
        selected = self.remote_audio_selected
        if not self.tools.available("audio") or selected not in self.remote_audio_files:
            self.error(tr("请先查询并选择历史录音，停止当前手势录制/音频接收后再下载。"))
            return
        self.set_audio_state({"state": "downloading", "message": tr("正在准备下载…")})
        if not self.request(lambda: self.worker.download_audio(self.remote_audio_files[selected], self.audio_directory)):
            self.set_audio_state({"state": "idle", "message": tr("下载请求失败")})

    def set_audio_state(self, payload):
        self.audio_state = payload["state"]
        self.audio_text = tr(payload.get("message", self.audio_state))
        self.plugins.audio_state_changed(self.audio_state)
        if self._recognition_ready and self.live and self.recognizer is None:
            self._sync_live()
        self.notify()

    def open_path(self, path):
        try:
            self.dialogs.open_path(Path(path).resolve(strict=True))
        except (OSError, ValueError) as exc:
            self.error(_exception_text(exc))

    def open_url(self, url):
        try:
            self.dialogs.open_url(url)
        except OSError as exc:
            self.error(_exception_text(exc))

    def _clear_remote_audio(self):
        self.remote_audio_files = {}
        self.remote_audio_selected = ""
        self.remoteAudioChanged.emit()

    # ------------------------------------------------------------------- events
    def handle(self, event, payload):
        if event == "plugin":
            self.plugins.handle(payload)
            return
        if event == "plugin_job":
            self.plugins.handle_job(payload)
            return
        if event == "tool_state" and payload.get("state") != "idle":
            self.plugins.disarm()
        if self.tools.handle(event, payload):
            return
        if event == "samples":
            self.samples(payload)
        elif event == "devices":
            self.scanned_devices = list(payload)
            self.refresh_device_choices()
            self.log(tr("扫描发现 {count} 个设备；已连接设备保留在历史中。", count=len(payload)))
        elif event == "connected":
            if not self.connecting:
                self.request(self.worker.disconnect)
                return
            self.connected, self.connecting = True, False
            self.set_battery_status(payload)
            self.remember_device(payload)
            self.set_stream_state({"state": "waiting", "message": tr("BLE 已连接，正在等待新的 IMU 数据…")})
            self.count = 0
            self.stream.clear()
            self._clear_remote_audio()
            self.status = tr("BLE 已连接 {address}；{model} {firmware}", address=payload["address"],
                             model=payload.get("model", ""), firmware=payload.get("firmware_version", ""))
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text})
            self.log(self.status)
        elif event == "battery":
            if self.connected and not self.closing:
                self.set_battery_status(payload)
                self.notify()
        elif event == "stream_state":
            self.set_stream_state(payload)
        elif event == "reconnecting":
            self.connected, self.connecting = False, True
            self.set_battery_status()
            self.set_stream_state({"state": "waiting", "message": tr("连接中断，正在自动重连")})
            self.status = tr(payload["message"])
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text})
            self.log(payload["message"])
        elif event == "disconnected":
            self.connected = self.connecting = False
            self.device_name = ""
            self.set_battery_status()
            self.set_stream_state({"state": "waiting", "message": tr("IMU 未连接")})
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text})
            self.status = tr("已断开；未完成的手势录制已取消")
            self.notify()
        elif event == "audio_state":
            self.set_audio_state(payload)
            self.log(payload.get("message", payload["state"]))
        elif event == "audio_progress":
            total = payload.get("total")
            if total:
                self.audio_text = tr("正在接收音频文件：{received:.1f} KiB / {total:.1f} KiB（不是录音计时）",
                                     received=payload["bytes"] / 1024, total=total / 1024)
            else:
                self.audio_text = tr("正在接收音频文件：{received:.1f} KiB（不是录音计时）",
                                     received=payload["bytes"] / 1024)
            self.notify()
        elif event == "audio_files":
            self.remote_audio_files = {f"录音索引 {entry['file_index']}": entry["file_index"] for entry in payload}
            self.remote_audio_selected = next(iter(self.remote_audio_files), "")
            self.remoteAudioChanged.emit()
            self.log(tr("戒指中有 {count} 个录音文件；不会自动删除设备文件。", count=len(payload)))
            self.notify()
        elif event == "audio_saved":
            self.plugins.audio_saved(payload)
            path = Path(payload["path"])
            duration = payload.get("duration_s")
            self.audio_rows.append(dict(path=path, name=path.name,
                                        duration=f"{duration:.1f} s" if duration is not None else tr("未解码"),
                                        size=f"{path.stat().st_size / 1024:.1f} KiB"))
            self.audioFilesChanged.emit()
            raw_path = payload.get("raw_path")
            self.log(tr("录音已保存：{path}；原始文件：{raw_path}", path=path, raw_path=raw_path)
                     if raw_path else tr("录音已保存：{path}", path=path))
        elif event == "scan_finished":
            self.scanning = False
            self.notify()
        elif event in ("warning", "error"):
            # RingWorker's battery-read warning keeps the original exception as data.
            prefix = "电量读取失败，将于下次刷新重试："
            message = (tr("电量读取失败，将于下次刷新重试：{error}", error=payload[len(prefix):])
                       if isinstance(payload, str) and payload.startswith(prefix) else payload)
            self.log(message, event)
        elif event == "progress":
            self.log(payload)
        elif event == "trained":
            bundle, summary, revision = payload
            self.training = False
            self.set_bundle(bundle, "trained", tr("训练成功：{summary}", summary=summary))
            if revision != self.revision:
                self.invalidate(tr("训练期间数据或参数已改变"))
            self._finish_background()
        elif event == "evaluated":
            result, revision = payload
            self._finish_background()
            metric = tr("单类正样本通过率（不代表误触率）") if len(result["per_class"]) == 1 else tr("已知手势留出识别率")
            text = tr("{metric}：{correct}/{total}（{accuracy:.1%}）；未做连续流负样本评估",
                      metric=metric, correct=result["correct"], total=result["total"], accuracy=result["accuracy"])
            if revision != self.revision:
                text = tr("{result}；数据/参数已改变，此为评估开始时的结果", result=text)
            self.evaluation_text = text
            self.evaluation_revision = revision
            self.log(text)
            for name, counts in result["per_class"].items():
                self.log(tr("{name}：{correct}/{total}；预测分布 {confusion}；跳过过短录制 {skipped} 次",
                            name=name, correct=counts["correct"], total=counts["total"],
                            confusion=result["confusion"][name], skipped=result["skipped_recordings"][name]))
            self.log(tr("留出评估不是跨会话/真机准确率；请另录独立数据验证。当前模型未被替换。"))
            self.notify()
        elif event == "evaluation_failed":
            self._finish_background()
            self.evaluation_text = tr("留出评估失败；带拒识标定时每类需至少 4 次有效录制，当前模型保持不变。")
            self.notify()
            self.error(payload)
        elif event == "train_failed":
            self._finish_background()
            self.model_text = tr("训练失败；没有生成新模型。请查看日志并修正数据或参数。")
            self.notify()
            self.error(payload)

    # ------------------------------------------------------------------ closing
    def request_close(self):
        """Begin shutdown if confirmed; closeFinished fires when cleanup ends."""
        if self.tools.firmware_locked:
            self.error(tr("固件升级进行中，不能退出；请等待完成或超时。"))
            return
        if self.closing or not self.confirm_discard():
            return
        if self.training and not self.dialogs.ask(
                tr("训练 / 评估尚未完成"), tr("关闭窗口将放弃本次结果；后台计算可能稍后才结束。继续？")):
            return
        if self.audio_state != "idle" and not self.dialogs.ask(
                tr("音频尚未结束"), tr("退出将停止接收/下载；远程录音采用断线停止策略。"
                "完整音频会继续保存，未完整音频不会标为完整文件。继续？")):
            return
        # Modal confirmations pump events; OTA may have started in the meantime.
        if self.tools.firmware_locked:
            return
        self.closing = True
        self.scanning = False
        self.cancel_take()
        self.stop_live()
        self.plugins.close()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.set_battery_status()
        self.status = tr("正在关闭设备，最多等待 8 秒…")
        self.notify()
        try:
            self._close_future = self.worker.close()
        except Exception as exc:
            self.error(tr("设备清理请求失败：{error}", error=_exception_text(exc)))
            self.closeFinished.emit()
            return
        self._close_deadline = time.monotonic() + 8
        self.wait_closed()

    def wait_closed(self):
        future = self._close_future
        if future.done():
            try:
                future.result()
            except Exception as exc:
                self.error(tr("设备清理失败：{error}", error=_exception_text(exc)))
            self.closeFinished.emit()
        elif time.monotonic() >= self._close_deadline:
            self.error(tr("设备清理超过 8 秒，窗口将关闭；蓝牙后台可能未正常退出。"))
            self.closeFinished.emit()
        else:
            QTimer.singleShot(100, self.wait_closed)
