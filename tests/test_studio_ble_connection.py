# SPDX-License-Identifier: MPL-2.0
"""Native connection tuning is simulated; these tests never access Bluetooth."""
import asyncio
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

from hmm_gesture_studio import _ble_connection as connection


class OtaConnectionTests(unittest.TestCase):
    def setUp(self):
        platform = patch.object(connection.sys, "platform", "darwin")
        platform.start()
        self.addCleanup(platform.stop)
        self.peripheral = object()
        self.request = Mock()
        self.manager = NS(setDesiredConnectionLatency_forPeripheral_=self.request)
        self.delegate = NS(central_manager=self.manager)
        self.backend = NS(_peripheral=self.peripheral, _central_manager_delegate=self.delegate)
        self.client = NS(is_connected=True, _backend=self.backend)
        self.ring = NS(is_connected=True, transport=NS(_client=self.client))

    def test_requests_low_on_existing_native_connection_exactly_once(self):
        self.assertEqual(connection.request_ota_low_latency(self.ring), "requested")
        self.request.assert_called_once_with(0, self.peripheral)
        self.assertIs(self.backend._central_manager_delegate, self.delegate)
        # No connect/disconnect methods or protocol APIs exist on these stubs.

    def test_non_macos_never_accesses_native_objects(self):
        class NoAccess:
            def __getattribute__(self, name):
                raise AssertionError("must not touch the connection")
        for platform in ("linux", "win32"):
            with self.subTest(platform=platform), patch.object(connection.sys, "platform", platform):
                self.assertEqual(connection.request_ota_low_latency(NoAccess()), "not_applicable")
        self.request.assert_not_called()

    def test_disconnected_or_missing_sdk_transport_is_unavailable(self):
        cases = [NS(is_connected=False), NS(is_connected=True),
                 NS(is_connected=True, transport=NS()),
                 NS(is_connected=True, transport=NS(_client=NS(is_connected=False)))]
        for ring in cases:
            with self.subTest(ring=ring):
                self.assertEqual(connection.request_ota_low_latency(ring), "unavailable")
        self.request.assert_not_called()

    def test_missing_backend_peripheral_manager_or_selector_is_unavailable(self):
        cases = [None, NS(), NS(_peripheral=None, _central_manager_delegate=self.delegate),
                 NS(_peripheral=self.peripheral),
                 NS(_peripheral=self.peripheral, _central_manager_delegate=NS()),
                 NS(_peripheral=self.peripheral, _central_manager_delegate=NS(central_manager=NS())),
                 NS(_peripheral=self.peripheral, _central_manager_delegate=NS(
                     central_manager=NS(setDesiredConnectionLatency_forPeripheral_=False)))]
        for backend in cases:
            with self.subTest(backend=backend):
                self.client._backend = backend
                self.assertEqual(connection.request_ota_low_latency(self.ring), "unavailable")
        self.request.assert_not_called()

    def test_native_exception_is_best_effort_failure_without_retry(self):
        self.request.side_effect = RuntimeError("synthetic native error")
        self.assertEqual(connection.request_ota_low_latency(self.ring), "failed")
        self.request.assert_called_once_with(0, self.peripheral)

    def test_backend_property_exception_is_best_effort_failure(self):
        class BrokenBackend:
            @property
            def _peripheral(self):
                raise RuntimeError("synthetic incompatible backend")
        self.client._backend = BrokenBackend()
        self.assertEqual(connection.request_ota_low_latency(self.ring), "failed")
        self.request.assert_not_called()

    def test_cancellation_is_not_swallowed(self):
        self.request.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            connection.request_ota_low_latency(self.ring)


if __name__ == "__main__":
    unittest.main()
