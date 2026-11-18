"""Source feature facade delegating media inspection and artwork discovery to `core.sources`."""

from __future__ import annotations

from soundweaver.core.sources import (
    AUDIO_SUFFIXES,
    IMAGE_SUFFIXES,
    TTML_SUFFIXES,
    Inspection,
    find_lyrics,
    folder_cover,
    inspect_source as inspect,
    is_audio,
    kind_of,
)

__all__ = [
    "AUDIO_SUFFIXES",
    "IMAGE_SUFFIXES",
    "TTML_SUFFIXES",
    "Inspection",
    "find_lyrics",
    "folder_cover",
    "inspect",
    "is_audio",
    "kind_of",
]
