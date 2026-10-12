"""The Atmos objects as an ADM BWF: one channel per object, the positions in `axml`.

Channel k is OAMD object k: bed objects (the LFE among them, taken from the
core as the speaker render does) become DirectSpeakers, ISF and dynamic
objects become Objects.  Each object's own gain, with its ramps, is applied
to its audio, so the file needs no gain metadata and renders the same mix.

The file is 24-bit PCM (format tag 1, which more readers accept than
WAVE_FORMAT_EXTENSIBLE) with a standard 40-byte-per-track `chna`; a JUNK
chunk reserves room for `ds64`, so a file past 4 GiB becomes BW64 (BS.2088).
Positions use ADM cartesian coordinates: X left -1 to right +1, Y back -1 to
front +1, Z the listener's plane 0 to the ceiling +1.
"""

from __future__ import annotations

import shutil
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import BinaryIO

import numpy as np

from dolbytool.core.paths import partial_path
from dolbytool.core.speakers import BED_POSITIONS, SIDE_POSITIONS

from .oamd import Program
from .render import joc_objects
from .sinks import RenderedFrame
from .trace import Key

BIT_DEPTH = 24
_FULL_SCALE = 2 ** (BIT_DEPTH - 1)
_RIFF_LIMIT = 0xFFFFFFFF
_DS64_SIZE = 28

# ADM speaker labels of the bed channels OAMD can name, with their room positions.
_ADM_LABELS = {
    "L": "RC_L", "R": "RC_R", "C": "RC_C", "LFE": "RC_LFE", "Ls": "RC_Ls", "Rs": "RC_Rs",
    "Lb": "RC_Lrs", "Rb": "RC_Rrs", "Tfl": "RC_Ltf", "Tfr": "RC_Rtf", "Tsl": "RC_Lts",
    "Tsr": "RC_Rts", "Tbl": "RC_Ltr", "Tbr": "RC_Rtr", "Lw": "RC_Lw", "Rw": "RC_Rw",
    "LFE2": "RC_LFE2",
}  # fmt: skip
_BED = {label: (BED_POSITIONS[label], adm) for label, adm in _ADM_LABELS.items()}
# With back surrounds in the bed, Ls and Rs are the side pair.
_SIDE = {"Ls": (SIDE_POSITIONS["Ls"], "RC_Lss"), "Rs": (SIDE_POSITIONS["Rs"], "RC_Rss")}

DIRECT_SPEAKERS = ("0001", "DirectSpeakers")
OBJECTS = ("0003", "Objects")


def _cartesian(x: float, y: float, z: float) -> tuple[float, float, float]:
    return 2 * x - 1, 1 - 2 * y, z


def _timecode(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:08.5f}"


def adm_block(f: RenderedFrame, channels: int) -> np.ndarray:
    """One frame's span with channel k carrying OAMD object k, its own gain applied."""
    meta = f.metadata
    block = np.zeros((channels, f.span.stop - f.span.start))
    for o, index in enumerate(joc_objects(meta)):
        block[index] = (f.essence[o] * f.envelope[o])[f.span]
    for index, label in enumerate(meta.program.bed_labels):
        if label == "LFE":
            block[index] = f.lfe[f.span]
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


class AdmFile:
    """Writes the header up front, takes the PCM in order, and the metadata at `close`."""

    def __init__(
        self, target: Path, sample_rate: int, length: int, program: Program, channels: int
    ) -> None:
        self.target, self.sample_rate, self.length = target, sample_rate, length
        self.program, self.channels = program, channels
        self.keys: dict[int, list[Key]] = {}
        self.data_bytes = 0
        target.parent.mkdir(parents=True, exist_ok=True)
        # written aside and moved into place by `close`, so a failed run leaves no torn file
        self.partial = partial_path(target)
        self.file: BinaryIO = open(self.partial, "wb")  # noqa: SIM115 - closed in close()
        w = self.file
        w.write(b"RIFF" + struct.pack("<I", 0) + b"WAVE")
        w.write(b"JUNK" + struct.pack("<I", _DS64_SIZE) + bytes(_DS64_SIZE))
        align = channels * BIT_DEPTH // 8
        w.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
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
                w.write(b"BW64" + struct.pack("<I", _RIFF_LIMIT) + b"WAVE")
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

    def _kind(self, index: int) -> tuple[str, str]:
        return DIRECT_SPEAKERS if index < len(self.program.bed_labels) else OBJECTS

    def _ids(self, index: int) -> dict[str, str]:
        code, _ = self._kind(index)
        n = f"{0x1001 + index:04X}"
        return {
            "object": f"AO_{n}",
            "pack": f"AP_{code}{n}",
            "channel": f"AC_{code}{n}",
            "stream": f"AS_{code}{n}",
            "track": f"AT_{code}{n}_01",
            "uid": f"ATU_{index + 1:08X}",
        }

    def _name(self, index: int) -> str:
        labels = self.program.bed_labels
        if index < len(labels):
            return f"Bed {labels[index]}"
        if index < self.program.anchored:
            return f"ISF {index}"
        return f"Object {index}"

    def _chna(self) -> bytes:
        out = bytearray(struct.pack("<HH", self.channels, self.channels))
        for index in range(self.channels):
            ids = self._ids(index)
            out += struct.pack("<H", index + 1)
            out += ids["uid"].encode().ljust(12, b"\0")
            out += ids["track"].encode().ljust(14, b"\0")
            out += ids["pack"].encode().ljust(11, b"\0")
            out += b"\0"
        return bytes(out)

    def _axml(self) -> bytes:
        duration = self.length / self.sample_rate

        root = ET.Element(
            "ebuCoreMain",
            {"xmlns": "urn:ebu:metadata-schema:ebuCore_2016", "version": "1.7"},
        )
        fmt = ET.SubElement(ET.SubElement(root, "coreMetadata"), "format")
        adm = ET.SubElement(fmt, "audioFormatExtended", {"version": "ITU-R_BS.2076-2"})
        programme = ET.SubElement(adm, "audioProgramme", {
            "audioProgrammeID": "APR_1001", "audioProgrammeName": self.target.stem,
            "start": _timecode(0), "end": _timecode(duration),
        })  # fmt: skip
        ET.SubElement(programme, "audioContentIDRef").text = "ACO_1001"
        content = ET.SubElement(adm, "audioContent", {
            "audioContentID": "ACO_1001", "audioContentName": self.target.stem,
        })  # fmt: skip
        for index in range(self.channels):
            ET.SubElement(content, "audioObjectIDRef").text = self._ids(index)["object"]

        # one audioObject per channel, in channel order
        for index in range(self.channels):
            ids, name = self._ids(index), self._name(index)
            obj = ET.SubElement(adm, "audioObject", {
                "audioObjectID": ids["object"], "audioObjectName": name,
                "start": _timecode(0), "duration": _timecode(duration),
            })  # fmt: skip
            ET.SubElement(obj, "audioPackFormatIDRef").text = ids["pack"]
            ET.SubElement(obj, "audioTrackUIDRef").text = ids["uid"]

        for index in range(self.channels):
            ids, name = self._ids(index), self._name(index)
            code, kind = self._kind(index)
            pack = ET.SubElement(adm, "audioPackFormat", {
                "audioPackFormatID": ids["pack"], "audioPackFormatName": name,
                "typeLabel": code, "typeDefinition": kind,
            })  # fmt: skip
            ET.SubElement(pack, "audioChannelFormatIDRef").text = ids["channel"]
            channel = ET.SubElement(adm, "audioChannelFormat", {
                "audioChannelFormatID": ids["channel"], "audioChannelFormatName": name,
                "typeLabel": code, "typeDefinition": kind,
            })  # fmt: skip
            if kind == "DirectSpeakers":
                self._bed_block(channel, ids, index, duration)
            else:
                keys = self.keys.get(index, [(0.0, 0.0, 0.5, 0.5, 0.0, True)])
                self._object_blocks(channel, ids, keys, duration)

        for index in range(self.channels):
            ids, name = self._ids(index), self._name(index)
            stream = ET.SubElement(adm, "audioStreamFormat", {
                "audioStreamFormatID": ids["stream"], "audioStreamFormatName": f"PCM_{name}",
                "formatLabel": "0001", "formatDefinition": "PCM",
            })  # fmt: skip
            ET.SubElement(stream, "audioChannelFormatIDRef").text = ids["channel"]
            ET.SubElement(stream, "audioTrackFormatIDRef").text = ids["track"]
            track = ET.SubElement(adm, "audioTrackFormat", {
                "audioTrackFormatID": ids["track"], "audioTrackFormatName": f"PCM_{name}",
                "formatLabel": "0001", "formatDefinition": "PCM",
            })  # fmt: skip
            ET.SubElement(track, "audioStreamFormatIDRef").text = ids["stream"]
            uid = ET.SubElement(adm, "audioTrackUID", {
                "UID": ids["uid"], "sampleRate": str(self.sample_rate), "bitDepth": str(BIT_DEPTH),
            })  # fmt: skip
            ET.SubElement(uid, "audioTrackFormatIDRef").text = ids["track"]
            ET.SubElement(uid, "audioPackFormatIDRef").text = ids["pack"]

        ET.indent(root)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)

    def _bed_block(self, channel: ET.Element, ids: dict[str, str], index: int, duration: float):
        labels = self.program.bed_labels
        label = labels[index]
        sides = "Lb" in labels or "Rb" in labels
        position, speaker = (_SIDE if sides and label in _SIDE else _BED)[label]
        block = ET.SubElement(channel, "audioBlockFormat", {
            "audioBlockFormatID": f"AB_{ids['channel'][3:]}_00000001",
            "rtime": _timecode(0), "duration": _timecode(duration),
        })  # fmt: skip
        ET.SubElement(block, "speakerLabel").text = speaker
        ET.SubElement(block, "cartesian").text = "1"
        for axis, value in zip("XYZ", _cartesian(*position), strict=True):
            ET.SubElement(block, "position", {"coordinate": axis}).text = f"{value:.4f}"

    @staticmethod
    def _object_blocks(channel: ET.Element, ids: dict[str, str], keys, duration: float) -> None:
        # Keys before the container starts collapse onto its first sample; keys past it go.
        clamped: list[tuple[float, float, float, float, float]] = []
        for t, ramp, x, y, z, _active in keys:
            t = max(0.0, t)
            if t >= duration:
                break
            if clamped and t <= clamped[-1][0]:
                clamped.pop()
            clamped.append((t, ramp, x, y, z))
        ends = [k[0] for k in clamped[1:]] + [duration]
        for n, ((t, ramp, x, y, z), end) in enumerate(zip(clamped, ends, strict=True), 1):
            block = ET.SubElement(channel, "audioBlockFormat", {
                "audioBlockFormatID": f"AB_{ids['channel'][3:]}_{n:08X}",
                "rtime": _timecode(t), "duration": _timecode(end - t),
            })  # fmt: skip
            ET.SubElement(block, "cartesian").text = "1"
            for axis, value in zip("XYZ", _cartesian(x, y, z), strict=True):
                ET.SubElement(block, "position", {"coordinate": axis}).text = f"{value:.4f}"
            jump = ET.SubElement(
                block, "jumpPosition", {"interpolationLength": f"{min(ramp, end - t):.5f}"}
            )
            jump.text = "1"
