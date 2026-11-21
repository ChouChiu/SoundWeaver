"""Unified spatial codec engines (Audio Vivid / AV3A, MPEG-H / 360RA, and native subprocess helpers)."""

from __future__ import annotations

from dataclasses import dataclass

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
        "av3a": EngineStatus("av3a", "soundweaver_av3a", av3a.find_decoder()),
        "vivid_encode": EngineStatus("vivid_encode", "vivid_encode", av3a.find_encoder()),
        "mpegh": EngineStatus("mpegh", "soundweaver_mpegh", mpegh.find_decoder()),
    }


__all__ = [
    "EngineStatus",
    "av3a",
    "available_engines",
    "mpegh",
    "native",
]
