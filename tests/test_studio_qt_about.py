# SPDX-License-Identifier: MPL-2.0
"""About page and contributor fetching, against a local server only."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from PySide6.QtCore import QBuffer, QIODevice, Qt
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtTest import QSignalSpy, QTest
    from PySide6.QtWidgets import QApplication
except ImportError as exc:
    raise unittest.SkipTest(f"Qt GUI libraries unavailable: {exc}")

from hmm_gesture_studio.device_history import DeviceHistory
from hmm_gesture_studio.qt import contributors, theme
from hmm_gesture_studio.qt.contributors import ContributorFetcher, failure_text, parse_contributors
from hmm_gesture_studio.qt.controller import StudioController
from hmm_gesture_studio.qt.pages.about import Avatar, package_version
from hmm_gesture_studio.qt.theme import apply_theme
from hmm_gesture_studio.qt.window import MainWindow

ALICE = dict(login="alice", type="User", avatar_url="https://avatars.githubusercontent.com/u/1?v=4",
             html_url="https://github.com/alice")


def png_bytes(color="#ff0000", size=8):
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


class ParseTests(unittest.TestCase):
    def test_keeps_people_in_order_and_skips_bots(self):
        bob = dict(ALICE, login="bob-2", html_url="https://github.com/bob-2")
        bot = dict(ALICE, login="dependabot[bot]", type="Bot")
        data = json.dumps([ALICE, bot, bob]).encode()
        self.assertEqual([c.login for c in parse_contributors(data)], ["alice", "bob-2"])

    def test_rejects_links_outside_expected_https_hosts(self):
        for bad in (dict(ALICE, html_url="https://evil.example/alice"),
                    dict(ALICE, html_url="http://github.com/alice"),
                    dict(ALICE, html_url="javascript:alert(1)"),
                    dict(ALICE, html_url="https://github.com.evil.example/alice"),
                    dict(ALICE, avatar_url="https://evil.example/a.png"),
                    dict(ALICE, avatar_url=None),
                    dict(ALICE, login="a b<script>"),
                    dict(ALICE, login=7),
                    "not an object"):
            self.assertEqual(parse_contributors(json.dumps([bad]).encode()), [], bad)

    def test_non_list_and_invalid_json_raise_value_error(self):
        for data in (b'{"message": "Not Found"}', b"{", b"\xff\xfe"):
            with self.assertRaises(ValueError):
                parse_contributors(data)

    def test_failure_messages_distinguish_rate_limit_and_offline(self):
        self.assertIn("上限", failure_text(403))
        self.assertIn("上限", failure_text(429))
        self.assertIn("找不到", failure_text(404))
        self.assertIn("500", failure_text(500))
        self.assertIn("无法连接", failure_text(None))

    def test_unknown_package_version_is_not_guessed(self):
        self.assertEqual(package_version("no-such-package-xyz"), "未知")


class Server:
    """Local HTTP server with a scripted response per path."""

    def __init__(self):
        self.responses = {}
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.requests.append((self.path, self.headers.get("User-Agent")))
                status, body = outer.responses.get(self.path.split("?")[0], (404, b"{}"))
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class QtBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])
        apply_theme(cls.qt)

    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.close)
        patcher = patch.object(contributors, "API_URL", f"{self.server.url}/contributors")
        patcher.start()
        self.addCleanup(patcher.stop)

    def use_local_avatar(self, data=None):
        """Point parsed avatars at the local server, past the production host check."""
        real = parse_contributors

        def local(raw):
            found = real(raw)
            return [contributors.Contributor(c.login, f"{self.server.url}/avatar/{c.login}", c.profile_url)
                    for c in found]
        patcher = patch.object(contributors, "parse_contributors", local)
        patcher.start()
        self.addCleanup(patcher.stop)


class FetcherTests(QtBase):
    def new_fetcher(self):
        # Qt-owned, as the page's child is: a garbage-collected fetcher could be
        # destroyed on the HTTP server's thread.
        return ContributorFetcher(self.qt)

    def wait(self, spy, count=1):
        for _ in range(100):
            if spy.count() >= count:
                return
            QTest.qWait(30)
        self.fail("signal did not arrive")

    def test_lists_then_loads_avatars_with_generic_user_agent(self):
        self.server.responses["/contributors"] = (200, json.dumps([ALICE]).encode())
        self.server.responses["/avatar/alice"] = (200, png_bytes())
        self.use_local_avatar()
        fetcher = self.new_fetcher()
        loaded, avatars = QSignalSpy(fetcher.loaded), QSignalSpy(fetcher.avatarLoaded)
        fetcher.fetch()
        self.wait(loaded)
        self.wait(avatars)
        self.assertEqual(self.server.requests[0][1], "hmm-gesture-studio")
        self.assertTrue(self.server.requests[1][0].endswith(f"?s={contributors.AVATAR_PIXELS}"))

    def test_http_and_connection_failures_are_reported(self):
        for status, expected in ((403, "上限"), (404, "找不到"), (500, "500")):
            self.server.responses["/contributors"] = (status, b"{}")
            fetcher = self.new_fetcher()
            failed = QSignalSpy(fetcher.failed)
            fetcher.fetch()
            self.wait(failed)
            self.assertIn(expected, failed.at(0)[0])
        with patch.object(contributors, "API_URL", "http://127.0.0.1:9/x"):
            fetcher = self.new_fetcher()
            failed = QSignalSpy(fetcher.failed)
            fetcher.fetch()
            self.wait(failed)
            self.assertIn("无法连接", failed.at(0)[0])

    def test_invalid_payload_and_bad_avatar_do_not_crash(self):
        self.server.responses["/contributors"] = (200, b"{oops")
        fetcher = self.new_fetcher()
        failed = QSignalSpy(fetcher.failed)
        fetcher.fetch()
        self.wait(failed)
        self.assertIn("无法解析", failed.at(0)[0])
        self.server.responses["/contributors"] = (200, json.dumps([ALICE]).encode())
        self.server.responses["/avatar/alice"] = (200, b"not an image")
        self.use_local_avatar()
        fetcher = self.new_fetcher()
        loaded, avatars = QSignalSpy(fetcher.loaded), QSignalSpy(fetcher.avatarLoaded)
        fetcher.fetch()
        self.wait(loaded)
        QTest.qWait(150)
        self.assertEqual(avatars.count(), 0)

    def test_new_fetch_supersedes_earlier_one(self):
        self.server.responses["/contributors"] = (200, json.dumps([ALICE]).encode())
        fetcher = self.new_fetcher()
        loaded = QSignalSpy(fetcher.loaded)
        fetcher.fetch()
        fetcher.fetch()
        self.wait(loaded)
        QTest.qWait(150)
        self.assertEqual(loaded.count(), 1)

    def test_oversized_response_is_aborted(self):
        self.server.responses["/contributors"] = (200, b"[" + b" " * 1024 + b"]")
        fetcher = self.new_fetcher()
        loaded, failed = QSignalSpy(fetcher.loaded), QSignalSpy(fetcher.failed)
        with patch.object(contributors, "MAX_LIST_BYTES", 16):
            fetcher.fetch()
            self.wait(failed)
        self.assertEqual(loaded.count(), 0)


class PageTests(QtBase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        self.dialogs = Mock()
        self.c = StudioController(self.dialogs, worker=Mock(), history=DeviceHistory(directory / "h.json"),
                                  directory=directory / "data", audio_directory=directory / "audio")
        self.addCleanup(self.c.executor.shutdown, wait=False)
        self.win = MainWindow(controller=self.c)
        self.page = self.win.pages["about"]
        self.win.show()
        self.addCleanup(self.win.hide)
        self.qt.processEvents()

    def wait_loaded(self):
        for _ in range(100):
            if not self.page.loading.isVisible():
                return
            QTest.qWait(30)
        self.fail("still loading")

    def test_no_network_until_page_is_opened_and_once_only(self):
        self.server.responses["/contributors"] = (200, b"[]")
        for name in ("capture", "train", "live", "device"):
            self.win.navigate(name)
        QTest.qWait(100)
        self.assertEqual(self.server.requests, [])
        self.win.nav_buttons["about"].click()
        self.assertTrue(self.page.loading.isVisible())
        self.assertTrue(self.page.loading.spinner.animating)
        self.assertFalse(self.page.refresh.isEnabled())
        self.wait_loaded()
        self.win.navigate("capture")
        self.win.navigate("about")
        QTest.qWait(100)
        self.assertEqual(len(self.server.requests), 1)

    def test_versions_come_from_installed_metadata(self):
        from importlib import metadata
        self.assertEqual(self.page.app_version.value.text(), metadata.version("hmm-gesture-studio"))
        self.assertEqual(self.page.sdk_version.value.text(), metadata.version("OpenZilo"))

    def test_repository_links_open_both_projects(self):
        for link in self.page.repository_links:
            link.click()
        self.assertEqual([call.args[0] for call in self.dialogs.open_url.call_args_list],
                         ["https://github.com/ziloai/OpenZilo-HMM", "https://github.com/ziloai/OpenZilo"])

    def test_contributors_show_name_and_circular_avatar_and_open_profile(self):
        self.server.responses["/contributors"] = (200, json.dumps([ALICE]).encode())
        self.server.responses["/avatar/alice"] = (200, png_bytes("#ff0000", 32))
        self.use_local_avatar()
        self.win.navigate("about")
        self.wait_loaded()
        tile = self.page.grid.tiles[0]
        self.assertEqual(tile.name.text(), "alice")
        self.assert_tile_fully_visible(tile)
        for _ in range(100):
            if tile.avatar.image is not None:
                break
            QTest.qWait(30)
        QTest.qWait(300)
        rendered = tile.avatar.grab().toImage()
        center = rendered.pixelColor(rendered.width() // 2, rendered.height() // 2)
        self.assertGreater(center.red(), 240)
        self.assertLess(center.green(), 40)
        corner = rendered.pixelColor(1, 1)
        self.assertFalse(corner.red() > 240 and corner.green() < 40, "corners are clipped to a circle")
        QTest.mouseClick(tile, Qt.MouseButton.LeftButton)
        self.dialogs.open_url.assert_called_once_with("https://github.com/alice")

    def assert_tile_fully_visible(self, tile):
        """Actual painted area: geometry of the tile alone once hid a clipped avatar."""
        self.qt.processEvents()
        self.assertEqual((tile.width(), tile.height()), (theme.CONTRIBUTOR_TILE, theme.CONTRIBUTOR_TILE))
        self.assertEqual(tile.avatar.visibleRegion().boundingRect().size(), tile.avatar.size())
        self.assertGreater(tile.name.height(), 0)
        self.assertEqual(tile.name.visibleRegion().boundingRect().size(), tile.name.size())

    def test_placeholder_shows_initial_without_image(self):
        avatar = Avatar("alice")
        avatar.show()
        self.assertIsNone(avatar.image)
        rendered = avatar.grab().toImage()
        self.assertNotEqual(rendered.pixelColor(10, avatar.height() // 2).name(), "#000000")
        avatar.close()

    def test_grid_height_follows_rows_at_any_width(self):
        people = [dict(ALICE, login=f"user{i}", html_url=f"https://github.com/user{i}") for i in range(25)]
        self.server.responses["/contributors"] = (200, json.dumps(people).encode())
        self.win.navigate("about")
        self.wait_loaded()
        self.qt.processEvents()
        wide = self.page.grid.height()
        self.win.resize(1100, 720)
        self.qt.processEvents()
        self.assertGreaterEqual(self.page.grid.height(), wide)
        for tile in self.page.grid.tiles:
            self.assertLessEqual(tile.y() + tile.height(), self.page.grid.height())

    def test_failure_shows_message_and_refresh_recovers_keeping_old_list_on_failure(self):
        self.server.responses["/contributors"] = (403, b"{}")
        self.win.navigate("about")
        self.wait_loaded()
        self.assertTrue(self.page.message.isVisible())
        self.assertIn("上限", self.page.message.text())
        self.assertTrue(self.page.refresh.isEnabled())
        self.server.responses["/contributors"] = (200, json.dumps([ALICE]).encode())
        self.page.refresh.click()
        self.assertFalse(self.page.message.isVisible())
        self.wait_loaded()
        self.assertEqual(len(self.page.grid.tiles), 1)
        self.server.responses["/contributors"] = (500, b"{}")
        self.page.refresh.click()
        self.wait_loaded()
        self.assertEqual(len(self.page.grid.tiles), 1)
        self.assertIn("500", self.page.message.text())

    def test_empty_list_says_so(self):
        self.server.responses["/contributors"] = (200, b"[]")
        self.win.navigate("about")
        self.wait_loaded()
        self.assertIn("暂无", self.page.message.text())

    def test_many_contributors_wrap_within_the_page(self):
        people = [dict(ALICE, login=f"user{i}", html_url=f"https://github.com/user{i}") for i in range(25)]
        self.server.responses["/contributors"] = (200, json.dumps(people).encode())
        self.win.resize(1100, 720)
        self.win.navigate("about")
        self.wait_loaded()
        self.qt.processEvents()
        self.assertLessEqual(self.page.widget().minimumSizeHint().width(), self.page.viewport().width())
        right = max(t.mapTo(self.page.grid, t.rect().topRight()).x() for t in self.page.grid.tiles)
        self.assertLessEqual(right, self.page.grid.width())
        self.assertGreater(len({t.y() for t in self.page.grid.tiles}), 1)


if __name__ == "__main__":
    unittest.main()
