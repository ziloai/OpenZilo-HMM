# SPDX-License-Identifier: MPL-2.0
"""Real signed SDK bundles; firmware installation is always mocked."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import zipfile

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
import openzilo as sdk

from hmm_gesture_studio import firmware


def signed_bundle(key, profile="production"):
    payload = b"RINGENC1" + struct.pack("<II", 1, 16) + bytes(32)
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    manifest = {
        "schema": 1, "algorithm": "ECDSA-P256-SHA256", "image": "app",
        "version": "V2.001.0002.0002-prod-12345678", "model": "Open Zilo",
        "board": "ring_dvt2/nrf54l15/cpuapp", "profile": profile,
        "security_version": 1, "source_versions": [],
        "key_id": hashlib.sha256(public_der).hexdigest(),
        "payload": {"file": "app.ota.bin", "format": "RINGENC1", "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest()},
    }
    raw = json.dumps(manifest).encode()
    signature = key.sign(b"OpenZilo firmware bundle v1\x00" + raw, ec.ECDSA(hashes.SHA256()))
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("manifest.json", raw)
        archive.writestr("manifest.sig", signature)
        archive.writestr("app.ota.bin", payload)
    return target.getvalue()


class FirmwareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.bundle_path = directory / "firmware.zip"
        self.key_path = directory / "trusted.pem"
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.key_bytes = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.bundle_bytes = signed_bundle(self.key)
        self.bundle_path.write_bytes(self.bundle_bytes)
        self.key_path.write_bytes(self.key_bytes)
        self.ring = Mock(spec=sdk.OpenZiloClient)
        self.emit = Mock()
        self.info = sdk.SystemInfo("V2.001.0002.0002-prod-12345678", 0, 0, 0, 90, False, "sn", "cpu", "Open Zilo")

    def inspect(self):
        return firmware.inspect_bundle(self.bundle_path, self.key_path)

    def test_offline_real_signature_and_complete_summary(self):
        with patch.object(sdk, "OpenZiloClient", side_effect=AssertionError("must stay offline")):
            verified = self.inspect()
        self.assertIsInstance(verified, firmware.VerifiedFirmware)
        self.assertEqual(verified.bundle, self.bundle_bytes)
        self.assertEqual(verified.public_key, self.key_bytes)
        for value in (self.info.firmware_version, "Open Zilo", "ring_dvt2/nrf54l15/cpuapp",
                      "security_version", "source_versions", "key_id", "sha256", "production"):
            self.assertIn(value, verified.summary)
        with self.assertRaises(FrozenInstanceError):
            verified.bundle = b"changed"

    def test_test_firmware_rejected_before_install(self):
        self.bundle_path.write_bytes(signed_bundle(self.key, "test"))
        with self.assertRaisesRegex(ValueError, "[Tt]est"):
            self.inspect()

    def test_corrupt_unsigned_and_wrong_trust_anchor_rejected(self):
        self.bundle_path.write_bytes(b"raw firmware")
        with self.assertRaises(ValueError):
            self.inspect()
        self.bundle_path.write_bytes(signed_bundle(ec.generate_private_key(ec.SECP256R1())))
        with self.assertRaisesRegex(ValueError, "signature"):
            self.inspect()

    async def test_install_uses_immutable_snapshot_despite_file_replacement(self):
        verified = self.inspect()
        self.bundle_path.write_bytes(b"replaced")
        self.key_path.unlink()
        async def update(ring, bundle, **kwargs):
            self.assertIs(ring, self.ring)
            self.assertEqual(bundle, self.bundle_bytes)
            self.assertEqual(kwargs["trusted_public_key"], self.key_bytes)
            self.assertFalse(kwargs["allow_test_firmware"])
            kwargs["progress"](12, 100)
            kwargs["progress"](100, 100)
            self.assertFalse(any(call.args[0] == "firmware_complete" for call in self.emit.call_args_list))
            return self.info
        with patch.object(sdk, "update_firmware", new=AsyncMock(side_effect=update)) as install:
            result = await firmware.install_firmware(self.ring, verified, emit=self.emit)
        install.assert_awaited_once()
        self.assertIs(result, self.info)
        self.assertEqual(self.emit.call_args_list[0].args, ("firmware_progress", {"sent": 12, "total": 100}))
        self.assertEqual(self.emit.call_args.args[0], "firmware_complete")
        self.assertIn(self.info.firmware_version, self.emit.call_args.args[1])

    async def test_failure_or_cancel_even_after_transfer_never_reports_success(self):
        verified = self.inspect()
        for failure in (sdk.TimeoutError("reboot unconfirmed"), sdk.TransportError("disconnect"), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                self.emit.reset_mock()
                async def update(*args, **kwargs):
                    kwargs["progress"](100, 100)
                    raise failure
                with patch.object(sdk, "update_firmware", new=AsyncMock(side_effect=update)):
                    with self.assertRaises(type(failure)):
                        await firmware.install_firmware(self.ring, verified, emit=self.emit)
                self.assertEqual([call.args[0] for call in self.emit.call_args_list], ["firmware_progress"])


if __name__ == "__main__":
    unittest.main()
