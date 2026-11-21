"""A 360RA / MPEG-H track as ADM BWF (ITU-R BS.2076).

MPEG-H carries music as objects: audio signals plus metadata saying where each
one sits, how loud it is and how wide it spreads.  The decoder renders them into
whatever layout the player asks for; what it also reports, frame by frame, is
the object signals and that metadata, which is what an ADM master describes.
Each object becomes one Objects track: every block pins where the object is
during that frame in the metadata's own coordinates, the object's own gain is
applied to its audio, and its spread becomes the block's width, height and
depth, so a renderer pans the file the way MPEG-H pans the stream.

A stream that carries a channel bed instead of objects is written as a
DirectSpeakers bed at the speakers the decoder rendered to, placed with this
package's CICP table.  A stream that carries a bed *and* objects is written as
objects only: the rendered bed has those objects mixed in, and ADM has no way
to take them back out again.

The file is 24-bit PCM with a 40-byte-per-track `chna`, and it becomes RF64 past
4 GiB.  Positions use ADM cartesian coordinates: X left -1 to right +1, Y back
-1 to front +1, Z the listener's plane 0 to the ceiling +1.
"""

from __future__ import annotations

import math
import struct
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from soundweaver.core.adm_bwf import (
    BIT_DEPTH,
    INTERPOLATION,
    TICKS_PER_SECOND,
    AdmBwf,
    events,
    number,
    pcm24,
    timecode,
)
from soundweaver.core.codecs.mpegh_eri import MpeghObject

if TYPE_CHECKING:
    from soundweaver.core.codecs.mpegh import CicpSpeaker

DIRECT_SPEAKERS = ("0001", "DirectSpeakers")
OBJECTS = ("0003", "Objects")
_FIRST_ID = 0x1001
# ADM takes one block per change; a mix that moves every frame writes one per frame.
_POSITION_PLACES = 4
_SIZE_PLACES = 3
# MPEG-H spread arrives in degrees; ADM width and height are fractions of the sphere.
_SPREAD_DEGREES = 180.0


@dataclass(frozen=True, slots=True)
class _Track:
    """One ADM track: either one of the decoded objects or one of the rendered speakers."""

    name: str
    object: int | None = None  # index into the frame's objects
    channel: int | None = None  # rendered PCM channel of a DirectSpeakers track
    speaker: CicpSpeaker | None = None


def position(azimuth: float, elevation: float) -> tuple[float, float, float]:
    """MPEG-H azimuth/elevation in degrees as ADM cartesian, clamped to the room."""
    az, el = math.radians(azimuth), math.radians(elevation)
    x = -math.sin(az) * math.cos(el)
    y = math.cos(az) * math.cos(el)
    z = math.sin(el)
    return (
        min(1.0, max(-1.0, x)),
        min(1.0, max(-1.0, y)),
        min(1.0, max(-1.0, z)),
    )


def key_of(
    meta: MpeghObject, start: float
) -> tuple[tuple[float, float, tuple[float, float, float], bool], tuple[float, tuple[float, float, float]]]:
    """Where an object is at `start`, and how it spreads there.

    Both the streaming writer and the metadata pass a playback decode runs read
    the timelines from here, so a file written either way describes the same mix.
    """
    place = tuple(round(value, _POSITION_PLACES) for value in position(meta.azimuth, meta.elevation))
    size = tuple(round(value, _SIZE_PLACES) for value in spread(meta))
    return (start, 0.0, place, meta.audible), (start, size)


def spread(meta: MpeghObject) -> tuple[float, float, float]:
    """The object's spread as the block's width, height and depth, each 0..1."""
    return (
        min(1.0, max(0.0, meta.width / _SPREAD_DEGREES)),
        min(1.0, max(0.0, meta.height / _SPREAD_DEGREES)),
        min(1.0, max(0.0, meta.depth)),
    )


class MpeghAdmFile(AdmBwf):
    """Takes the frames' object signals and metadata, and writes the master at `close`."""

    def __init__(
        self,
        target: Path,
        sample_rate: int,
        length: int | None,
        objects: int,
        *,
        bed: Sequence[CicpSpeaker] = (),
        positions: dict[int, list[tuple[float, float, tuple[float, float, float], bool]]] | None = None,
        sizes: dict[int, list[tuple[float, tuple[float, float, float]]]] | None = None,
    ) -> None:
        if objects:
            self.tracks: tuple[_Track, ...] = tuple(
                _Track(f"Object {index + 1}", object=index) for index in range(objects)
            )
        else:
            self.tracks = tuple(
                _Track(speaker.name, channel=index, speaker=speaker)
                for index, speaker in enumerate(bed)
            )
        if not self.tracks:
            raise ValueError("an ADM file needs objects or a bed")
        self.objects = objects
        # A playback decode reads the metadata in a pass of its own and hands it in here;
        # otherwise the frames fill it in as they arrive.
        self.positions = positions if positions is not None else {}
        self.sizes = sizes if sizes is not None else {}
        self.traced = positions is not None
        self.clipped = 0
        # Nought means the length is not known yet: the frames decide, and `close` sets it.
        super().__init__(target, sample_rate, length or 0, len(self.tracks), self._chna())

    def write_frame(
        self,
        signals: np.ndarray,
        objects: Sequence[MpeghObject],
        rendered: np.ndarray | None = None,
    ) -> None:
        """Add one frame: `signals` are (objects, samples), `rendered` is (channels, samples)."""
        if self.objects and len(objects) != self.objects:
            raise ValueError(
                f"frame reports {len(objects)} objects, the file was started with {self.objects}"
            )
        # The render decides how far along the container each frame runs, and its first
        # frame can be short; the object signals sit on that same timeline already.
        frames = rendered.shape[1] if rendered is not None else signals.shape[1]
        if self.length:
            span = min(signals.shape[1], self.length - self.frames_written)
        else:
            span = frames
        if span <= 0:
            return
        start = self.frames_written / self.sample_rate
        block = np.zeros((len(self.tracks), span), dtype=np.float32)
        for index, track in enumerate(self.tracks):
            if track.object is not None:
                meta = objects[track.object]
                block[index] = signals[track.object, :span] * np.float32(meta.gain)
                if not self.traced:
                    self._trace(index, start, meta)
            elif rendered is not None and track.channel is not None:
                block[index] = rendered[track.channel, :span]
        data, clipped = pcm24(block)
        self.write_pcm(data)
        self.clipped += clipped

    def _trace(self, index: int, start: float, meta: MpeghObject) -> None:
        """Keep the position and spread timelines, one entry per change."""
        key, size = key_of(meta, start)
        keys = self.positions.setdefault(index, [])
        if not keys or keys[-1][2:] != key[2:]:
            keys.append(key)
        if any(size[1]):
            sizes = self.sizes.setdefault(index, [])
            if not sizes or sizes[-1][1] != size[1]:
                sizes.append(size)

    def publish(self) -> Path:
        """Write the header and metadata now, then keep taking PCM into the same file.

        A playback decode needs a readable ADM before it has written the whole
        track; the timelines it collected in its metadata pass are complete, so
        what is published stays right as the audio arrives.
        """
        return super().prepare_readback(self._axml())

    def close(self) -> None:
        """Write the metadata from the timelines the frames collected."""
        self.length = self.frames_written
        super().close(self._axml())

    # ---- metadata ----------------------------------------------------------------------
    #
    # Packs, channels and audioObjects count from 0x1001; the audioObject ids, stream
    # and track names follow the Atmos writer's scheme so the two files look alike.

    def _elements(self) -> list[tuple[str, str, tuple[str, str], tuple[int, ...]]]:
        """(audioObjectID, pack id, type, tracks) of each audioObject, in file order."""
        if self.objects:
            return [
                (f"AO_{_FIRST_ID + index:04X}", f"AP_0003{_FIRST_ID + index:04X}", OBJECTS, (index,))
                for index in range(len(self.tracks))
            ]
        return [("AO_1001", f"AP_0001{_FIRST_ID:04X}", DIRECT_SPEAKERS, tuple(range(len(self.tracks))))]

    def _track_ids(self, index: int) -> dict[str, str]:
        code = DIRECT_SPEAKERS[0] if self.tracks[index].speaker is not None else OBJECTS[0]
        n = f"{code}{_FIRST_ID + index:04X}"
        return {
            "channel": f"AC_{n}",
            "stream": f"AS_{n}",
            "track": f"AT_{n}_01",
            "uid": f"ATU_{index + 1:08X}",
        }

    def _packs(self) -> dict[int, str]:
        return {index: pack for _, pack, _, tracks in self._elements() for index in tracks}

    def _chna(self) -> bytes:
        packs = self._packs()
        out = bytearray(struct.pack("<HH", len(self.tracks), len(self.tracks)))
        for index in range(len(self.tracks)):
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
            "ebuCoreMain", {"xmlns": "urn:ebu:metadata-schema:ebuCore_2016", "version": "1.7"}
        )
        fmt = ET.SubElement(ET.SubElement(root, "coreMetadata"), "format")
        adm = ET.SubElement(fmt, "audioFormatExtended")
        adm.set("version", "ITU-R_BS.2076-1")
        programme = ET.SubElement(
            adm,
            "audioProgramme",
            {
                "audioProgrammeID": "APR_1001",
                "audioProgrammeName": "360RA_Master",
                "start": timecode(0),
                "end": timecode(duration),
            },
        )  # fmt: skip
        ET.SubElement(programme, "audioContentIDRef").text = "ACO_1001"
        content = ET.SubElement(
            adm,
            "audioContent",
            {"audioContentID": "ACO_1001", "audioContentName": "360RA_Content"},
        )  # fmt: skip
        for object_id, *_ in elements:
            ET.SubElement(content, "audioObjectIDRef").text = object_id

        for object_id, pack, (_, kind), tracks in elements:
            obj = ET.SubElement(
                adm,
                "audioObject",
                {
                    "audioObjectID": object_id,
                    "audioObjectName": self._object_name(kind, tracks),
                    "start": timecode(0),
                    "duration": timecode(duration),
                },
            )  # fmt: skip
            ET.SubElement(obj, "audioPackFormatIDRef").text = pack
            for index in tracks:
                ET.SubElement(obj, "audioTrackUIDRef").text = self._track_ids(index)["uid"]

        for _, pack, (code, kind), tracks in elements:
            element = ET.SubElement(
                adm,
                "audioPackFormat",
                {
                    "audioPackFormatID": pack,
                    "audioPackFormatName": self._object_name(kind, tracks),
                    "typeLabel": code,
                    "typeDefinition": kind,
                },
            )  # fmt: skip
            for index in tracks:
                ET.SubElement(element, "audioChannelFormatIDRef").text = self._track_ids(index)[
                    "channel"
                ]

        for index, track in enumerate(self.tracks):
            ids = self._track_ids(index)
            (code, kind) = DIRECT_SPEAKERS if track.speaker is not None else OBJECTS
            channel = ET.SubElement(
                adm,
                "audioChannelFormat",
                {
                    "audioChannelFormatID": ids["channel"],
                    "audioChannelFormatName": track.name,
                    "typeLabel": code,
                    "typeDefinition": kind,
                },
            )  # fmt: skip
            if track.speaker is not None:
                self._bed_block(channel, ids, track.speaker)
            else:
                self._object_blocks(channel, ids, index, duration)

        for index, track in enumerate(self.tracks):
            ids = self._track_ids(index)
            stream = ET.SubElement(
                adm,
                "audioStreamFormat",
                {
                    "audioStreamFormatID": ids["stream"],
                    "audioStreamFormatName": f"PCM_{track.name}",
                    "formatLabel": "0001",
                    "formatDefinition": "PCM",
                },
            )  # fmt: skip
            ET.SubElement(stream, "audioChannelFormatIDRef").text = ids["channel"]
            ET.SubElement(stream, "audioPackFormatIDRef").text = packs[index]
            ET.SubElement(stream, "audioTrackFormatIDRef").text = ids["track"]

        for index, track in enumerate(self.tracks):
            ids = self._track_ids(index)
            element = ET.SubElement(
                adm,
                "audioTrackFormat",
                {
                    "audioTrackFormatID": ids["track"],
                    "audioTrackFormatName": f"PCM_{track.name}",
                    "formatLabel": "0001",
                    "formatDefinition": "PCM",
                },
            )  # fmt: skip
            ET.SubElement(element, "audioStreamFormatIDRef").text = ids["stream"]

        for index in range(len(self.tracks)):
            ids = self._track_ids(index)
            uid = ET.SubElement(
                adm,
                "audioTrackUID",
                {
                    "UID": ids["uid"],
                    "sampleRate": str(self.sample_rate),
                    "bitDepth": str(BIT_DEPTH),
                },
            )  # fmt: skip
            ET.SubElement(uid, "audioTrackFormatIDRef").text = ids["track"]
            ET.SubElement(uid, "audioPackFormatIDRef").text = packs[index]

        ET.indent(root)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _object_name(self, kind: str, tracks: tuple[int, ...]) -> str:
        if kind == "DirectSpeakers":
            return "Bed"
        return self.tracks[tracks[0]].name

    def _bed_block(self, channel: ET.Element, ids: dict[str, str], speaker: CicpSpeaker) -> None:
        """A bed channel holds one block for the whole programme, where its speaker stands."""
        if speaker.is_lfe:
            ET.SubElement(channel, "frequency", typeDefinition="lowPass").text = "120"
        block = ET.SubElement(
            channel,
            "audioBlockFormat",
            {"audioBlockFormatID": f"AB_{ids['channel'][3:]}_00000001"},
        )  # fmt: skip
        for axis, value in zip(
            ("azimuth", "elevation"), (speaker.azimuth_deg, speaker.elevation_deg), strict=True
        ):
            ET.SubElement(block, "position", {"coordinate": axis}).text = number(value)

    def _object_blocks(
        self, channel: ET.Element, ids: dict[str, str], index: int, duration: float
    ) -> None:
        timeline = self._block_events(index, duration)
        ends = [t for t, *_ in timeline[1:]] + [duration]
        for n, ((t, place, size), end) in enumerate(zip(timeline, ends, strict=True), 1):
            block = ET.SubElement(
                channel,
                "audioBlockFormat",
                {
                    "audioBlockFormatID": f"AB_{ids['channel'][3:]}_{n:08X}",
                    "rtime": timecode(t),
                    "duration": timecode(max(0.0, end - t)),
                },
            )  # fmt: skip
            ET.SubElement(block, "cartesian").text = "1"
            for axis, value in zip("XYZ", place, strict=True):
                ET.SubElement(block, "position", {"coordinate": axis}).text = number(value)
            if any(size):
                for axis, value in zip(("width", "height", "depth"), size, strict=True):
                    ET.SubElement(block, axis).text = number(value)
            ET.SubElement(
                block, "jumpPosition", {"interpolationLength": "0" if n == 1 else INTERPOLATION}
            ).text = "1"

    def _block_events(
        self, index: int, duration: float
    ) -> list[tuple[float, tuple[float, float, float], tuple[float, float, float]]]:
        """The object's rooms over time, with its spread at the same instants."""
        keys = self.positions.get(index, [])
        if not keys:
            return [(0.0, (0.0, 1.0, 0.0), (0.0, 0.0, 0.0))]
        placed = events(((t, ramp, place) for t, ramp, place, _ in keys), duration, (0.0, 1.0, 0.0))
        by_position = {round(t * TICKS_PER_SECOND): place for t, place in placed}
        by_size = {
            round(t * TICKS_PER_SECOND): size for t, size in self.sizes.get(index, [])
        }
        place, size = by_position[0], by_size.get(0, (0.0, 0.0, 0.0))
        end_tick = round(duration * TICKS_PER_SECOND)
        merged: list[tuple[float, tuple[float, float, float], tuple[float, float, float]]] = []
        for tick in sorted(by_position.keys() | by_size.keys()):
            if tick and tick >= end_tick:
                break
            place = by_position.get(tick, place)
            size = by_size.get(tick, size)
            merged.append((tick / TICKS_PER_SECOND, place, size))
        return merged
