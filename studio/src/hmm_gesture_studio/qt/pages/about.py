# SPDX-License-Identifier: MPL-2.0
"""Versions, source repositories and contributors."""
from __future__ import annotations

from importlib import metadata

from PySide6.QtCore import QRectF, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPainterPath
from PySide6.QtWidgets import QGridLayout, QPushButton, QVBoxLayout, QWidget

from .. import theme
from ..contributors import ContributorFetcher
from ..motion import Busy
from ..widgets import Card, Page, StatTile, button, label, row, set_text, set_tone

REPOSITORIES = (("OpenZilo-HMM", "手势识别与训练平台", "https://github.com/ziloai/OpenZilo-HMM"),
                ("OpenZilo", "戒指 SDK", "https://github.com/ziloai/OpenZilo"))
_TILE_WIDTH = _TILE_HEIGHT = theme.CONTRIBUTOR_TILE
_AVATAR_SIZE = 64


def package_version(name):
    """Read installed metadata without importing the package; never guess."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "未知"


class Avatar(QWidget):
    """Circular image; shows the name's initial until the image arrives."""

    def __init__(self, login):
        super().__init__()
        self.setFixedSize(QSize(_AVATAR_SIZE, _AVATAR_SIZE))
        self._initial = login[:1].upper()
        self.image = None
        self._opacity = 1.0
        self._fade = QVariantAnimation(self)
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.setDuration(theme.MOTION)
        self._fade.valueChanged.connect(self._set_opacity)

    def set_image(self, image):
        self.image = image
        self._fade.start()

    def _set_opacity(self, value):
        self._opacity = float(value)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addEllipse(QRectF(self.rect()))
        painter.setClipPath(path)
        painter.fillPath(path, QColor(theme.CONTROL_GRAY))
        painter.setPen(QColor(theme.SLATE))
        font = painter.font()
        font.setPixelSize(24)
        font.setWeight(font.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._initial)
        if self.image is not None:
            painter.setOpacity(self._opacity)
            # Center-crop so a non-square image is not distorted.
            side = min(self.image.width(), self.image.height())
            source = QRectF((self.image.width() - side) / 2, (self.image.height() - side) / 2, side, side)
            painter.drawImage(QRectF(self.rect()), self.image, source)
        painter.end()


class ContributorTile(QPushButton):
    """Avatar over user name; opens the GitHub profile."""

    def __init__(self, contributor, on_open):
        super().__init__()
        self.contributor = contributor
        self.setObjectName("contributor")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(contributor.login)
        self.clicked.connect(lambda _checked=False: on_open(contributor.profile_url))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 8, 4, 6)
        layout.setSpacing(8)
        self.avatar = Avatar(contributor.login)
        layout.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignHCenter)
        self.name = label()
        self.name.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.name.setText(QFontMetrics(self.name.font()).elidedText(
            contributor.login, Qt.TextElideMode.ElideRight, _TILE_WIDTH - 8))
        layout.addWidget(self.name)
        for child in (self.avatar, self.name):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)


class ContributorGrid(QWidget):
    """Wraps tiles to the available width."""

    def __init__(self):
        super().__init__()
        self.tiles = []
        self._columns = 0
        self._layout = QGridLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setFixedHeight(0)

    def set_tiles(self, tiles):
        for tile in self.tiles:
            self._layout.removeWidget(tile)
            tile.deleteLater()
        self.tiles = tiles
        self._columns = 0
        self._arrange()

    def _arrange(self):
        columns = max(1, (self.width() + 8) // (_TILE_WIDTH + 8))
        if columns == self._columns:
            return
        self._columns = columns
        for index, tile in enumerate(self.tiles):
            self._layout.addWidget(tile, index // columns, index % columns)
        # The row count depends on width, which a layout's minimum height cannot express.
        rows = (len(self.tiles) + columns - 1) // columns
        self.setFixedHeight(max(0, rows * (_TILE_HEIGHT + 8) - 8))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._arrange()


class AboutPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        self._requested = False
        self.fetcher = ContributorFetcher(self)
        top = self.add_section()
        self.header(top, "关于", "OpenZilo 手势与录音工作室")
        self.app_version = StatTile("应用版本", package_version("hmm-gesture-studio"))
        self.sdk_version = StatTile("OpenZilo SDK", package_version("OpenZilo"))
        top.add(row(self.app_version, self.sdk_version, spacing=64))

        more = self.add_section()
        more.add(label("GitHub 仓库", "section"))
        repositories = Card()
        more.add(repositories)
        self.repository_links = []
        for name, description, url in REPOSITORIES:
            link = button(url.removeprefix("https://") + " ›", lambda url=url: controller.open_url(url), kind="link")
            self.repository_links.append(link)
            line = row(label(name, "strong"), label(description, tone="muted"))
            line.addWidget(link)
            repositories.add(line)

        people = self.add_section()
        self.refresh = button("刷新", self.load)
        people.add(row(label("贡献者", "section"), self.refresh))
        card = Card()
        people.add(card)
        self.loading = Busy("正在从 GitHub 获取…")
        card.add(self.loading)
        self.message = label(tone="muted", wrap=True)
        self.message.hide()
        card.add(self.message)
        self.grid = ContributorGrid()
        card.add(self.grid)
        self.finish()
        self.fetcher.loaded.connect(self._loaded)
        self.fetcher.failed.connect(self._failed)
        self.fetcher.avatarLoaded.connect(self._avatar_loaded)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._requested:  # No network traffic until the page is opened.
            self.load()

    def load(self):
        self._requested = True
        self.refresh.setEnabled(False)
        self.loading.set_busy(True)
        self.message.hide()
        self.fetcher.fetch()

    def _finish_loading(self):
        self.refresh.setEnabled(True)
        self.loading.set_busy(False)

    def _show_message(self, text, tone):
        set_text(self.message, text)
        set_tone(self.message, tone)
        self.message.show()

    def _loaded(self, contributors):
        self._finish_loading()
        self.grid.set_tiles([ContributorTile(c, self.controller.open_url) for c in contributors])
        if not contributors:
            self._show_message("暂无贡献者信息。", "muted")

    def _failed(self, text):
        # Keep anything shown earlier: stale names beat an empty card.
        self._finish_loading()
        self._show_message(text, "warning")

    def _avatar_loaded(self, login, image):
        for tile in self.grid.tiles:
            if tile.contributor.login == login:
                tile.avatar.set_image(image)
