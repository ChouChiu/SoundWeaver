"""Speaker layouts a Dolby Atmos track can be rendered to, and a multichannel file can carry.

Each layout is a room of speaker rows: rows on the floor and rows on the
ceiling, each at a depth y (0 front, 1 back) with its speakers spread evenly
from left to right.  The channel order is ffmpeg's for the layout of that
name, which is also WAVE_FORMAT_EXTENSIBLE order, so a render can be read
back with the same indices a multichannel file of that layout has.

Labels are the ones OAMD names bed channels with: in a 5.X layout Ls and Rs
are the surrounds and take the back of the room (TS 103 420 table A.7,
note 2); in a 7.X layout they are the side pair and Lb and Rb the back.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import cached_property

logger = logging.getLogger(__name__)

Row = tuple[float, tuple[str, ...]]

# Room positions (x 0..1 left to right, y 0..1 front to back, z 0..1 up) of
# every bed channel OAMD can name, for a layout that has no speaker of that name.
BED_POSITIONS = {
    "L": (0.0, 0.0, 0.0), "R": (1.0, 0.0, 0.0), "C": (0.5, 0.0, 0.0),
    "LFE": (0.5, 0.0, -1.0),
    "Ls": (0.0, 1.0, 0.0), "Rs": (1.0, 1.0, 0.0),
    "Lb": (0.0, 1.0, 0.0), "Rb": (1.0, 1.0, 0.0),
    "Tfl": (0.0, 0.0, 1.0), "Tfr": (1.0, 0.0, 1.0),
    "Tsl": (0.0, 0.5, 1.0), "Tsr": (1.0, 0.5, 1.0),
    "Tbl": (0.0, 1.0, 1.0), "Tbr": (1.0, 1.0, 1.0),
    "Lw": (0.0, 0.25, 0.0), "Rw": (1.0, 0.25, 0.0),
    "LFE2": (0.5, 0.0, -1.0),
}  # fmt: skip
# With back surrounds in the bed, Ls and Rs are the side pair.
SIDE_POSITIONS = {"Ls": (0.0, 0.5, 0.0), "Rs": (1.0, 0.5, 0.0)}

_STEREO: Row = (0.0, ("L", "R"))
_FRONT: Row = (0.0, ("L", "C", "R"))
_SURROUND: Row = (1.0, ("Ls", "Rs"))
_SIDE: Row = (0.5, ("Ls", "Rs"))
_BACK: Row = (1.0, ("Lb", "Rb"))
_WIDE: Row = (0.25, ("Lw", "Rw"))
_TOP_MIDDLE: Row = (0.5, ("Tsl", "Tsr"))
_TOP_FRONT: Row = (0.0, ("Tfl", "Tfr"))
_TOP_BACK: Row = (1.0, ("Tbl", "Tbr"))


@dataclass(frozen=True)
class SpeakerLayout:
    name: str
    channels: tuple[str, ...]  # in file order, the LFE among them
    floor: tuple[Row, ...]
    top: tuple[Row, ...] = ()

    def __str__(self) -> str:
        return self.name

    @cached_property
    def index(self) -> dict[str, int]:
        return {label: i for i, label in enumerate(self.channels)}

    @cached_property
    def positions(self) -> dict[str, tuple[float, float, float]]:
        """Where each speaker but the LFE stands in the unit room."""
        out = {}
        for z, rows in ((0.0, self.floor), (1.0, self.top)):
            for y, names in rows:
                span = len(names) - 1
                for i, name in enumerate(names):
                    out[name] = (i / span if span else 0.5, y, z)
        return out

    @cached_property
    def surrounds(self) -> tuple[int, ...]:
        """Channels of the floor speakers behind the front row: what a surround trim lowers."""
        return tuple(self.index[name] for y, names in self.floor if y > 0 for name in names)

    @property
    def heights(self) -> tuple[str, ...]:
        return tuple(name for _, names in self.top for name in names)

    def has(self, *labels: str) -> bool:
        return all(label in self.index for label in labels)

    def sides(self) -> tuple[tuple[int, ...], tuple[int, ...]]:
        """Every channel off the front row and its centre, by side: (left, right)."""
        front = {"L", "R", "C", "LFE"}
        left = tuple(
            self.index[n] for n, (x, _, _) in self.positions.items() if n not in front and x < 0.5
        )
        right = tuple(
            self.index[n] for n, (x, _, _) in self.positions.items() if n not in front and x > 0.5
        )
        return left, right


LAYOUTS = (
    SpeakerLayout("2.0", ("L", "R"), (_STEREO,)),
    SpeakerLayout("5.1", ("L", "R", "C", "LFE", "Ls", "Rs"), (_FRONT, _SURROUND)),
    SpeakerLayout(
        "5.1.2", ("L", "R", "C", "LFE", "Ls", "Rs", "Tsl", "Tsr"),
        (_FRONT, _SURROUND), (_TOP_MIDDLE,),
    ),
    SpeakerLayout(
        "5.1.4", ("L", "R", "C", "LFE", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr"),
        (_FRONT, _SURROUND), (_TOP_FRONT, _TOP_BACK),
    ),
    SpeakerLayout(
        "5.1.6",
        ("L", "R", "C", "LFE", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr", "Tsl", "Tsr"),
        (_FRONT, _SURROUND), (_TOP_FRONT, _TOP_MIDDLE, _TOP_BACK),
    ),
    SpeakerLayout("7.1", ("L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs"), (_FRONT, _SIDE, _BACK)),
    SpeakerLayout(
        "7.1.2", ("L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs", "Tsl", "Tsr"),
        (_FRONT, _SIDE, _BACK), (_TOP_MIDDLE,),
    ),
    SpeakerLayout(
        "7.1.4", ("L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr"),
        (_FRONT, _SIDE, _BACK), (_TOP_FRONT, _TOP_BACK),
    ),
    SpeakerLayout(
        "7.1.6",
        ("L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs",
         "Tfl", "Tfr", "Tbl", "Tbr", "Tsl", "Tsr"),
        (_FRONT, _SIDE, _BACK), (_TOP_FRONT, _TOP_MIDDLE, _TOP_BACK),
    ),
    SpeakerLayout(
        "9.1.2",
        ("L", "R", "C", "LFE", "Lb", "Rb", "Lw", "Rw", "Ls", "Rs", "Tsl", "Tsr"),
        (_FRONT, _WIDE, _SIDE, _BACK), (_TOP_MIDDLE,),
    ),
    SpeakerLayout(
        "9.1.4",
        ("L", "R", "C", "LFE", "Lb", "Rb", "Lw", "Rw", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr"),
        (_FRONT, _WIDE, _SIDE, _BACK), (_TOP_FRONT, _TOP_BACK),
    ),
    SpeakerLayout(
        "9.1.6",
        ("L", "R", "C", "LFE", "Lb", "Rb", "Lw", "Rw", "Ls", "Rs",
         "Tfl", "Tfr", "Tbl", "Tbr", "Tsl", "Tsr"),
        (_FRONT, _WIDE, _SIDE, _BACK), (_TOP_FRONT, _TOP_MIDDLE, _TOP_BACK),
    ),
)  # fmt: skip
BY_NAME = {layout.name: layout for layout in LAYOUTS}
SURROUND_5_1 = BY_NAME["5.1"]
# what a Dolby Atmos track renders to unless told otherwise
DEFAULT_ATMOS = BY_NAME["5.1.4"]

# ffmpeg layout names whose channel order matches one of the layouts above
_FFMPEG = {
    "stereo": "2.0", "2.0": "2.0",
    "5.1": "5.1", "5.1(side)": "5.1",
    "5.1.2": "5.1.2", "5.1.2(back)": "5.1.2", "5.1.4": "5.1.4",
    "7.1": "7.1", "7.1.2": "7.1.2", "7.1.4": "7.1.4",
    "9.1.4": "9.1.4", "9.1.6": "9.1.6",
}  # fmt: skip


def atmos_layout(name: str) -> SpeakerLayout:
    """The render layout called `name`, the default for a name no longer offered."""
    return BY_NAME.get(name, DEFAULT_ATMOS)


def file_layout(ffmpeg_name: str, channels: int) -> SpeakerLayout | None:
    """The layout of a multichannel file: what ffprobe names it, else its first six as 5.1.

    None below six channels, where there is no discrete surround master to read.
    """
    known = BY_NAME.get(_FFMPEG.get(ffmpeg_name, ""))
    if known is not None and len(known.channels) == channels:
        return known
    if channels < len(SURROUND_5_1.channels):
        return None
    logger.warning(
        "%d-channel layout %r is not one this tool knows: reading its first six channels as 5.1",
        channels,
        ffmpeg_name or "unknown",
    )
    return SURROUND_5_1
