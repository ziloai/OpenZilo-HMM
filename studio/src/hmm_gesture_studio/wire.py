# SPDX-License-Identifier: MPL-2.0
"""Wire IDs used by Studio's queue ownership adapter (SDK 0.6.2).

These are protocol identifiers, not SDK exports. No private SDK modules are
imported; only the pinned client's receive queues require private access.
"""
from enum import IntEnum


class AudioCommand(IntEnum):
    START_EXTRACT = 0x0503
    DATA_FRAME = 0x0505
    END_EXTRACT = 0x0507
    RECORD_RESULT = 0x050A


class SensorCommand(IntEnum):
    DATA_FRAME = 0x0605


class ErrorCode(IntEnum):
    CMD_NOT_EXIST = 5
    DEVICE_BUSY = 2
