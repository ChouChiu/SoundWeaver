"""Stem kinds, which ones each channel layout splits into, and their Chinese names.

Shared by every feature that lists tracks; the split itself lives in
`features/stems`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path


class StemKind(StrEnum):
    """One stem, named by the marker its file carries.

    Markers stay English: a file name that changed with the interface
    language would rename the same stem on the next run.
    """

    CENTER = "center"
    INSTRUMENTAL = "instrumental"
    SIDE = "side"
    MID = "mid"
    LOW = "low"
    MID_BAND = "mid_band"
    HIGH = "high"
    SURROUND = "surround"
    LFE = "lfe"
    FRONT = "front"
    FRONT_NO_CENTER = "front_no_center"
    HEIGHT = "height"
    HEIGHT_FRONT = "height_front"
    HEIGHT_REAR = "height_rear"
    DOWNMIX = "downmix"


class StemLayout(StrEnum):
    """Which path the channel count of the input picked."""

    ATMOS = "atmos"
    SURROUND = "surround"
    STEREO = "stereo"
    MONO = "mono"


_LAYOUT_STEMS: dict[StemLayout, tuple[StemKind, ...]] = {
    StemLayout.ATMOS: (
        StemKind.SURROUND,
        StemKind.CENTER,
        StemKind.LFE,
        StemKind.FRONT,
        StemKind.FRONT_NO_CENTER,
        StemKind.HEIGHT,
        StemKind.HEIGHT_FRONT,
        StemKind.HEIGHT_REAR,
        StemKind.DOWNMIX,
        StemKind.LOW,
        StemKind.HIGH,
    ),
    StemLayout.SURROUND: (
        StemKind.SURROUND,
        StemKind.CENTER,
        StemKind.LFE,
        StemKind.FRONT,
        StemKind.FRONT_NO_CENTER,
        StemKind.DOWNMIX,
        StemKind.LOW,
        StemKind.HIGH,
    ),
    StemLayout.STEREO: (
        StemKind.CENTER,
        StemKind.INSTRUMENTAL,
        StemKind.SIDE,
        StemKind.MID,
        StemKind.LOW,
        StemKind.HIGH,
    ),
    StemLayout.MONO: (StemKind.LOW, StemKind.MID_BAND, StemKind.HIGH),
}

# Six channels is where a discrete surround master starts.  Anything between
# three and five is a layout the split has no decode matrix for, so it is
# folded to its first two channels and split as stereo rather than guessed at.
# Ten is a 5.1.4 render: the 5.1 floor followed by four height channels, which
# is what a Dolby Atmos source is decoded to.
SURROUND_CHANNELS = 6
ATMOS_CHANNELS = 10


def layout_for(channels: int) -> StemLayout:
    if channels == ATMOS_CHANNELS:
        return StemLayout.ATMOS
    if channels >= SURROUND_CHANNELS:
        return StemLayout.SURROUND
    return StemLayout.MONO if channels == 1 else StemLayout.STEREO


def layout_stems(layout: StemLayout) -> tuple[StemKind, ...]:
    return _LAYOUT_STEMS[layout]


def stem_path(source: Path, output_dir: Path, kind: StemKind) -> Path:
    return output_dir / f"{source.stem}_{kind.value}.wav"


STEM_NAMES = {
    "surround": "环绕声道",
    "center": "中置声道",
    "lfe": "低频效果",
    "front": "前置声道",
    "front_no_center": "前置去中置",
    "height": "顶部声道",
    "height_front": "顶部前置",
    "height_rear": "顶部后置",
    "downmix": "立体声缩混",
    "low": "低频段",
    "mid_band": "中频段",
    "high": "高频段",
    "instrumental": "伴奏",
    "side": "侧边信号",
    "mid": "中间信号",
}

STEM_HINTS = {
    "surround": "左右环绕声道，已分摊中置成分",
    "center": "中置声道原样提取，通常是人声",
    "lfe": "低频效果声道 (LFE)",
    "front": "前置左右声道原样提取",
    "front_no_center": "前置左右声道，频谱上抵消中置泄漏",
    "height": "四个顶部声道缩混为立体声",
    "height_front": "顶部前置左右声道原样提取",
    "height_rear": "顶部后置左右声道原样提取",
    "downmix": "全部声道缩混为立体声",
    "low": "缩混后按分频点切出的低频部分",
    "mid_band": "分频点到 4 kHz 之间",
    "high": "缩混后按分频点切出的高频部分",
    "instrumental": "去掉中置后的立体声",
    "side": "左右声道之差",
    "mid": "左右声道之和",
}


def stem_label(kind: str) -> str:
    return STEM_NAMES.get(kind, kind)


def stem_hint(kind: str) -> str:
    return STEM_HINTS.get(kind, "")
