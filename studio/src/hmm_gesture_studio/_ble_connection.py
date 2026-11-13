# SPDX-License-Identifier: MPL-2.0
"""Optional connection tuning, isolated from SDK firmware/protocol handling.

CoreBluetooth's central-side latency selector is private. Keep all SDK/Bleak
backend access here and degrade gracefully when either implementation changes.
No native modules are imported, so other platforms can import Studio normally.
"""
from __future__ import annotations

import sys
from typing import Literal

LatencyRequestStatus = Literal["not_applicable", "unavailable", "requested", "failed"]


def request_ota_low_latency(ring: object) -> LatencyRequestStatus:
    """Request low latency on the existing macOS connection, once per OTA attempt.

    The caller must own the BLE session on its worker thread. This neither
    connects nor disconnects, touches GATT, nor replaces a native delegate.
    'requested' means the void native call returned, NOT that parameters changed.
    Do not guess the previous settings or try to restore them: Studio ends the
    session after each OTA attempt; the OS owns the negotiated link parameters.
    """
    if sys.platform != "darwin":
        return "not_applicable"
    try:
        if not getattr(ring, "is_connected", False):
            return "unavailable"
        transport = getattr(ring, "transport", None)
        client = getattr(transport, "_client", None)
        if client is None or not client.is_connected:
            return "unavailable"
        backend = getattr(client, "_backend", None)
        peripheral = getattr(backend, "_peripheral", None)
        delegate = getattr(backend, "_central_manager_delegate", None)
        manager = getattr(delegate, "central_manager", None)
        request = getattr(manager, "setDesiredConnectionLatency_forPeripheral_", None)
        if peripheral is None or not callable(request):
            return "unavailable"
        # On the tested macOS runtime bluetoothd identifies 0 as "low".
        # It is a latency preference, NOT interval=0 or peripheral latency=0.
        # Avoid the undocumented completion-block ABI entirely.
        request(0, peripheral)
    except Exception:
        # This is an optional optimization. Actual connection/OTA failures must
        # still be handled by the SDK; never turn this into an upgrade retry.
        return "failed"
    return "requested"
