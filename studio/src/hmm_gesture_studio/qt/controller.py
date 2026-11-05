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
from .models import AccelerationModel, LiveStreamModel, analyze_recording
from .tools_controller import ToolsController

MIN_TAKE_SAMPLES = 12
PARAM_DEFAULTS = dict(n_states="6", cutoff_hz="10", window_size="8", window_overlap="4",
                      sample_rate_hz="25", energy_threshold="1500", median_kernel="5",
                      segmentation_mode="motion")
_STARTUP_HINTS = (
    "每次只录制一个完整动作；拒识标定每类至少 3 次，留出评估至少 4 次，建议采集更多变化。",
    "原始六轴顺序：ax, ay, az, gx, gy, gz；录制/识别保留原始单位，合加速度与方向请看“诊断”页。",
    "响指/敲击请先应用短促动作预设；单类不显示虚构置信度，正样本标定仍不能保证消除误触。",
    "训练、录制和识别必须使用相同采样率。",
    "连接后订阅 IMU，无需切换模式；录音页支持远程录音、实时 PCM 保存与保存通知监听。",
)


class StudioController(QObject):
    stateChanged = Signal()
    logged = Signal(str, str, str)  # time, level (info/warning/error), text
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

    def __init__(self, dialogs, *, worker=None, history=None, directory=None, audio_directory=None):
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
        self.status = "未连接；可扫描或直接输入地址"
        self.stream_text = "IMU 未就绪；连接后自动订阅，无需切换模式"
        self.raw_note = ""
        self.battery_text = "电量：—"
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
        self.preview_title = "尚无录制"
        self.preview_view = None
        self.preview_message = "停止一次录制后显示滤波波形；也可在数据集中选中已保存手势。"
        # Model.
        self.training = False
        self.closing = False
        self.bundle = None
        self.origin = None
        self.stale = False
        self.revision = 0
        self.evaluation_revision = None
        self.mode_text = "当前分段：连续动作（帧数按采样率换算）"
        self.model_text = "尚未训练或加载模型"
        self.evaluation_text = "拒识标定训练至少 3 次；留出评估至少 4 次。单类只能评估正样本通过率，不能测误触率。"
        self.recognizer = None
        self.live = False
        self.recognition_origin = 0
        self.prediction = None
        self.prediction_serial = 0
        self.result_text = "—"
        # Audio.
        self.audio_state = "idle"
        self.audio_text = "未开启接收"
        self.audio_rows = []
        self.remote_audio_files = {}
        self.remote_audio_selected = ""
        self._tick_dirty = False
        self._timer = None
        self._close_future = None
        self._close_deadline = 0.0

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.refresh_device_choices()
        if self.device_history.warning:
            self.log(self.device_history.warning)
        self.worker.start()
        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self.poll)
        self._timer.start()
        for hint in _STARTUP_HINTS:
            self.log(hint)
        self.notify()

    def notify(self):
        self.stateChanged.emit()

    def poll(self):
        if self.closing:
            return
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
                self.log(f"处理 {event} 事件失败：{exc}")

    def log(self, text, level="info"):
        self.logged.emit(time.strftime("%H:%M:%S"), level, str(text))

    def error(self, text):
        self.log(text, "error")
        self.dialogs.error("操作失败", str(text))

    def request(self, action):
        if self.closing:
            return False
        try:
            action()
            return True
        except Exception as exc:
            self.error(str(exc))
            return False

    def device_request(self, action, *, imu=False):
        if self.tools.busy or (imu and (not self.connected or self.audio_state != "idle"
                                        or not self.tools.supports("imu"))):
            self.error("请等待当前操作结束；IMU 需要已连接且支持此能力的设备。")
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
        return not self.closing and not self.tools.busy and not self.training and self._imu_ready

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
        active = f"；正在录制：{len(self.take)} 样本" if self.take is not None else ""
        return f"未保存：{len(lengths)} 次，长度 {lengths}{active}"

    @property
    def device_summary(self):
        if self.connected:
            return self.device_name or self.address
        return "正在连接…" if self.connecting else "未连接"

    # ----------------------------------------------------- parameters & model
    def invalidate(self, reason):
        self.revision += 1
        if self.evaluation_revision is not None:
            self.evaluation_text = "数据或参数已改变；请重新进行留出评估。"
            self.evaluation_revision = None
        if self.bundle is not None and self.origin == "trained" and not self.stale:
            self.stale = True
            self.stop_live()
            self.model_text = f"{reason}；旧模型已失效，请重新训练。"
            self.log(f"{reason}；旧模型不可导出或试识别。")

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
            self.log("已应用预设；请重新训练。原始录制不变，短促模式只使用冲击前后窗口。")
        except (ValueError, TypeError) as exc:
            self.error(str(exc))

    def parameters_changed(self):
        self.invalidate("训练参数已改变")
        try:
            pipeline = self.pipeline_settings()
            config = self.segmentation_settings(pipeline)
            if config.mode == "impulse":
                self.mode_text = (f"短促动作：加速度相邻差触发，前 {config.pre_roll / pipeline.sample_rate_hz:.2f}s "
                                  f"+ 后 {config.post_roll / pipeline.sample_rate_hz:.2f}s；"
                                  "训练默认自动裁剪，可在波形中手动框选")
            else:
                self.mode_text = (f"连续动作：结束静止 {config.min_offset_frames / pipeline.sample_rate_hz:.2f}s，"
                                  f"最长 {config.max_gesture_len / pipeline.sample_rate_hz:.1f}s（按采样率换算）")
        except (ValueError, TypeError):
            self.mode_text = "参数尚未完整或无效；请检查采样率、低通、中值窗口和触发阈值。"
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
        return [f"第 {i + 1} 次 · {len(rep)} 样本" for i, rep in enumerate(self.preview_repetitions)]

    def set_preview_index(self, index):
        if index != self.preview_index:
            self.preview_index = index
            self.show_preview()

    def show_preview(self):
        index = self.preview_index
        self.preview_view = None
        if not self.preview_repetitions or not 0 <= index < len(self.preview_repetitions):
            self.preview_message = "暂无录制波形"
        else:
            try:
                # Use the recording's actual rate, not a potentially different training rate.
                pipeline = self.pipeline_settings(self.preview_rate)
                self.preview_view = analyze_recording(
                    self.preview_repetitions[index], pipeline,
                    segmentation=self.segmentation_settings(pipeline),
                    training_region=self.preview_training_regions[index])
                self.preview_message = self.preview_view.status
            except (ValueError, TypeError) as exc:
                self.preview_message = f"无法预览，请检查滤波参数：{exc}"
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
                    raise ValueError(f"手动训练框过短，至少需要 {minimum} 个采样点，请扩大选框。")
            if region == self.preview_training_regions[index]:
                return
            if self.preview_is_pending:
                self.pending_regions[index] = region
                self.preview_training_regions[index] = region
                saved_text = "随录制一起保存"
            elif self.preview_path is not None:
                # Re-read before editing; never overwrite externally changed raw
                # recordings or unrelated annotations with a stale preview copy.
                dataset = load_dataset(self.preview_path)
                if (not self.same_rate(dataset.sample_rate_hz, self.preview_rate)
                        or len(dataset.repetitions) != len(self.preview_repetitions)
                        or any(not np.array_equal(a, b) for a, b in
                               zip(dataset.repetitions, self.preview_repetitions))):
                    raise ValueError("录制文件已变化，请刷新数据集后重新选择。")
                regions = list(dataset.training_regions or [None] * len(dataset.repetitions))
                regions[index] = region
                dataset.training_regions = regions
                save_dataset_file(dataset, self.preview_path)
                self.preview_training_regions = list(regions)
                for key, (path, _) in list(self.records.items()):
                    if path == self.preview_path:
                        self.records[key] = (path, dataset)
                self.invalidate("训练裁剪框已改变")
                saved_text = "已保存到原文件；需重新训练"
            else:
                return
            detail = (f"手动训练框 [{region[0]}, {region[1]})，{region[1] - region[0]} 样本"
                      if region is not None else "已恢复自动裁剪")
            self.log(f"第 {index + 1} 次：{detail}；{saved_text}。原始录制不变。")
        except (ValueError, OSError, TypeError) as exc:
            self.error(str(exc))
        finally:
            self.show_preview()
            self.notify()

    # ------------------------------------------------------------------ capture
    def set_name(self, name):
        if name == self.name:
            return
        self.name = name
        if self.pending or self.take is not None:
            self.log("手势名称改变，已丢弃未保存数据，避免混入新手势。")
            self.clear_takes()
        self.notify()

    def cancel_take(self):
        if self.take is not None:
            self.log("未完成的本次录制已取消。")
        self.take = None
        self.notify()

    def clear_takes(self):
        self.cancel_take()
        self.pending.clear()
        self.pending_regions.clear()
        self.pending_rate = None
        self.notify()
        if self.preview_is_pending:
            self.preview_recordings([], None, "暂无未保存录制")

    def discard_takes(self):
        if self.confirm_discard():
            self.clear_takes()

    def confirm_discard(self):
        return not (self.pending or self.take is not None) or self.dialogs.ask(
            "丢弃未保存录制？", "继续将丢弃未保存的重复和正在录制的数据。")

    def toggle_record(self):
        # Process already-received batches at the boundary so idle samples do
        # not leak into a new take, and the last batch is retained on stop.
        self.drain_events()
        if self.tools.busy:
            self.error("工具操作期间不能录制手势。")
            return
        if self.take is not None:
            samples = self.take
            self.take = None
            if len(samples) < MIN_TAKE_SAMPLES:
                self.log(f"本次仅 {len(samples)} 个样本，少于 {MIN_TAKE_SAMPLES}，已拒绝。")
                if samples:
                    self.preview_recordings([samples], self.take_rate, "本次过短（未保留）",
                                            pending=True, reveal=True)
            else:
                try:
                    dataset = GestureDataset(self.name.strip(), [np.asarray(samples)], self.take_rate)
                    self.pending.extend(dataset.repetitions)
                    self.pending_regions.append(None)
                    self.pending_rate = self.take_rate
                    self.preview_recordings(self.pending, self.pending_rate, f"未保存：{dataset.name}",
                                            pending=True, reveal=True, training_regions=self.pending_regions)
                except Exception as exc:
                    self.error(str(exc))
            self.notify()
            return
        if not self._imu_ready or not self.name.strip():
            self.error("请输入手势名称，等待 IMU 恢复上报；录音期间不能录制手势。")
            return
        if self.pending and not self.same_rate(self.pending_rate, self.actual_rate):
            self.error("未保存录制与当前设备采样率不一致，请先保存或撤销。")
            return
        self.take_rate = self.actual_rate
        self.take = []
        self.notify()

    def undo(self):
        if self.take is not None:
            self.error("请先停止本次录制。")
        elif self.pending:
            self.pending.pop()
            self.pending_regions.pop()
            self.preview_recordings(self.pending, self.pending_rate, f"未保存：{self.name}", pending=True,
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
                raise ValueError(f"文件名冲突：{target.name} 属于“{existing.name}”，不会覆盖。请换一个名称。")
            if not self.same_rate(existing.sample_rate_hz, dataset.sample_rate_hz):
                raise ValueError("同名数据的采样率不同，不能追加；请选择其他名称或目录。")
            if not self.dialogs.ask("追加重复？", f"“{dataset.name}”已有 {len(existing.repetitions)} 次。\n"
                                    f"追加本次 {len(dataset.repetitions)} 次？原数据将保留。"):
                return False
            regions = ((existing.training_regions or [None] * len(existing.repetitions))
                       + (dataset.training_regions or [None] * len(dataset.repetitions)))
            dataset = GestureDataset(dataset.name, existing.repetitions + dataset.repetitions,
                                     dataset.sample_rate_hz, regions)
        if len(dataset.repetitions) < minimum:
            raise ValueError(f"保存录制需要至少 {minimum} 次重复（可以包含已有数据）。")
        self.directory.mkdir(parents=True, exist_ok=True)
        saved = save_dataset(dataset, self.directory)
        self.log(f"已保存 {saved}；共 {len(dataset.repetitions)} 次，{dataset.sample_rate_hz:g} Hz。")
        self.refresh()
        return True

    def save_takes(self):
        if self.take is not None or not self.pending:
            self.error("请先停止录制，并至少保留一次有效重复。")
            return
        try:
            dataset = GestureDataset(self.name.strip(), list(self.pending), self.pending_rate,
                                     list(self.pending_regions))
            if self.persist(dataset, minimum=2):
                path = dataset_path(dataset.name, self.directory)
                saved = load_dataset(path)
                self.preview_recordings(saved.repetitions, saved.sample_rate_hz, f"刚保存：{saved.name}",
                                        path=path, training_regions=saved.training_regions)
                self.clear_takes()
        except Exception as exc:
            self.error(str(exc))

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
            self.preview_recordings(dataset.repetitions, dataset.sample_rate_hz, f"已保存：{dataset.name}",
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
        self.invalidate("数据目录或数据集已刷新")
        if self.preview_path is not None:
            self.preview_recordings([], None, "请重新选择已保存手势")
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
                    self.log(f"无效数据 {path.name}：{exc}；修正或移出目录后才能训练。")
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
            self.error(str(exc))
            return False

    def delete_datasets(self):
        if self.locked:
            return
        selected = list(self.selected_ids)
        if not selected:
            self.error("请先在数据集列表中选择要删除的手势。")
            return
        if any(item not in self.records for item in selected):
            self.error("所选手势已变化，请刷新数据集后重新选择。")
            return
        directory = self.directory
        targets = [(item, *self.records[item]) for item in selected]
        details = "\n".join(f"“{dataset.name}”（{len(dataset.repetitions)} 次录制）\n  {path}"
                            for _, path, dataset in targets)
        if not self.dialogs.ask(
                "永久删除选中手势？", f"将永久删除选中的 {len(targets)} 个手势：\n\n{details}\n\n"
                "所列 JSON 中的全部录制和训练框标注都会删除，无法撤销。\n"
                "未保存录制、正在采集的数据和已导出的模型文件均保留。\n"
                "当前训练出的模型需要重新训练；独立加载的模型包不受影响。", danger=True):
            return
        # Recheck after the modal, and never expand to a newly selected row or path.
        if self.locked:
            return
        if self.directory != directory or any(
                item not in self.records or self.records[item][0] != path for item, path, _ in targets):
            self.error("所选手势已变化，请刷新数据集后重新选择。")
            return
        deleted, missing, failures, removed = [], [], [], []
        for item, path, _ in targets:
            try:
                path.unlink()
            except FileNotFoundError:
                missing.append(str(path))
            except OSError as exc:
                failures.append(f"{path}：{exc}")
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
        result = [f"删除结果：成功删除 {len(deleted)} 个；已不存在 {len(missing)} 个；失败 {len(failures)} 个。"]
        for label, paths in (("已删除", deleted), ("文件已不存在（非本次删除）", missing), ("删除失败", failures)):
            if paths:
                result.append(f"{label}：\n" + "\n".join(paths))
        if not refreshed:
            result.append("列表刷新失败，请手动刷新；已删除的文件不会恢复。")
        if missing or failures or not refreshed:
            self.error("\n".join(result))
        else:
            self.log("\n".join(result))
        self.notify()

    def import_json(self):
        for path in self.dialogs.open_files("数据 JSON (*.json)"):
            try:
                if Path(path).resolve().parent == self.directory.resolve():
                    self.log(f"{Path(path).name} 已在当前目录，仅刷新。")
                    self.refresh()
                else:
                    self.persist(load_dataset(path))
            except Exception as exc:
                self.error(f"{Path(path).name}：{exc}")

    def import_csv(self):
        name = self.name.strip()
        if not name:
            self.error("导入 CSV 前请输入手势名称，并设置训练参数中的采样率。")
            return
        paths = self.dialogs.open_files("六轴 CSV (*.csv)")
        if not paths:
            return
        try:
            rate = float(self.params["sample_rate_hz"])
            repetitions = [np.loadtxt(path, delimiter=",", ndmin=2) for path in paths]
            self.persist(GestureDataset(name, repetitions, rate))
        except Exception as exc:
            self.error(str(exc))

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
            self.error("请先停止录制并保存数据。")
            return
        if self.pending and not self.dialogs.ask("仍有未保存录制", "训练只使用已保存数据。继续？"):
            return
        try:
            # Read files again: a dataset might have changed outside this process.
            self.refresh()
            if self.invalid_files:
                raise ValueError(f"目录中有无效数据：{', '.join(self.invalid_files)}。请先修正或移出目录。")
            datasets = [record[1] for record in self.records.values()]
            if not datasets:
                raise ValueError("当前目录没有有效数据。")
            pipeline = self.pipeline_settings()
            segmentation = self.segmentation_settings(pipeline)
            n_states = int(self.params["n_states"])
            if n_states < 1:
                raise ValueError("状态数必须为正整数。")
        except Exception as exc:
            self.error(str(exc))
            return
        # A failed new run must never leave a previous model looking current.
        self.bundle = None
        self.origin = None
        revision = self.revision
        summary = "；".join(f"{d.name}: {len(d.repetitions)} 次/{sum(map(len, d.repetitions))} 样本" for d in datasets)
        self.model_text = "正在后台训练，请查看日志…"
        self.log(f"训练开始：{summary}")
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
                events.put(("train_failed", str(exc)))
        self.executor.submit(run)

    def evaluate(self):
        if self.training:
            return
        if self.take is not None:
            self.error("请先停止本次录制；评估只使用已保存数据。")
            return
        if self.pending and not self.dialogs.ask("仍有未保存录制", "评估只使用已保存数据。继续？"):
            return
        try:
            # Do not refresh/invalidate the current model for a read-only evaluation.
            datasets = [load_dataset(path) for path in sorted(self.directory.glob("*.json"))]
            if not datasets:
                raise ValueError("当前目录没有评估数据。")
            pipeline = self.pipeline_settings()
            segmentation = self.segmentation_settings(pipeline)
            n_states = int(self.params["n_states"])
        except Exception as exc:
            self.error(str(exc))
            return
        self.evaluation_text = "正在后台逐次留出评估；不会替换当前模型…"
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
                events.put(("evaluation_failed", str(exc)))
        self.executor.submit(run)

    def set_bundle(self, bundle, origin, summary):
        self.stop_live()
        self.bundle = bundle
        self.origin = origin
        self.stale = False
        mode = "短促动作" if bundle.segmentation.mode == "impulse" else "连续动作"
        rejection = "带拒识标定（非概率）" if bundle.rejection else "无拒识标定；单类不能开启自动识别，请重新训练"
        self.model_text = (f"{summary}；手势：{', '.join(bundle.gesture_names)}；{bundle.pipeline.sample_rate_hz:g} Hz；"
                           f"{mode}，中值 {bundle.pipeline.median_kernel} / 低通 {bundle.pipeline.cutoff_hz:g} Hz；{rejection}")
        self.log(self.model_text)
        self.notify()

    def load_bundle(self):
        try:
            path = self.dialogs.open_file("手势模型包（JSON） (*.json)")
            if path:
                self.set_bundle(GestureBundle.load(path), "imported", f"已加载 {Path(path).name}（独立于当前训练数据）")
        except Exception as exc:
            self.error(str(exc))

    def export_bundle(self):
        if not self.can_export:
            return
        try:
            path = self.dialogs.save_file("model.gesture.json", "手势模型包（JSON） (*.json)")
            if path:
                self.bundle.save(path)
                self.log(f"模型包已导出：{path}")
        except Exception as exc:
            self.error(str(exc))

    # -------------------------------------------------------- live recognition
    def stop_live(self):
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
                raise ValueError("请先训练有效模型或加载模型包。")
            if self.tools.busy or not self._imu_ready:
                raise ValueError("请先连接设备并等待 IMU 恢复上报；录音期间不能试识别。")
            if not self.same_rate(self.actual_rate, self.bundle.pipeline.sample_rate_hz):
                raise ValueError(f"采样率不匹配：设备 {self.actual_rate:g} Hz，模型 {self.bundle.pipeline.sample_rate_hz:g} Hz。")
            self.recognizer = GestureRecognizer(self.bundle)
            if len(self.bundle.gesture_names) == 1 and not self.recognizer.has_rejection:
                raise ValueError("这个单类旧模型没有拒识标定，普通运动也会被当成手势。请至少录制 3 次，选择合适预设后重新训练。")
            self.recognition_origin = self.stream.next_sample
            if not self.recognizer.has_rejection:
                self.log("警告：旧模型未配置未知动作拒识，建议重新训练。")
            self.log("实时试识别已开启；仅通过检查的片段画紫框。单类没有相对置信度，拒识标定仍需用负样本检验。")
        except Exception as exc:
            self.stop_live()
            self.error(str(exc))

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
                self.log(f"试识别已停止：{exc}")

    def _record_prediction(self, prediction):
        assessment = ("单类匹配，通过拒识检查（无相对置信度）" if prediction.confidence is None else
                      f"相对置信度 {prediction.confidence:.3f}（非概率）")
        self.prediction = (prediction.name, f"{assessment}；分数 {prediction.score:.3f}")
        self.prediction_serial += 1
        self.result_text = f"{prediction.name}；{self.prediction[1]}"
        self.log(self.result_text)
        if prediction.start_sample is not None and prediction.end_sample is not None:
            self.stream.add_recognition(
                self.recognition_origin + prediction.start_sample,
                self.recognition_origin + prediction.end_sample,
                prediction.name if prediction.confidence is None else f"{prediction.name} {prediction.confidence:.2f}")

    def set_stream_state(self, payload):
        active = payload["state"] == "active"
        message = payload.get("message", "IMU 正常上报" if active else "IMU 暂停，等待恢复订阅（无需切换模式）")
        if active:
            rate = float(payload["sample_rate_hz"])
            if not self.imu_active or not self.same_rate(self.actual_rate, rate):
                self.stream.set_sample_rate(rate)
                if self.recognizer is not None:
                    self.recognizer.reset()
                    self.recognizer = None
            self.actual_rate = rate
            message = f"{message}；{rate:g} Hz"
            if payload.get("accel_range_g") is not None:
                message += f"；加速度 ±{payload['accel_range_g']} g，陀螺仪 ±{payload['gyro_range_dps']} °/s"
        else:
            self.actual_rate = None
            self.cancel_take()
            if self.connected and self.live:
                if self.recognizer is not None:
                    self.recognizer.reset()
                self.recognizer = None
                self.prediction = None
                self.result_text = "识别已暂停；IMU 恢复后自动继续（请先静止）"
            else:
                self.stop_live()
            self.latest_sample = None
            self.raw_note = "无新数据，未断言 BLE 断开"
        self.imu_active = active
        self.acceleration.set_stream_state(payload)
        self.stream_text = message
        if active and self.live and self.recognizer is None and self.audio_state in {"idle", "listening"}:
            self._sync_live()
        self.notify()

    def set_battery_status(self, payload=None):
        """Render only reported values; 0% is valid, absent/out-of-range is not."""
        text, tone = "电量：—", "muted"
        if payload is not None:
            percent = payload.get("battery_percent")
            charging = payload.get("battery_charging") is True
            valid = type(percent) is int and 0 <= percent <= 100
            text = f"电量：{percent}%" if valid else "电量：未知"
            if charging:
                text += " · 充电中"
                tone = "good"
            elif valid and percent <= 20:
                text += " · 低电量"
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
            name = scanned.get(key, {}).get("name") or entry["name"] or "未命名"
            rssi = scanned.get(key, {}).get("rssi")
            entries.append(dict(name=name, address=entry["address"], source="历史", rssi=rssi))
            seen.add(key)
        for entry in self.scanned_devices:
            key = entry["address"].casefold()
            if key not in seen:
                entries.append(dict(name=entry.get("name") or "未命名", address=entry["address"],
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
            self.log(f"设备已连接，但无法保存历史：{exc}")
        known = next((e["name"] for e in self.device_history.entries
                      if e["address"].casefold() == address.casefold()), "")
        self.device_name = known or name or payload.get("model", "")
        self.address = address
        self.refresh_device_choices()

    def clear_device_history(self):
        if not self.dialogs.ask("清空设备历史？", "仅删除本机保存的设备历史，不会断开当前设备。"):
            return
        try:
            self.device_history.clear()
            self.refresh_device_choices()
        except OSError as exc:
            self.error(str(exc))

    def scan(self):
        if not self.can_scan:
            self.error("请等待当前操作结束；连接设备期间不能扫描，请先断开。")
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
            self.error("请等待工具操作完成后再连接。")
            return
        address = self.address.strip()
        if not address:
            self.error("请先选择设备或输入地址。")
            return
        if self.connected or self.connecting:
            self.error("请先断开当前设备 / 取消正在进行的连接。")
            return
        if self.scanning:
            self.error("请等待扫描结束后再连接。")
            return
        if not self.confirm_discard():
            return
        self.clear_takes()
        self.stop_live()
        self.connecting = True
        self.set_battery_status()
        self.status = "正在连接；可以点击断开取消"
        if not self.request(lambda: self.worker.connect(address)):
            self.connecting = False
            self.status = "连接失败"
        self.notify()

    def disconnect_device(self):
        if self.tools.firmware_locked:
            self.error("固件升级进行中，不能断开；请等待完成或超时。")
            return
        if self.audio_state != "idle" and not self.dialogs.ask(
                "音频尚未结束", "断开将停止接收/下载；远程录音采用断线停止策略。"
                "未完整接收的音频不会标为完整文件，设备副本可之后查询。继续？"):
            return
        if self.tools.firmware_locked:
            return
        self.tools.capabilities = {}
        self.tools.clear_calibration_sample()
        self.tools.connection_epoch += 1
        self.set_stream_state({"state": "waiting", "message": "IMU 已停止"})
        self.cancel_take()
        self.stop_live()
        self.connected = False
        self.connecting = False
        self.actual_rate = None
        self.set_battery_status()
        self.status = "正在断开"
        self.request(self.worker.disconnect)
        self.notify()

    # -------------------------------------------------------------------- audio
    def choose_audio_directory(self):
        if self.tools.busy or self.audio_state != "idle":
            self.error("工具运行、录音、接收、下载或保存期间不能更改目录。")
            return
        path = self.dialogs.directory(str(self.audio_directory.parent))
        if path:
            self.audio_directory = Path(path)
            self.notify()

    def start_audio(self):
        if not self.tools.available("audio"):
            self.error("请连接支持录音的戒指，并等待手势录制、音频或工具结束。")
            return
        self.set_audio_state({"state": "starting", "message": "正在开启保存通知监听（不占用手势采集）…"})
        if not self.request(lambda: self.worker.start_audio(self.audio_directory)):
            self.set_audio_state({"state": "idle", "message": "开启接收失败"})

    def stop_audio(self):
        if self.tools.busy:
            return
        if self.tools.remote_session or self.audio_state in {"recording", "live"}:
            self.tools.finish_recording()
            return
        if self.audio_state not in {"idle", "saving", "stopping"}:
            if self.audio_state in {"receiving", "downloading"} and not self.dialogs.ask(
                    "中断音频传输？", "未接收完整的文件不会保存。之后可从戒指历史录音重新下载。"):
                return
            if self.tools.busy:
                return
            self.set_audio_state({"state": "stopping", "message": "正在停止电脑端接收（不控制戒指录音）…"})
            self.request(self.worker.stop_audio)

    def list_audio(self):
        if not self.tools.available("audio"):
            self.error("请连接支持录音的戒指，停止手势录制、音频和工具后再查询。")
            return
        self.stop_live()
        self.set_audio_state({"state": "listing", "message": "正在查询历史录音…"})
        if not self.request(self.worker.list_audio):
            self.set_audio_state({"state": "idle", "message": "查询请求失败"})

    def set_remote_audio_selected(self, label):
        self.remote_audio_selected = label

    def download_audio(self):
        selected = self.remote_audio_selected
        if not self.tools.available("audio") or selected not in self.remote_audio_files:
            self.error("请先查询并选择历史录音，停止当前手势录制/音频接收后再下载。")
            return
        self.stop_live()
        self.set_audio_state({"state": "downloading", "message": "正在准备下载…"})
        if not self.request(lambda: self.worker.download_audio(self.remote_audio_files[selected], self.audio_directory)):
            self.set_audio_state({"state": "idle", "message": "下载请求失败"})

    def set_audio_state(self, payload):
        self.audio_state = payload["state"]
        self.audio_text = payload.get("message", self.audio_state)
        if self.audio_state in {"idle", "listening"} and self.imu_active and self.live and self.recognizer is None:
            self._sync_live()
        self.notify()

    def open_path(self, path):
        try:
            self.dialogs.open_path(Path(path).resolve(strict=True))
        except (OSError, ValueError) as exc:
            self.error(str(exc))

    def open_url(self, url):
        try:
            self.dialogs.open_url(url)
        except OSError as exc:
            self.error(str(exc))

    def _clear_remote_audio(self):
        self.remote_audio_files = {}
        self.remote_audio_selected = ""
        self.remoteAudioChanged.emit()

    # ------------------------------------------------------------------- events
    def handle(self, event, payload):
        if self.tools.handle(event, payload):
            return
        if event == "samples":
            self.samples(payload)
        elif event == "devices":
            self.scanned_devices = list(payload)
            self.refresh_device_choices()
            self.log(f"扫描发现 {len(payload)} 个设备；已连接设备保留在历史中。")
        elif event == "connected":
            if not self.connecting:
                self.request(self.worker.disconnect)
                return
            self.connected, self.connecting = True, False
            self.set_battery_status(payload)
            self.remember_device(payload)
            self.set_stream_state({"state": "waiting", "message": "BLE 已连接，正在等待新的 IMU 数据…"})
            self.count = 0
            self.stream.clear()
            self._clear_remote_audio()
            self.status = (f"BLE 已连接 {payload['address']}；{payload.get('model', '')} "
                           f"{payload.get('firmware_version', '')}")
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
            self.set_stream_state({"state": "waiting", "message": "连接中断，正在自动重连"})
            self.status = payload["message"]
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text})
            self.log(payload["message"])
        elif event == "disconnected":
            self.connected = self.connecting = False
            self.device_name = ""
            self.set_battery_status()
            self.set_stream_state({"state": "waiting", "message": "IMU 未连接"})
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text})
            self.status = "已断开；未完成的手势录制已取消"
            self.notify()
        elif event == "audio_state":
            self.set_audio_state(payload)
            self.log(payload.get("message", payload["state"]))
        elif event == "audio_progress":
            total = payload.get("total")
            suffix = f" / {total / 1024:.1f} KiB" if total else ""
            self.audio_text = f"正在接收音频文件：{payload['bytes'] / 1024:.1f} KiB{suffix}（不是录音计时）"
            self.notify()
        elif event == "audio_files":
            self.remote_audio_files = {f"录音索引 {entry['file_index']}": entry["file_index"] for entry in payload}
            self.remote_audio_selected = next(iter(self.remote_audio_files), "")
            self.remoteAudioChanged.emit()
            self.log(f"戒指中有 {len(payload)} 个录音文件；不会自动删除设备文件。")
            self.notify()
        elif event == "audio_saved":
            path = Path(payload["path"])
            duration = payload.get("duration_s")
            self.audio_rows.append(dict(path=path, name=path.name,
                                        duration=f"{duration:.1f} s" if duration is not None else "未解码",
                                        size=f"{path.stat().st_size / 1024:.1f} KiB"))
            self.audioFilesChanged.emit()
            raw_path = payload.get("raw_path")
            self.log(f"录音已保存：{path}" + (f"；原始文件：{raw_path}" if raw_path else ""))
        elif event == "scan_finished":
            self.scanning = False
            self.notify()
        elif event in ("warning", "error"):
            self.log(payload, event)
        elif event == "progress":
            self.log(payload)
        elif event == "trained":
            bundle, summary, revision = payload
            self.training = False
            self.set_bundle(bundle, "trained", f"训练成功：{summary}")
            if revision != self.revision:
                self.invalidate("训练期间数据或参数已改变")
            self._finish_background()
        elif event == "evaluated":
            result, revision = payload
            self._finish_background()
            metric = "单类正样本通过率（不代表误触率）" if len(result["per_class"]) == 1 else "已知手势留出识别率"
            text = f"{metric}：{result['correct']}/{result['total']}（{result['accuracy']:.1%}）；未做连续流负样本评估"
            if revision != self.revision:
                text += "；数据/参数已改变，此为评估开始时的结果"
            self.evaluation_text = text
            self.evaluation_revision = revision
            self.log(text)
            for name, counts in result["per_class"].items():
                self.log(f"{name}：{counts['correct']}/{counts['total']}；预测分布 {result['confusion'][name]}；"
                         f"跳过过短录制 {result['skipped_recordings'][name]} 次")
            self.log("留出评估不是跨会话/真机准确率；请另录独立数据验证。当前模型未被替换。")
            self.notify()
        elif event == "evaluation_failed":
            self._finish_background()
            self.evaluation_text = "留出评估失败；带拒识标定时每类需至少 4 次有效录制，当前模型保持不变。"
            self.notify()
            self.error(payload)
        elif event == "train_failed":
            self._finish_background()
            self.model_text = "训练失败；没有生成新模型。请查看日志并修正数据或参数。"
            self.notify()
            self.error(payload)

    # ------------------------------------------------------------------ closing
    def request_close(self):
        """Begin shutdown if confirmed; closeFinished fires when cleanup ends."""
        if self.tools.firmware_locked:
            self.error("固件升级进行中，不能退出；请等待完成或超时。")
            return
        if self.closing or not self.confirm_discard():
            return
        if self.training and not self.dialogs.ask(
                "训练 / 评估尚未完成", "关闭窗口将放弃本次结果；后台计算可能稍后才结束。继续？"):
            return
        if self.audio_state != "idle" and not self.dialogs.ask(
                "音频尚未结束", "退出将停止接收/下载；远程录音采用断线停止策略。"
                "完整音频会继续保存，未完整音频不会标为完整文件。继续？"):
            return
        # Modal confirmations pump events; OTA may have started in the meantime.
        if self.tools.firmware_locked:
            return
        self.closing = True
        self.scanning = False
        self.cancel_take()
        self.stop_live()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.set_battery_status()
        self.status = "正在关闭设备，最多等待 8 秒…"
        self.notify()
        try:
            self._close_future = self.worker.close()
        except Exception as exc:
            self.error(f"设备清理请求失败：{exc}")
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
                self.error(f"设备清理失败：{exc}")
            self.closeFinished.emit()
        elif time.monotonic() >= self._close_deadline:
            self.error("设备清理超过 8 秒，窗口将关闭；蓝牙后台可能未正常退出。")
            self.closeFinished.emit()
        else:
            QTimer.singleShot(100, self.wait_closed)
