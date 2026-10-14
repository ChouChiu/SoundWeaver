"""Stem kinds, which ones each channel layout splits into, and their Chinese names.

Shared by every feature that lists tracks; the split itself lives in
`features/stems`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from dolbytool.core.speakers import SpeakerLayout


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
    BACK = "back"
    WIDE = "wide"
    LFE = "lfe"
    FRONT = "front"
    FRONT_NO_CENTER = "front_no_center"
    HEIGHT = "height"
    HEIGHT_FRONT = "height_front"
    HEIGHT_REAR = "height_rear"
    HEIGHT_SIDE = "height_side"
    DOWNMIX = "downmix"


class StemLayout(StrEnum):
    """Which path the input picked: a Dolby Atmos render, a surround master, or neither."""

    ATMOS = "atmos"
    SURROUND = "surround"
    STEREO = "stereo"
    MONO = "mono"


_STEREO_STEMS = (
    StemKind.CENTER,
    StemKind.INSTRUMENTAL,
    StemKind.SIDE,
    StemKind.MID,
    StemKind.LOW,
    StemKind.HIGH,
)
_MONO_STEMS = (StemKind.LOW, StemKind.MID_BAND, StemKind.HIGH)

# Six channels is where a discrete surround master starts.  Anything between
# three and five is a layout the split has no decode matrix for, so it is
# folded to its first two channels and split as stereo rather than guessed at.
SURROUND_CHANNELS = 6


def layout_for(channels: int, atmos: bool = False) -> StemLayout:
    if atmos:
        return StemLayout.ATMOS
    if channels >= SURROUND_CHANNELS:
        return StemLayout.SURROUND
    return StemLayout.MONO if channels == 1 else StemLayout.STEREO


def surround_stems(speakers: SpeakerLayout) -> tuple[StemKind, ...]:
    """What a discrete master of `speakers` splits into: every pair it has, then the folds."""
    kinds = [
        StemKind.FRONT,
        StemKind.CENTER,
    ]
    if speakers.has("Ls", "Rs"):
        kinds.append(StemKind.SURROUND)
    kinds += [
        StemKind.LFE,
        StemKind.INSTRUMENTAL,
        StemKind.FRONT_NO_CENTER,
    ]
    if speakers.has("Lb", "Rb"):
        kinds.append(StemKind.BACK)
    if speakers.has("Lw", "Rw"):
        kinds.append(StemKind.WIDE)
    if speakers.heights:
        kinds.append(StemKind.HEIGHT)
        if speakers.has("Tfl", "Tfr", "Tbl", "Tbr"):
            kinds += [StemKind.HEIGHT_FRONT, StemKind.HEIGHT_REAR]
        if speakers.has("Tsl", "Tsr"):
            kinds.append(StemKind.HEIGHT_SIDE)
    kinds += [StemKind.DOWNMIX, StemKind.LOW, StemKind.HIGH]
    return tuple(kinds)


def layout_stems(layout: StemLayout, speakers: SpeakerLayout | None) -> tuple[StemKind, ...]:
    if layout is StemLayout.MONO:
        return _MONO_STEMS
    if layout is StemLayout.STEREO or speakers is None:
        return _STEREO_STEMS
    return surround_stems(speakers)


def stem_path(source: Path, output_dir: Path, kind: StemKind) -> Path:
    return output_dir / f"{source.stem}_{kind.value}.wav"


def stem_label(kind: str) -> str:
    from dolbytool.core.i18n import t

    key = f"stem.{kind}"
    res = t(key)
    return res if res != key else STEM_NAMES.get(kind, kind)


def stem_hint(kind: str) -> str:
    from dolbytool.core.i18n import t

    key = f"stem.hint.{kind}"
    res = t(key)
    return res if res != key else STEM_HINTS.get(kind, "")


STEM_NAMES = {
    "surround": "环绕声道",
    "back": "后环绕声道",
    "wide": "前宽声道",
    "center": "中置声道",
    "lfe": "低频效果 (LFE)",
    "front": "前置主声道",
    "front_no_center": "前置声道（消除中置）",
    "height": "顶部声道缩混",
    "height_front": "顶部前置声道",
    "height_rear": "顶部后置声道",
    "height_side": "顶部侧置声道",
    "downmix": "立体声下混",
    "low": "低频段",
    "mid_band": "中频段",
    "high": "高频段",
    "instrumental": "伴奏信号",
    "side": "侧边差分 (L-R)",
    "mid": "中间共模 (L+R)",
}

STEM_HINTS = {
    "surround": "左/右离散环绕声道 (Ls / Rs)",
    "back": "左/右后环绕声道 (Lb / Rb)",
    "wide": "左/右前宽声道 (Lw / Rw)",
    "center": "中置独立声道",
    "lfe": "超低音声道 (Low-Frequency Effects)",
    "front": "左/右前置主声道",
    "front_no_center": "左/右前置声道（相干抵消中置成分）",
    "height": "全部顶部声道的立体声缩混",
    "height_front": "左/右顶部前置声道 (Tfl / Tfr)",
    "height_rear": "左/右顶部后置声道 (Tbl / Tbr)",
    "height_side": "左/右顶部侧置声道 (Tsl / Tsr)",
    "downmix": "全声道立体声下混 (Stereo Downmix)",
    "low": "分频点以下频段信号",
    "mid_band": "分频点至 4 kHz 频段信号",
    "high": "分频点以上频段信号",
    "instrumental": "消除中置后的立体声伴奏",
    "side": "左右声道差分信号 (Side)",
    "mid": "左右声道求和信号 (Mid)",
}

