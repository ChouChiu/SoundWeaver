"""Unified spatial codec engines (Audio Vivid / AV3A, MPEG-H / 360RA, and native subprocess helpers)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from soundweaver.core.codecs import av3a, mpegh, native


@dataclass(frozen=True)
class EngineStatus:
    """Availability of a bundled or PATH native binary."""

    key: str
    binary: str
    path: str | None

    @property
    def available(self) -> bool:
        return self.path is not None


def available_engines() -> dict[str, EngineStatus]:
    """Inspect availability of all bundled spatial audio native tools."""
    return {
        "vivid": EngineStatus("vivid", "vivid", av3a.find_decoder()),
        "vivid_encode": EngineStatus("vivid_encode", "vivid_encode", av3a.find_encoder()),
        "vivid_binaural": EngineStatus("vivid_binaural", "vivid_binaural", av3a.find_binaural_renderer()),
        "mpegh": EngineStatus("mpegh", "ia_mpeghd_testbench", mpegh.find_decoder()),
    }


def detect_format(path: Path) -> str | None:
    """Return `'av3a'` or `'mpegh'` when `path` is a supported spatial bitstream/container."""
    if av3a.is_av3a_file(path):
        return "av3a"
    if mpegh.is_mpegh_file(path):
        return "mpegh"
    return None


__all__ = [
    "EngineStatus",
    "av3a",
    "available_engines",
    "detect_format",
    "mpegh",
    "native",
]
