# SPDX-License-Identifier: MPL-2.0
"""Tk desktop trainer; importing this module never initializes Tk or Bluetooth."""
from __future__ import annotations

import queue
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np


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

        self.root = root
        self.events = queue.Queue()
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.worker = RingWorker(lambda event, payload: self.events.put((event, payload)))
        self.directory = Path.cwd() / "gestures"
        self.records = {}
        self.invalid_files = []
        self.devices = {}
        self.pending = []
        self.take = None
        self.take_rate = None
        self.pending_rate = None
        self.connected = False
        self.connecting = False
        self.actual_rate = None
        self.count = 0
        self.training = False
        self.closing = False
        self.bundle = None
        self.recognizer = None
        self.origin = None
        self.stale = False
        self.revision = 0
        self.locked = []
        self.root.title("手势训练工作室")
        self.root.geometry("1020x850")
        self.root.minsize(980, 800)
        self._build()
        self.worker.start()
        self.refresh()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(50, self.poll)
        self.log("手势模式：每次只录制一个完整动作；每个手势至少两次有效重复。")
        self.log("原始六轴顺序：ax, ay, az, gx, gy, gz（设备原始整数，不转换单位）。")
        self.log("置信度是模型比较指标，不是概率；训练、录制和识别必须使用相同采样率。")

    def _build(self):
        body = ttk.Frame(self.root, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(5, weight=1)
        work = ttk.Frame(body)
        work.grid(row=0, column=0, sticky="ew")
        self.dir_text = tk.StringVar(value=str(self.directory))
        ttk.Label(work, textvariable=self.dir_text).pack(side="left", fill="x", expand=True)
        self.button(work, "选择数据目录", self.choose_directory, lock=True).pack(side="right")

        device = ttk.LabelFrame(body, text="1. 设备连接", padding=6)
        device.grid(row=1, column=0, sticky="ew", pady=5)
        self.button(device, "扫描", lambda: self.request(self.worker.scan)).grid(row=0, column=0)
        self.device_choice = ttk.Combobox(device, state="readonly", width=32)
        self.device_choice.grid(row=0, column=1, padx=5)
        self.device_choice.bind("<<ComboboxSelected>>", self.select_device)
        self.address = tk.StringVar()
        ttk.Entry(device, textvariable=self.address, width=30).grid(row=0, column=2)
        self.connect_button = self.button(device, "连接", self.connect)
        self.connect_button.grid(row=0, column=3, padx=5)
        self.button(device, "断开 / 取消连接", self.disconnect).grid(row=0, column=4)
        self.status = tk.StringVar(value="未连接；可扫描或直接输入地址")
        ttk.Label(device, textvariable=self.status).grid(row=1, column=0, columnspan=5, sticky="w")
        self.raw = tk.StringVar(value="ax / ay / az / gx / gy / gz：—；样本数：0")
        ttk.Label(device, textvariable=self.raw).grid(row=2, column=0, columnspan=5, sticky="w")

        recording = ttk.LabelFrame(body, text="2. 录制数据（每次至少 12 个样本）", padding=6)
        recording.grid(row=2, column=0, sticky="ew", pady=5)
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

        data = ttk.LabelFrame(body, text="3. 数据集（训练使用列表中全部有效 JSON）", padding=6)
        data.grid(row=3, column=0, sticky="ew", pady=5)
        data.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(data, columns=("name", "reps", "rate", "lengths"),
                                 show="headings", height=5)
        for key, title, width in [("name", "名称", 220), ("reps", "重复数", 70),
                                  ("rate", "采样率 Hz", 90), ("lengths", "各次样本数", 350)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width)
        self.tree.grid(row=0, column=0, sticky="ew")
        scroll = ttk.Scrollbar(data, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        actions = ttk.Frame(data)
        actions.grid(row=1, column=0, sticky="w", pady=4)
        self.button(actions, "刷新", self.refresh, lock=True).pack(side="left")
        self.button(actions, "导入 JSON", self.import_json, lock=True).pack(side="left", padx=5)
        self.button(actions, "导入 CSV（多选）", self.import_csv, lock=True).pack(side="left")
        ttk.Label(actions, text="CSV：无表头，六列逗号分隔；每文件一次重复").pack(side="left", padx=8)

        model = ttk.LabelFrame(body, text="4. 训练与实时试识别", padding=6)
        model.grid(row=4, column=0, sticky="ew", pady=5)
        self.params = {}
        definitions = [("n_states", "状态数", "6"), ("cutoff_hz", "低通 Hz", "10"),
                       ("window_size", "窗口", "8"), ("window_overlap", "重叠", "4"),
                       ("sample_rate_hz", "采样率 Hz", "25"),
                       ("energy_threshold", "能量阈值", "1500")]
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
            var.trace_add("write", lambda *_: self.invalidate("训练参数已改变"))
        model_actions = ttk.Frame(model)
        model_actions.pack(fill="x", pady=5)
        self.button(model_actions, "训练全部数据", self.train, lock=True).pack(side="left")
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

        logs = ttk.LabelFrame(body, text="日志", padding=4)
        logs.grid(row=5, column=0, sticky="nsew", pady=5)
        self.log_widget = tk.Text(logs, height=8, state="disabled", wrap="word")
        self.log_widget.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(logs, command=self.log_widget.yview)
        bar.pack(side="right", fill="y")
        self.log_widget.configure(yscrollcommand=bar.set)

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
        if self.bundle is not None and self.origin == "trained" and not self.stale:
            self.stale = True
            self.stop_live()
            self.export_button.configure(state="disabled")
            self.model_text.set(f"{reason}；旧模型已失效，请重新训练。")
            self.log(f"{reason}；旧模型不可导出或试识别。")

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
        self.pending_rate = None
        self.update_takes()

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
            else:
                try:
                    dataset = GestureDataset(self.name.get().strip(), [np.asarray(samples)], self.take_rate)
                    self.pending.extend(dataset.repetitions)
                    self.pending_rate = self.take_rate
                except Exception as exc:
                    self.error(str(exc))
            self.update_takes()
            return
        if not self.connected or not self.name.get().strip():
            self.error("请连接设备并输入手势名称。")
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
            self.update_takes()

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
            dataset = GestureDataset(dataset.name, existing.repetitions + dataset.repetitions,
                                     dataset.sample_rate_hz)
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
            dataset = GestureDataset(self.name.get().strip(), list(self.pending), self.pending_rate)
            if self.persist(dataset, minimum=2):
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
            params = {key: var.get() for key, var in self.params.items()}
            pipeline = PipelineConfig(sample_rate_hz=float(params["sample_rate_hz"]),
                cutoff_hz=float(params["cutoff_hz"]), window_size=int(params["window_size"]),
                window_overlap=int(params["window_overlap"]))
            segmentation = SegmentationConfig(energy_threshold=float(params["energy_threshold"]))
            n_states = int(params["n_states"])
            if n_states < 1:
                raise ValueError("状态数必须为正整数。")
        except Exception as exc:
            self.error(str(exc))
            return
        self.stop_live()
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
                    segmentation=segmentation, progress=lambda text: self.events.put(("progress", text)))
                self.events.put(("trained", (bundle, summary, revision)))
            except Exception as exc:
                self.events.put(("train_failed", str(exc)))
        self.executor.submit(run)

    def finish_training(self):
        self.training = False
        for widget in self.locked:
            widget.configure(state="normal")
        self.update_takes()
        self.export_button.configure(state="normal" if self.bundle is not None and not self.stale else "disabled")

    def set_bundle(self, bundle, origin, summary):
        self.stop_live()
        self.bundle = bundle
        self.origin = origin
        self.stale = False
        self.model_text.set(f"{summary}；手势：{', '.join(bundle.gesture_names)}；{bundle.pipeline.sample_rate_hz:g} Hz")
        self.export_button.configure(state="normal")
        self.log(self.model_text.get())

    def load_bundle(self):
        path = filedialog.askopenfilename(parent=self.root, filetypes=[("手势模型包", "*.gesture.json"), ("JSON", "*.json")])
        if path:
            try:
                self.set_bundle(GestureBundle.load(path), "imported", f"已加载 {Path(path).name}（独立于当前训练数据）")
            except Exception as exc:
                self.error(str(exc))

    def export_bundle(self):
        if self.bundle is None or self.stale or self.training:
            return
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".gesture.json",
                                           filetypes=[("手势模型包", "*.gesture.json")])
        if path:
            try:
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
            if not self.connected:
                raise ValueError("请先连接设备。")
            if not self.same_rate(self.actual_rate, self.bundle.pipeline.sample_rate_hz):
                raise ValueError(f"采样率不匹配：设备 {self.actual_rate:g} Hz，模型 {self.bundle.pipeline.sample_rate_hz:g} Hz。")
            self.recognizer = GestureRecognizer(self.bundle)
            self.log("实时试识别已开启；置信度非概率。")
        except Exception as exc:
            self.stop_live()
            self.error(str(exc))

    def samples(self, payload):
        if not self.connected or not payload:
            return
        self.count += len(payload)
        self.raw.set(f"ax / ay / az / gx / gy / gz：{' / '.join(map(str, payload[-1]))}；样本数：{self.count}")
        if self.take is not None:
            self.take.extend([list(sample) for sample in payload])
            self.update_takes()
        if self.recognizer is not None:
            try:
                for start in range(0, len(payload), 32):
                    for prediction in self.recognizer.feed(payload[start:start + 32]):
                        text = f"{prediction.name}；置信度 {prediction.confidence:.3f}（非概率）；分数 {prediction.score:.3f}"
                        self.result.set(f"识别结果：{text}")
                        self.log(text)
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
        self.root.after(50, self.poll)

    def handle(self, event, payload):
        if event == "samples":
            self.samples(payload)
        elif event == "devices":
            self.devices = {f"{d.get('name') or '未命名'} | {d['address']} | RSSI {d.get('rssi', '—')}": d["address"] for d in payload}
            self.device_choice.configure(values=list(self.devices))
            self.log(f"扫描发现 {len(self.devices)} 个设备。")
        elif event == "connected":
            if not self.connecting:
                self.request(self.worker.disconnect)
                return
            self.connected, self.connecting = True, False
            self.actual_rate = float(payload["sample_rate_hz"])
            self.count = 0
            self.status.set(f"已连接 {payload['address']}；实际 {self.actual_rate:g} Hz；"
                            f"加速度量程 ±{payload['accel_range_g']} g；陀螺仪量程 ±{payload['gyro_range_dps']} °/s")
            self.log(self.status.get())
        elif event == "disconnected":
            self.connected = self.connecting = False
            self.actual_rate = None
            self.cancel_take()
            self.stop_live()
            self.status.set("已断开；未完成录制已取消")
        elif event in ("warning", "error", "progress"):
            self.log(f"{event}：{payload}")
        elif event == "trained":
            bundle, summary, revision = payload
            self.finish_training()
            self.set_bundle(bundle, "trained", f"训练成功：{summary}")
            if revision != self.revision:
                self.invalidate("训练期间数据或参数已改变")
        elif event == "train_failed":
            self.finish_training()
            self.model_text.set("训练失败；没有生成新模型。请查看日志并修正数据或参数。")
            self.error(payload)

    def close(self):
        if self.closing or not self.confirm_discard():
            return
        if self.training and not messagebox.askyesno("训练尚未完成", "关闭窗口将放弃本次训练结果；后台计算可能稍后才结束。继续？", parent=self.root):
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
