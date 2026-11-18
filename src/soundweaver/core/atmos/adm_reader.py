"""Read Dolby Atmos / EBU ADM BWF (RIFF / RF64 / BW64) files.

Parses `axml` and `chna` chunks without loading the full PCM payload into
memory, exposes random-access PCM reading, renders beds + dynamic objects to
any `SpeakerLayout`, and builds `ObjectTrace` for 3D soundfield video.
"""

from __future__ import annotations

import logging
import math
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import numpy as np

from soundweaver.core.cancel import CancellationToken
from soundweaver.core.pcm import AudioData, create_pcm_audio
from soundweaver.core.speakers import BED_POSITIONS, DEFAULT_ATMOS, SIDE_POSITIONS, SpeakerLayout

from .oamd import Program
from .render import _place
from .trace import SILENCE_DB, Key, ObjectPath, ObjectTrace

logger = logging.getLogger(__name__)

_WAVE_HEADER_IDS = (b"RIFF", b"RF64", b"BW64")
_FORMAT_PCM = 1
_FORMAT_FLOAT = 3
_FORMAT_EXTENSIBLE = 0xFFFE

# Map ADM speakerLabel values to SoundWeaver bed channel labels.
_SPEAKER_LABEL_MAP = {
    "RC_L": "L", "M+030": "L", "M+022": "L",
    "RC_R": "R", "M-030": "R", "M-022": "R",
    "RC_C": "C", "M+000": "C",
    "RC_LFE": "LFE", "LFE": "LFE", "LFE1": "LFE",
    "RC_LFE2": "LFE2", "LFE2": "LFE2",
    "RC_Ls": "Ls", "RC_Lss": "Ls", "M+090": "Ls", "M+110": "Ls",
    "RC_Rs": "Rs", "RC_Rss": "Rs", "M-090": "Rs", "M-110": "Rs",
    "RC_Lrs": "Lb", "M+135": "Lb", "M+150": "Lb",
    "RC_Rrs": "Rb", "M-135": "Rb", "M-150": "Rb",
    "RC_Lw": "Lw", "M+060": "Lw",
    "RC_Rw": "Rw", "M-060": "Rw",
    "RC_Ltf": "Tfl", "U+030": "Tfl", "U+045": "Tfl",
    "RC_Rtf": "Tfr", "U-030": "Tfr", "U-045": "Tfr",
    "RC_Lts": "Tsl", "U+090": "Tsl", "U+110": "Tsl",
    "RC_Rts": "Tsr", "U-090": "Tsr", "U-110": "Tsr",
    "RC_Ltb": "Tbl", "U+135": "Tbl",
    "RC_Rtb": "Tbr", "U-135": "Tbr",
}


class ADMReadError(ValueError):
    """Raised when an ADM BWF file is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    offset: int
    size: int


@dataclass(frozen=True, slots=True)
class ADMBlock:
    start: float
    duration: float
    x: float  # Room X: 0 (left) .. 1 (right)
    y: float  # Room Y: 0 (front) .. 1 (back)
    z: float  # Room Z: 0 (floor) .. 1 (ceiling)
    gain: float
    active: bool


@dataclass(frozen=True, slots=True)
class ADMTrack:
    channel_index: int  # 0-based PCM channel index
    name: str
    is_object: bool
    is_lfe: bool
    speaker_label: str | None
    blocks: tuple[ADMBlock, ...]


@dataclass(frozen=True, slots=True)
class ADMInfo:
    sample_rate: int
    channels: int
    bits_per_sample: int
    frame_count: int
    duration: float
    bed_channels: tuple[str, ...]
    object_count: int
    tracks: tuple[ADMTrack, ...]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_timecode(tc: str | None) -> float:
    if not tc:
        return 0.0
    text = tc.strip()
    if not text:
        return 0.0
    parts = text.split(":")
    try:
        if len(parts) == 3:
            h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
            return h * 3600.0 + m * 60.0 + s
        return float(text)
    except ValueError:
        return 0.0


def _adm_to_room(x: float, y: float, z: float, *, polar: bool = False) -> tuple[float, float, float]:
    """Convert ADM cartesian (-1..+1) or polar (az, el, dist) to room (0..1)."""
    if polar:
        az = math.radians(x)
        el = math.radians(y)
        r = max(0.0, min(1.0, z))
        # ADM polar: az=0 front, +90 left, -90 right
        cx = -math.sin(az) * math.cos(el) * r
        cy = math.cos(az) * math.cos(el) * r
        cz = math.sin(el) * r
        x, y, z = cx, cy, cz
    rx = max(0.0, min(1.0, (x + 1.0) * 0.5))
    ry = max(0.0, min(1.0, (1.0 - y) * 0.5))
    rz = max(0.0, min(1.0, z))
    return rx, ry, rz


def _scan_chunks(fh: BinaryIO) -> tuple[dict[str, ChunkInfo], int]:
    fh.seek(0, 2)
    file_size = fh.tell()
    if file_size < 12:
        raise ADMReadError("File too small to be a WAV/RF64 file")
    fh.seek(0)
    header = fh.read(12)
    if header[:4] not in _WAVE_HEADER_IDS or header[8:12] != b"WAVE":
        raise ADMReadError("Not a RIFF/RF64/BW64 WAVE file")

    chunks: dict[str, ChunkInfo] = {}
    ds64_data_size: int | None = None
    ds64_table: dict[str, int] = {}
    offset = 12
    while offset + 8 <= file_size:
        fh.seek(offset)
        raw_hdr = fh.read(8)
        if len(raw_hdr) < 8:
            break
        cid = raw_hdr[:4].decode("latin-1", errors="replace")
        length = struct.unpack("<I", raw_hdr[4:8])[0]
        payload_offset = offset + 8
        if cid == "ds64" and length >= 28:
            ds64_raw = fh.read(min(length, file_size - payload_offset))
            if len(ds64_raw) >= 28:
                _, ds64_data_size, _, table_len = struct.unpack("<QQQI", ds64_raw[:28])
                pos = 28
                for _ in range(table_len):
                    if pos + 12 > len(ds64_raw):
                        break
                    tid = ds64_raw[pos : pos + 4].decode("latin-1", errors="replace")
                    tsize = struct.unpack("<Q", ds64_raw[pos + 4 : pos + 12])[0]
                    ds64_table[tid] = tsize
                    pos += 12
        if length == 0xFFFFFFFF:
            if cid in ds64_table:
                length = ds64_table[cid]
            elif cid == "data" and ds64_data_size is not None:
                length = ds64_data_size
            else:
                raise ADMReadError(f"Missing 64-bit size for chunk {cid!r}")
        chunks[cid] = ChunkInfo(payload_offset, length)
        offset = payload_offset + length + (length & 1)
    return chunks, file_size


def is_adm_bwf(path: Path) -> bool:
    """Fast header check for whether `path` is a WAV/RF64 file with `axml` and `chna` chunks."""
    if path.suffix.lower() not in (".wav", ".bwf", ".rf64"):
        return False
    try:
        with path.open("rb") as fh:
            chunks, _ = _scan_chunks(fh)
        return "axml" in chunks and "chna" in chunks and "fmt " in chunks and "data" in chunks
    except (OSError, ADMReadError):
        return False


def _parse_fmt(raw: bytes) -> tuple[int, int, int, int, int]:
    if len(raw) < 16:
        raise ADMReadError("Truncated fmt chunk")
    codec, channels, sample_rate, _, block_align, bits = struct.unpack("<HHIIHH", raw[:16])
    if codec == _FORMAT_EXTENSIBLE and len(raw) >= 40:
        codec = struct.unpack("<H", raw[24:26])[0]
    if codec not in (_FORMAT_PCM, _FORMAT_FLOAT):
        raise ADMReadError(f"Unsupported ADM PCM format tag: {codec}")
    if bits not in (16, 24, 32):
        raise ADMReadError(f"Unsupported ADM bit depth: {bits}")
    return codec, channels, sample_rate, block_align, bits


def _parse_chna(raw: bytes) -> list[tuple[int, str, str, str]]:
    if len(raw) < 4:
        raise ADMReadError("Truncated chna chunk")
    num_tracks, num_uids = struct.unpack("<HH", raw[:4])
    entries: list[tuple[int, str, str, str]] = []
    pos = 4
    while pos + 40 <= len(raw) and len(entries) < max(num_uids, num_tracks, len(raw) // 40):
        track_idx = struct.unpack("<H", raw[pos : pos + 2])[0]
        if track_idx == 0:
            pos += 40
            continue
        uid = raw[pos + 2 : pos + 14].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
        track_ref = raw[pos + 14 : pos + 28].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
        pack_ref = raw[pos + 28 : pos + 39].split(b"\x00", 1)[0].decode("ascii", errors="ignore").strip()
        entries.append((track_idx - 1, uid, track_ref, pack_ref))
        pos += 40
    return entries


def _parse_axml(xml_bytes: bytes) -> tuple[dict[str, tuple[str, str, list[ADMBlock], str | None]], dict[str, str], dict[str, str]]:
    """Parse axml into channel formats, trackFormat->channelFormat map, and trackUID->channelFormat map."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ADMReadError(f"Invalid axml XML: {exc}") from exc

    channel_formats: dict[str, tuple[str, str, list[ADMBlock], str | None]] = {}
    track_to_channel: dict[str, str] = {}
    uid_to_channel: dict[str, str] = {}
    stream_to_channel: dict[str, str] = {}

    for elem in root.iter():
        tag = _local(elem.tag)
        if tag == "audioStreamFormat":
            sid = elem.attrib.get("audioStreamFormatID", "")
            for child in elem:
                if _local(child.tag) == "audioChannelFormatIDRef" and child.text:
                    stream_to_channel[sid] = child.text.strip()
        elif tag == "audioTrackFormat":
            tid = elem.attrib.get("audioTrackFormatID", "")
            for child in elem:
                if _local(child.tag) == "audioStreamFormatIDRef" and child.text:
                    sref = child.text.strip()
                    if sref in stream_to_channel:
                        track_to_channel[tid] = stream_to_channel[sref]
        elif tag == "audioTrackUID":
            uid = elem.attrib.get("UID", "")
            for child in elem:
                ctag = _local(child.tag)
                if ctag == "audioChannelFormatIDRef" and child.text:
                    uid_to_channel[uid] = child.text.strip()
                elif ctag == "audioTrackFormatIDRef" and child.text and uid not in uid_to_channel:
                    tref = child.text.strip()
                    if tref in track_to_channel:
                        uid_to_channel[uid] = track_to_channel[tref]
        elif tag == "audioChannelFormat":
            cid = elem.attrib.get("audioChannelFormatID", "")
            cname = elem.attrib.get("audioChannelFormatName", cid)
            type_label = elem.attrib.get("typeLabel", "0001")
            blocks: list[ADMBlock] = []
            speaker_label: str | None = None
            for child in elem:
                if _local(child.tag) != "audioBlockFormat":
                    continue
                start = _parse_timecode(child.attrib.get("rtime"))
                dur = _parse_timecode(child.attrib.get("duration"))
                x, y, z = 0.0, 1.0, 0.0
                polar = False
                gain = 1.0
                for sub in child:
                    stag = _local(sub.tag)
                    val_text = (sub.text or "").strip()
                    if stag == "position" and val_text:
                        coord = sub.attrib.get("coordinate", "").lower()
                        try:
                            v = float(val_text)
                        except ValueError:
                            v = 0.0
                        if coord in ("azimuth", "az"):
                            polar, x = True, v
                        elif coord in ("elevation", "el"):
                            polar, y = True, v
                        elif coord in ("distance", "r"):
                            polar, z = True, v
                        elif coord == "x":
                            x = v
                        elif coord == "y":
                            y = v
                        elif coord == "z":
                            z = v
                    elif stag == "gain" and val_text:
                        try:
                            g = float(val_text)
                            if sub.attrib.get("gainUnit", "").lower() == "db":
                                gain = 10.0 ** (g / 20.0)
                            else:
                                gain = g
                        except ValueError:
                            pass
                    elif stag == "speakerLabel" and val_text and speaker_label is None:
                        speaker_label = _SPEAKER_LABEL_MAP.get(val_text, val_text)
                rx, ry, rz = _adm_to_room(x, y, z, polar=polar)
                blocks.append(ADMBlock(start, dur, rx, ry, rz, gain, gain > 0.0))
            channel_formats[cid] = (type_label, cname, blocks, speaker_label)

    return channel_formats, track_to_channel, uid_to_channel


class PreparedADM:
    """Random-access ADM BWF reader with speaker rendering and object tracing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with path.open("rb") as fh:
            chunks, _ = _scan_chunks(fh)
            for req in ("fmt ", "data", "axml", "chna"):
                if req not in chunks:
                    raise ADMReadError(f"Missing {req!r} chunk in {path.name}")

            fh.seek(chunks["fmt "].offset)
            codec, channels, sample_rate, block_align, bits = _parse_fmt(fh.read(chunks["fmt "].size))
            fh.seek(chunks["chna"].offset)
            chna_entries = _parse_chna(fh.read(chunks["chna"].size))
            fh.seek(chunks["axml"].offset)
            axml_bytes = fh.read(chunks["axml"].size)

        channel_formats, track_to_channel, uid_to_channel = _parse_axml(axml_bytes)
        self._codec = codec
        self.channels = channels
        self.sample_rate = sample_rate
        self._stride = block_align
        self._bits = bits
        self._data_chunk = chunks["data"]
        self.frame_count = self._data_chunk.size // self._stride
        self.duration = self.frame_count / self.sample_rate if self.sample_rate else 0.0

        tracks: list[ADMTrack] = []
        bed_labels: list[str] = []
        seen_channels: set[int] = set()
        for ch_idx, uid, track_ref, _ in chna_entries:
            if ch_idx < 0 or ch_idx >= channels or ch_idx in seen_channels:
                continue
            seen_channels.add(ch_idx)
            cf_id = uid_to_channel.get(uid) or track_to_channel.get(track_ref)
            if not cf_id and track_ref.startswith("AT_") and len(track_ref) >= 11:
                cf_id = "AC_" + track_ref[3:11]
            cf = channel_formats.get(cf_id or "")
            if cf is None:
                tracks.append(ADMTrack(ch_idx, f"Ch {ch_idx + 1}", False, False, None, ()))
                continue
            type_label, cname, blocks, spk = cf
            is_obj = type_label == "0003"
            is_lfe = (spk in ("LFE", "LFE2")) or ("lfe" in cname.lower())
            if not is_obj and spk and spk not in bed_labels:
                bed_labels.append(spk)
            tracks.append(ADMTrack(ch_idx, cname, is_obj, is_lfe, spk, tuple(blocks)))

        self._fh = None
        tracks.sort(key=lambda t: t.channel_index)
        self.tracks = tuple(tracks)
        self.info = ADMInfo(
            sample_rate=self.sample_rate,
            channels=self.channels,
            bits_per_sample=self._bits,
            frame_count=self.frame_count,
            duration=self.duration,
            bed_channels=tuple(bed_labels),
            object_count=sum(1 for t in self.tracks if t.is_object),
            tracks=self.tracks,
        )

    def close(self) -> None:
        """Close the cached read handle if open."""
        fh = getattr(self, "_fh", None)
        if fh is not None:
            self._fh = None
            fh.close()

    def __del__(self) -> None:
        self.close()

    def read_frames(self, start_frame: int, count: int) -> np.ndarray:
        """Read `count` frames starting at `start_frame` as float32 shape `(frames, channels)`."""
        if start_frame < 0 or start_frame >= self.frame_count or count <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        actual = min(count, self.frame_count - start_frame)
        offset = self._data_chunk.offset + start_frame * self._stride
        if self._fh is None or self._fh.closed:
            self._fh = self.path.open("rb")
        self._fh.seek(offset)
        raw = self._fh.read(actual * self._stride)
        actual = len(raw) // self._stride
        if actual == 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        raw = raw[: actual * self._stride]

        if self._codec == _FORMAT_FLOAT and self._bits == 32:
            arr = np.frombuffer(raw, dtype="<f4").astype(np.float32, copy=False)
            return np.nan_to_num(arr.reshape(actual, self.channels), nan=0.0, posinf=0.0, neginf=0.0)
        if self._bits == 16:
            arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif self._bits == 24:
            u8 = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            i32 = (
                u8[:, 0].astype(np.int32)
                | (u8[:, 1].astype(np.int32) << 8)
                | (u8[:, 2].astype(np.int32) << 16)
            )
            i32 = np.where(i32 & 0x800000, i32 | ~0xFFFFFF, i32)
            arr = i32.astype(np.float32) / 8388608.0
        else:
            arr = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0

        return arr.reshape(actual, self.channels)

    def _track_gains_at(self, track: ADMTrack, time_s: float, layout: SpeakerLayout) -> np.ndarray:
        """Speaker gain vector for `track` at `time_s`."""
        if track.is_lfe:
            g = np.zeros(len(layout.channels), dtype=np.float32)
            if layout.has("LFE"):
                g[layout.index["LFE"]] = 1.0
            return g
        if not track.is_object and track.speaker_label:
            if track.speaker_label in layout.index:
                g = np.zeros(len(layout.channels), dtype=np.float32)
                g[layout.index[track.speaker_label]] = 1.0
                return g
            pos = (
                SIDE_POSITIONS.get(track.speaker_label)
                if layout.has("Lb", "Rb") and track.speaker_label in SIDE_POSITIONS
                else BED_POSITIONS.get(track.speaker_label)
            )
            if pos is not None:
                return _place(layout, *pos).astype(np.float32)
        if not track.blocks:
            return np.zeros(len(layout.channels), dtype=np.float32)
        # Find active block at time_s
        blk = track.blocks[0]
        for b in track.blocks:
            if b.start <= time_s:
                blk = b
            else:
                break
        if not blk.active or blk.gain <= 0.0:
            return np.zeros(len(layout.channels), dtype=np.float32)
        return (_place(layout, blk.x, blk.y, blk.z) * blk.gain).astype(np.float32)

    def render_to_layout(
        self,
        layout: SpeakerLayout,
        token: CancellationToken,
        on_progress=None,
    ) -> AudioData:
        """Render all ADM beds and dynamic objects into `layout` as temporary PCM AudioData."""
        chunk_frames = 4096
        audio = create_pcm_audio(len(layout.channels), self.frame_count, self.sample_rate)
        try:
            pos = 0
            while pos < self.frame_count:
                token.raise_if_cancelled()
                pcm = self.read_frames(pos, chunk_frames)
                if pcm.shape[0] == 0:
                    break
                n = pcm.shape[0]
                mid_t = (pos + n * 0.5) / self.sample_rate
                out = np.zeros((n, len(layout.channels)), dtype=np.float32)
                for tr in self.tracks:
                    g = self._track_gains_at(tr, mid_t, layout)
                    if np.any(g):
                        out += pcm[:, tr.channel_index : tr.channel_index + 1] * g[np.newaxis, :]
                audio.samples[:, pos : pos + n] = out.T
                pos += n
                if on_progress is not None and self.frame_count > 0:
                    on_progress(min(1.0, pos / self.frame_count))
            return audio
        except BaseException:
            audio.cleanup()
            raise

    def build_trace(
        self,
        layout: SpeakerLayout = DEFAULT_ATMOS,
        step_seconds: float = 0.02,
        token: CancellationToken | None = None,
        on_progress=None,
    ) -> ObjectTrace:
        """Build `ObjectTrace` for 3D soundfield visualization from ADM BWF."""
        obj_tracks = [tr for tr in self.tracks if tr.is_object]
        speakers = tuple(s for s in layout.channels if s != "LFE")
        spk_indices = [layout.index[s] for s in speakers]

        frames_per_step = max(1, round(step_seconds * self.sample_rate))
        actual_step = frames_per_step / self.sample_rate
        n_steps = max(1, math.ceil(self.frame_count / frames_per_step)) if self.frame_count else 1

        levels = np.full((len(obj_tracks), n_steps), SILENCE_DB, dtype=np.float32)
        shares = np.zeros((len(obj_tracks), n_steps, len(speakers)), dtype=np.float32)

        for step_idx in range(n_steps):
            if token is not None:
                token.raise_if_cancelled()
            start = step_idx * frames_per_step
            pcm = self.read_frames(start, frames_per_step)
            t_mid = (start + frames_per_step * 0.5) / self.sample_rate
            for oi, tr in enumerate(obj_tracks):
                if pcm.shape[0] > 0:
                    peak = float(np.max(np.abs(pcm[:, tr.channel_index])))
                    levels[oi, step_idx] = 20.0 * math.log10(peak) if peak > 1e-6 else SILENCE_DB
                g = self._track_gains_at(tr, t_mid, layout)
                energy = g[spk_indices] ** 2
                tot = float(np.sum(energy))
                if tot > 0.0:
                    shares[oi, step_idx, :] = energy / tot
            if on_progress is not None:
                on_progress((step_idx + 1) / n_steps)

        paths: list[ObjectPath] = []
        for oi, tr in enumerate(obj_tracks):
            keys: list[Key] = []
            prev_t = 0.0
            for blk in tr.blocks:
                ramp = max(0.0, blk.start - prev_t) if keys else 0.0
                keys.append((blk.start, ramp, blk.x, blk.y, blk.z, blk.active))
                prev_t = blk.start
            if not keys:
                keys.append((0.0, 0.0, 0.5, 0.5, 0.0, True))
            paths.append(ObjectPath(index=oi, keys=keys, levels=levels[oi], shares=shares[oi]))

        return ObjectTrace(
            sample_rate=self.sample_rate,
            level_start=0.0,
            level_step=actual_step,
            duration=self.duration,
            program=Program(
                bed_labels=tuple(self.info.bed_channels),
                isf_objects=0,
                dynamic_objects=len(obj_tracks),
            ),
            objects=tuple(paths),
            speakers=speakers,
        )
