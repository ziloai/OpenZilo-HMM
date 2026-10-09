# SPDX-License-Identifier: MPL-2.0
"""Tk desktop trainer; importing this module never initializes Tk or Bluetooth."""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np


# Cocoa/Tk can abort when a compound extension resolves to a nil UTType.
# Use JSON for native filters/default extensions; .gesture.json is a filename
# convention, while GestureBundle.load validates the actual model format.
_BUNDLE_FILETYPES = (("手势模型包（JSON）", "*.json"),)


class Studio:
    def __init__(self, root):
        global tk, ttk, filedialog, messagebox
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
        # Keep optional desktop dependencies out of the inference package.
        global PipelineConfig, SegmentationConfig, GestureRecognizer, GestureBundle
        global GestureDataset, load_dataset, save_dataset, dataset_path, train_datasets
        from hmm_gesture import (
            PipelineConfig, SegmentationConfig, GestureRecognizer, GestureBundle,
        )
        from .datasets import GestureDataset, load_dataset, save_dataset, dataset_path
        from .training import train_datasets
        from .device import RingWorker
        from .device_history import DeviceHistory

        self.root = root
        self.events = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.worker = RingWorker(lambda event, payload: self.events.put((event, payload)))
        self.directory = Path.cwd() / "gestures"
        self.records = {}
        self.invalid_files = []
        self.devices = {}
        self.scanned_devices = []
        self.device_history = DeviceHistory()
        self.preview_repetitions = []
        self.preview_rate = None
        self.preview_is_pending = False
        self.preview_training_regions = []
        self.preview_path = None
        self.preview_editable = False
        self.recognition_origin = 0
        self.pending = []
        self.pending_regions = []
        self.take = None
        self.take_rate = None
        self.pending_rate = None
        self.connected = False
        self.connecting = False
        self.actual_rate = None
        self.imu_active = False
        self.audio_state = "idle"
        self.audio_directory = Path.cwd() / "audio"
        self.audio_files = {}
        self.remote_audio_files = {}
        self.count = 0
        self.training = False
        self.closing = False
        self.bundle = None
        self.recognizer = None
        self.origin = None
        self.stale = False
        self.revision = 0
        self.evaluation_revision = None
        self.locked = []
        self.root.title("OpenZilo 手势与录音工作室")
        self.root.geometry("1060x860")
        self.root.minsize(980, 760)
        self._build()
        self.refresh_device_choices()
        if self.device_history.warning:
            self.log(self.device_history.warning)
        self.set_audio_state({"state": "idle", "message": "未开启接收"})
        self.worker.start()
        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self.poll)
        self.log("每次只录制一个完整动作；拒识标定每类至少 3 次，留出评估至少 4 次，建议采集更多变化。")
        self.log("原始六轴顺序：ax, ay, az, gx, gy, gz（设备原始整数，不转换单位）。")
        self.log("响指/敲击请先应用短促动作预设；单类不显示虚构置信度，正样本标定仍不能保证消除误触。")
        self.log("训练、录制和识别必须使用相同采样率。")
        self.log("实时曲线页可暂停显示；录音页先开启接收，再在戒指上长按录音、松开推送。")

    def _build(self):
        body = ttk.Frame(self.root, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self.tabs = ttk.Notebook(body)
        self.tabs.grid(row=1, column=0, sticky="nsew", pady=5)
        self.gesture_tab = ttk.Frame(self.tabs)
        # The controls must remain reachable on smaller desktop displays.
        gesture_canvas = tk.Canvas(self.gesture_tab, highlightthickness=0)
        gesture_scroll = ttk.Scrollbar(self.gesture_tab, orient="vertical", command=gesture_canvas.yview)
        gesture_scroll.pack(side="right", fill="y")
        gesture_canvas.pack(side="left", fill="both", expand=True)
        gesture_canvas.configure(yscrollcommand=gesture_scroll.set)
        gesture = ttk.Frame(gesture_canvas, padding=6)
        gesture.columnconfigure(0, weight=1)
        content = gesture_canvas.create_window(0, 0, window=gesture, anchor="nw")
        gesture.bind("<Configure>", lambda event: gesture_canvas.configure(scrollregion=gesture_canvas.bbox("all")))
        gesture_canvas.bind("<Configure>", lambda event: gesture_canvas.itemconfigure(content, width=event.width))

        def scroll_gesture(event):
            if not (str(event.widget).startswith(str(gesture)) or event.widget is gesture_canvas):
                return
            if gesture_canvas.yview() == (0.0, 1.0):
                return
            if getattr(event, "num", None) in (4, 5):
                steps = -1 if event.num == 4 else 1
            else:
                steps = -int(event.delta if sys.platform == "darwin" else event.delta / 120)
            if steps:
                gesture_canvas.yview_scroll(steps, "units")
                return "break"
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind(sequence, scroll_gesture, add="+")
        chart = ttk.Frame(self.tabs, padding=6)
        audio = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(self.gesture_tab, text="手势录制与训练")
        self.tabs.add(chart, text="实时六轴曲线")
        self.preview_tab = ttk.Frame(self.tabs, padding=6)
        self.tabs.add(self.preview_tab, text="录制波形（滤波后）")
        self.tabs.add(audio, text="戒指录音")
        from .plotting import IMUPlot, RecordingPlot
        self.plot = IMUPlot(chart)
        self.plot.widget.pack(fill="both", expand=True)
        preview_controls = ttk.Frame(self.preview_tab)
        preview_controls.pack(fill="x", pady=5)
        self.preview_title = tk.StringVar(value="尚无录制")
        ttk.Label(preview_controls, textvariable=self.preview_title).pack(side="left")
        self.preview_choice = ttk.Combobox(preview_controls, state="readonly", width=25)
        self.preview_choice.pack(side="left", padx=8)
        self.preview_choice.bind("<<ComboboxSelected>>", self.show_preview)
        self.button(preview_controls, "返回录制 / 训练", lambda: self.tabs.select(self.gesture_tab)).pack(side="right")
        self.preview_plot = RecordingPlot(self.preview_tab, on_training_region=self.set_preview_training_region)
        self.preview_plot.widget.pack(fill="both", expand=True)
        self._build_audio(audio)
        work = ttk.Frame(gesture)
        work.grid(row=0, column=0, sticky="ew")
        self.dir_text = tk.StringVar(value=str(self.directory))
        ttk.Label(work, textvariable=self.dir_text).pack(side="left", fill="x", expand=True)
        self.button(work, "选择数据目录", self.choose_directory, lock=True).pack(side="right")

        device = ttk.LabelFrame(body, text="设备连接（切换模式不会主动断开 BLE）", padding=6)
        device.grid(row=0, column=0, sticky="ew", pady=5)
        self.button(device, "扫描", lambda: self.request(self.worker.scan)).grid(row=0, column=0)
        device.columnconfigure(1, weight=1)
        self.device_choice = ttk.Combobox(device, state="readonly", width=24)
        self.device_choice.grid(row=0, column=1, padx=5, sticky="ew")
        self.device_choice.bind("<<ComboboxSelected>>", self.select_device)
        self.address = tk.StringVar()
        ttk.Entry(device, textvariable=self.address, width=24).grid(row=0, column=2)
        self.connect_button = self.button(device, "连接", self.connect)
        self.connect_button.grid(row=0, column=3, padx=5)
        self.button(device, "断开 / 取消连接", self.disconnect).grid(row=0, column=4)
        self.button(device, "清空设备历史", self.clear_device_history).grid(row=0, column=5, padx=5)
        self.status = tk.StringVar(value="未连接；可扫描或直接输入地址")
        ttk.Label(device, textvariable=self.status).grid(row=1, column=0, columnspan=5, sticky="w")
        self.raw = tk.StringVar(value="ax / ay / az / gx / gy / gz：—；样本数：0")
        ttk.Label(device, textvariable=self.raw).grid(row=2, column=0, columnspan=5, sticky="w")
        self.stream_text = tk.StringVar(value="IMU 未就绪；连接后自动等待手势模式")
        ttk.Label(device, textvariable=self.stream_text).grid(row=3, column=0, columnspan=4, sticky="w")
        self.button(device, "重试 IMU 上报", lambda: self.request(self.worker.retry_imu)).grid(row=3, column=4)

        recording = ttk.LabelFrame(gesture, text="录制数据（每次至少 12 个样本）", padding=6)
        recording.grid(row=1, column=0, sticky="ew", pady=5)
        ttk.Label(recording, text="手势名称").grid(row=0, column=0)
        self.name = tk.StringVar()
        self.name_entry = ttk.Entry(recording, textvariable=self.name, width=24)
        self.name_entry.grid(row=0, column=1, padx=5)
        self.locked.append(self.name_entry)
        self.name.trace_add("write", self.name_changed)
        self.record_button = self.button(recording, "开始一次录制", self.toggle_record, lock=True)
        self.record_button.grid(row=0, column=2)
        self.button(recording, "撤销最近一次", self.undo, lock=True).grid(row=0, column=3, padx=5)
        self.button(recording, "保存 / 追加", self.save_takes, lock=True).grid(row=0, column=4)
        self.button(recording, "清空未保存录制", self.discard_takes, lock=True).grid(row=0, column=5, padx=5)
        self.take_text = tk.StringVar(value="未保存：0 次")
        ttk.Label(recording, textvariable=self.take_text).grid(row=1, column=0, columnspan=5, sticky="w")

        data = ttk.LabelFrame(gesture, text="数据集（训练使用列表中全部有效 JSON）", padding=6)
        data.grid(row=2, column=0, sticky="ew", pady=5)
        data.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(data, columns=("name", "reps", "rate", "lengths"),
                                 show="headings", height=5)
        for key, title, width in [("name", "名称", 220), ("reps", "重复数", 70),
                                  ("rate", "采样率 Hz", 90), ("lengths", "各次样本数", 350)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width)
        self.tree.grid(row=0, column=0, sticky="ew")
        self.tree.bind("<<TreeviewSelect>>", self.preview_dataset)
        scroll = ttk.Scrollbar(data, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        actions = ttk.Frame(data)
        actions.grid(row=1, column=0, sticky="w", pady=4)
        self.button(actions, "刷新", self.refresh, lock=True).pack(side="left")
        self.button(actions, "导入 JSON", self.import_json, lock=True).pack(side="left", padx=5)
        self.button(actions, "导入 CSV（多选）", self.import_csv, lock=True).pack(side="left")
        ttk.Label(actions, text="CSV：无表头，六列逗号分隔；每文件一次重复").pack(side="left", padx=8)

        model = ttk.LabelFrame(gesture, text="训练与实时试识别", padding=6)
        model.grid(row=3, column=0, sticky="ew", pady=5)
        self.params = {}
        definitions = [("n_states", "状态数", "6"), ("cutoff_hz", "低通 Hz", "10"),
                       ("window_size", "窗口", "8"), ("window_overlap", "重叠", "4"),
                       ("sample_rate_hz", "采样率 Hz", "25"),
                       ("energy_threshold", "触发阈值", "1500"),
                       ("median_kernel", "中值点数(1关)", "5")]
        fields = ttk.Frame(model)
        fields.pack(fill="x")
        for col, (key, label, default) in enumerate(definitions):
            field = ttk.Frame(fields)
            field.grid(row=0, column=col, padx=4, sticky="w")
            ttk.Label(field, text=label).pack(anchor="w")
            var = tk.StringVar(value=default)
            entry = ttk.Entry(field, textvariable=var, width=11)
            entry.pack()
            self.params[key] = var
            self.locked.append(entry)
            var.trace_add("write", self.parameters_changed)
        self.params["segmentation_mode"] = tk.StringVar(value="motion")
        self.params["segmentation_mode"].trace_add("write", self.parameters_changed)
        presets = ttk.Frame(model)
        presets.pack(fill="x", pady=4)
        self.button(presets, "应用短促动作 / 响指预设", lambda: self.apply_preset("impulse"), lock=True).pack(side="left")
        self.button(presets, "应用连续动作预设", lambda: self.apply_preset("motion"), lock=True).pack(side="left", padx=5)
        self.mode_text = tk.StringVar(value="当前分段：连续动作（帧数按采样率换算）")
        ttk.Label(model, textvariable=self.mode_text, wraplength=880).pack(anchor="w")
        model_actions = ttk.Frame(model)
        model_actions.pack(fill="x", pady=5)
        self.button(model_actions, "训练全部数据", self.train, lock=True).pack(side="left")
        self.button(model_actions, "留出评估", self.evaluate, lock=True).pack(side="left", padx=5)
        self.button(model_actions, "加载模型包", self.load_bundle, lock=True).pack(side="left", padx=5)
        self.export_button = self.button(model_actions, "导出模型包", self.export_bundle)
        self.export_button.pack(side="left")
        self.export_button.configure(state="disabled")
        self.live = tk.BooleanVar(value=False)
        ttk.Checkbutton(model_actions, text="实时试识别", variable=self.live,
                        command=self.toggle_live).pack(side="left", padx=10)
        self.model_text = tk.StringVar(value="尚未训练或加载模型")
        ttk.Label(model, textvariable=self.model_text, wraplength=940).pack(anchor="w")
        self.result = tk.StringVar(value="识别结果：—")
        ttk.Label(model, textvariable=self.result).pack(anchor="w")
        self.evaluation_text = tk.StringVar(value="拒识标定训练至少 3 次；留出评估至少 4 次。单类只能评估正样本通过率，不能测误触率。")
        ttk.Label(model, textvariable=self.evaluation_text, wraplength=900).pack(anchor="w")

        logs = ttk.LabelFrame(body, text="日志", padding=4)
        logs.grid(row=2, column=0, sticky="nsew", pady=5)
        self.log_widget = tk.Text(logs, height=5, state="disabled", wrap="word")
        self.log_widget.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(logs, command=self.log_widget.yview)
        bar.pack(side="right", fill="y")
        self.log_widget.configure(yscrollcommand=bar.set)

    def _build_audio(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(4, weight=1)
        ttk.Label(parent, text="先开启接收，再将戒指切到录音模式：长按戒指按键录音，松开后自动推送文件。\n"
                  "SDK 不支持电脑开始/停止录音；待机监听不阻塞手势，仅传输时暂停 IMU，切回手势模式自动恢复。\n"
                  "原始 .bin 始终保留；安装 ffmpeg 后同时转为可播放 WAV。若漏收或中断，可从历史录音重新下载。",
                  wraplength=900).grid(row=0, column=0, sticky="w", pady=8)
        directory = ttk.Frame(parent)
        directory.grid(row=1, column=0, sticky="ew", pady=6)
        self.audio_dir_text = tk.StringVar(value=str(self.audio_directory))
        ttk.Label(directory, textvariable=self.audio_dir_text).pack(side="left", fill="x", expand=True)
        self.audio_dir_button = self.button(directory, "选择录音目录", self.choose_audio_directory)
        self.audio_dir_button.pack(side="right")
        controls = ttk.Frame(parent)
        controls.grid(row=2, column=0, sticky="ew", pady=6)
        self.audio_start_button = self.button(controls, "开启自动接收", self.start_audio)
        self.audio_start_button.pack(side="left")
        self.audio_stop_button = self.button(controls, "停止接收 / 下载", self.stop_audio)
        self.audio_stop_button.pack(side="left", padx=6)
        self.audio_stop_button.configure(state="disabled")
        self.audio_text = tk.StringVar(value="未开启接收")
        ttk.Label(controls, textvariable=self.audio_text, wraplength=630).pack(side="left", padx=10)
        remote = ttk.Frame(parent)
        remote.grid(row=3, column=0, sticky="ew", pady=6)
        self.audio_list_button = self.button(remote, "查询戒指历史录音", self.list_audio)
        self.audio_list_button.pack(side="left")
        self.remote_audio_choice = ttk.Combobox(remote, state="readonly", width=35)
        self.remote_audio_choice.pack(side="left", padx=6)
        self.audio_download_button = self.button(remote, "下载选中录音", self.download_audio)
        self.audio_download_button.pack(side="left")
        self.audio_tree = ttk.Treeview(parent, columns=("file", "duration", "size"), show="headings")
        for key, label, width in (("file", "本次会话保存的文件", 500), ("duration", "音频时长", 100), ("size", "大小", 100)):
            self.audio_tree.heading(key, text=label)
            self.audio_tree.column(key, width=width)
        self.audio_tree.grid(row=4, column=0, sticky="nsew", pady=8)
        bar = ttk.Scrollbar(parent, command=self.audio_tree.yview)
        bar.grid(row=4, column=1, sticky="ns")
        self.audio_tree.configure(yscrollcommand=bar.set)
        actions = ttk.Frame(parent)
        actions.grid(row=5, column=0, sticky="w")
        self.button(actions, "打开选中文件", self.open_audio_file).pack(side="left")
        self.button(actions, "打开录音目录", lambda: self.open_path(self.audio_directory)).pack(side="left", padx=6)

    def choose_audio_directory(self):
        if self.audio_state != "idle":
            self.error("接收、下载或保存期间不能更改目录。")
            return
        path = filedialog.askdirectory(parent=self.root, initialdir=str(self.audio_directory.parent))
        if path:
            self.audio_directory = Path(path)
            self.audio_dir_text.set(str(self.audio_directory))

    def start_audio(self):
        if not self.connected or self.audio_state != "idle":
            self.error("请先连接戒指，并等待上次接收或保存结束。")
            return
        if self.take is not None:
            self.error("请先停止本次手势录制，再开启录音接收。")
            return
        self.set_audio_state({"state": "starting", "message": "正在开启自动监听（不占用手势采集）…"})
        if not self.request(lambda: self.worker.start_audio(self.audio_directory)):
            self.set_audio_state({"state": "idle", "message": "开启接收失败"})

    def stop_audio(self):
        if self.audio_state not in {"idle", "saving", "stopping"}:
            if self.audio_state in {"receiving", "downloading"} and not messagebox.askyesno(
                    "中断音频传输？", "未接收完整的文件不会保存。之后可从戒指历史录音重新下载。", parent=self.root):
                return
            self.set_audio_state({"state": "stopping", "message": "正在停止电脑端接收（不控制戒指录音）…"})
            self.request(self.worker.stop_audio)

    def list_audio(self):
        if not self.connected or self.audio_state != "idle" or self.take is not None:
            self.error("请先连接戒指，停止手势录制和音频接收/下载后再查询。")
            return
        self.stop_live()
        self.set_audio_state({"state": "listing", "message": "正在查询历史录音…"})
        if not self.request(self.worker.list_audio):
            self.set_audio_state({"state": "idle", "message": "查询请求失败"})

    def download_audio(self):
        selected = self.remote_audio_choice.get()
        if not self.connected or self.audio_state != "idle" or self.take is not None or selected not in self.remote_audio_files:
            self.error("请先查询并选择历史录音，停止当前手势录制/音频接收后再下载。")
            return
        self.stop_live()
        self.set_audio_state({"state": "downloading", "message": "正在准备下载…"})
        if not self.request(lambda: self.worker.download_audio(self.remote_audio_files[selected], self.audio_directory)):
            self.set_audio_state({"state": "idle", "message": "下载请求失败"})

    def set_audio_state(self, payload):
        self.audio_state = payload["state"]
        self.audio_text.set(payload.get("message", self.audio_state))
        idle = self.audio_state == "idle"
        self.audio_start_button.configure(state="normal" if idle and self.connected else "disabled")
        self.audio_stop_button.configure(state="normal" if self.audio_state not in {"idle", "saving", "stopping"} else "disabled")
        self.audio_dir_button.configure(state="normal" if idle else "disabled")
        self.audio_list_button.configure(state="normal" if idle and self.connected else "disabled")
        self.audio_download_button.configure(state="normal" if idle and self.connected and self.remote_audio_files else "disabled")
        if self.audio_state in {"idle", "listening"} and self.imu_active and self.live.get() and self.recognizer is None:
            self.toggle_live()

    def open_path(self, path):
        try:
            path = Path(path).resolve(strict=True)
            if sys.platform == "win32":
                os.startfile(path)
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])
        except (OSError, ValueError) as exc:
            self.error(str(exc))

    def open_audio_file(self):
        selection = self.audio_tree.selection()
        if selection:
            self.open_path(self.audio_files[selection[0]])

    def button(self, parent, text, command, lock=False):
        button = ttk.Button(parent, text=text, command=command)
        if lock:
            self.locked.append(button)
        return button

    def log(self, text):
        self.log_widget.configure(state="normal")
        self.log_widget.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        if int(self.log_widget.index("end-1c").split(".")[0]) > 1500:
            self.log_widget.delete("1.0", "300.0")
        self.log_widget.see("end")
        self.log_widget.configure(state="disabled")

    def error(self, text):
        self.log(f"错误：{text}")
        messagebox.showerror("操作失败", str(text), parent=self.root)

    def request(self, action):
        if self.closing:
            return False
        try:
            action()
            return True
        except Exception as exc:
            self.error(str(exc))
            return False

    def invalidate(self, reason):
        self.revision += 1
        if self.evaluation_revision is not None:
            self.evaluation_text.set("数据或参数已改变；请重新进行留出评估。")
            self.evaluation_revision = None
        if self.bundle is not None and self.origin == "trained" and not self.stale:
            self.stale = True
            self.stop_live()
            self.export_button.configure(state="disabled")
            self.model_text.set(f"{reason}；旧模型已失效，请重新训练。")
            self.log(f"{reason}；旧模型不可导出或试识别。")

    def pipeline_settings(self, sample_rate=None):
        return PipelineConfig(
            sample_rate_hz=float(self.params["sample_rate_hz"].get()) if sample_rate is None else sample_rate,
            cutoff_hz=float(self.params["cutoff_hz"].get()),
            median_kernel=int(self.params["median_kernel"].get()),
            window_size=int(self.params["window_size"].get()),
            window_overlap=int(self.params["window_overlap"].get()),
            filter_initialization="steady",
        )

    def segmentation_settings(self, pipeline):
        return SegmentationConfig.for_sample_rate(
            pipeline.sample_rate_hz, mode=self.params["segmentation_mode"].get(),
            energy_threshold=float(self.params["energy_threshold"].get()), min_samples=pipeline.min_samples,
        )

    def apply_preset(self, mode):
        try:
            rates = {dataset.sample_rate_hz for _, dataset in self.records.values()}
            rate = (self.actual_rate or self.preview_rate or
                    (next(iter(rates)) if len(rates) == 1 else float(self.params["sample_rate_hz"].get())))
            # Validate before writing anything to the fields.
            PipelineConfig(sample_rate_hz=rate, cutoff_hz=min(40.0, 0.3 * rate))
            window = max(4, round(rate * (0.08 if mode == "impulse" else 0.32)))
            values = dict(sample_rate_hz=f"{rate:g}",
                          cutoff_hz=f"{min(40.0, rate * 0.3) if mode == 'impulse' else min(10.0, rate * 0.4):g}",
                          median_kernel="1" if mode == "impulse" else "5",
                          window_size=str(window), window_overlap=str(window // 2),
                          energy_threshold="8000" if mode == "impulse" else "1500",
                          segmentation_mode=mode)
            self._setting_preset = True
            try:
                for key, value in values.items():
                    self.params[key].set(value)
            finally:
                self._setting_preset = False
            self.parameters_changed()
            self.log("已应用预设；请重新训练。原始录制不变，短促模式只使用冲击前后窗口。")
        except (ValueError, TypeError) as exc:
            self.error(str(exc))

    def parameters_changed(self, *_):
        if getattr(self, "_setting_preset", False):
            return
        self.invalidate("训练参数已改变")
        try:
            pipeline = self.pipeline_settings()
            config = self.segmentation_settings(pipeline)
            if config.mode == "impulse":
                self.mode_text.set(f"短促动作：加速度相邻差触发，前 {config.pre_roll / pipeline.sample_rate_hz:.2f}s "
                                   f"+ 后 {config.post_roll / pipeline.sample_rate_hz:.2f}s；训练默认自动裁剪，可在波形页手动框选")
            else:
                self.mode_text.set(f"连续动作：结束静止 {config.min_offset_frames / pipeline.sample_rate_hz:.2f}s，"
                                   f"最长 {config.max_gesture_len / pipeline.sample_rate_hz:.1f}s（按采样率换算）")
        except (ValueError, TypeError):
            self.mode_text.set("参数尚未完整或无效；请检查采样率、低通、中值窗口和触发阈值。")
        self.show_preview()

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
        self.preview_title.set(title)
        choices = [f"第 {i + 1} 次 · {len(rep)} 样本" for i, rep in enumerate(repetitions)]
        self.preview_choice.configure(values=choices)
        if choices:
            self.preview_choice.current(len(choices) - 1)
        else:
            self.preview_choice.set("")
        self.show_preview()
        if reveal:
            self.tabs.select(self.preview_tab)

    def show_preview(self, _event=None):
        index = self.preview_choice.current()
        self.preview_plot.set_editable(False)
        if not self.preview_repetitions or not 0 <= index < len(self.preview_repetitions):
            self.preview_plot.clear()
            return
        try:
            # Use the recording's actual rate, not a potentially different training rate.
            pipeline = self.pipeline_settings(self.preview_rate)
            self.preview_plot.show_recording(self.preview_repetitions[index], pipeline,
                                             segmentation=self.segmentation_settings(pipeline),
                                             training_region=self.preview_training_regions[index])
            self.preview_plot.set_editable(self.preview_editable and not self.training and not self.closing)
        except (ValueError, TypeError) as exc:
            self.preview_plot.clear(f"无法预览，请检查滤波参数：{exc}")

    def preview_dataset(self, _event=None):
        selected = self.tree.selection()
        if selected and selected[0] in self.records:
            path, dataset = self.records[selected[0]]
            self.preview_recordings(dataset.repetitions, dataset.sample_rate_hz,
                                    f"已保存：{dataset.name}", path=path,
                                    training_regions=dataset.training_regions)

    def set_preview_training_region(self, region):
        """Commit an annotation, never destructively crop a recording or append it."""
        if self.training or self.closing or not self.preview_editable:
            return
        index = self.preview_choice.current()
        if not 0 <= index < len(self.preview_repetitions):
            return
        try:
            from .datasets import save_dataset_file, validate_training_region
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

    def update_takes(self):
        lengths = [len(rep) for rep in self.pending]
        active = f"；正在录制：{len(self.take)} 样本" if self.take is not None else ""
        self.take_text.set(f"未保存：{len(lengths)} 次，长度 {lengths}{active}")
        self.record_button.configure(text="停止并保留本次" if self.take is not None else "开始一次录制")
        self.name_entry.configure(state="disabled" if self.pending or self.take is not None or self.training or self.closing else "normal")

    def cancel_take(self):
        if self.take is not None:
            self.log("未完成的本次录制已取消。")
        self.take = None
        self.update_takes()

    def clear_takes(self):
        self.cancel_take()
        self.pending.clear()
        self.pending_regions.clear()
        self.pending_rate = None
        self.update_takes()
        if self.preview_is_pending:
            self.preview_recordings([], None, "暂无未保存录制")

    def discard_takes(self):
        if self.confirm_discard():
            self.clear_takes()

    def name_changed(self, *_):
        if self.pending or self.take is not None:
            self.log("手势名称改变，已丢弃未保存数据，避免混入新手势。")
            self.clear_takes()

    def confirm_discard(self):
        return not (self.pending or self.take is not None) or messagebox.askyesno(
            "丢弃未保存录制？", "继续将丢弃未保存的重复和正在录制的数据。", parent=self.root)

    def choose_directory(self):
        directory = filedialog.askdirectory(parent=self.root, initialdir=str(self.directory.parent))
        if not directory or Path(directory) == self.directory or not self.confirm_discard():
            return
        self.clear_takes()
        self.directory = Path(directory)
        self.dir_text.set(str(self.directory))
        self.refresh()

    def refresh(self):
        self.invalidate("数据目录或数据集已刷新")
        if self.preview_path is not None:
            self.preview_recordings([], None, "请重新选择已保存手势")
        self.records.clear()
        self.invalid_files = []
        self.tree.delete(*self.tree.get_children())
        try:
            paths = sorted(self.directory.glob("*.json"))
            for path in paths:
                try:
                    dataset = load_dataset(path)
                    item = self.tree.insert("", "end", values=(dataset.name, len(dataset.repetitions),
                        dataset.sample_rate_hz, str([len(rep) for rep in dataset.repetitions])))
                    self.records[item] = (path, dataset)
                except Exception as exc:
                    self.invalid_files.append(path.name)
                    self.log(f"无效数据 {path.name}：{exc}；修正或移出目录后才能训练。")
        except Exception as exc:
            self.error(str(exc))

    def refresh_device_choices(self):
        scanned = {d["address"].casefold(): d for d in self.scanned_devices}
        entries = []
        seen = set()
        for entry in self.device_history.entries:
            key = entry["address"].casefold()
            scan = scanned.get(key, {})
            name = scan.get("name") or entry["name"] or "未命名"
            entries.append((f"历史 · {name} | {entry['address']}", entry["address"]))
            seen.add(key)
        for entry in self.scanned_devices:
            if entry["address"].casefold() not in seen:
                entries.append((f"{entry.get('name') or '未命名'} | {entry['address']} | RSSI {entry.get('rssi', '—')}", entry["address"]))
                seen.add(entry["address"].casefold())
        self.devices = dict(entries)
        self.device_choice.configure(values=list(self.devices))
        address = self.address.get().strip()
        selected = next((label for label, value in entries if value.casefold() == address.casefold()), "")
        if not address and entries:
            selected, address = entries[0]
            self.address.set(address)
        self.device_choice.set(selected)

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
        self.address.set(address)
        self.refresh_device_choices()

    def clear_device_history(self):
        if not messagebox.askyesno("清空设备历史？", "仅删除本机保存的设备历史，不会断开当前设备。", parent=self.root):
            return
        try:
            self.device_history.clear()
            self.refresh_device_choices()
        except OSError as exc:
            self.error(str(exc))

    def select_device(self, _event):
        self.address.set(self.devices.get(self.device_choice.get(), ""))

    def connect(self):
        address = self.address.get().strip()
        if not address:
            self.error("请先选择设备或输入地址。")
            return
        if self.connected or self.connecting:
            self.error("请先断开当前设备 / 取消正在进行的连接。")
            return
        if not self.confirm_discard():
            return
        self.clear_takes()
        self.stop_live()
        self.connecting = True
        self.status.set("正在连接；可以点击断开取消")
        if not self.request(lambda: self.worker.connect(address)):
            self.connecting = False
            self.status.set("连接失败")

    def disconnect(self):
        if self.audio_state != "idle" and not messagebox.askyesno(
                "音频接收尚未结束", "断开将停止电脑接收/下载，不控制戒指录音。未完整接收的文件需之后重新下载。继续？", parent=self.root):
            return
        self.set_stream_state({"state": "waiting", "message": "IMU 已停止"})
        self.cancel_take()
        self.stop_live()
        self.connected = False
        self.connecting = False
        self.actual_rate = None
        self.status.set("正在断开")
        self.request(self.worker.disconnect)

    def toggle_record(self):
        # Process already-received batches at the boundary so idle samples do
        # not leak into a new take, and the last batch is retained on stop.
        self.drain_events()
        if self.take is not None:
            samples = self.take
            self.take = None
            if len(samples) < 12:
                self.log(f"本次仅 {len(samples)} 个样本，少于 12，已拒绝。")
                if samples:
                    self.preview_recordings([samples], self.take_rate, "本次过短（未保留）", pending=True, reveal=True)
            else:
                try:
                    dataset = GestureDataset(self.name.get().strip(), [np.asarray(samples)], self.take_rate)
                    self.pending.extend(dataset.repetitions)
                    self.pending_regions.append(None)
                    self.pending_rate = self.take_rate
                    self.preview_recordings(self.pending, self.pending_rate,
                                            f"未保存：{dataset.name}", pending=True, reveal=True,
                                            training_regions=self.pending_regions)
                except Exception as exc:
                    self.error(str(exc))
            self.update_takes()
            return
        if not self.connected or not self.imu_active or self.audio_state not in {"idle", "listening"} or not self.name.get().strip():
            self.error("请输入手势名称，等待 IMU 恢复上报；录音期间不能录制手势。")
            return
        if self.pending and not self.same_rate(self.pending_rate, self.actual_rate):
            self.error("未保存录制与当前设备采样率不一致，请先保存或撤销。")
            return
        self.take_rate = self.actual_rate
        self.take = []
        self.update_takes()

    def undo(self):
        if self.take is not None:
            self.error("请先停止本次录制。")
        elif self.pending:
            self.pending.pop()
            self.pending_regions.pop()
            self.update_takes()
            self.preview_recordings(self.pending, self.pending_rate, f"未保存：{self.name.get()}", pending=True,
                                    training_regions=self.pending_regions)

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
            if not messagebox.askyesno("追加重复？", f"“{dataset.name}”已有 {len(existing.repetitions)} 次。\n"
                                      f"追加本次 {len(dataset.repetitions)} 次？原数据将保留。", parent=self.root):
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
            dataset = GestureDataset(self.name.get().strip(), list(self.pending), self.pending_rate,
                                     list(self.pending_regions))
            if self.persist(dataset, minimum=2):
                path = dataset_path(dataset.name, self.directory)
                saved = load_dataset(path)
                self.preview_recordings(saved.repetitions, saved.sample_rate_hz, f"刚保存：{saved.name}",
                                        path=path, training_regions=saved.training_regions)
                self.clear_takes()
        except Exception as exc:
            self.error(str(exc))

    def import_json(self):
        paths = filedialog.askopenfilenames(parent=self.root, filetypes=[("数据 JSON", "*.json")])
        for path in paths:
            try:
                if Path(path).resolve().parent == self.directory.resolve():
                    self.log(f"{Path(path).name} 已在当前目录，仅刷新。")
                    self.refresh()
                else:
                    self.persist(load_dataset(path))
            except Exception as exc:
                self.error(f"{Path(path).name}：{exc}")

    def import_csv(self):
        name = self.name.get().strip()
        if not name:
            self.error("导入 CSV 前请输入手势名称，并设置训练参数中的采样率。")
            return
        paths = filedialog.askopenfilenames(parent=self.root, filetypes=[("六轴 CSV", "*.csv")])
        if not paths:
            return
        try:
            rate = float(self.params["sample_rate_hz"].get())
            repetitions = [np.loadtxt(path, delimiter=",", ndmin=2) for path in paths]
            self.persist(GestureDataset(name, repetitions, rate))
        except Exception as exc:
            self.error(str(exc))

    def train(self):
        if self.training:
            return
        if self.take is not None:
            self.error("请先停止录制并保存数据。")
            return
        if self.pending and not messagebox.askyesno("仍有未保存录制", "训练只使用已保存数据。继续？", parent=self.root):
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
            n_states = int(self.params["n_states"].get())
            if n_states < 1:
                raise ValueError("状态数必须为正整数。")
        except Exception as exc:
            self.error(str(exc))
            return
        self.stop_live()
        self.preview_plot.set_editable(False)
        # A failed new run must never leave a previous model looking current.
        self.bundle = None
        self.origin = None
        self.training = True
        for widget in self.locked:
            widget.configure(state="disabled")
        self.export_button.configure(state="disabled")
        revision = self.revision
        summary = "；".join(f"{d.name}: {len(d.repetitions)} 次/{sum(map(len, d.repetitions))} 样本" for d in datasets)
        self.model_text.set("正在后台训练，请查看日志…")
        self.log(f"训练开始：{summary}")

        def run():
            try:
                bundle = train_datasets(datasets, pipeline, n_states=n_states,
                    segmentation=segmentation, calibrate_rejection=True,
                    progress=lambda text: self.events.put(("progress", text)))
                self.events.put(("trained", (bundle, summary, revision)))
            except Exception as exc:
                self.events.put(("train_failed", str(exc)))
        self.executor.submit(run)

    def evaluate(self):
        if self.training:
            return
        if self.take is not None:
            self.error("请先停止本次录制；评估只使用已保存数据。")
            return
        if self.pending and not messagebox.askyesno("仍有未保存录制", "评估只使用已保存数据。继续？", parent=self.root):
            return
        try:
            from .evaluation import evaluate_leave_one_out
            # Do not refresh/invalidate the current model for a read-only evaluation.
            datasets = [load_dataset(path) for path in sorted(self.directory.glob("*.json"))]
            if not datasets:
                raise ValueError("当前目录没有评估数据。")
            pipeline = self.pipeline_settings()
            segmentation = self.segmentation_settings(pipeline)
            n_states = int(self.params["n_states"].get())
        except Exception as exc:
            self.error(str(exc))
            return
        self.stop_live()
        self.training = True
        for widget in self.locked:
            widget.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.preview_plot.set_editable(False)
        self.evaluation_text.set("正在后台逐次留出评估；不会替换当前模型…")
        revision = self.revision

        def run():
            try:
                result = evaluate_leave_one_out(datasets, pipeline, n_states,
                    segmentation=segmentation, calibrate_rejection=True,
                    progress=lambda text: self.events.put(("progress", text)))
                self.events.put(("evaluated", (result, revision)))
            except Exception as exc:
                self.events.put(("evaluation_failed", str(exc)))
        self.executor.submit(run)

    def finish_training(self):
        self.training = False
        self.show_preview()
        for widget in self.locked:
            widget.configure(state="normal")
        self.update_takes()
        self.export_button.configure(state="normal" if self.bundle is not None and not self.stale else "disabled")

    def set_bundle(self, bundle, origin, summary):
        self.stop_live()
        self.bundle = bundle
        self.origin = origin
        self.stale = False
        mode = "短促动作" if bundle.segmentation.mode == "impulse" else "连续动作"
        rejection = "带拒识标定（非概率）" if bundle.rejection else "无拒识标定；单类不能开启自动识别，请重新训练"
        self.model_text.set(f"{summary}；手势：{', '.join(bundle.gesture_names)}；{bundle.pipeline.sample_rate_hz:g} Hz；"
                            f"{mode}，中值 {bundle.pipeline.median_kernel} / 低通 {bundle.pipeline.cutoff_hz:g} Hz；{rejection}")
        self.export_button.configure(state="normal")
        self.log(self.model_text.get())

    def load_bundle(self):
        try:
            path = filedialog.askopenfilename(parent=self.root, filetypes=_BUNDLE_FILETYPES)
            if path:
                self.set_bundle(GestureBundle.load(path), "imported", f"已加载 {Path(path).name}（独立于当前训练数据）")
        except Exception as exc:
            self.error(str(exc))

    def export_bundle(self):
        if self.bundle is None or self.stale or self.training:
            return
        try:
            path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".json",
                                               initialfile="model.gesture.json", filetypes=_BUNDLE_FILETYPES)
            if path:
                self.bundle.save(path)
                self.log(f"模型包已导出：{path}")
        except Exception as exc:
            self.error(str(exc))

    def stop_live(self):
        self.live.set(False)
        if self.recognizer is not None:
            self.recognizer.reset()
        self.recognizer = None
        self.result.set("识别结果：—")

    def toggle_live(self):
        if not self.live.get():
            self.stop_live()
            return
        try:
            if self.training or self.bundle is None or self.stale:
                raise ValueError("请先训练有效模型或加载模型包。")
            if not self.connected or not self.imu_active or self.audio_state not in {"idle", "listening"}:
                raise ValueError("请先连接设备并等待 IMU 恢复上报；录音期间不能试识别。")
            if not self.same_rate(self.actual_rate, self.bundle.pipeline.sample_rate_hz):
                raise ValueError(f"采样率不匹配：设备 {self.actual_rate:g} Hz，模型 {self.bundle.pipeline.sample_rate_hz:g} Hz。")
            self.recognizer = GestureRecognizer(self.bundle)
            if len(self.bundle.gesture_names) == 1 and not self.recognizer.has_rejection:
                raise ValueError("这个单类旧模型没有拒识标定，普通运动也会被当成手势。请至少录制 3 次，选择合适预设后重新训练。")
            self.recognition_origin = self.plot.next_sample
            if not self.recognizer.has_rejection:
                self.log("警告：旧模型未配置未知动作拒识，建议重新训练。")
            self.log("实时试识别已开启；仅通过检查的片段画紫框。单类没有相对置信度，拒识标定仍需用负样本检验。")
        except Exception as exc:
            self.stop_live()
            self.error(str(exc))

    def samples(self, payload):
        if not self.connected or not self.imu_active or not payload:
            return
        self.plot.append_samples(payload)
        self.count += len(payload)
        self.raw.set(f"ax / ay / az / gx / gy / gz：{' / '.join(map(str, payload[-1]))}；样本数：{self.count}")
        if self.take is not None:
            self.take.extend([list(sample) for sample in payload])
            self.update_takes()
        if self.recognizer is not None:
            try:
                for start in range(0, len(payload), 32):
                    for prediction in self.recognizer.feed(payload[start:start + 32]):
                        assessment = ("单类匹配，通过拒识检查（无相对置信度）" if prediction.confidence is None else
                                      f"相对置信度 {prediction.confidence:.3f}（非概率）")
                        text = f"{prediction.name}；{assessment}；分数 {prediction.score:.3f}"
                        self.result.set(f"识别结果：{text}")
                        self.log(text)
                        if prediction.start_sample is not None and prediction.end_sample is not None:
                            self.plot.add_recognition(self.recognition_origin + prediction.start_sample,
                                                      self.recognition_origin + prediction.end_sample,
                                                      prediction.name if prediction.confidence is None else
                                                      f"{prediction.name} {prediction.confidence:.2f}")
            except Exception as exc:
                self.stop_live()
                self.log(f"试识别已停止：{exc}")

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

    def poll(self):
        if self.closing:
            return
        self.drain_events()
        self.plot.redraw()
        self.preview_plot.redraw()
        self.root.after(50, self.poll)

    def set_stream_state(self, payload):
        active = payload["state"] == "active"
        message = payload.get("message", "IMU 正常上报" if active else "IMU 暂停，等待手势模式")
        if active:
            rate = float(payload["sample_rate_hz"])
            if not self.imu_active or not self.same_rate(self.actual_rate, rate):
                self.plot.set_sample_rate(rate)
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
            if self.connected and self.live.get():
                if self.recognizer is not None:
                    self.recognizer.reset()
                self.recognizer = None
                self.result.set("识别已暂停；IMU 恢复后自动继续（请先静止）")
            else:
                self.stop_live()
            self.raw.set("ax / ay / az / gx / gy / gz：—（无新数据，未断言 BLE 断开）")
        self.imu_active = active
        self.stream_text.set(message)
        self.plot.set_status(message)
        if active and self.live.get() and self.recognizer is None and self.audio_state in {"idle", "listening"}:
            self.toggle_live()

    def handle(self, event, payload):
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
            self.remember_device(payload)
            self.set_stream_state({"state": "waiting", "message": "BLE 已连接，正在等待新的 IMU 数据…"})
            self.count = 0
            self.plot.clear()
            self.remote_audio_files = {}
            self.remote_audio_choice.configure(values=[])
            self.remote_audio_choice.set("")
            self.status.set(f"BLE 已连接 {payload['address']}；{payload.get('model', '')} "
                            f"{payload.get('firmware_version', '')}")
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text.get()})
            self.log(self.status.get())
        elif event == "stream_state":
            self.set_stream_state(payload)
        elif event == "reconnecting":
            self.connected, self.connecting = False, True
            self.set_stream_state({"state": "waiting", "message": "连接中断，正在自动重连"})
            self.status.set(payload["message"])
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text.get()})
            self.log(payload["message"])
        elif event == "disconnected":
            self.connected = self.connecting = False
            self.set_stream_state({"state": "waiting", "message": "IMU 未连接"})
            self.set_audio_state({"state": self.audio_state, "message": self.audio_text.get()})
            self.status.set("已断开；未完成的手势录制已取消")
        elif event == "audio_state":
            self.set_audio_state(payload)
            self.log(payload.get("message", payload["state"]))
        elif event == "audio_progress":
            total = payload.get("total")
            suffix = f" / {total / 1024:.1f} KiB" if total else ""
            self.audio_text.set(f"正在接收音频文件：{payload['bytes'] / 1024:.1f} KiB{suffix}（不是录音计时）")
        elif event == "audio_files":
            self.remote_audio_files = {f"录音索引 {entry['file_index']}": entry['file_index'] for entry in payload}
            self.remote_audio_choice.configure(values=list(self.remote_audio_files))
            self.remote_audio_choice.set(next(iter(self.remote_audio_files), ""))
            self.log(f"戒指中有 {len(payload)} 个录音文件；不会自动删除设备文件。")
        elif event == "audio_saved":
            path = Path(payload["path"])
            duration = payload.get("duration_s")
            item = self.audio_tree.insert("", "end", values=(path.name, f"{duration:.1f} s" if duration is not None else "未解码",
                                                           f"{path.stat().st_size / 1024:.1f} KiB"))
            self.audio_files[item] = path
            self.log(f"录音已保存：{path}；原始文件：{payload['raw_path']}")
        elif event in ("warning", "error", "progress"):
            self.log(f"{event}：{payload}")
        elif event == "trained":
            bundle, summary, revision = payload
            self.finish_training()
            self.set_bundle(bundle, "trained", f"训练成功：{summary}")
            if revision != self.revision:
                self.invalidate("训练期间数据或参数已改变")
        elif event == "evaluated":
            result, revision = payload
            self.finish_training()
            metric = "单类正样本通过率（不代表误触率）" if len(result["per_class"]) == 1 else "已知手势留出识别率"
            text = f"{metric}：{result['correct']}/{result['total']}（{result['accuracy']:.1%}）；未做连续流负样本评估"
            if revision != self.revision:
                text += "；数据/参数已改变，此为评估开始时的结果"
            self.evaluation_text.set(text)
            self.evaluation_revision = revision
            self.log(text)
            for name, counts in result["per_class"].items():
                self.log(f"{name}：{counts['correct']}/{counts['total']}；预测分布 {result['confusion'][name]}；"
                         f"跳过过短录制 {result['skipped_recordings'][name]} 次")
            self.log("留出评估不是跨会话/真机准确率；请另录独立数据验证。当前模型未被替换。")
        elif event == "evaluation_failed":
            self.finish_training()
            self.evaluation_text.set("留出评估失败；带拒识标定时每类需至少 4 次有效录制，当前模型保持不变。")
            self.error(payload)
        elif event == "train_failed":
            self.finish_training()
            self.model_text.set("训练失败；没有生成新模型。请查看日志并修正数据或参数。")
            self.error(payload)

    def close(self):
        if self.closing or not self.confirm_discard():
            return
        if self.training and not messagebox.askyesno("训练 / 评估尚未完成", "关闭窗口将放弃本次结果；后台计算可能稍后才结束。继续？", parent=self.root):
            return
        if self.audio_state != "idle" and not messagebox.askyesno(
                "音频接收尚未结束", "退出将停止电脑接收/下载，不控制戒指录音。完整收到的音频会继续保存；未完整文件需重新下载。继续？", parent=self.root):
            return
        self.closing = True
        self.cancel_take()
        self.stop_live()
        self.executor.shutdown(wait=False, cancel_futures=True)
        for widget in self.locked:
            widget.configure(state="disabled")
        self.status.set("正在关闭设备，最多等待 8 秒…")
        try:
            future = self.worker.close()
        except Exception as exc:
            self.error(f"设备清理请求失败：{exc}")
            self.root.destroy()
            return
        deadline = time.monotonic() + 8

        def wait_closed():
            if future.done():
                try:
                    future.result()
                except Exception as exc:
                    self.error(f"设备清理失败：{exc}")
                self.root.destroy()
            elif time.monotonic() >= deadline:
                self.error("设备清理超过 8 秒，窗口将关闭；蓝牙后台可能未正常退出。")
                self.root.destroy()
            else:
                self.root.after(100, wait_closed)
        wait_closed()


def main() -> None:
    global tk, ttk, filedialog, messagebox
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError as exc:
        raise SystemExit("无法启动桌面平台：此 Python 未安装 Tkinter。请安装带 Tk 支持的 Python。") from exc
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise SystemExit(f"无法初始化桌面显示：{exc}。请在有图形桌面的环境运行。") from exc
    try:
        Studio(root)
    except Exception as exc:
        messagebox.showerror("启动失败", str(exc), parent=root)
        root.destroy()
        raise SystemExit(f"桌面平台启动失败：{exc}") from exc
    root.mainloop()
