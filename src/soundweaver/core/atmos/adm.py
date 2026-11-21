"""The Atmos objects as a Dolby Atmos master ADM BWF (Dolby Atmos Master ADM Profile v1.1).

The bed objects OAMD names become one DirectSpeakers bed in the smallest of
the profile's channel configurations that holds them (table 2-21), with the
channels the stream does not carry left silent.  A bed channel no
configuration has (the wides, the front and rear heights, a second LFE, a
repeat from another bed instance) is written as an Objects track pinned where
that speaker stands.  ISF objects follow, each pinned where its ring puts it, then
the dynamic objects, one Objects track each.
Each object's own gain, with its ramps, is applied to its audio, so the file
needs no gain metadata and renders the same mix.

Object blocks are discrete events (section 2.5.2): every glide is sampled
into blocks under 20 ms apart, each carrying the profile's fixed
interpolationLength, and the renderer smooths between them.

The file is 24-bit PCM (format tag 1) with a 40-byte-per-track `chna`; a JUNK
chunk reserves room for `ds64`, so a file past 4 GiB becomes RF64 (section
7.1 rules out BW64).  Positions use ADM cartesian coordinates: X left -1 to
right +1, Y back -1 to front +1, Z the listener's plane 0 to the ceiling +1.

Without bed placeholders the writer uses general ITU ADM and explicitly
identifies the LFE as a DirectSpeakers channel.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from soundweaver.core.adm_bwf import (
    BIT_DEPTH,
    # The Atmos decoder writes its blocks through these; re-exported for it.
    INTERPOLATION,
    TICKS_PER_SECOND,
    AdmBwf,
    events,
    number,
    pcm24,
    quantize,
    timecode,
)
from soundweaver.core.speakers import BED_POSITIONS

from .oamd import Program
from .render import joc_objects
from .sinks import RenderedFrame
from .trace import Key, SizeKey

__all__ = ["AdmFile", "adm_block", "pcm24"]

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
    # Table 2-14 of the Dolby Atmos Master ADM Profile puts the 5.X surrounds RC_Ls/RC_Rs
    # at the back corners, where the 7.X rear pair RC_Lrs/RC_Rrs also stands.
    "Ls": ("RoomCentricLeftSurround", "RC_Ls", (-1.0, -1.0, 0.0)),
    "Rs": ("RoomCentricRightSurround", "RC_Rs", (1.0, -1.0, 0.0)),
}
_ITU_SPEAKER_LABELS = {
    "L": "M+030", "R": "M-030", "C": "M+000", "LFE": "LFE",
    "Ls": "M+110", "Rs": "M-110", "Lss": "M+090", "Rss": "M-090",
    "Lrs": "M+135", "Rrs": "M-135", "Lts": "U+090", "Rts": "U-090",
}

DIRECT_SPEAKERS = ("0001", "DirectSpeakers")
OBJECTS = ("0003", "Objects")
# Section 3.5: an Objects audioObject takes an id from 0x100b on, past a full 7.1.2 bed.
_FIRST_OBJECT_ID = 0x100B
# The playback decode re-serializes the whole metadata tree on refresh, so it
# only does so once the scanned timeline grew this far past the written one.
# The scan runs tens of times faster than playback, so the blocks the reader's
# playhead reaches are always tens of seconds ahead of it.
_REFRESH_SPAN = 30.0



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
    raise AssertionError("unreachable: the largest bed set holds every channel taken")


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
        _Track(index, f"ISF {index}", pinned=position)
        if (position := program.isf_position(index)) is not None
        else _Track(index, f"Object {index}")
        for index in range(len(labels), objects)
    )
    return name, bed, pinned + rest


def _cartesian(x: float, y: float, z: float) -> tuple[float, float, float]:
    return 2 * x - 1, 1 - 2 * y, z


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


def _object_events(
    keys: list[Key], sizes: list[SizeKey], duration: float
) -> list[tuple[float, tuple[float, ...], float]]:
    positions = events(
        ((t, ramp, (x, y, z)) for t, ramp, x, y, z, _ in keys), duration, (0.5, 0.5, 0.0)
    )
    if not sizes:
        return [(t, position, 0.0) for t, position in positions]

    # Combine the independent timelines at the precision of an ADM timecode.
    by_position = {round(t * TICKS_PER_SECOND): p for t, p in positions}
    by_size = {
        round(t * TICKS_PER_SECOND): s
        for t, (s,) in events(((t, ramp, (s,)) for t, ramp, s in sizes), duration, (0.0,))
    }
    position, size = by_position[0], by_size[0]
    end_tick = round(duration * TICKS_PER_SECOND)
    merged = []
    for tick in sorted(by_position.keys() | by_size.keys()):
        if tick and tick >= end_tick:
            break
        position = by_position.get(tick, position)
        size = by_size.get(tick, size)
        merged.append((tick / TICKS_PER_SECOND, position, size))
    return merged


class AdmFile(AdmBwf):
    """The Dolby layout on top of `AdmBwf`: a DirectSpeakers bed and the Objects tracks."""

    def __init__(
        self,
        target: Path,
        sample_rate: int,
        length: int,
        program: Program,
        objects: int,
        *,
        layout: tuple[str, tuple[_Track, ...], tuple[_Track, ...]] | None = None,
        remove_bed_placeholders: bool = False,
    ) -> None:
        self.program, self.objects = program, objects
        if layout is not None:
            self.bed_name, self.bed, self.others = layout
        else:
            self.bed_name, self.bed, self.others = _tracks(program, objects)
        self.general_adm = remove_bed_placeholders
        if self.general_adm:
            self.bed = tuple(track for track in self.bed if track.source is not None)
        self.tracks = self.bed + self.others
        # which OAMD object each track of the file carries, for `adm_block`
        self.sources = tuple(t.source for t in self.tracks)
        self.channels = len(self.tracks)
        self.keys: dict[int, list[Key]] = {}
        self.size_keys: dict[int, list[SizeKey]] = {}
        self._written_until = 0.0
        super().__init__(target, sample_rate, length, self.channels, self._chna())

    def prepare_readback(
        self, keys: dict[int, list[Key]], sizes: dict[int, list[SizeKey]] | None = None
    ) -> Path:
        """Publish the header at the stable, private path owned by playback."""
        self.keys, self.size_keys = keys, sizes or {}
        self._written_until = self._timeline_time()
        return super().prepare_readback(self._axml())

    def close(self, keys: dict[int, list[Key]], sizes: dict[int, list[SizeKey]] | None = None) -> None:
        """Finish the file with the dynamic objects' position and size timelines."""
        self.keys, self.size_keys = keys, sizes or {}
        super().close(self._axml())

    def refresh(self, *, force: bool = False) -> None:
        """Rewrite the axml from the object timelines collected so far.

        `prepare_readback` stores the caller's dictionaries by reference, so the
        decoder's growing tracer is already visible here; the gate only bounds how
        often the whole metadata tree is re-serialized.
        """
        grown = self._timeline_time()
        if not force and self._written_until and grown - self._written_until < _REFRESH_SPAN:
            return
        self._written_until = grown
        super().refresh(self._axml())

    def _timeline_time(self) -> float:
        """How far the collected object timelines reach, in seconds."""
        return max((keys[-1][0] for keys in self.keys.values() if keys), default=0.0)

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
        if self.general_adm:
            adm.set("version", "ITU-R_BS.2076-1")
        programme = ET.SubElement(adm, "audioProgramme", {
            "audioProgrammeID": "APR_1001", "audioProgrammeName": "Atmos_Master",
            "start": timecode(0), "end": timecode(duration),
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
                "start": timecode(0), "duration": timecode(duration),
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
                    sizes = []
                else:
                    keys = self.keys.get(track.source, [(0.0, 0.0, 0.5, 0.5, 0.0, True)])
                    sizes = self.size_keys.get(track.source, [])
                self._object_blocks(channel, ids, _object_events(keys, sizes, duration), duration)

        for index, track in enumerate(self.tracks):
            ids = self._track_ids(index)
            stream = ET.SubElement(adm, "audioStreamFormat", {
                "audioStreamFormatID": ids["stream"], "audioStreamFormatName": f"PCM_{track.name}",
                "formatLabel": "0001", "formatDefinition": "PCM",
            })  # fmt: skip
            ET.SubElement(stream, "audioChannelFormatIDRef").text = ids["channel"]
            # The Dolby profile requires both references on the stream (2.3).
            if not self.general_adm:
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
            if self.general_adm:
                return "LFE" if len(self.bed) == 1 and self.bed[0].channel == "LFE" else "Bed"
            return f"Bed {self.bed_name}"
        return self.tracks[tracks[0]].name

    def _bed_block(self, channel: ET.Element, ids: dict[str, str], assignment: str) -> None:
        # a bed channel holds one block for the whole programme, with no rtime or duration (2.5)
        _, speaker, position = _BED_CHANNELS[assignment]
        if self.general_adm and assignment == "LFE":
            ET.SubElement(channel, "frequency", typeDefinition="lowPass").text = "120"
        block = ET.SubElement(channel, "audioBlockFormat", {
            "audioBlockFormatID": f"AB_{ids['channel'][3:]}_00000001",
        })  # fmt: skip
        axes = ("X", "Y", "Z")
        if self.general_adm:
            speaker = _ITU_SPEAKER_LABELS[assignment]
            x, y, z = position
            axes = ("azimuth", "elevation")
            position = (
                math.degrees(math.atan2(-x, y)),
                math.degrees(math.atan2(z, math.hypot(x, y))),
            )
        ET.SubElement(block, "speakerLabel").text = speaker
        if not self.general_adm:
            ET.SubElement(block, "cartesian").text = "1"
        for axis, value in zip(axes, position, strict=True):
            ET.SubElement(block, "position", {"coordinate": axis}).text = number(value)

    def _object_blocks(
        self,
        channel: ET.Element,
        ids: dict[str, str],
        events: list[tuple[float, tuple[float, ...], float]],
        duration: float,
    ) -> None:
        if self.general_adm:
            # Quantize boundaries once so consecutive durations match their rtimes.
            # Keep the last position when events serialize to the same boundary.
            quantized, duration = quantize(
                [(t, (position, size)) for t, position, size in events], duration
            )
            events = [(t, position, size) for t, (position, size) in quantized]
        ends = [t for t, _, _ in events[1:]] + [duration]
        for n, ((t, position, size), end) in enumerate(zip(events, ends, strict=True), 1):
            block = ET.SubElement(channel, "audioBlockFormat", {
                "audioBlockFormatID": f"AB_{ids['channel'][3:]}_{n:08X}",
                "rtime": timecode(t), "duration": timecode(end - t),
            })  # fmt: skip
            ET.SubElement(block, "cartesian").text = "1"
            for axis, value in zip("XYZ", _cartesian(*position), strict=True):
                ET.SubElement(block, "position", {"coordinate": axis}).text = number(value)
            if size > 0:
                for axis in ("width", "height", "depth"):
                    ET.SubElement(block, axis).text = number(size)
            interpolation = "0" if n == 1 else INTERPOLATION
            if self.general_adm and n > 1:
                interpolation = f"{min(float(INTERPOLATION), end - t):.6f}"
            jump = ET.SubElement(block, "jumpPosition", interpolationLength=interpolation)
            jump.text = "1"
