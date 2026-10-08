# SPDX-License-Identifier: MPL-2.0
"""Small, local history of successfully connected devices, independent of Tk/BLE."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from hmm_gesture.bundle import write_json

MAX_DEVICES = 20
MAX_HISTORY_BYTES = 64 * 1024


def history_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "hmm-gesture-studio" / "devices.json"


def _text(value, maximum=256):
    return (isinstance(value, str) and len(value) <= maximum
            and not any(ord(c) < 32 for c in value))


class DeviceHistory:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else history_path()
        self.entries: list[dict[str, str]] = []
        self.warning: str | None = None
        try:
            if not self.path.exists():
                return
            if self.path.stat().st_size > MAX_HISTORY_BYTES:
                raise ValueError("device history is too large")
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or type(data.get("version")) is not int
                    or data["version"] != 1 or not isinstance(data.get("devices"), list)):
                raise ValueError("unsupported device history")
            seen = set()
            for item in data["devices"]:
                if not isinstance(item, dict):
                    raise ValueError("invalid device entry")
                entry = {key: item.get(key, "") for key in ("address", "name", "last_connected")}
                if not all(_text(value) for value in entry.values()) or not entry["address"].strip():
                    raise ValueError("invalid device address/name")
                entry["address"] = entry["address"].strip()
                key = entry["address"].casefold()
                if key not in seen:
                    self.entries.append(entry)
                    seen.add(key)
            self.entries = self.entries[:MAX_DEVICES]
        except (OSError, ValueError, UnicodeError) as exc:
            self.entries = []
            self.warning = f"无法读取设备历史（不影响连接）：{exc}"

    def remember(self, address: str, name: str = "") -> None:
        """Call only after a successful connection; failures never enter history."""
        if not _text(address) or not address.strip() or not _text(name):
            raise ValueError("invalid device address/name")
        address = address.strip()
        previous = next((e for e in self.entries if e["address"].casefold() == address.casefold()), None)
        entry = {"address": address, "name": name or (previous["name"] if previous else ""),
                 "last_connected": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        entries = [entry] + [e for e in self.entries if e["address"].casefold() != address.casefold()]
        entries = entries[:MAX_DEVICES]
        # Keep both memory and the old file unchanged if the atomic save fails.
        write_json(self.path, {"version": 1, "devices": entries}, max_bytes=MAX_HISTORY_BYTES)
        self.entries = entries

    def clear(self) -> None:
        write_json(self.path, {"version": 1, "devices": []}, max_bytes=MAX_HISTORY_BYTES)
        self.entries = []
