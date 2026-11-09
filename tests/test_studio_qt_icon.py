# SPDX-License-Identifier: MPL-2.0
"""Packaged application icon loading and window inheritance, without hardware."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError as exc:
    raise unittest.SkipTest(f"Qt GUI libraries unavailable: {exc}")

from hmm_gesture_studio.qt.app import application_icon


class ApplicationIconTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_packaged_icon_renders_at_desktop_sizes(self):
        icon = application_icon()
        self.assertFalse(icon.isNull())
        for size in (16, 32, 64, 128, 256, 512, 1024):
            with self.subTest(size=size):
                image = icon.pixmap(size, size).toImage()
                self.assertFalse(image.isNull())
                self.assertEqual(image.width(), size)
                self.assertEqual(image.height(), size)
                self.assertEqual(image.pixelColor(0, 0).alpha(), 0)
                self.assertEqual(image.pixelColor(size // 2, size // 2).alpha(), 255)

    def test_windows_inherit_application_icon(self):
        previous = self.app.windowIcon()
        self.addCleanup(self.app.setWindowIcon, previous)
        icon = application_icon()
        self.app.setWindowIcon(icon)
        window = QWidget()
        self.addCleanup(window.deleteLater)
        self.assertEqual(window.windowIcon().cacheKey(), icon.cacheKey())
