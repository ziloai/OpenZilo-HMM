# SPDX-License-Identifier: MPL-2.0
"""Application preferences, independent of connected hardware."""
from PySide6.QtCore import QSignalBlocker

from ..i18n import language_manager, resolve, tr
from ..widgets import Card, Combo, Page


class SettingsPage(Page):
    def __init__(self, controller, navigate=None):
        super().__init__(controller, navigate)
        section = self.add_section()
        self.header(section, tr("设置"))
        card = Card(tr("语言"))
        section.add(card)
        self.language = Combo()
        self.language.setMinimumWidth(240)
        self.language.setMaximumWidth(360)
        for preference in language_manager.preferences:
            self.language.addItem("", preference)
        card.add(self.language)
        self.language.currentIndexChanged.connect(self._choose_language)
        self.finish()
        self.retranslate()
        self.render_state()

    def _choose_language(self, index):
        if index >= 0:
            language_manager.set_preference(self.language.itemData(index))

    def retranslate(self):
        with QSignalBlocker(self.language):
            for index, text in enumerate((tr("跟随系统"), "English", "中文")):
                self.language.setItemText(index, resolve(text))
        self.language.setAccessibleName(resolve(tr("界面语言")))

    def render_state(self):
        with QSignalBlocker(self.language):
            self.language.setCurrentIndex(self.language.findData(language_manager.preference))
