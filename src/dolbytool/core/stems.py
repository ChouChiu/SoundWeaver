"""Stem kinds, which ones each channel layout splits into, and their display names.

Shared by every feature that lists tracks; the split itself lives in
`features/stems`.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from dolbytool.core.i18n import t
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
    """What a discrete master of `speakers` splits into: every pair it has, then the folds.

    Every surround layout has L, R, C, LFE and a surround pair.  A layout with
    one height pair (x.1.2) has the height fold and nothing else overhead,
    since the fold of one pair is that pair.
    """
    kinds = [
        StemKind.FRONT,
        StemKind.CENTER,
        StemKind.SURROUND,
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
    if len(speakers.heights) > 2:
        if speakers.has("Tfl", "Tfr", "Tbl", "Tbr"):
            kinds += [StemKind.HEIGHT_FRONT, StemKind.HEIGHT_REAR]
        if speakers.has("Tsl", "Tsr"):
            kinds.append(StemKind.HEIGHT_SIDE)
    kinds += [StemKind.DOWNMIX, StemKind.LOW, StemKind.HIGH]
    return tuple(kinds)


def layout_stems(layout: StemLayout, speakers: SpeakerLayout | None) -> tuple[StemKind, ...]:
    if layout is StemLayout.MONO:
        return _MONO_STEMS
    if (
        layout is StemLayout.STEREO
        or speakers is None
        or len(speakers.channels) < SURROUND_CHANNELS
    ):
        return _STEREO_STEMS
    return surround_stems(speakers)


def stem_path(source: Path, output_dir: Path, kind: StemKind) -> Path:
    return output_dir / f"{source.stem}_{kind.value}.wav"


def stem_label(kind: str) -> str:
    return t(f"stem.{kind}")


def stem_hint(kind: str) -> str:
    return t(f"stem.hint.{kind}")
