# SPDX-License-Identifier: MPL-2.0
"""Local connection history does not require a display, SDK, or hardware."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from hmm_gesture_studio.device_history import DeviceHistory, MAX_DEVICES, history_path


class DeviceHistoryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config" / "devices.json"

    def test_persists_and_deduplicates_recent_successes(self):
        history = DeviceHistory(self.path)
        self.assertEqual(history.entries, [])
        self.assertFalse(self.path.exists())
        history.remember(" A-B-C ", "戒指一")
        history.remember("AA:BB:CC", "戒指二")
        history.remember("a-b-c")
        loaded = DeviceHistory(self.path)
        self.assertEqual([e["address"] for e in loaded.entries], ["a-b-c", "AA:BB:CC"])
        self.assertEqual(loaded.entries[0]["name"], "戒指一")
        self.assertTrue(loaded.entries[0]["last_connected"])
        self.assertIsNone(loaded.warning)

    def test_history_is_bounded_and_can_be_cleared(self):
        history = DeviceHistory(self.path)
        for i in range(MAX_DEVICES + 3):
            history.remember(str(i))
        self.assertEqual(len(history.entries), MAX_DEVICES)
        self.assertEqual(history.entries[-1]["address"], "3")
        history.clear()
        self.assertEqual(DeviceHistory(self.path).entries, [])

    def test_malformed_history_is_nonfatal(self):
        self.path.parent.mkdir()
        for contents in ("{broken", "[]", '{"version":true,"devices":[]}',
                         '{"version":1,"devices":[{"address":42}]}',
                         '{"version":1,"devices":[{"address":"ring","name":null}]}'):
            self.path.write_text(contents)
            history = DeviceHistory(self.path)
            self.assertEqual(history.entries, [])
            self.assertIsNotNone(history.warning)
        self.path.write_bytes(b"\xff\xfe")
        self.assertIsNotNone(DeviceHistory(self.path).warning)

    def test_failed_write_preserves_previous_history(self):
        history = DeviceHistory(self.path)
        history.remember("old", "first")
        before = self.path.read_bytes()
        with patch("hmm_gesture.bundle.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                history.remember("new")
            with self.assertRaises(OSError):
                history.clear()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(history.entries[0]["address"], "old")
        self.assertEqual(len(history.entries), 1)

    def test_bad_values_do_not_change_file(self):
        history = DeviceHistory(self.path)
        for address, name in (("", ""), ("  ", ""), ("ring\n", ""), ("a" * 300, ""), ("ring", None)):
            with self.assertRaises(ValueError):
                history.remember(address, name)
        self.assertFalse(self.path.exists())

    def test_settings_path_is_not_working_directory(self):
        with patch("hmm_gesture_studio.device_history.sys.platform", "linux"), patch.dict(os.environ, XDG_CONFIG_HOME="/tmp/example-settings"):
            self.assertEqual(history_path(), Path("/tmp/example-settings/hmm-gesture-studio/devices.json"))


if __name__ == "__main__":
    unittest.main()
