# SPDX-License-Identifier: MPL-2.0
"""SDK feature controls. Tk is imported only while building widgets.

The controller is main-thread only; background inspection returns immutable
firmware snapshots through Studio's event queue, never through a Tk callback.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path


_DEFAULTS = dict(channels="2", bitrate="16000", gain_left_q8="256",
                 gain_right_q8="256", duration_s="10")


def recording_options(values):
    """Validate editable values before enqueueing any hardware operation."""
    result = {}
    for key, label, low, high in (
        ("channels", "声道", 1, 2), ("bitrate", "码率", 8000, 32000),
        ("gain_left_q8", "左增益 Q8", 0, 1024),
        ("gain_right_q8", "右增益 Q8", 0, 1024),
    ):
        raw = values[key]
        if isinstance(raw, bool) or not str(raw).strip().isascii():
            raise ValueError(f"{label}必须是 {low}–{high} 的整数。")
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"{label}必须是 {low}–{high} 的整数。") from exc
        if not low <= value <= high:
            raise ValueError(f"{label}必须在 {low}–{high} 之间。")
        result[key] = value
    try:
        milliseconds = Decimal(str(values["duration_s"])) * 1000
        if (not milliseconds.is_finite() or not 20 <= milliseconds <= 60000
                or milliseconds % 20):
            raise ValueError
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("最长时间必须在 0.02–60 秒之间，且为 0.02 秒的整数倍。") from exc
    result["max_duration_ms"] = int(milliseconds)
    return result


class SDKControls:
    def __init__(self, studio):
        self.studio = studio
        self.capabilities = {}
        self.tool_state = "idle"
        self.awaiting_continue = False
        self.remote_session = False
        self.verified = None
        self.inspection_pending = False
        self.inspection_id = 0
        self.connection_epoch = 0
        self.buttons = {}
        self.fields = {}
        self.texts = {}
        self.entries = []
        self.firmware_entries = []
        self.text_views = {}

    @property
    def busy(self):
        return self.tool_state != "idle"

    @property
    def firmware_locked(self):
        return self.tool_state == "firmware"

    def available(self, action):
        app = self.studio
        if app.closing:
            return False
        ready = app.connected and not self.busy and app.take is None
        idle = ready and app.audio_state == "idle"
        if action == "inspect":
            return not self.busy and not self.inspection_pending
        if action == "finish":
            return app.connected and not self.busy and app.audio_state in {"recording", "live"}
        if action == "continue":
            return app.connected and self.tool_state == "calibrating" and self.awaiting_continue
        if action == "cancel":
            return app.connected and self.tool_state in {"calibrating", "diagnostics"}
        if action in {"read", "record", "audio"}:
            return idle and self.capabilities.get("recording_control", False)
        if action == "live_audio":
            return (idle and self.capabilities.get("recording_control", False)
                    and self.capabilities.get("live_audio", False))
        if action in {"accel", "quick", "full", "profile", "reset", "diagnostics"}:
            return idle and self.capabilities.get("imu", False)
        if action == "install":
            return idle and self.capabilities.get("ota", False) and self.verified is not None and not self.inspection_pending
        return False

    def _text(self, key, value):
        if key in self.texts:
            self.texts[key].set(str(value))
        if key in self.text_views:
            view = self.text_views[key]
            view.configure(state="normal")
            view.delete("1.0", "end")
            view.insert("1.0", str(value))
            view.configure(state="disabled")

    def _button(self, parent, key, title, action):
        button = self.studio.button(parent, title, action)
        button.pack(side="left", padx=(0, 5), pady=4)
        self.buttons[key] = button
        return button

    def _text_view(self, parent, key, initial, height):
        import tkinter as tk
        from tkinter import ttk
        frame = ttk.Frame(parent)
        frame.pack(fill="both", expand=True, pady=6)
        view = tk.Text(frame, width=42, height=height, wrap="word", state="disabled")
        view.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, command=view.yview)
        scroll.pack(side="right", fill="y")
        view.configure(yscrollcommand=scroll.set)
        self.text_views[key] = view
        self._text(key, initial)

    def build_audio(self, parent):
        import tkinter as tk
        from tkinter import ttk
        form = ttk.LabelFrame(parent, text="远程录音 / 实时 PCM", padding=6)
        form.pack(fill="x")
        fields = ttk.Frame(form)
        fields.pack(fill="x")
        labels = dict(channels="声道（1 / 2）", bitrate="码率（bits/s）",
                      gain_left_q8="左增益 Q8", gain_right_q8="右增益 Q8", duration_s="最长（秒）")
        for key, default in _DEFAULTS.items():
            cell = ttk.Frame(fields)
            cell.pack(side="left", padx=(0, 12))
            ttk.Label(cell, text=labels[key]).pack(anchor="w")
            var = tk.StringVar(value=default)
            self.fields[key] = var
            entry = ttk.Entry(cell, textvariable=var, width=12)
            entry.pack()
            self.entries.append(entry)
        actions = ttk.Frame(form)
        actions.pack(fill="x")
        self._button(actions, "read", "读取设置 / 状态", self.read_config)
        self._button(actions, "record", "开始远程录音", self.start_recording)
        self._button(actions, "finish", "停止并保存", self.finish_recording)
        self._button(actions, "live_audio", "实时 PCM 接收并保存", lambda: self.start_recording(live=True))
        self.texts["recording"] = tk.StringVar(value="连接后按设备能力启用；增益 256 = 1 倍。")
        ttk.Label(form, textvariable=self.texts["recording"], wraplength=900).pack(anchor="w")
        ttk.Label(form, text="远程保存到戒指后下载；实时接收同时保留设备副本，完整 END 后生成 WAV，不播放扬声器。\n"
                  "音频期间暂停 IMU，结束后恢复；最长时间以 0.02 秒递增。", wraplength=900).pack(anchor="w", pady=4)

    def build_tools(self, parent):
        import tkinter as tk
        from tkinter import ttk
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        self.texts["capabilities"] = tk.StringVar(value="未连接；连接后查询设备能力。")
        ttk.Label(parent, textvariable=self.texts["capabilities"], wraplength=900).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=6)
        calibration = ttk.LabelFrame(parent, text="本机 IMU 校准", padding=10)
        calibration.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        firmware = ttk.LabelFrame(parent, text="签名固件 OTA", padding=10)
        firmware.grid(row=0, column=1, sticky="nsew")
        actions = ttk.Frame(calibration)
        actions.pack(fill="x")
        for mode, label in (("accel", "加速度六面"), ("quick", "陀螺仪静止"), ("full", "完整校准")):
            self._button(actions, mode, label, lambda mode=mode: self.start_calibration(mode))
        self.texts["calibration"] = tk.StringVar(value="等待开始；收到摆放提示后才能继续采集。")
        ttk.Label(calibration, textvariable=self.texts["calibration"], wraplength=430).pack(anchor="w", pady=10)
        actions = ttk.Frame(calibration)
        actions.pack(fill="x")
        self._button(actions, "continue", "继续采集", self.continue_calibration)
        self._button(actions, "cancel", "取消 / 停止诊断", self.stop_tool)
        actions = ttk.Frame(calibration)
        actions.pack(fill="x")
        self._button(actions, "profile", "查看配置", self.show_profile)
        self._button(actions, "reset", "重置配置", self.reset_profile)
        self._button(actions, "diagnostics", "校准诊断", self.start_diagnostics)
        self._text_view(calibration, "profile", "尚未读取本机配置", 6)
        self.texts["sample"] = tk.StringVar(value="校准诊断：—")
        ttk.Label(calibration, textvariable=self.texts["sample"], wraplength=430).pack(anchor="w", pady=8)
        ttk.Label(calibration, text="校准独占 IMU，停止手势识别；验证通过才替换本机配置。\n"
                  "诊断仅显示 g / °/s，不进入原始录制或训练流水线。", wraplength=430).pack(anchor="w", pady=8)
        self.zip_path = tk.StringVar()
        self.key_path = tk.StringVar()
        for variable, label, key in ((self.zip_path, "固件 ZIP", "zip"), (self.key_path, "可信公钥 PEM", "key")):
            ttk.Label(firmware, text=label).pack(anchor="w")
            row = ttk.Frame(firmware)
            row.pack(fill="x")
            entry = ttk.Entry(row, textvariable=variable, width=30)
            entry.pack(side="left", fill="x", expand=True)
            button = self.studio.button(row, "选择", lambda key=key: self.choose_firmware_file(key))
            button.pack(side="left")
            self.firmware_entries.extend((entry, button))
            variable.trace_add("write", self.invalidate_inspection)
        actions = ttk.Frame(firmware)
        actions.pack(fill="x")
        self._button(actions, "inspect", "离线验签并查看", self.inspect_firmware)
        self._text_view(firmware, "firmware", "选择独立可信公钥；拒绝测试固件。", 10)
        actions = ttk.Frame(firmware)
        actions.pack(fill="x")
        self._button(actions, "install", "确认设备并升级", self.install_firmware)
        self.progress = ttk.Progressbar(firmware, maximum=100)
        self.progress.pack(fill="x", pady=8)
        self.texts["firmware_status"] = tk.StringVar(value="尚未升级")
        ttk.Label(firmware, textvariable=self.texts["firmware_status"], wraplength=430).pack(anchor="w")
        ttk.Label(firmware, text="升级独占连接，期间不能断开或普通退出。\n"
                  "发送 100% 不代表成功：等待重启、重连与版本核验。", wraplength=430).pack(anchor="w", pady=12)
        self.refresh()

    def refresh(self):
        app = self.studio
        for key, button in self.buttons.items():
            button.configure(state="normal" if self.available(key) else "disabled")
        for entry in self.entries:
            entry.configure(state="normal" if self.available("record") else "disabled")
        for entry in self.firmware_entries:
            entry.configure(state="disabled" if self.busy or self.inspection_pending or app.closing else "normal")
        # These controls already exist on Studio; keep one final gating pass so
        # queued audio/stream events cannot re-enable controls during an OTA.
        enabled = self.available("audio")
        for name, allowed in (
            ("audio_start_button", enabled), ("audio_list_button", enabled),
            ("audio_download_button", enabled and bool(app.remote_audio_files)),
            ("audio_dir_button", not self.busy and app.audio_state == "idle"),
            ("audio_stop_button", not self.busy and not self.remote_session and
             app.audio_state not in {"idle", "recording", "live", "saving", "stopping"}),
            ("record_button", not self.busy and not app.training and (app.take is not None or
             app.connected and app.imu_active and self.capabilities.get("imu", False)
             and app.audio_state in {"idle", "listening"})),
            ("live_button", not self.busy and not app.training and app.connected and app.imu_active
             and self.capabilities.get("imu", False) and app.audio_state in {"idle", "listening"}),
            ("retry_imu_button", not self.busy and app.connected and app.audio_state == "idle" and
             self.capabilities.get("imu", False)),
            ("scan_button", not self.busy),
            ("connect_button", not self.busy and not app.connected and not app.connecting),
            ("disconnect_button", not self.firmware_locked and (app.connected or app.connecting)),
        ):
            widget = getattr(app, name, None)
            if widget is not None:
                widget.configure(state="normal" if allowed and not app.closing else "disabled")

    def _require(self, action):
        if self.available(action):
            return True
        self.studio.error("请连接支持此功能的设备，并先停止手势录制、音频或其他工具。")
        return False

    def read_config(self):
        if self._require("read"):
            app = self.studio
            app.stop_live()
            app.set_audio_state({"state": "listing", "message": "正在读取录音设置 / 状态…"})
            if not app.request(app.worker.read_recording_config):
                app.set_audio_state({"state": "idle", "message": "读取设置请求失败"})

    def start_recording(self, *, live=False):
        if not self._require("live_audio" if live else "record"):
            return
        try:
            options = recording_options({key: var.get() for key, var in self.fields.items()})
        except (ValueError, KeyError) as exc:
            self.studio.error(str(exc))
            return
        app = self.studio
        app.stop_live()
        self.remote_session = True
        app.set_audio_state({"state": "starting", "message": "正在启动实时接收…" if live else "正在启动远程录音…"})
        if not app.request(lambda: app.worker.start_remote_recording(options, app.audio_directory, live=live)):
            self.remote_session = False
            app.set_audio_state({"state": "idle", "message": "启动录音失败"})

    def finish_recording(self):
        if not self._require("finish"):
            return
        app = self.studio
        previous = app.audio_state
        app.set_audio_state({"state": "stopping", "message": "正在停止并等待完整音频保存…"})
        if not app.request(app.worker.finish_remote_recording):
            app.set_audio_state({"state": previous, "message": "停止请求失败，可重试"})

    def _start_tool(self, action, state, operation):
        if not self._require(action):
            return
        self.studio.stop_live()
        self.tool_state = state
        self.awaiting_continue = False
        self.refresh()
        if not self.studio.request(operation):
            self.tool_state = "idle"
            self.refresh()

    def start_calibration(self, mode):
        if mode not in {"accel", "quick", "full"}:
            raise ValueError("未知校准模式")
        self._text("calibration", "正在准备校准，请等待摆放提示…")
        self._start_tool(mode, "calibrating", lambda: self.studio.worker.start_calibration(mode))

    def continue_calibration(self):
        if self._require("continue"):
            self.awaiting_continue = False
            self.refresh()
            self.studio.request(self.studio.worker.continue_calibration)

    def stop_tool(self):
        if self._require("cancel"):
            self.awaiting_continue = False
            self.refresh()
            self.studio.request(self.studio.worker.stop_tool)

    def start_diagnostics(self):
        self._start_tool("diagnostics", "diagnostics", self.studio.worker.start_diagnostics)

    def show_profile(self):
        self._start_tool("profile", "calibrating", self.studio.worker.show_calibration)

    def reset_profile(self):
        from tkinter import messagebox
        if self._require("reset") and messagebox.askyesno(
                "重置本机校准？", "仅删除本机校准配置，不修改手势数据或模型。", parent=self.studio.root, default="no"):
            self._start_tool("reset", "calibrating", self.studio.worker.reset_calibration)

    def choose_firmware_file(self, kind):
        from tkinter import filedialog
        if self.busy or self.inspection_pending:
            return
        variable = self.zip_path if kind == "zip" else self.key_path
        path = filedialog.askopenfilename(parent=self.studio.root,
            filetypes=[("固件 ZIP", "*.zip")] if kind == "zip" else [("公钥 PEM", "*.pem"), ("所有文件", "*")])
        if path and not self.busy and not self.inspection_pending:
            variable.set(path)

    def invalidate_inspection(self, *_):
        self.inspection_id += 1
        self.verified = None
        self._text("firmware", "文件选择已改变，请重新离线验签。")
        self.refresh()

    def inspect_firmware(self):
        if not self._require("inspect"):
            return
        zip_path, key_path = self.zip_path.get().strip(), self.key_path.get().strip()
        if not zip_path or not key_path:
            self.studio.error("请选择固件 ZIP 和独立可信的公钥 PEM。")
            return
        self.inspection_id += 1
        token = self.inspection_id
        self.verified = None
        self.inspection_pending = True
        self._text("firmware", "正在后台离线验签；不会连接或操作设备…")
        self.refresh()
        events = self.studio.events

        def run():
            try:
                from .firmware import inspect_bundle
                verified = inspect_bundle(Path(zip_path), Path(key_path))
            except Exception as exc:
                events.put(("firmware_inspection_failed", {"token": token, "error": str(exc)}))
            else:
                events.put(("firmware_inspected", {"token": token, "verified": verified}))
        try:
            self.studio.executor.submit(run)
        except Exception as exc:
            self.inspection_pending = False
            self._text("firmware", f"无法启动验签：{exc}")
            self.refresh()
            self.studio.error(str(exc))

    def install_firmware(self):
        from tkinter import messagebox
        if not self._require("install"):
            return
        app = self.studio
        verified, epoch = self.verified, self.connection_epoch
        if not messagebox.askyesno("确认设备并升级固件？",
                f"当前设备：{app.status.get()}\n\n{verified.summary}\n\n"
                "将写入已验签的固件快照。升级期间不能断开或退出，发送完成后还需核验版本。继续？",
                parent=app.root, default="no", icon="warning"):
            return
        # Native dialogs pump Tk's queue; connection, selection and exclusivity
        # may have changed while the user was reading the summary.
        if verified is not self.verified or epoch != self.connection_epoch or not self._require("install"):
            return
        app.stop_live()
        self.tool_state = "firmware"  # lock before enqueue, not on a later event
        self.awaiting_continue = False
        self._text("firmware_status", "升级已提交；等待设备检查与发送…")
        if hasattr(self, "progress"):
            self.progress.configure(value=0)
        self.refresh()
        if not app.request(lambda: app.worker.install_firmware(verified)):
            self.tool_state = "idle"
            self._text("firmware_status", "升级提交失败；未开始升级")
            self.refresh()

    def handle(self, event, payload):
        """Called exclusively by Studio.handle / Studio.poll on the Tk thread."""
        if event == "capabilities":
            self.capabilities = {key: payload.get(key) is True for key in
                                 ("recording_control", "live_audio", "imu", "ota")}
            descriptions = [f"{label}：{'支持' if self.capabilities[key] else '不支持'}" for key, label in
                            (("recording_control", "录音控制"), ("live_audio", "实时 PCM"),
                             ("imu", "IMU"), ("ota", "签名 OTA"))]
            self._text("capabilities", " · ".join(descriptions))
            self._text("recording", " · ".join(descriptions[:2]))
        elif event in {"connected", "disconnected", "reconnecting"}:
            self.connection_epoch += 1
            if event != "connected":
                self.capabilities = {}
                self.awaiting_continue = False
                self._text("capabilities", "未连接；重新连接后重新查询设备能力。")
            self.refresh()
            return False
        elif event == "audio_state":
            if payload["state"] == "idle":
                self.remote_session = False
            elif payload["state"] in {"recording", "live"}:
                self.remote_session = True
            return False
        elif event == "tool_state":
            self.tool_state = payload["state"]
            self.awaiting_continue = False
            self._text("firmware_status" if self.firmware_locked else "calibration", payload.get("message", self.tool_state))
        elif event == "calibration_prompt":
            if self.tool_state == "calibrating":
                self.awaiting_continue = True
                self._text("calibration", payload)
        elif event == "calibration_progress":
            self._text("calibration", payload)
        elif event == "calibration_profile":
            self._text("profile", payload)
        elif event == "calibration_sample":
            self._text("sample", f"加速度 g：{payload['accel_g']}\n陀螺仪 °/s：{payload['gyro_dps']}\n"
                       f"已应用校准：{payload['applied_components']}")
        elif event == "recording_config":
            for key in _DEFAULTS:
                if key in self.fields:
                    self.fields[key].set(str(Decimal(payload["max_duration_ms"]) / 1000) if key == "duration_s" else str(payload[key]))
        elif event == "recording_status":
            self._text("recording", payload)
        elif event == "firmware_progress":
            sent, total = payload["sent"], payload["total"]
            percent = min(100, max(0, sent / total * 100)) if total else 0
            if hasattr(self, "progress"):
                self.progress.configure(value=percent)
            self._text("firmware_status", f"已发送 {sent} / {total} 字节；仍需等待重启与版本核验。")
        elif event == "firmware_complete":
            self._text("firmware_status", payload)
            self.studio.log(payload)
        elif event in {"firmware_inspected", "firmware_inspection_failed"}:
            self.inspection_pending = False
            if payload["token"] == self.inspection_id:
                if event == "firmware_inspected":
                    self.verified = payload["verified"]
                    self._text("firmware", self.verified.summary)
                else:
                    self.verified = None
                    self._text("firmware", f"验签失败：{payload['error']}")
                    self.studio.error(payload["error"])
        elif event == "error":
            # A generic error may be unrelated. Only worker tool_state=idle
            # releases an accepted OTA; otherwise close could interrupt it.
            if self.busy:
                self._text("firmware_status" if self.firmware_locked else "calibration", payload)
            return False
        else:
            return False
        self.refresh()
        return True
