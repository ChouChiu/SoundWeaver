"""The Atmos objects as a Dolby Atmos master ADM BWF (Dolby Atmos Master ADM Profile v1.1).

The bed objects OAMD names become one DirectSpeakers bed in the smallest of
the profile's channel configurations that holds them (table 2-21), with the
channels the stream does not carry left silent.  A bed channel no
configuration has (the wides, the front and rear heights, a second LFE, a
repeat from another bed instance) is written as an Objects track pinned where
that speaker stands.  ISF and dynamic objects follow, one Objects track each.
Each object's own gain, with its ramps, is applied to its audio, so the file
needs no gain metadata and renders the same mix.

Object blocks are discrete events (section 2.5.2): every glide is sampled
into blocks under 20 ms apart, each carrying the profile's fixed
interpolationLength, and the renderer smooths between them.

The file is 24-bit PCM (format tag 1) with a 40-byte-per-track `chna`; a JUNK
chunk reserves room for `ds64`, so a file past 4 GiB becomes RF64 (section
7.1 rules out BW64).  Positions use ADM cartesian coordinates: X left -1 to
right +1, Y back -1 to front +1, Z the listener's plane 0 to the ceiling +1.
"""

from __future__ import annotations

import math
import shutil
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import numpy as np

from dolbytool.core.paths import partial_path
from dolbytool.core.speakers import BED_POSITIONS

from .oamd import Program
from .render import joc_objects
from .sinks import RenderedFrame
from .trace import Key

BIT_DEPTH = 24
_FULL_SCALE = 2 ** (BIT_DEPTH - 1)
_RIFF_LIMIT = 0xFFFFFFFF
_DS64_SIZE = 28

# Table 2-21: the channel configurations a bed may take, smallest first, in channel order.
_BED_SETS = (
    ("2.0", ("L", "R")),
    ("3.0", ("L", "R", "C")),
    ("5.0", ("L", "R", "C", "Ls", "Rs")),
    ("5.1", ("L", "R", "C", "LFE", "Ls", "Rs")),
    ("7.0", ("L", "R", "C", "Lss", "Rss", "Lrs", "Rrs")),
    ("7.1", ("L", "R", "C", "LFE", "Lss", "Rss", "Lrs", "Rrs")),
    ("7.0.2", ("L", "R", "C", "Lss", "Rss", "Lrs", "Rrs", "Lts", "Rts")),
    ("7.1.2", ("L", "R", "C", "LFE", "Lss", "Rss", "Lrs", "Rrs", "Lts", "Rts")),
)
# Tables 2-11 and 2-14: each bed channel's audioChannelFormatName, speakerLabel and position.
_BED_CHANNELS = {
    "L": ("RoomCentricLeft", "RC_L", (-1.0, 1.0, 0.0)),
    "R": ("RoomCentricRight", "RC_R", (1.0, 1.0, 0.0)),
    "C": ("RoomCentricCenter", "RC_C", (0.0, 1.0, 0.0)),
    "LFE": ("RoomCentricLFE", "RC_LFE", (-1.0, 1.0, -1.0)),
    "Lss": ("RoomCentricLeftSideSurround", "RC_Lss", (-1.0, 0.0, 0.0)),
    "Rss": ("RoomCentricRightSideSurround", "RC_Rss", (1.0, 0.0, 0.0)),
    "Lrs": ("RoomCentricLeftRearSurround", "RC_Lrs", (-1.0, -1.0, 0.0)),
    "Rrs": ("RoomCentricRightRearSurround", "RC_Rrs", (1.0, -1.0, 0.0)),
    "Lts": ("RoomCentricLeftTopSurround", "RC_Lts", (-1.0, 0.0, 1.0)),
    "Rts": ("RoomCentricRightTopSurround", "RC_Rts", (1.0, 0.0, 1.0)),
    # Table 2-14: Dolby Atmos Master ADM Profile specifies (-1.0, -1.0, 0.0) for both RC_Ls/RC_Rs
    # and RC_Lrs/RC_Rrs in room-centric Cartesian space (MediaInfo conforms via alias tolerance).
    "Ls": ("RoomCentricLeftSurround", "RC_Ls", (-1.0, -1.0, 0.0)),
    "Rs": ("RoomCentricRightSurround", "RC_Rs", (1.0, -1.0, 0.0)),
}

DIRECT_SPEAKERS = ("0001", "DirectSpeakers")
OBJECTS = ("0003", "Objects")
# Section 3.5: an Objects audioObject takes an id from 0x100b on, past a full 7.1.2 bed.
_FIRST_OBJECT_ID = 0x100B

# Table 2-15: the interpolationLength of every block but the first, which has 0.
_INTERPOLATION = "0.005208"
# Section 2.5.2: a glide is sampled into blocks less than this far apart.
_SAMPLE_PERIOD = 0.02
# Events closer than a timecode's last digit are one event: the later one wins.
_SAME_TIME = 1e-5


def _assignment(label: str, seven: bool, backs: bool) -> str | None:
    """Which channel of a 5.X (or smaller) or a 7.X configuration carries the OAMD bed `label`.

    In a 7.X bed the OAMD surrounds are the side pair when the stream has back
    surrounds too, else the rear pair, which stands where a 5.X surround does.
    """
    if label in ("L", "R", "C", "LFE"):
        return label
    if not seven:
        return label if label in ("Ls", "Rs") else None
    side = {"Ls": "L", "Lb": "L", "Tsl": "L", "Rs": "R", "Rb": "R", "Tsr": "R"}.get(label)
    if side is None:
        return None
    if label in ("Tsl", "Tsr"):
        return f"{side}ts"
    return f"{side}ss" if label in ("Ls", "Rs") and backs else f"{side}rs"


def _bed(labels: tuple[str, ...]) -> tuple[str, dict[int, str]]:
    """The configuration a bed of `labels` takes, and the channel each OAMD bed object fills.

    The bed objects 7.1.2 has a channel for go into the smallest configuration
    that holds them all; the rest, and a second object for a channel already
    filled, are left out of the map.  An empty name means there is no bed.
    """
    backs = "Lb" in labels or "Rb" in labels
    largest = _BED_SETS[-1][1]
    held, taken = [], set()
    for index, label in enumerate(labels):
        channel = _assignment(label, True, backs)
        if channel in largest and channel not in taken:
            taken.add(channel)
            held.append(index)
    if not held:
        return "", {}
    for name, channels in _BED_SETS:
        seven = "Lss" in channels
        fits = {index: _assignment(labels[index], seven, backs) for index in held}
        if all(channel in channels for channel in fits.values()):
            return name, fits  # type: ignore[return-value]
    raise AssertionError("7.1.2 holds every bed channel it took")


@dataclass(frozen=True, slots=True)
class _Track:
    source: int | None  # the OAMD object whose audio this track carries; None for silence
    name: str
    channel: str | None = None  # the bed channel it is, for a DirectSpeakers track
    pinned: tuple[float, float, float] | None = None  # room position of a pinned bed object


def _tracks(program: Program, objects: int) -> tuple[str, tuple[_Track, ...], tuple[_Track, ...]]:
    """The bed's configuration, its tracks in channel order, and the Objects tracks."""
    labels = program.bed_labels
    name, assigned = _bed(labels)
    by_channel = {channel: index for index, channel in assigned.items()}
    channels = dict(_BED_SETS).get(name, ())
    bed = tuple(
        _Track(by_channel.get(channel), _BED_CHANNELS[channel][0], channel) for channel in channels
    )
    pinned = tuple(
        _Track(index, f"Bed {label}", pinned=BED_POSITIONS[label])
        for index, label in enumerate(labels)
        if index not in assigned
    )
    rest = tuple(
        _Track(index, f"ISF {index}" if index < program.anchored else f"Object {index}")
        for index in range(len(labels), objects)
    )
    return name, bed, pinned + rest


def _cartesian(x: float, y: float, z: float) -> tuple[float, float, float]:
    return 2 * x - 1, 1 - 2 * y, z


def _timecode(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:08.5f}"


def _number(value: float) -> str:
    """A position in decimal without leading zeros (section 2.1), and no negative zero."""
    return f"{value + 0.0:.4f}".replace("-0.0000", "0.0000")


def adm_block(f: RenderedFrame, sources: tuple[int | None, ...]) -> np.ndarray:
    """One frame's span with track k carrying OAMD object `sources[k]`, its own gain applied."""
    meta = f.metadata
    audio = {index: f.essence[o] * f.envelope[o] for o, index in enumerate(joc_objects(meta))}
    for index, label in enumerate(meta.program.bed_labels):
        if label.startswith("LFE"):
            audio[index] = f.lfe
    block = np.zeros((len(sources), f.span.stop - f.span.start))
    for track, source in enumerate(sources):
        if source is not None:
            block[track] = audio[source][f.span]
    return block


def pcm24(block: np.ndarray) -> tuple[bytes, int]:
    """(channels, samples) as interleaved little-endian 24-bit PCM, and how many samples clipped."""
    scaled = np.round(block.T * _FULL_SCALE)
    over = np.abs(scaled) > _FULL_SCALE - 1
    clipped = int(over.sum())
    if clipped:
        np.clip(scaled, -_FULL_SCALE, _FULL_SCALE - 1, out=scaled)
    raw = np.ascontiguousarray(scaled, dtype="<i4").view(np.uint8).reshape(-1, 4)[:, :3]
    return raw.tobytes(), clipped


def _events(keys: list[Key], duration: float) -> list[tuple[float, tuple[float, float, float]]]:
    """The (time, room position) events an object's blocks hold, the first at 0.

    A key glides from where the object is to its position over its ramp, or
    until the next key takes over; the glide is sampled evenly, under
    `_SAMPLE_PERIOD` apart.  Events before the container starts collapse onto
    its first sample, events past it go, and an event that leaves the
    position where it was is dropped.
    """
    timed: list[tuple[float, tuple[float, float, float]]] = []
    here: tuple[float, float, float] | None = None
    for n, (t, ramp, x, y, z, _active) in enumerate(keys):
        end = keys[n + 1][0] if n + 1 < len(keys) else math.inf
        target = (x, y, z)
        if here is None or ramp <= 0:
            timed.append((t, target))
            here = target
            continue
        start = here
        here = _lerp(start, target, min(1.0, (end - t) / ramp))
        steps = int(ramp / _SAMPLE_PERIOD) + 1
        for k in range(1, steps + 1):
            at = t + ramp * k / steps
            if at > end + _SAME_TIME:
                # cut short by the next key: hold where the glide got to
                timed.append((end, here))
                break
            timed.append((at, _lerp(start, target, k / steps)))

    events: list[tuple[float, tuple[float, float, float]]] = []
    for t, position in timed:
        t = max(0.0, t)
        if t >= duration:
            break
        if events and t <= events[-1][0] + _SAME_TIME:
            events.pop()
        if not events or events[-1][1] != position:
            events.append((t, position))
    if not events:
        return [(0.0, timed[0][1] if timed else (0.5, 0.5, 0.0))]
    events[0] = (0.0, events[0][1])
    return events


def _lerp(a: tuple[float, ...], b: tuple[float, ...], f: float) -> tuple[float, float, float]:
    return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f


class AdmFile:
    """Writes the header up front, takes the PCM in order, and the metadata at `close`."""

    def __init__(
        self, target: Path, sample_rate: int, length: int, program: Program, objects: int
    ) -> None:
        self.target, self.sample_rate, self.length = target, sample_rate, length
        self.program, self.objects = program, objects
        self.bed_name, self.bed, self.others = _tracks(program, objects)
        self.tracks = self.bed + self.others
        # which OAMD object each track of the file carries, for `adm_block`
        self.sources = tuple(t.source for t in self.tracks)
        self.channels = len(self.tracks)
        self.keys: dict[int, list[Key]] = {}
        self.data_bytes = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        # written aside and moved into place by `close`, so a failed run leaves no torn file
        self.partial = partial_path(target)
        self.file: BinaryIO = open(self.partial, "wb")  # noqa: SIM115 - closed in close()
        w = self.file
        w.write(b"RIFF" + struct.pack("<I", 0) + b"WAVE")
        w.write(b"JUNK" + struct.pack("<I", _DS64_SIZE) + bytes(_DS64_SIZE))
        align = self.channels * BIT_DEPTH // 8
        w.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, self.channels, sample_rate,
                                      sample_rate * align, align, BIT_DEPTH))  # fmt: skip
        chna = self._chna()
        w.write(b"chna" + struct.pack("<I", len(chna)) + chna)
        w.write(b"data" + struct.pack("<I", 0))
        self.data_start = w.tell()

    def append(self, part: Path) -> None:
        """Copy a run of PCM written by `pcm24` onto the end of the data chunk."""
        with part.open("rb") as source:
            shutil.copyfileobj(source, self.file, 1 << 22)
            self.data_bytes += source.tell()

    def close(self, keys: dict[int, list[Key]]) -> None:
        """Finish the file; `keys` are the dynamic objects' positions from the trace."""
        self.keys = keys
        w = self.file
        try:
            if self.data_bytes % 2:
                w.write(b"\0")
            axml = self._axml()
            w.write(b"axml" + struct.pack("<I", len(axml)) + axml)
            if len(axml) % 2:
                w.write(b"\0")
            riff = w.tell() - 8
            data = self.data_bytes
            if riff > _RIFF_LIMIT or data > _RIFF_LIMIT:
                w.seek(0)
                w.write(b"RF64" + struct.pack("<I", _RIFF_LIMIT) + b"WAVE")
                w.write(b"ds64" + struct.pack("<I", _DS64_SIZE))
                frames = data // (self.channels * BIT_DEPTH // 8)
                w.write(struct.pack("<QQQI", riff, data, frames, 0))
                data = _RIFF_LIMIT
            else:
                w.seek(4)
                w.write(struct.pack("<I", riff))
            w.seek(self.data_start - 4)
            w.write(struct.pack("<I", data))
            w.close()
            self.partial.replace(self.target)
        finally:
            self.discard()

    def discard(self) -> None:
        self.file.close()
        self.partial.unlink(missing_ok=True)

    # ---- metadata ----------------------------------------------------------------------
    #
    # Section 3: packs, channels and audioObjects each count from 0x1001, the
    # stream and track formats share the channels' count, the track UIDs and
    # blocks count from 1.  The bed comes first, as one pack and one audioObject
    # whose id reserves as many values as it has tracks.

    def _elements(self) -> list[tuple[str, str, tuple[str, str], tuple[int, ...]]]:
        """(audioObjectID, pack id, type, tracks) of each audioObject, in file order."""
        out = []
        pack, track = 0x1001, 0
        if self.bed:
            out.append(("AO_1001", f"AP_0001{pack:04X}", DIRECT_SPEAKERS,
                        tuple(range(len(self.bed)))))  # fmt: skip
            pack, track = pack + 1, len(self.bed)
        number = max(_FIRST_OBJECT_ID, 0x1001 + len(self.bed))
        for i in range(track, self.channels):
            out.append((f"AO_{number:04X}", f"AP_0003{pack:04X}", OBJECTS, (i,)))
            number, pack = number + 1, pack + 1
        return out

    def _track_ids(self, index: int) -> dict[str, str]:
        code = DIRECT_SPEAKERS[0] if index < len(self.bed) else OBJECTS[0]
        n = f"{code}{0x1001 + index:04X}"
        return {
            "channel": f"AC_{n}",
            "stream": f"AS_{n}",
            "track": f"AT_{n}_01",
            "uid": f"ATU_{index + 1:08X}",
        }

    def _packs(self) -> dict[int, str]:
        """The pack id of each track."""
        return {i: pack for _, pack, _, tracks in self._elements() for i in tracks}

    def _chna(self) -> bytes:
        packs = self._packs()
        out = bytearray(struct.pack("<HH", self.channels, self.channels))
        for index in range(self.channels):
            ids = self._track_ids(index)
            out += struct.pack("<H", index + 1)
            out += ids["uid"].encode().ljust(12, b"\0")
            out += ids["track"].encode().ljust(14, b"\0")
            out += packs[index].encode().ljust(11, b"\0")
            out += b"\0"
        return bytes(out)

    def _axml(self) -> bytes:
        duration = self.length / self.sample_rate
        elements = self._elements()
        packs = self._packs()

        root = ET.Element(
            "ebuCoreMain",
            {"xmlns": "urn:ebu:metadata-schema:ebuCore_2016", "version": "1.7"},
        )
        fmt = ET.SubElement(ET.SubElement(root, "coreMetadata"), "format")
        adm = ET.SubElement(fmt, "audioFormatExtended")
        programme = ET.SubElement(adm, "audioProgramme", {
            "audioProgrammeID": "APR_1001", "audioProgrammeName": "Atmos_Master",
            "start": _timecode(0), "end": _timecode(duration),
        })  # fmt: skip
        ET.SubElement(programme, "audioContentIDRef").text = "ACO_1001"
        content = ET.SubElement(adm, "audioContent", {
            "audioContentID": "ACO_1001", "audioContentName": "Atmos_Master_Content",
        })  # fmt: skip
        for object_id, *_ in elements:
            ET.SubElement(content, "audioObjectIDRef").text = object_id
        # present only for the schema (table 2-26): the values are fixed, not a description
        ET.SubElement(content, "dialogue", {"mixedContentKind": "0"}).text = "2"

        for object_id, pack, (_, kind), tracks in elements:
            obj = ET.SubElement(adm, "audioObject", {
                "audioObjectID": object_id, "audioObjectName": self._object_name(kind, tracks),
                "start": _timecode(0), "duration": _timecode(duration),
            })  # fmt: skip
            ET.SubElement(obj, "audioPackFormatIDRef").text = pack
            for index in tracks:
                ET.SubElement(obj, "audioTrackUIDRef").text = self._track_ids(index)["uid"]

        for _, pack, (code, kind), tracks in elements:
            element = ET.SubElement(adm, "audioPackFormat", {
                "audioPackFormatID": pack, "audioPackFormatName": self._object_name(kind, tracks),
                "typeLabel": code, "typeDefinition": kind,
            })  # fmt: skip
            for index in tracks:
                ET.SubElement(element, "audioChannelFormatIDRef").text = self._track_ids(index)[
                    "channel"
                ]

        for index, track in enumerate(self.tracks):
            ids = self._track_ids(index)
            code, kind = DIRECT_SPEAKERS if index < len(self.bed) else OBJECTS
            channel = ET.SubElement(adm, "audioChannelFormat", {
                "audioChannelFormatID": ids["channel"], "audioChannelFormatName": track.name,
                "typeLabel": code, "typeDefinition": kind,
            })  # fmt: skip
            if track.channel is not None:
                self._bed_block(channel, ids, track.channel)
            else:
                if track.pinned is not None:
                    keys = [(0.0, 0.0, *track.pinned, True)]
                else:
                    keys = self.keys.get(track.source, [(0.0, 0.0, 0.5, 0.5, 0.0, True)])
                self._object_blocks(channel, ids, _events(keys, duration), duration)

        for index, track in enumerate(self.tracks):
            ids = self._track_ids(index)
            stream = ET.SubElement(adm, "audioStreamFormat", {
                "audioStreamFormatID": ids["stream"], "audioStreamFormatName": f"PCM_{track.name}",
                "formatLabel": "0001", "formatDefinition": "PCM",
            })  # fmt: skip
            ET.SubElement(stream, "audioChannelFormatIDRef").text = ids["channel"]
            # BS.2076-0 has a stream name a channel or a pack; the profile has both (2.3)
            ET.SubElement(stream, "audioPackFormatIDRef").text = packs[index]
            ET.SubElement(stream, "audioTrackFormatIDRef").text = ids["track"]

        for index, track in enumerate(self.tracks):
            ids = self._track_ids(index)
            element = ET.SubElement(adm, "audioTrackFormat", {
                "audioTrackFormatID": ids["track"], "audioTrackFormatName": f"PCM_{track.name}",
                "formatLabel": "0001", "formatDefinition": "PCM",
            })  # fmt: skip
            ET.SubElement(element, "audioStreamFormatIDRef").text = ids["stream"]

        for index in range(self.channels):
            ids = self._track_ids(index)
            uid = ET.SubElement(adm, "audioTrackUID", {
                "UID": ids["uid"], "sampleRate": str(self.sample_rate), "bitDepth": str(BIT_DEPTH),
            })  # fmt: skip
            ET.SubElement(uid, "audioTrackFormatIDRef").text = ids["track"]
            ET.SubElement(uid, "audioPackFormatIDRef").text = packs[index]

        ET.indent(root)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _object_name(self, kind: str, tracks: tuple[int, ...]) -> str:
        if kind == "DirectSpeakers":
            return f"Bed {self.bed_name}"
        return self.tracks[tracks[0]].name

    @staticmethod
    def _bed_block(channel: ET.Element, ids: dict[str, str], assignment: str) -> None:
        # a bed channel holds one block for the whole programme, with no rtime or duration (2.5)
        _, speaker, position = _BED_CHANNELS[assignment]
        block = ET.SubElement(channel, "audioBlockFormat", {
            "audioBlockFormatID": f"AB_{ids['channel'][3:]}_00000001",
        })  # fmt: skip
        ET.SubElement(block, "speakerLabel").text = speaker
        ET.SubElement(block, "cartesian").text = "1"
        for axis, value in zip("XYZ", position, strict=True):
            ET.SubElement(block, "position", {"coordinate": axis}).text = _number(value)

    @staticmethod
    def _object_blocks(
        channel: ET.Element,
        ids: dict[str, str],
        events: list[tuple[float, tuple[float, float, float]]],
        duration: float,
    ) -> None:
        ends = [t for t, _ in events[1:]] + [duration]
        for n, ((t, position), end) in enumerate(zip(events, ends, strict=True), 1):
            block = ET.SubElement(channel, "audioBlockFormat", {
                "audioBlockFormatID": f"AB_{ids['channel'][3:]}_{n:08X}",
                "rtime": _timecode(t), "duration": _timecode(end - t),
            })  # fmt: skip
            ET.SubElement(block, "cartesian").text = "1"
            for axis, value in zip("XYZ", _cartesian(*position), strict=True):
                ET.SubElement(block, "position", {"coordinate": axis}).text = _number(value)
            jump = ET.SubElement(
                block, "jumpPosition", {"interpolationLength": "0" if n == 1 else _INTERPOLATION}
            )
            jump.text = "1"
