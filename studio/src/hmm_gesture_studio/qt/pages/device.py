# SPDX-License-Identifier: MPL-2.0
"""Scan, connect and inspect the ring and its IMU stream."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QHeaderView, QTableWidgetItem

from ...plotting import AXES
from ..widgets import Card, Page, StatTile, button, field, label, line_edit, row, set_field_text, set_text, set_tone, table


class DevicePage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        c = controller
        top = self.add_section()
        self.header(top, "设备", "IMU 无需切换模式；连接后按设备能力开放功能。macOS 地址通常是 UUID。")
        self.status = label(wrap=True, selectable=True)
        top.add(self.status)
        self.battery = label(role="strong")
        top.add(self.battery)
        self.address = line_edit(on_edit=c.set_address, placeholder="设备地址")
        self.address.setMinimumWidth(320)
        self.connect_button = button("连接", c.connect_device, kind="primary")
        self.disconnect_button = button("断开 / 取消连接", c.disconnect_device)
        top.add(row(self.address, self.connect_button, self.disconnect_button))

        band = self.add_section(band=True)
        band.add(label("可用设备", "section"))
        devices = Card(tone="white")
        band.add(devices)
        self.scan = button("扫描", c.scan)
        self.clear_history = button("清空设备历史", c.clear_device_history)
        devices.add(row(self.scan, self.clear_history))
        devices.add(label("历史设备可直接连接，无需重新扫描；最多保留最近 20 台。", "caption", wrap=True))
        self.table = table(("名称", "地址", "来源", "RSSI"), height=190)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(0, 220)
        self.table.setColumnWidth(2, 70)
        self.table.itemSelectionChanged.connect(self._selected)
        devices.add(self.table)

        imu = Card("IMU 上报", tone="white")
        band.add(imu)
        self.stream = label(wrap=True)
        imu.add(self.stream)
        self.tiles = [StatTile(name) for name in AXES]
        self.count = StatTile("样本数", "0")
        grid = QGridLayout()
        grid.setHorizontalSpacing(36)
        grid.setVerticalSpacing(12)
        for index, tile in enumerate(self.tiles):
            grid.addWidget(tile, index // 3, index % 3)
        grid.addWidget(self.count, 0, 3)
        grid.setColumnStretch(4, 1)
        imu.add(grid)
        self.raw_note = label(role="caption")
        imu.add(self.raw_note)
        self.retry = button("重试 IMU 上报", c.retry_imu)
        imu.add(row(self.retry))
        capabilities = Card("设备能力", tone="white")
        band.add(capabilities)
        self.capabilities = label(wrap=True)
        capabilities.add(self.capabilities)
        self.finish()
        c.devicesChanged.connect(self._devices_changed)
        self._devices_changed()

    def _devices_changed(self):
        c = self.controller
        self.table.blockSignals(True)
        self.table.setRowCount(len(c.device_entries))
        for index, entry in enumerate(c.device_entries):
            rssi = "—" if entry["rssi"] is None else str(entry["rssi"])
            for column, value in enumerate((entry["name"], entry["address"], entry["source"], rssi)):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, entry["address"])
                self.table.setItem(index, column, item)
            if entry["address"].casefold() == c.address.strip().casefold():
                self.table.selectRow(index)
        self.table.blockSignals(False)
        set_field_text(self.address, c.address)

    def _selected(self):
        selected = self.table.selectedItems()
        if selected:
            self.controller.set_address(selected[0].data(Qt.ItemDataRole.UserRole))

    def render_state(self):
        c = self.controller
        set_text(self.status, c.status)
        set_text(self.battery, c.battery_text)
        set_tone(self.battery, {"normal": None}.get(c.battery_tone, c.battery_tone))
        set_field_text(self.address, c.address)
        self.address.setEnabled(c.can_connect)
        self.connect_button.setEnabled(c.can_connect)
        self.disconnect_button.setEnabled(c.can_disconnect)
        self.scan.setEnabled(c.can_scan)
        self.table.setEnabled(c.can_connect)
        set_text(self.stream, c.stream_text)
        for index, tile in enumerate(self.tiles):
            tile.set_value("—" if c.latest_sample is None else str(c.latest_sample[index]))
        self.count.set_value(str(c.count))
        set_text(self.raw_note, c.raw_note)
        self.retry.setEnabled(c.can_retry_imu)
        set_text(self.capabilities, c.tools.capabilities_text)
