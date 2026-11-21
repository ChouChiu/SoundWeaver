"""MPEG-H 3D Audio (360 Reality Audio / 360RA) detection, CICP layouts, decoding, ADM, and streaming.

The bundled decoder renders a stream to the CICP layout it is asked for.  It can
also hand back the objects themselves — signals and the metadata they are panned
with — which is what the 360RA ADM export is built from, so one decode pass
serves the stems, the player and the master.
"""

from __future__ import annotations

import json
import logging
import math
import struct
import subprocess
import tempfile
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from soundweaver.core import mp4
from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.codecs.mpegh_adm import MpeghAdmFile, key_of
from soundweaver.core.codecs.mpegh_eri import MpeghObject, read_record
from soundweaver.core.codecs.native import NativePcmStream, NativeProcess, find_binary, run_native
from soundweaver.core.codecs.packet_io import ChunkReader, read_bits
from soundweaver.core.codecs.pcm_stream import BlockPcmStream, PcmStream
from soundweaver.core.pcm import AudioData, read_audio
from soundweaver.core.pcm import scratch as pcm_scratch
from soundweaver.core.process import NO_WINDOW

logger = logging.getLogger(__name__)

MPEGH_SUFFIXES = (".mhas",)
MPEGH_SAMPLE_ENTRIES = (b"mhm1", b"mhm2", b"mha1", b"mha2")

# Reorder decoded CICP 13 (22.2) raw testbench output to standard SoundWeaver / WalkMix order.
# The legacy testbench already applies this permutation in its streaming mode.
CICP_22_2_REMAP: tuple[int, ...] = (
    6, 7, 2, 3, 4, 5, 0, 1, 8, 9, 10, 11,
    12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
)

_WORKER = "soundweaver_mpegh"
_LEGACY = "ia_mpeghd_testbench"
_U32 = struct.Struct("<I")
# setup: CICP layout, raw-frame flag, and the object flag
_WORKER_SETUP = struct.Struct("<3I")
# status, consumed, init flag, rate, channels, render bytes, then the object payload sizes
_WORKER_REPLY = struct.Struct("<10I")
_FLOAT_PROTOCOL = 0x80000000
_OBJECT_PROTOCOL = 0x40000000
_OBJECT_FLAG = 0x1
_INIT_MORE_INPUT = 0x1000
_EXEC_MORE_INPUT = 0x1800
_PCM16_UNITS = 256.0
_SIGNAL_SCALE = 1.0 / 8388608.0
_Mp4Source = tuple[mp4.SampleEntry, mp4.SampleTable, bytes]

# Available CICP indices sorted canonically (default 13 = 22.2 / 360RA WalkMix max bed).
SUPPORTED_CICP: tuple[int, ...] = (
    1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
)


class MpeghError(RuntimeError):
    """Raised when MPEG-H / 360RA probing, decoding, or streaming fails."""


@dataclass(frozen=True)
class CicpSpeaker:
    """One speaker position in a CICP layout."""

    name: str
    azimuth_deg: float
    elevation_deg: float
    is_lfe: bool = False

    @property
    def layer(self) -> str:
        """Elevation band used by the 360RA WalkMix spherical monitor."""
        if self.elevation_deg >= 20.0:
            return "upper"
        if self.elevation_deg <= -10.0:
            return "lower"
        return "middle"

    def cartesian(self) -> tuple[float, float, float]:
        """Convert (az, el) degrees to right-hand dome coordinates matching WalkMix."""
        az = math.radians(self.azimuth_deg)
        el = math.radians(self.elevation_deg)
        x = -math.sin(az) * math.cos(el) * 0.92
        y = math.cos(az) * math.cos(el) * 0.92
        z = 0.2 + math.sin(el) * 0.72
        return (x, y, z)


@dataclass(frozen=True)
class CicpLayout:
    """MPEG-H CICP target speaker layout definition."""

    index: int
    label: str
    speakers: tuple[CicpSpeaker, ...]

    @property
    def channels(self) -> int:
        return len(self.speakers)

    @property
    def channel_names(self) -> tuple[str, ...]:
        return tuple(s.name for s in self.speakers)

@dataclass(frozen=True)
class MpeghInfo:
    """Probed metadata for an MPEG-H / 360RA bitstream or MP4 container."""

    sample_rate: int
    channels: int
    duration: float
    cicp: int
    container: str


@dataclass(frozen=True, slots=True)
class MpeghFrame:
    """One decoded frame: the rendered layout, and the objects when they were asked for."""

    # (samples, channels) of the rendered CICP layout, in this package's channel order
    samples: np.ndarray
    # the frame's objects, in bitstream order, and their (objects, samples) signals
    objects: tuple[MpeghObject, ...] = ()
    signals: np.ndarray | None = None
    @property
    def rendered(self) -> np.ndarray:
        """The rendered layout as (channels, samples), the order ADM tracks use."""
        return self.samples.T


def _sp(specs: tuple[tuple[str, float, float] | tuple[str, float, float, bool], ...]) -> tuple[CicpSpeaker, ...]:
    out: list[CicpSpeaker] = []
    for item in specs:
        if len(item) == 4:
            out.append(CicpSpeaker(item[0], float(item[1]), float(item[2]), bool(item[3])))
        else:
            out.append(CicpSpeaker(item[0], float(item[1]), float(item[2]), False))
    return tuple(out)


CICP_LAYOUTS: dict[int, CicpLayout] = {
    1: CicpLayout(1, "1.0 Mono (C)", _sp((("C", 0, 0),))),
    2: CicpLayout(2, "2.0 Stereo (L, R)", _sp((("L", 30, 0), ("R", -30, 0)))),
    3: CicpLayout(3, "3.0 (L, R, C)", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0)))),
    4: CicpLayout(4, "3/1 (L, R, C, Cs)", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("Cs", 180, 0)))),
    5: CicpLayout(5, "5.0", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("Ls", 110, 0), ("Rs", -110, 0)))),
    6: CicpLayout(6, "5.1", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0)))),
    7: CicpLayout(7, "7.1 SDDS", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0), ("Lc", 60, 0), ("Rc", -60, 0)))),
    9: CicpLayout(9, "2/1 (L, R, Cs)", _sp((("L", 30, 0), ("R", -30, 0), ("Cs", 180, 0)))),
    10: CicpLayout(10, "4.0 Quad", _sp((("L", 30, 0), ("R", -30, 0), ("Ls", 110, 0), ("Rs", -110, 0)))),
    11: CicpLayout(11, "6.1", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0), ("Cs", 180, 0)))),
    12: CicpLayout(12, "7.1", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0), ("Lrs", 135, 0), ("Rrs", -135, 0)))),
    13: CicpLayout(
        13,
        "22.2 (360RA)",
        _sp((
            ("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 45, -15, True),
            ("Lrs", 135, 0), ("Rrs", -135, 0), ("Lc", 60, 0), ("Rc", -60, 0),
            ("Cs", 180, 0), ("LFE2", -45, -15, True), ("Lss", 90, 0), ("Rss", -90, 0),
            ("Lv", 45, 35), ("Rv", -45, 35), ("Cv", 0, 35), ("Ts", 0, 90),
            ("Lvr", 135, 35), ("Rvr", -135, 35), ("Lvss", 90, 35), ("Rvss", -90, 35),
            ("Cvr", 180, 35), ("Cb", 0, -15), ("Lb", 45, -15), ("Rb", -45, -15),
        )),
    ),
    14: CicpLayout(14, "5.1.2", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0), ("Lv", 30, 35), ("Rv", -30, 35)))),
    15: CicpLayout(15, "5.5.2 (360RA)", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 45, -15, True), ("Lrs", 135, 0), ("Rrs", -135, 0), ("LFE2", -45, -15, True), ("Lss", 90, 0), ("Rss", -90, 0), ("Lv", 45, 35), ("Rv", -45, 35), ("Cvr", 180, 35)))),
    16: CicpLayout(16, "5.1.4", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0), ("Lv", 30, 35), ("Rv", -30, 35), ("Lvr", 110, 35), ("Rvr", -110, 35)))),
    17: CicpLayout(17, "5.1.6 (360RA)", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0), ("Lv", 30, 35), ("Rv", -30, 35), ("Cv", 0, 35), ("Lvr", 110, 35), ("Rvr", -110, 35), ("Ts", 0, 90)))),
    18: CicpLayout(18, "7.1.6", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Ls", 110, 0), ("Rs", -110, 0), ("Lrs", 150, 0), ("Rrs", -150, 0), ("Lv", 30, 35), ("Rv", -30, 35), ("Cv", 0, 35), ("Lvr", 110, 35), ("Rvr", -110, 35), ("Ts", 0, 90)))),
    19: CicpLayout(19, "7.1.4 (5/6.1)", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Lrs", 135, 0), ("Rrs", -135, 0), ("Lss", 90, 0), ("Rss", -90, 0), ("Lv", 30, 35), ("Rv", -30, 35), ("Lvr", 135, 35), ("Rvr", -135, 35)))),
    20: CicpLayout(20, "9.1.4 (7/6.1)", _sp((("L", 30, 0), ("R", -30, 0), ("C", 0, 0), ("LFE", 0, 0, True), ("Lrs", 135, 0), ("Rrs", -135, 0), ("Lss", 90, 0), ("Rss", -90, 0), ("Lv", 45, 35), ("Rv", -45, 35), ("Lvs", 135, 35), ("Rvs", -135, 35), ("Leos", 60, 0), ("Reos", -60, 0)))),
}


def cicp_layout(cicp: int) -> CicpLayout:
    """Return `CicpLayout` for `cicp`, falling back to CICP 13 (22.2) if unknown."""
    return CICP_LAYOUTS.get(cicp, CICP_LAYOUTS[13])


def find_decoder() -> str | None:
    """Locate the MPEG-H frame decoder or a configured legacy testbench."""
    return find_binary(_WORKER, "MPEGH_DECODER_BIN") or find_binary(_LEGACY)


def is_mpegh_file(path: Path) -> bool:
    """Detect raw `.mhas` bitstreams or MP4/M4A containers with MPEG-H audio (`mhm1`, `mha1`, etc.)."""
    suf = path.suffix.lower()
    if suf in MPEGH_SUFFIXES:
        return mp4.has_mhas_sync(path)
    if suf in (".mp4", ".m4a", ".m4v", ".mov"):
        return mp4.has_mp4_sample_entry(path, MPEGH_SAMPLE_ENTRIES)
    return False


def _ffprobe_duration_rate(path: Path) -> tuple[int, float]:
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=sample_rate:format=duration",
                "-of",
                "json",
                str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15.0,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return (48000, 0.0)
    if proc.returncode != 0:
        return (48000, 0.0)
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    sr = int(streams[0].get("sample_rate") or 48000) if streams else 48000
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    return (sr, dur)


def _mhas_escaped(data: bytes, pos: int, widths: tuple[int, ...]) -> tuple[int, int]:
    value = 0
    for width in widths:
        part = read_bits(data, pos, width)
        pos += width
        value += part
        if part < (1 << width) - 1:
            break
    return value, pos


def _mhas_sample_rate(path: Path) -> int | None:
    """Read the MPEG-H 3D Audio config sample rate from a bounded MHAS prefix."""
    try:
        with path.open("rb") as source:
            data = source.read(65536)
        sync = data.find(b"\xc0\x01\xa5", 0, 4096)
        if sync < 0:
            return None
        pos = (sync + 3) * 8
        while pos + 16 <= len(data) * 8:
            kind, pos = _mhas_escaped(data, pos, (3, 8, 8))
            _, pos = _mhas_escaped(data, pos, (2, 8, 32))
            size, pos = _mhas_escaped(data, pos, (11, 24, 24))
            if kind == 1:
                if size < 2:
                    return None
                index = read_bits(data, pos + 8, 5)
                if index == 31 and size >= 5:
                    explicit = read_bits(data, pos + 13, 24)
                    return explicit or None
                return {3: 48000, 4: 44100, 5: 32000, 6: 24000, 7: 22050, 8: 16000}.get(index)
            pos += size * 8
    except (OSError, ValueError):
        pass
    return None


def probe_mpegh(
    path: Path, *, cicp: int = 13, binary: str | None = None, measure_duration: bool = True,
) -> MpeghInfo:
    """Probe sample rate, target layout channel count, and duration of an MPEG-H file."""
    layout = cicp_layout(cicp)
    raw = path.suffix.lower() in MPEGH_SUFFIXES
    container = "MHAS" if raw else "MP4 MPEG-H"
    header_rate = _mhas_sample_rate(path) if raw else None
    sr, dur = (header_rate, 0.0) if header_rate else _ffprobe_duration_rate(path)
    if raw and measure_duration and dur <= 0.0:
        exe = binary or find_decoder()
        if exe:
            with closing(stream_mpegh(path, cicp=cicp, binary=exe)) as stream:
                sr = stream.sample_rate
                frames = 0
                while len(chunk := stream.read_frames(4096)):
                    frames += len(chunk)
                dur = frames / sr
    return MpeghInfo(
        sample_rate=sr or 48000,
        channels=layout.channels,
        duration=dur,
        cicp=layout.index,
        container=container,
    )


def decode_mpegh(
    path: Path,
    token: CancellationToken | None = None,
    *,
    cicp: int = 13,
    binary: str | None = None,
    adm: Path | None = None,
    pcm: bool = True,
    length: int | None = None,
    on_adm_chunk: Callable[[Path, int], None] | None = None,
) -> AudioData | None:
    """Decode once into a disk-backed float32 buffer, and optionally into ADM BWF.

    `adm` writes the stream's objects (or, when it carries none, the rendered bed)
    as an ADM BWF master; `pcm` spools the rendered layout for the stems, the
    player and the videos.  Either may be asked for alone.  `length` is the
    container's length in samples, which keeps the master on the container's
    timeline where the decoder pads a frame at its ends.
    """
    tok = token or CancellationToken()
    tok.raise_if_cancelled()
    layout = cicp_layout(cicp)
    exe, source = decoder_input(path, binary)
    if Path(exe).stem != _WORKER:
        if adm is not None:
            raise MpeghError(
                "360RA ADM export needs the bundled MPEG-H worker: "
                "run `python scripts/build_native.py`."
            )
        with tempfile.TemporaryDirectory(prefix="soundweaver_mpegh_") as tmp:
            wav = Path(tmp) / "decoded.wav"
            cmd = [exe, f"-ifile:{path}", f"-ofile:{wav}", "-pcmsz:24", f"-cicp:{layout.index}"]
            run_native(cmd, tok, MpeghError)
            audio = read_audio(wav, tok)
        if layout.index == 13 and audio.channels == len(CICP_22_2_REMAP):
            audio.samples[:] = audio.samples[list(CICP_22_2_REMAP)]
        return audio
    return _decode_worker(
        exe, path, layout, source, tok, adm=adm, pcm=pcm, length=length, on_adm_chunk=on_adm_chunk
    )


@dataclass(frozen=True, slots=True)
class _Timelines:
    """The object timelines of a whole track, read before any audio is written."""

    samples: int
    objects: int
    positions: dict[int, list[tuple[float, float, tuple[float, float, float], bool]]]
    sizes: dict[int, list[tuple[float, tuple[float, float, float]]]]


def _collect_timelines(
    exe: str,
    path: Path,
    cicp: int,
    source: _Mp4Source | None,
    token: CancellationToken,
) -> _Timelines:
    """One pass over the objects for a playback decode: positions, spreads and length."""
    blocks = decoded_blocks(exe, path, cicp, source, None, token, objects=True)
    rate, _channels = next(blocks)
    positions: dict[int, list[tuple[float, float, tuple[float, float, float], bool]]] = {}
    sizes: dict[int, list[tuple[float, tuple[float, float, float]]]] = {}
    objects = 0
    samples = 0
    try:
        for frame in blocks:
            token.raise_if_cancelled()
            objects = max(objects, len(frame.objects))
            start = samples / rate
            for meta in frame.objects:
                key, size = key_of(meta, start)
                keys = positions.setdefault(meta.index, [])
                if not keys or keys[-1][2:] != key[2:]:
                    keys.append(key)
                if any(size[1]):
                    size_keys = sizes.setdefault(meta.index, [])
                    if not size_keys or size_keys[-1][1] != size[1]:
                        size_keys.append(size)
            samples += len(frame.samples)
    finally:
        blocks.close()
    return _Timelines(samples, objects, positions, sizes)


def _decode_worker(
    exe: str,
    path: Path,
    layout: CicpLayout,
    source: _Mp4Source | None,
    token: CancellationToken,
    *,
    adm: Path | None,
    pcm: bool,
    length: int | None,
    on_adm_chunk: Callable[[Path, int], None] | None = None,
) -> AudioData | None:
    """Drain one worker run into the PCM buffer and the ADM file it was asked for."""
    blocks = decoded_blocks(exe, path, layout.index, source, None, token, objects=adm is not None)
    rate, channels = next(blocks)
    writer: MpeghAdmFile | None = None
    scratch: Path | None = None
    output = None
    frames = 0
    published = 0
    try:
        if adm is not None and on_adm_chunk is not None:
            # A player needs a readable master before the audio exists: read the
            # timelines once, publish them, then stream the signals into the file.
            timelines = _collect_timelines(exe, path, layout.index, source, token)
            # The container's duration can run a frame past what the render covers.
            span = min(length, timelines.samples) if length else timelines.samples
            writer = MpeghAdmFile(
                adm,
                rate,
                span,
                timelines.objects,
                positions=timelines.positions,
                sizes=timelines.sizes,
            )
            on_adm_chunk(writer.publish(), 0)
        if pcm:
            scratch = pcm_scratch()
            output = scratch.open("wb")
        for frame in blocks:
            token.raise_if_cancelled()
            if adm is not None and writer is None:
                writer = MpeghAdmFile(
                    adm,
                    rate,
                    length,
                    len(frame.objects),
                    bed=() if frame.objects else layout.speakers,
                )
            if writer is not None:
                signals = (
                    frame.signals
                    if frame.signals is not None
                    else np.zeros((0, len(frame.samples)), np.float32)
                )
                writer.write_frame(signals, frame.objects, frame.rendered)
            if output is not None:
                output.write(memoryview(np.ascontiguousarray(frame.samples)).cast("B"))
                frames += len(frame.samples)
            if on_adm_chunk is not None and writer is not None:
                written = writer.frames_written
                if written - published >= rate // 2:
                    writer.file.flush()
                    published = written
                    on_adm_chunk(adm, written)  # type: ignore[misc]
        if writer is not None:
            if writer.clipped:
                logger.warning("adm: %d samples clipped at full scale", writer.clipped)
            writer.close()
            writer = None
        if not pcm:
            return None
        if not frames:
            raise MpeghError("The decoder produced no PCM frames.")
        token.raise_if_cancelled()
        pcm_view = np.memmap(scratch, mode="r+", dtype=np.float32, shape=(frames, channels))
        return AudioData(pcm_view.T, rate, backing_path=scratch)
    except BaseException:
        if writer is not None:
            writer.discard()
        if scratch is not None:
            scratch.unlink(missing_ok=True)
        raise
    finally:
        blocks.close()
        if output is not None:
            output.close()


def _mp4_source(path: Path) -> _Mp4Source | None:
    moov = mp4.find_mp4_moov(path)
    entry = mp4.find_sample_entry(moov, MPEGH_SAMPLE_ENTRIES) if moov is not None else None
    if entry is None:
        raise MpeghError("No MPEG-H audio track found in the MP4 container.")
    table = mp4.read_sample_table(entry)
    if table is None:
        return None
    if entry.kind in (b"mhm1", b"mhm2"):
        return entry, table, b""
    # MPEG-H AudioSampleEntry has the standard 28-byte ISO base followed by mhaC.
    config = b""
    for kind, start, end in mp4.iter_boxes(entry.data, 28):
        if kind == b"maei":
            # Scene metadata lives outside the samples; only the legacy testbench reads it.
            return None
        if kind == b"mhaC" and end - start >= 5:
            size = int.from_bytes(entry.data[start + 3 : start + 5], "big")
            if size and start + 5 + size <= end:
                config = entry.data[start + 5 : start + 5 + size]
    # mha1 carries the config in-band in every sample, so with no mhaC box the raw
    # samples still self-initialize the decoder, exactly like the mhm1 branch above.
    return entry, table, config


def _float_pcm(raw: bytes, channels: int, cicp: int) -> np.ndarray:
    if len(raw) % (channels * 4):
        raise MpeghError("MPEG-H decoder returned an incomplete PCM frame.")
    synth = np.frombuffer(raw, "<f4").reshape(-1, channels)
    if cicp == 13 and channels == len(CICP_22_2_REMAP):
        synth = synth[:, CICP_22_2_REMAP]
    # Retain PCM24 saturation and truncation for parity with reference exports.
    pcm = synth * np.float32(256)
    np.clip(pcm, -8388608, 8388607, out=pcm)
    np.trunc(pcm, out=pcm)
    pcm += np.float32(0)  # Integer PCM has no negative zero.
    pcm *= np.float32(1 / 8388608)
    return pcm


def _signals(raw: bytes, objects: int, samples: int) -> np.ndarray:
    """Interleaved 24-bit object signals as (objects, samples) float32."""
    needed = objects * samples * 3
    if len(raw) < needed:
        raise MpeghError("MPEG-H decoder returned an incomplete object frame.")
    packed = np.frombuffer(raw[:needed], np.uint8).reshape(samples, objects, 3).astype(np.int32)
    value = packed[..., 0] | packed[..., 1] << 8 | packed[..., 2] << 16
    value = np.where(value >= 1 << 23, value - (1 << 24), value)
    return (value * np.float32(_SIGNAL_SCALE)).T.astype(np.float32)


def decoded_blocks(
    exe: str,
    path: Path,
    cicp: int,
    mp4_source: _Mp4Source | None,
    on_stderr: Callable[[str], None] | None,
    token: CancellationToken | None,
    *,
    objects: bool = False,
) -> Generator[tuple[int, int] | MpeghFrame, None, None]:
    """Stream the worker's frames, asking it for the objects as well when wanted."""
    try:
        with ExitStack() as stack:
            config = b""
            if mp4_source is None:
                source = stack.enter_context(path.open("rb"))
            else:
                entry, table, config = mp4_source
                source = stack.enter_context(closing(ChunkReader(table.read_chunks(path, entry.index))))
            worker = stack.enter_context(closing(
                NativeProcess([exe], token=token, on_stderr=on_stderr, writable=True),
            ))
            worker.send(_WORKER_SETUP.pack(cicp, bool(config), _OBJECT_FLAG if objects else 0))
            capacity, = _U32.unpack(worker.reply(_U32.size))
            if not capacity & _FLOAT_PROTOCOL:
                raise MpeghError("Rebuild the MPEG-H worker to enable float PCM output.")
            if objects and not capacity & _OBJECT_PROTOCOL:
                raise MpeghError(
                    "Rebuild the MPEG-H worker to enable object extraction for ADM export."
                )
            capacity &= ~(_FLOAT_PROTOCOL | _OBJECT_PROTOCOL)
            if not 0 < capacity <= 16 * 1024 * 1024 or len(config) > capacity:
                raise MpeghError("Invalid MPEG-H decoder input capacity.")
            refill = config + source.read(capacity - len(config))
            pending = 0
            pcm_format = None
            while pending or refill:
                if token is not None:
                    token.raise_if_cancelled()
                worker.send(_U32.pack(len(refill)) + refill)
                pending += len(refill)
                (
                    status, consumed, ready, rate, channels, size,
                    meta_len, _oam_len, _ch_len, signal_len,
                ) = _WORKER_REPLY.unpack(worker.reply(_WORKER_REPLY.size))  # fmt: skip
                pcm = worker.reply(size)
                record = worker.reply(meta_len) if meta_len else b""
                # The decoder's ERI metadata bitstreams describe the same state the
                # record carries; only the record is read, so they are drained here.
                if _oam_len:
                    worker.reply(_oam_len)
                if _ch_len:
                    worker.reply(_ch_len)
                raw_signals = worker.reply(signal_len) if signal_len else b""
                if status not in (0, _INIT_MORE_INPUT, _EXEC_MORE_INPUT):
                    raise MpeghError(f"MPEG-H decoder error 0x{status:08x}.")
                if not 0 < consumed <= pending:
                    raise MpeghError("MPEG-H decoder did not consume a valid input span.")
                pending -= consumed
                if pcm:
                    # The core can report the reference layout at initialization.
                    # The first PCM block determines the actual render layout.
                    if not ready or rate <= 0 or not 1 <= channels <= 24:
                        raise MpeghError("MPEG-H decoder returned an invalid PCM format.")
                    if pcm_format is None:
                        pcm_format = rate, channels
                        yield pcm_format
                    elif pcm_format != (rate, channels):
                        raise MpeghError("MPEG-H PCM format changes within the stream.")
                    rendered = _float_pcm(pcm, channels, cicp)
                    members = read_record(record) if record else ()
                    # The object signals carry the whole frame; the render can be shorter
                    # at the head (the decoder's delay), and the ADM writer trims it there.
                    signals = (
                        _signals(raw_signals, len(members), len(raw_signals) // (3 * len(members)))
                        if raw_signals and members
                        else None
                    )
                    yield MpeghFrame(rendered, members, signals)
                refill = source.read(capacity - pending)
                if not refill and not ready and status == _INIT_MORE_INPUT:
                    break
            if pcm_format is None:
                raise MpeghError("No complete MPEG-H configuration found.")
            worker.finish_input()
            worker.check_exit()
    except (MpeghError, ProcessingCancelled):
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise MpeghError(str(error)) from error


def decoder_input(
    path: Path, binary: str | None,
) -> tuple[str, _Mp4Source | None]:
    exe = binary or find_decoder()
    if not exe:
        raise MpeghError(
            "MPEG-H decoder not found. Run `python scripts/build_native.py` or set MPEGH_DECODER_BIN."
        )
    if Path(exe).stem != _WORKER or path.suffix.lower() in MPEGH_SUFFIXES:
        return exe, None
    try:
        source = _mp4_source(path)
    except (OSError, ValueError) as error:
        raise MpeghError(str(error)) from error
    if source is not None:
        return exe, source
    fallback = find_binary(_LEGACY)
    if fallback is None:
        raise MpeghError("This MPEG-H MP4 requires the legacy testbench decoder.")
    return fallback, None


def _rendered(
    blocks: Generator[tuple[int, int] | MpeghFrame, None, None],
) -> Generator[tuple[int, int] | np.ndarray, None, None]:
    """Expose decoded frames as the plain sample blocks `BlockPcmStream` reads."""
    rate_and_channels = next(blocks)
    yield rate_and_channels
    for frame in blocks:
        assert isinstance(frame, MpeghFrame)
        yield frame.samples


def stream_mpegh(
    path: Path,
    *,
    cicp: int = 13,
    binary: str | None = None,
    on_stderr: Callable[[str], None] | None = None,
    token: CancellationToken | None = None,
) -> PcmStream:
    """Open a real-time PCM stream at the selected CICP layout."""
    if token is not None:
        token.raise_if_cancelled()
    exe, source = decoder_input(path, binary)
    layout = cicp_layout(cicp)
    if Path(exe).stem == _WORKER:
        return BlockPcmStream(
            _rendered(decoded_blocks(exe, path, layout.index, source, on_stderr, token))
        )

    cmd = [
        exe, f"-ifile:{path}", "-ofile:stream.wav",
        "-pcmsz:24", f"-cicp:{layout.index}", "-stream:1",
    ]
    return NativePcmStream(cmd, on_stderr=on_stderr, token=token)


__all__ = [
    "CICP_22_2_REMAP",
    "CICP_LAYOUTS",
    "MPEGH_SAMPLE_ENTRIES",
    "MPEGH_SUFFIXES",
    "SUPPORTED_CICP",
    "CicpLayout",
    "CicpSpeaker",
    "MpeghError",
    "MpeghFrame",
    "MpeghInfo",
    "cicp_layout",
    "decode_mpegh",
    "find_decoder",
    "is_mpegh_file",
    "probe_mpegh",
    "stream_mpegh",
]
