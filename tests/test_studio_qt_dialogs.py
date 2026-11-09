# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEvent, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QWidget
except ImportError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"PySide6 unavailable: {exc}")

from hmm_gesture_studio.qt import theme
from hmm_gesture_studio.qt.dialogs import QtDialogs, StudioDialog, _Scrim
from hmm_gesture_studio.qt.theme import apply_theme


class StudioDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.window = QWidget()
        self.window.resize(800, 600)
        self.window.show()
        self.dialogs = QtDialogs(self.window)
        self.addCleanup(self.window.deleteLater)

    def during(self, action):
        """Run action once the real modal loop is up, with the open dialog."""
        def run():
            action(QApplication.activeModalWidget())
        QTimer.singleShot(0, run)

    def scrims(self):
        return [item for item in self.window.findChildren(_Scrim) if item.isVisible()]

    def test_enter_confirms_ordinary_question(self):
        self.during(lambda box: QTest.keyClick(box, Qt.Key.Key_Return))
        self.assertTrue(self.dialogs.ask("标题", "说明"))

    def test_enter_cancels_dangerous_question(self):
        def press(box):
            self.assertEqual(box.confirm_button.property("kind"), "danger")
            QTest.keyClick(box, Qt.Key.Key_Return)
        self.during(press)
        self.assertFalse(self.dialogs.ask("永久删除？", "说明", danger=True))

    def test_escape_cancels(self):
        self.during(lambda box: QTest.keyClick(box, Qt.Key.Key_Escape))
        self.assertFalse(self.dialogs.ask("标题", "说明"))

    def test_buttons_report_their_answer(self):
        self.during(lambda box: box.confirm_button.click())
        self.assertTrue(self.dialogs.ask("标题", "说明", danger=True))
        self.during(lambda box: box.cancel_button.click())
        self.assertFalse(self.dialogs.ask("标题", "说明"))

    def test_error_has_single_action(self):
        def check(box):
            self.assertIsNone(box.cancel_button)
            box.confirm_button.click()
        self.during(check)
        self.dialogs.error("操作失败", "原因")

    def test_scrim_covers_window_and_is_removed(self):
        def check(box):
            self.assertEqual([item.geometry() for item in self.scrims()], [self.window.rect()])
            box.reject()
        self.during(check)
        self.dialogs.ask("标题", "说明")
        self.assertEqual(self.scrims(), [])

    def test_nested_error_keeps_one_scrim_until_outer_closes(self):
        def outer(box):
            def inner(error_box):
                self.assertEqual(len(self.scrims()), 1)
                error_box.confirm_button.click()
            self.during(inner)
            self.dialogs.error("操作失败", "原因")
            self.assertEqual(len(self.scrims()), 1)
            box.reject()
        self.during(outer)
        self.dialogs.ask("标题", "说明")
        self.assertEqual(self.scrims(), [])

    def test_centered_on_window_and_follows_it(self):
        box = StudioDialog(self.window, "标题", "说明", confirm="好")
        self.addCleanup(box.deleteLater)
        box.center_on_anchor()
        self.assertEqual(box.geometry().center(), self.window.frameGeometry().center())
        self.window.resize(1000, 700)
        QApplication.sendPostedEvents(None, QEvent.Type.Resize)
        self.assertEqual(box.geometry().center(), self.window.frameGeometry().center())

    def test_multiline_text_is_fully_visible_without_scrolling(self):
        text = "将删除插件托管代码、环境、数据、设置、绑定和密钥。不会删除原始来源目录。\n第二段\n第三段"
        box = StudioDialog(self.window, "移除插件？", text, confirm="继续", cancel="取消", danger=True)
        self.addCleanup(box.deleteLater)
        box.center_on_anchor()
        box.show()
        QApplication.processEvents()
        self.assertEqual(box.body.verticalScrollBar().maximum(), 0)
        self.assertGreater(box.body.height(), box.body.fontMetrics().lineSpacing() * 3)
        box.hide()

    def test_long_unbroken_text_wraps_and_scrolls_within_limits(self):
        box = StudioDialog(self.window, "标题", "/很长的路径" * 400, confirm="好")
        self.addCleanup(box.deleteLater)
        box.center_on_anchor()
        self.assertEqual(box.width(), theme.DIALOG_WIDTH)
        self.assertEqual(box.body.height(), theme.DIALOG_BODY_MAX_HEIGHT)
        self.assertEqual(box.body.horizontalScrollBar().maximum(), 0)

    def test_works_without_parent(self):
        self.during(lambda box: box.confirm_button.click())
        QtDialogs().error("启动失败", "原因")


if __name__ == "__main__":
    unittest.main()
