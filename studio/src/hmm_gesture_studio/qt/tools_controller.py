# SPDX-License-Identifier: MPL-2.0
"""Device-tool state: capabilities, ring recording, calibration and signed OTA.

Main-thread only. Background inspection returns immutable firmware snapshots via
the event queue. Any modal dialog runs a nested event loop, so every confirmation
is followed by a recheck before anything is sent to hardware.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from ..sdk_controls import recording_options
from .i18n import tr

DEFAULT_FIELDS = dict(channels="2", bitrate="16000", gain_left_q8="256",
                      gain_right_q8="256", duration_s="10")
_CAPABILITIES = (("recording_control", tr("录音控制")), ("live_audio", tr("实时 PCM")),
                 ("audio_files", tr("音频文件")), ("imu", "IMU"), ("ota", tr("签名 OTA")))


class ToolsController:
    def __init__(self, app):
        self.app = app
        self.capabilities = {}
        self.tool_state = "idle"
        self.awaiting_continue = False
        self.diagnostics_stopping = False
        self.remote_session = False
        self.playback = False
        self.verified = None
        self.inspection_pending = False
        self.inspection_id = 0
        self.connection_epoch = 0
        self.fields = dict(DEFAULT_FIELDS)
        self.zip_path = ""
        self.key_path = ""
        self.progress = 0.0
        self.capabilities_text = tr("未连接；连接后查询设备能力。")
        self.recording_text = tr("连接后按设备能力启用；增益 256 = 1 倍。")
        self.calibration_text = tr("等待开始；收到摆放提示后才能继续采集。")
        self.profile_text = tr("尚未读取本机配置")
        self.sample_text = tr("校准诊断：—")
        self.firmware_text = tr("选择独立可信公钥；拒绝测试固件。")
        self.firmware_status = tr("尚未升级")

    @property
    def busy(self):
        return self.tool_state != "idle"

    @property
    def firmware_locked(self):
        return self.tool_state == "firmware"

    def supports(self, name):
        return self.capabilities.get(name, False)

    def available(self, action):
        app = self.app
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
        if action == "audio":
            return idle and self.supports("audio_files")
        if action in {"read", "record"}:
            return idle and self.supports("recording_control")
        if action in {"live_audio", "playback"}:
            return idle and self.supports("recording_control") and self.supports("live_audio")
        if action in {"accel", "quick", "full", "profile", "reset", "diagnostics"}:
            return idle and self.supports("imu")
        if action == "install":
            return (idle and self.supports("ota") and self.verified is not None
                    and not self.inspection_pending)
        return False

    @property
    def firmware_files_editable(self):
        return not (self.busy or self.inspection_pending or self.app.closing)

    def clear_calibration_sample(self):
        self.diagnostics_stopping = True
        self.sample_text = tr("校准诊断：—（无实时数据）")

    def _require(self, action):
        if self.available(action):
            return True
        self.app.error(tr("请连接支持此功能的设备，并先停止手势录制、音频或其他工具。"))
        return False

    def set_field(self, key, value):
        self.fields[key] = value

    def set_playback(self, enabled):
        self.playback = bool(enabled)

    def read_config(self):
        if self._require("read"):
            app = self.app
            app.set_audio_state({"state": "listing", "message": tr("正在读取录音设置 / 状态…")})
            if not app.request(app.worker.read_recording_config):
                app.set_audio_state({"state": "idle", "message": tr("读取设置请求失败")})

    def start_recording(self, *, live=False, owner=None, session_id=None, max_seconds=None) -> bool:
        """Submit audio without resetting live inference; SDK failures never open dialogs."""
        action = "live_audio" if live else "record"
        if owner is not None:
            if not self.available(action):
                raise ValueError(tr("请连接支持此功能的设备，并先停止手势录制、音频或其他工具。"))
        elif not self._require(action):
            return False
        try:
            if type(live) is not bool:
                raise ValueError("live must be a boolean")
            if owner is not None and (not isinstance(owner, str) or not owner.strip()
                                      or not isinstance(session_id, str) or not session_id.strip()):
                raise ValueError("Recording owner and session_id must be nonempty strings")
            fields = dict(self.fields)
            if max_seconds is not None:
                fields["duration_s"] = max_seconds
            options = recording_options(fields)
        except (ValueError, KeyError) as exc:
            if owner is not None:
                raise ValueError(str(exc)) from exc
            self.app.error(str(exc))
            return False
        app = self.app
        self.remote_session = True
        app.set_audio_state({"state": "starting", "message": tr("正在启动实时接收…") if live else tr("正在启动远程录音…")})
        kwargs = dict(live=live, playback=bool(live and self.playback))
        if owner is not None:
            kwargs.update(owner=owner, session_id=session_id)

        def submit():
            app.worker.start_remote_recording(options, app.audio_directory, **kwargs)

        try:
            if owner is not None:
                submit()  # app.request reports errors with a modal dialog; RPC must not.
                accepted = True
            else:
                accepted = app.request(submit)
        except Exception:
            self.remote_session = False
            app.set_audio_state({"state": "idle", "message": tr("启动录音失败")})
            raise
        if not accepted:
            self.remote_session = False
            app.set_audio_state({"state": "idle", "message": tr("启动录音失败")})
        return accepted

    def finish_recording(self, *, owner=None) -> bool:
        # Session ownership is checked by PluginsController, not by this UI helper.
        if owner is not None:
            if not self.available("finish"):
                raise ValueError(tr("请连接支持此功能的设备，并先停止手势录制、音频或其他工具。"))
        elif not self._require("finish"):
            return False
        app = self.app
        previous = app.audio_state
        app.set_audio_state({"state": "stopping", "message": tr("正在停止并等待完整音频保存…")})
        try:
            if owner is not None:
                app.worker.finish_remote_recording()
                accepted = True
            else:
                accepted = app.request(app.worker.finish_remote_recording)
        except Exception:
            app.set_audio_state({"state": previous, "message": tr("停止请求失败，可重试")})
            raise
        if not accepted:
            app.set_audio_state({"state": previous, "message": tr("停止请求失败，可重试")})
        return accepted

    def _start_tool(self, action, state, operation):
        if not self._require(action):
            return
        self.app.stop_live()
        self.tool_state = state
        self.awaiting_continue = False
        self.clear_calibration_sample()
        self.diagnostics_stopping = False
        self.app.notify()
        if not self.app.request(operation):
            self.tool_state = "idle"
            self.app.notify()

    def start_calibration(self, mode):
        if mode not in {"accel", "quick", "full"}:
            raise ValueError(tr("未知校准模式"))
        self.calibration_text = tr("正在准备校准，请等待摆放提示…")
        self._start_tool(mode, "calibrating", lambda: self.app.worker.start_calibration(mode))

    def continue_calibration(self):
        if self._require("continue"):
            self.awaiting_continue = False
            self.app.notify()
            self.app.request(self.app.worker.continue_calibration)

    def stop_tool(self):
        if self._require("cancel"):
            self.clear_calibration_sample()
            self.awaiting_continue = False
            self.app.notify()
            self.app.request(self.app.worker.stop_tool)

    def start_diagnostics(self):
        self._start_tool("diagnostics", "diagnostics", self.app.worker.start_diagnostics)

    def show_profile(self):
        self._start_tool("profile", "calibrating", self.app.worker.show_calibration)

    def reset_profile(self):
        if self._require("reset") and self.app.dialogs.ask(
                tr("重置本机校准？"), tr("仅删除本机校准配置，不修改手势数据或模型。"), danger=True):
            self._start_tool("reset", "calibrating", self.app.worker.reset_calibration)

    def choose_firmware_file(self, kind):
        if not self.firmware_files_editable:
            return
        filters = tr("固件 ZIP (*.zip)") if kind == "zip" else tr("公钥 PEM (*.pem);;所有文件 (*)")
        path = self.app.dialogs.open_file(filters)
        if path and self.firmware_files_editable:
            self.set_firmware_path(kind, path)

    def set_firmware_path(self, kind, path):
        if kind == "zip":
            self.zip_path = path
        else:
            self.key_path = path
        self.invalidate_inspection()

    def invalidate_inspection(self):
        self.inspection_id += 1
        self.verified = None
        self.firmware_text = tr("文件选择已改变，请重新离线验签。")
        self.app.notify()

    def inspect_firmware(self):
        if not self._require("inspect"):
            return
        zip_path, key_path = self.zip_path.strip(), self.key_path.strip()
        if not zip_path or not key_path:
            self.app.error(tr("请选择固件 ZIP 和独立可信的公钥 PEM。"))
            return
        self.inspection_id += 1
        token = self.inspection_id
        self.verified = None
        self.inspection_pending = True
        self.firmware_text = tr("正在后台离线验签；不会连接或操作设备…")
        self.app.notify()
        events = self.app.events

        def run():
            try:
                from ..firmware import inspect_bundle
                verified = inspect_bundle(Path(zip_path), Path(key_path))
            except Exception as exc:
                events.put(("firmware_inspection_failed", {"token": token, "error": str(exc)}))
            else:
                events.put(("firmware_inspected", {"token": token, "verified": verified}))
        try:
            self.app.executor.submit(run)
        except Exception as exc:
            self.inspection_pending = False
            self.firmware_text = tr("无法启动验签：{error}", error=exc)
            self.app.notify()
            self.app.error(str(exc))

    def install_firmware(self):
        if not self._require("install"):
            return
        app = self.app
        verified, epoch = self.verified, self.connection_epoch
        if not app.dialogs.ask(
                tr("确认设备并升级固件？"),
                tr("当前设备：{status}\n\n{summary}\n\n"
                   "将写入已验签的固件快照。升级期间不能断开或退出，发送完成后还需核验版本。继续？",
                   status=app.status, summary=verified.summary),
                danger=True):
            return
        # The dialog pumps events; connection, selection or exclusivity may have changed.
        if verified is not self.verified or epoch != self.connection_epoch or not self._require("install"):
            return
        app.stop_live()
        self.tool_state = "firmware"  # Lock before enqueue, not on a later event.
        self.awaiting_continue = False
        self.firmware_status = tr("升级已提交；等待设备检查与发送…")
        self.progress = 0.0
        app.notify()
        if not app.request(lambda: app.worker.install_firmware(verified)):
            self.tool_state = "idle"
            self.firmware_status = tr("升级提交失败；未开始升级")
            app.notify()

    def handle(self, event, payload):
        """Return True when the event is fully consumed here."""
        if event == "capabilities":
            self.capabilities = {key: payload.get(key) is True for key, _ in _CAPABILITIES}
            descriptions = [tr("{label}：{support}", label=label,
                               support=tr("支持") if self.capabilities[key] else tr("不支持"))
                            for key, label in _CAPABILITIES]
            self.capabilities_text = tr("{recording} · {live} · {files} · {imu} · {ota}",
                                        **dict(zip(("recording", "live", "files", "imu", "ota"), descriptions)))
            self.recording_text = tr("{recording} · {live} · {files}",
                                     **dict(zip(("recording", "live", "files"), descriptions)))
        elif event in {"connected", "disconnected", "reconnecting"}:
            self.connection_epoch += 1
            if event != "connected":
                self.capabilities = {}
                self.awaiting_continue = False
                self.clear_calibration_sample()
                self.capabilities_text = tr("未连接；重新连接后重新查询设备能力。")
            return False
        elif event == "audio_state":
            if payload["state"] == "idle":
                self.remote_session = False
            elif payload["state"] in {"recording", "live"}:
                self.remote_session = True
            return False
        elif event == "tool_state":
            self.tool_state = payload["state"]
            if self.tool_state != "diagnostics":
                self.clear_calibration_sample()
            self.awaiting_continue = False
            message = tr(payload.get("message", self.tool_state))
            if self.firmware_locked:
                self.firmware_status = message
            else:
                self.calibration_text = message
        elif event == "calibration_prompt":
            if self.tool_state == "calibrating":
                self.awaiting_continue = True
                self.calibration_text = tr(payload)
        elif event == "calibration_progress":
            self.calibration_text = tr(payload)
        elif event == "calibration_profile":
            self.profile_text = tr(payload)
        elif event == "calibration_sample":
            if self.app.connected and self.tool_state == "diagnostics" and not self.diagnostics_stopping:
                applied = payload["applied_components"]
                note = (tr("仅列出的组件应用校准；诊断读数不代表精度已验证。") if applied else
                        tr("未应用校准：仅显示原始物理单位读数，不能据此认定精度已验证。"))
                self.sample_text = tr("加速度 g：{accel}\n陀螺仪 °/s：{gyro}\n实际应用校准组件：{applied}\n{note}",
                                      accel=payload["accel_g"], gyro=payload["gyro_dps"],
                                      applied=applied, note=note)
        elif event == "recording_config":
            for key in DEFAULT_FIELDS:
                self.fields[key] = (str(Decimal(payload["max_duration_ms"]) / 1000) if key == "duration_s"
                                    else str(payload[key]))
            self.app.audioFieldsChanged.emit()
        elif event == "recording_status":
            self.recording_text = tr(payload)
        elif event == "firmware_progress":
            sent, total = payload["sent"], payload["total"]
            self.progress = min(100.0, max(0.0, sent / total * 100)) if total else 0.0
            self.firmware_status = tr("已发送 {sent} / {total} 字节；仍需等待重启与版本核验。", sent=sent, total=total)
        elif event == "firmware_complete":
            self.firmware_status = tr(payload)
            self.app.log(self.firmware_status)
        elif event in {"firmware_inspected", "firmware_inspection_failed"}:
            self.inspection_pending = False
            if payload["token"] == self.inspection_id:
                if event == "firmware_inspected":
                    self.verified = payload["verified"]
                    self.firmware_text = tr(self.verified.summary)
                else:
                    self.verified = None
                    self.firmware_text = tr("验签失败：{error}", error=payload["error"])
                    self.app.error(payload["error"])
        elif event == "error":
            # A generic error may be unrelated. Only worker tool_state=idle
            # releases an accepted OTA; otherwise close could interrupt it.
            if self.busy:
                if self.firmware_locked:
                    self.firmware_status = tr(payload)
                else:
                    self.calibration_text = tr(payload)
            return False
        else:
            return False
        self.app.notify()
        return True
