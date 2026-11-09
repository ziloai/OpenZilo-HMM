# SPDX-License-Identifier: MPL-2.0
"""Application preferences, independent of connected hardware."""
from PySide6.QtCore import QSignalBlocker

from ..i18n import language_manager, resolve, tr
from ..motion import Busy
from ..widgets import Card, Combo, Page, button, label, row, set_text


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

        storage = Card(tr("缓存与插件数据"))
        section.add(storage)
        storage.add(label(tr("清理所有已安装插件的缓存和日志；也可额外清除插件数据。"), wrap=True))
        storage.add(label(tr("保留插件安装、设置、密钥、绑定和模型，不影响采集数据和宿主录音。"), tone="muted", wrap=True))
        self.clear_cache = button(tr("清理缓存"), controller.plugins.clear_storage)
        self.clear_plugin_data = button(tr("清除插件数据"),
                                        lambda: controller.plugins.clear_storage(include_data=True), kind="danger")
        storage.add(row(self.clear_cache, self.clear_plugin_data))
        self.storage_hint = storage.add(label(tone="muted", wrap=True))
        self.storage_busy = storage.add(Busy(tr("正在清理插件存储…")))
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
        plugins = self.controller.plugins
        self.clear_cache.setEnabled(plugins.can_clear_storage)
        self.clear_plugin_data.setEnabled(plugins.can_clear_storage)
        set_text(self.storage_hint, plugins.storage_cleanup_hint)
        self.storage_hint.setVisible(plugins.busy_text is None)
        self.storage_busy.set_busy(plugins.busy_text is not None, plugins.busy_text)
