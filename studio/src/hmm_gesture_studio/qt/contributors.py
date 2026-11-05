# SPDX-License-Identifier: MPL-2.0
"""Fetch the public contributor list from GitHub. Sends no device or gesture data.

Responses are untrusted: only https links on the expected hosts are kept, so a
malformed response can neither open an arbitrary URL nor download from elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

API_URL = "https://api.github.com/repos/ziloai/OpenZilo-HMM/contributors?per_page=100"
AVATAR_HOST = "avatars.githubusercontent.com"
PROFILE_HOST = "github.com"
AVATAR_PIXELS = 144  # Enough for the displayed circle on a 2x screen.
TIMEOUT_MS = 15000
MAX_LIST_BYTES = 2 * 1024 * 1024
MAX_AVATAR_BYTES = 5 * 1024 * 1024
_LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


@dataclass(frozen=True)
class Contributor:
    login: str
    avatar_url: str
    profile_url: str


def _https_on(url, host):
    if not isinstance(url, str):
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == host


def parse_contributors(data) -> list[Contributor]:
    """Return people in GitHub's order, skipping bots and malformed entries."""
    entries = json.loads(bytes(data).decode("utf-8"))
    if not isinstance(entries, list):
        raise ValueError("unexpected contributors response")
    result = []
    for entry in entries:
        if (isinstance(entry, dict) and entry.get("type") == "User"
                and isinstance(entry.get("login"), str) and _LOGIN.fullmatch(entry["login"])
                and _https_on(entry.get("avatar_url"), AVATAR_HOST)
                and _https_on(entry.get("html_url"), PROFILE_HOST)):
            result.append(Contributor(entry["login"], entry["avatar_url"], entry["html_url"]))
    return result


def failure_text(status, network_error=None):
    if status in (403, 429):
        return "GitHub 请求次数已达上限，请稍后重试。"
    if status == 404:
        return "无法访问贡献者列表：GitHub 上找不到该仓库。"
    if status:
        return f"GitHub 返回错误（HTTP {status}），请稍后重试。"
    return f"无法连接 GitHub，请检查网络后重试。{network_error or ''}".rstrip()


class ContributorFetcher(QObject):
    """Main-thread and asynchronous; a new fetch supersedes any earlier one."""

    loaded = Signal(list)
    failed = Signal(str)
    avatarLoaded = Signal(str, QImage)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._manager = None
        self._replies = []

    def _get(self, url, limit, on_done):
        if self._manager is None:
            self._manager = QNetworkAccessManager(self)
        request = QNetworkRequest(QUrl(url))
        request.setHeader(QNetworkRequest.KnownHeaders.UserAgentHeader, "hmm-gesture-studio")
        request.setRawHeader(b"Accept", b"application/json, image/*")
        request.setTransferTimeout(TIMEOUT_MS)
        reply = self._manager.get(request)
        self._replies.append(reply)

        def progress(received, _total):
            if received > limit:
                reply.abort()

        def finished():
            if reply in self._replies:
                self._replies.remove(reply)
                on_done(reply)
            reply.deleteLater()
        reply.downloadProgress.connect(progress)
        reply.finished.connect(finished)

    def fetch(self):
        for reply in self._replies[:]:
            self._replies.remove(reply)  # Aborted replies must not report back.
            reply.abort()
        self._get(API_URL, MAX_LIST_BYTES, self._list_finished)

    def _list_finished(self, reply):
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if reply.error() != QNetworkReply.NetworkError.NoError or status != 200:
            self.failed.emit(failure_text(status, ""))
            return
        try:
            contributors = parse_contributors(reply.readAll().data())
        except ValueError:  # Includes JSON and Unicode decoding errors.
            self.failed.emit("GitHub 返回的数据无法解析，请稍后重试。")
            return
        self.loaded.emit(contributors)
        for contributor in contributors:
            separator = "&" if "?" in contributor.avatar_url else "?"
            self._get(f"{contributor.avatar_url}{separator}s={AVATAR_PIXELS}", MAX_AVATAR_BYTES,
                      lambda reply, login=contributor.login: self._avatar_finished(login, reply))

    def _avatar_finished(self, login, reply):
        # A missing avatar is not an error: the tile keeps its initial.
        if reply.error() == QNetworkReply.NetworkError.NoError:
            image = QImage.fromData(reply.readAll().data())
            if not image.isNull():
                self.avatarLoaded.emit(login, image)
