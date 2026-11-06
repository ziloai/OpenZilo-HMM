# SPDX-License-Identifier: MPL-2.0
"""Presentation-only translations; source strings and format values stay separate.

Do not translate protocol identifiers, user input or paths. Use tr() at the
point where application-owned text is composed, and resolve() at Qt boundaries.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QLocale, QObject, QSettings, Signal, Slot


def system_language() -> str:
    """Chinese system locales (including Traditional Chinese) use Chinese.

    All other locales fall back to English; OS UI language, not number/date
    formatting, determines the default.
    """
    languages = QLocale.system().uiLanguages()
    language = languages[0] if languages else QLocale.system().name()
    return "zh" if language.lower().replace("_", "-").split("-")[0] == "zh" else "en"


class LanguageManager(QObject):
    changed = Signal()
    preferences = ("system", "en", "zh")

    def __init__(self):
        super().__init__()
        self.preference = "system"
        self.language = system_language()
        self.settings = None

    def initialize(self, settings=None):
        """Load preferences once at application startup, never in core inference."""
        self.settings = settings if settings is not None else QSettings("OpenZilo", "Studio")
        self.set_preference(self.settings.value("ui/language", "system"), persist=False)

    def set_preference(self, preference, *, persist=True):
        # Invalid/old preferences recover to the OS language.
        preference = preference if preference in self.preferences else "system"
        language = system_language() if preference == "system" else preference
        changed = (preference, language) != (self.preference, self.language)
        self.preference, self.language = preference, language
        if persist and self.settings is not None:
            self.settings.setValue("ui/language", preference)
            self.settings.sync()
        if changed:
            self.changed.emit()


language_manager = LanguageManager()
_catalog = None


def catalog() -> dict[str, str]:
    global _catalog
    if _catalog is None:
        _catalog = {}
        for path in sorted(Path(__file__).with_name("locales").glob("*_en.json")):
            _catalog.update(json.loads(path.read_text(encoding="utf-8")))
    return _catalog


class Text(str):
    """A string compatible with existing controller APIs, with its source intact.

    Qt consumes the underlying str, so always resolve at display boundaries.
    Nested translated values also survive language changes; user values are
    formatted as data, never looked up as translation keys.
    """

    def __new__(cls, source, values):
        obj = super().__new__(cls, _format(source, values))
        obj.source = source
        obj.values = values
        return obj

    def translated(self):
        return _format(self.source, self.values)

    def __str__(self):
        return self.translated()


def _format(source, values):
    template = catalog().get(source, source) if language_manager.language == "en" else source
    return template.format(**{key: resolve(value) for key, value in values.items()}) if values else template


def tr(source: str, **values) -> Text:
    if isinstance(source, Text) and not values:
        return source
    return Text(source, values)


def resolve(value):
    if isinstance(value, Text):
        return value.translated()
    if isinstance(value, (tuple, list)):
        return type(value)(resolve(item) for item in value)
    return value


class _TextBindings(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.widget = widget
        self.values = {}
        self.shown = {}
        language_manager.changed.connect(self.refresh)

    def set(self, method, value):
        self.values[method] = value
        self._apply(method, value)

    def _apply(self, method, value):
        text = resolve(value)
        if self.shown.get(method) != text:
            getattr(self.widget, method)(text)
            self.shown[method] = text

    @Slot()
    def refresh(self):
        for method, value in self.values.items():
            self._apply(method, value)


def bind_text(widget, text, method="setText"):
    """Keep a widget's presentation updated without rebuilding it or its state."""
    bindings = getattr(widget, "_i18n_bindings", None)
    if bindings is None:
        bindings = _TextBindings(widget)
        widget._i18n_bindings = bindings
    bindings.set(method, text)
