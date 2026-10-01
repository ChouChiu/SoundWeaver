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
    "lfe": "低频效果 (LFE)",
    "front": "前置主声道",
    "front_no_center": "前置声道（消除中置）",
    "height": "顶部声道缩混",
    "height_front": "顶部前置声道",
    "height_rear": "顶部后置声道",
    "downmix": "立体声下混",
    "low": "低频段",
    "mid_band": "中频段",
    "high": "高频段",
    "instrumental": "伴奏信号",
    "side": "侧边差分 (L-R)",
    "mid": "中间共模 (L+R)",
}

STEM_HINTS = {
    "surround": "左/右环绕声道（含分摊中置成分）",
    "center": "中置独立声道",
    "lfe": "超低音声道 (Low-Frequency Effects)",
    "front": "左/右前置主声道",
    "front_no_center": "左/右前置声道（频谱抵消中置成分）",
    "height": "四个顶部声道立体声缩混 (Tfl/Tfr/Tbl/Tbr)",
    "height_front": "左/右顶部前置声道 (Tfl / Tfr)",
    "height_rear": "左/右顶部后置声道 (Tbl / Tbr)",
    "downmix": "全声道立体声下混 (Stereo Downmix)",
    "low": "分频点以下频段信号",
    "mid_band": "分频点至 4 kHz 频段信号",
    "high": "分频点以上频段信号",
    "instrumental": "消除中置后的立体声伴奏",
    "side": "左右声道差分信号 (Side)",
    "mid": "左右声道求和信号 (Mid)",
}


def stem_label(kind: str) -> str:
    return STEM_NAMES.get(kind, kind)


def stem_hint(kind: str) -> str:
    return STEM_HINTS.get(kind, "")
