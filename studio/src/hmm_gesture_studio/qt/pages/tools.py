# SPDX-License-Identifier: MPL-2.0
"""Local IMU calibration and signed firmware updates."""
from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation
from PySide6.QtWidgets import QHBoxLayout, QProgressBar

from .. import theme
from ..motion import Busy
from ..widgets import (
    Card, DeviceNotice, Page, ReadOnlyText, button, field, label, line_edit, row, set_field_text, set_text,
)


class ToolsPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        t = controller.tools
        top = self.add_section()
        self.header(top, "设备工具", "校准与升级独占 IMU 或连接；开始前请停止录音和手势录制。")
        self.notice = DeviceNotice(self, "尚未连接戒指。")
        top.add(self.notice)
        self.capabilities = label(tone="muted", wrap=True)
        top.add(self.capabilities)

        calibration = Card("本机 IMU 校准")
        top.add(calibration)
        calibration.add(label("校准独占 IMU，停止手势识别；全部验证通过才替换本机配置，不向戒指写参数。"
                              "校准值不进入手势训练、录制或识别。", "caption", wrap=True))
        self.accel = button("加速度六面", lambda: t.start_calibration("accel"))
        self.quick = button("陀螺仪静止", lambda: t.start_calibration("quick"))
        self.full = button("完整校准", lambda: t.start_calibration("full"))
        calibration.add(row(self.accel, self.quick, self.full))
        self.calibration_text = label(wrap=True)
        calibration.add(self.calibration_text)
        self.resume = button("继续采集", t.continue_calibration, kind="primary")
        self.cancel = button("取消 / 停止诊断", t.stop_tool)
        calibration.add(row(self.resume, self.cancel))
        self.profile = button("查看配置", t.show_profile)
        self.reset = button("重置配置", t.reset_profile, kind="danger")
        self.diagnostics = button("校准诊断", t.start_diagnostics)
        calibration.add(row(self.profile, self.diagnostics, self.reset))
        self.profile_text = ReadOnlyText(t.profile_text, height=110)
        calibration.add(self.profile_text)
        self.sample = label(wrap=True, selectable=True)
        calibration.add(self.sample)
        calibration.add(label("诊断仅显示 g / °/s，不进入原始录制或训练流水线。", "caption", wrap=True))

        more = self.add_section()
        more.add(label("签名固件 OTA", "section"))
        more.add(label("不能信任固件包自带的公钥。拒绝测试固件；不支持原始 BIN。", tone="muted", wrap=True))
        firmware = Card()
        more.add(firmware)
        paths = QHBoxLayout()
        paths.setSpacing(20)
        self.zip = line_edit(on_edit=lambda value: t.set_firmware_path("zip", value), placeholder="固件 .ota.zip")
        self.zip_choose = button("选择", lambda: t.choose_firmware_file("zip"))
        self.key = line_edit(on_edit=lambda value: t.set_firmware_path("key", value), placeholder="独立来源的公钥 .pem")
        self.key_choose = button("选择", lambda: t.choose_firmware_file("key"))
        paths.addLayout(field("固件 ZIP", row(self.zip, self.zip_choose, stretch=False)), 1)
        paths.addLayout(field("可信公钥 PEM", row(self.key, self.key_choose, stretch=False)), 1)
        firmware.add(paths)
        self.inspect = button("离线验签并查看", t.inspect_firmware)
        self.inspecting = Busy("正在离线验签…")
        firmware.add(row(self.inspect, self.inspecting))
        self.firmware_text = ReadOnlyText(t.firmware_text, height=150)
        firmware.add(self.firmware_text)
        self.install = button("确认设备并升级", t.install_firmware, kind="primary")
        firmware.add(row(self.install))
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self._progress_motion = QPropertyAnimation(self.progress, b"value", self)
        self._progress_motion.setDuration(theme.MOTION)
        firmware.add(self.progress)
        self.firmware_status = label(wrap=True)
        firmware.add(self.firmware_status)
        firmware.add(label("升级独占连接，期间不能断开或普通退出。发送 100% 不代表成功：等待重启、重连与版本核验。"
                           "失败不会自动重试；固件更新后需要重新检查本机校准配置。", "caption", wrap=True))
        self.finish()

    def render_state(self):
        t = self.controller.tools
        self.notice.render_state()
        set_text(self.capabilities, t.capabilities_text)
        for key, widget in (("accel", self.accel), ("quick", self.quick), ("full", self.full),
                            ("continue", self.resume), ("cancel", self.cancel), ("profile", self.profile),
                            ("reset", self.reset), ("diagnostics", self.diagnostics),
                            ("inspect", self.inspect), ("install", self.install)):
            widget.setEnabled(t.available(key))
        set_text(self.calibration_text, t.calibration_text)
        self.profile_text.set_value(t.profile_text)
        set_text(self.sample, t.sample_text)
        editable = t.firmware_files_editable
        for widget in (self.zip, self.key, self.zip_choose, self.key_choose):
            widget.setEnabled(editable)
        set_field_text(self.zip, t.zip_path)
        set_field_text(self.key, t.key_path)
        self.firmware_text.set_value(t.firmware_text)
        target = round(t.progress)
        if self._progress_motion.endValue() != target:
            self._progress_motion.stop()
            self._progress_motion.setStartValue(self.progress.value())
            self._progress_motion.setEndValue(target)
            self._progress_motion.start()
        self.inspecting.set_busy(t.inspection_pending)
        set_text(self.firmware_status, t.firmware_status)
