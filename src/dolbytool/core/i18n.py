"""Interface strings in Simplified Chinese, Traditional Chinese and English.

Each language is one JSON file in `dolbytool/locales/` mapping a dotted key to
its text, with `str.format` fields where the text takes values.  A key the
current language lacks falls back to Simplified Chinese, which has them all;
`scripts/check_locales.py` keeps the three files in step.
"""

from __future__ import annotations

import json
import logging
import sys
from functools import cache
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


def _find_locales_dir() -> Path:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "dolbytool" / "locales")
            candidates.append(Path(meipass) / "locales")
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "_internal" / "dolbytool" / "locales")
        candidates.append(exe_dir / "_internal" / "locales")
        candidates.append(exe_dir / "dolbytool" / "locales")
        candidates.append(exe_dir / "locales")
    candidates.append(Path(__file__).resolve().parent.parent / "locales")
    try:
        from importlib import resources

        candidates.append(Path(str(resources.files("dolbytool") / "locales")))
    except (ImportError, TypeError, OSError) as error:
        logger.debug("cannot resolve locales via importlib.resources: %s", error)

    for candidate in candidates:
        if (candidate / f"{LANG_ZH_CN}.json").is_file():
            return candidate
    return candidates[0]


LOCALES_DIR: Final = _find_locales_dir()

_language = LANG_ZH_CN


@cache
def _bundle(lang: str) -> dict[str, str]:
    path = LOCALES_DIR / f"{lang}.json"
    if not path.is_file() and getattr(sys, "frozen", False):
        for candidate in [
            getattr(sys, "_MEIPASS", None) and Path(sys._MEIPASS) / "dolbytool" / "locales",
            getattr(sys, "_MEIPASS", None) and Path(sys._MEIPASS) / "locales",
            Path(sys.executable).resolve().parent / "_internal" / "dolbytool" / "locales",
            Path(sys.executable).resolve().parent / "locales",
        ]:
            if candidate and (candidate / f"{lang}.json").is_file():
                path = candidate / f"{lang}.json"
                break
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("cannot load the %s strings from %s", lang, path)
        return {}
    return bundle


def get_language() -> str:
    return _language


def set_language(lang: str) -> None:
    """Switch the language strings are looked up in; an unknown one means Simplified Chinese."""
    global _language
    _language = lang if lang in SUPPORTED_LANGUAGES else LANG_ZH_CN


def t(key: str, **kwargs: object) -> str:
    """The text of `key` in the current language, its fields filled from `kwargs`."""
    text = _bundle(_language).get(key) or _bundle(LANG_ZH_CN).get(key)
    if text is None:
        logger.warning("no string for %s", key)
        return key
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        logger.warning("string %s does not take %s", key, sorted(kwargs))
        return text
