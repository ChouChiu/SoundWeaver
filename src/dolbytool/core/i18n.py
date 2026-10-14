"""Internationalization (i18n) module for DolbyTool.

Loads localization bundles from JSON resource files in `dolbytool/locales/`:
- zh_cn.json (Simplified Chinese)
- zh_tw.json (Traditional Chinese)
- en.json    (English)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

LANG_ZH_CN: Final = "zh_cn"
LANG_ZH_TW: Final = "zh_tw"
LANG_EN: Final = "en"

SUPPORTED_LANGUAGES: Final = (LANG_ZH_CN, LANG_ZH_TW, LANG_EN)

LANGUAGE_NAMES: Final = {
    LANG_ZH_CN: "简体中文",
    LANG_ZH_TW: "繁體中文",
    LANG_EN: "English",
}

LOCALES_DIR: Final = Path(__file__).resolve().parent.parent / "locales"


class I18nManager:
    """Manages language bundles loaded from JSON files and string lookups."""

    def __init__(self, default_lang: str = LANG_ZH_CN, locales_dir: Path = LOCALES_DIR) -> None:
        self._current_lang = default_lang
        self._locales_dir = locales_dir
        self._cache: dict[str, dict[str, str]] = {}
        self._listeners: list[Callable[[str], None]] = []

    @property
    def language(self) -> str:
        return self._current_lang

    def set_language(self, lang: str) -> None:
        if lang not in SUPPORTED_LANGUAGES:
            lang = LANG_ZH_CN
        if self._current_lang != lang:
            self._current_lang = lang
            self._load_bundle(lang)
            for listener in list(self._listeners):
                try:
                    listener(lang)
                except Exception as e:
                    logger.exception("Error in i18n listener: %s", e)

    def add_listener(self, callback: Callable[[str], None]) -> None:
        if callback not in self._listeners:
            self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[str], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _load_bundle(self, lang: str) -> dict[str, str]:
        if lang in self._cache:
            return self._cache[lang]
        file_path = self._locales_dir / f"{lang}.json"
        if file_path.is_file():
            try:
                content = file_path.read_text(encoding="utf-8")
                bundle = json.loads(content)
                if isinstance(bundle, dict):
                    self._cache[lang] = bundle
                    return bundle
            except Exception as e:
                logger.error("Failed to load locale %s from %s: %s", lang, file_path, e)
        self._cache[lang] = {}
        return self._cache[lang]

    def t(self, key: str, **kwargs) -> str:
        """Translate a key into current language with optional formatting."""
        bundle = self._load_bundle(self._current_lang)
        val = bundle.get(key)
        if val is None:
            # Fallback to zh_cn if current language has no entry
            if self._current_lang != LANG_ZH_CN:
                zh_bundle = self._load_bundle(LANG_ZH_CN)
                val = zh_bundle.get(key)
            if val is None:
                val = key

        if kwargs:
            try:
                return val.format(**kwargs)
            except Exception:
                return val
        return val


_MANAGER = I18nManager()


def get_language() -> str:
    return _MANAGER.language


def set_language(lang: str) -> None:
    _MANAGER.set_language(lang)


def add_language_listener(callback: Callable[[str], None]) -> None:
    _MANAGER.add_listener(callback)


def remove_language_listener(callback: Callable[[str], None]) -> None:
    _MANAGER.remove_listener(callback)


def t(key: str, **kwargs) -> str:
    return _MANAGER.t(key, **kwargs)
