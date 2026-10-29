# SPDX-License-Identifier: MPL-2.0
"""Signed production APP updates with an immutable, offline-verified snapshot.

The UI must show summary and obtain a second confirmation before installation.
It supplies an independently trusted public key, never a key from the bundle.
Run inspect_bundle off the UI thread. The worker owns BLE exclusively during
installation, with recording, downloads and IMU stopped beforehand.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path

import openzilo as sdk


@dataclass(frozen=True)
class VerifiedFirmware:
    """Bytes inspected before confirmation; later path changes cannot affect OTA."""

    bundle: bytes
    public_key: bytes
    summary: str

    def __post_init__(self) -> None:
        if not isinstance(self.bundle, bytes) or not isinstance(self.public_key, bytes):
            raise TypeError("Verified firmware and public key must be immutable bytes")
        if not isinstance(self.summary, str):
            raise TypeError("Verified firmware summary must be a string")


def inspect_bundle(firmware_path: str | Path, key_path: str | Path) -> VerifiedFirmware:
    """Read once, verify offline, reject test packages and expose all signed fields."""
    bundle = Path(firmware_path).read_bytes()
    public_key = Path(key_path).read_bytes()
    metadata = sdk.inspect_firmware_bundle(bundle, trusted_public_key=public_key)
    if metadata.profile == "test":
        raise ValueError("Test firmware is not permitted in Studio")
    summary = json.dumps(
        {"signature_verified": True, "manifest": metadata.manifest},
        ensure_ascii=False, indent=2,
    )
    return VerifiedFirmware(bundle, public_key, summary)


async def install_firmware(
    ring: sdk.OpenZiloClient, verified: VerifiedFirmware, *, emit: Callable[[str, object], None],
) -> sdk.SystemInfo:
    """Only the SDK's post-reboot identity/version verification means success.

    No file re-reads or automatic retries. Errors and cancellation propagate;
    cancellation cannot retract bytes already received by the ring. Reconnect
    and query its version before deciding whether to retry an unconfirmed update.
    """
    def progress(sent: int, total: int) -> None:
        emit("firmware_progress", {"sent": sent, "total": total})

    info = await sdk.update_firmware(
        ring, verified.bundle, trusted_public_key=verified.public_key,
        allow_test_firmware=False, progress=progress,
    )
    emit("firmware_complete", f"Firmware verified after reboot: {info.firmware_version}")
    return info
