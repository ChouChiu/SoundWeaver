"""MPEG-H 3D Audio (360 Reality Audio / 360RA) detection, CICP layouts, decoding, and streaming."""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from soundweaver.core import mp4
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.native import NativePcmStream, find_binary, run_native
from soundweaver.core.pcm import AudioData, read_audio
from soundweaver.core.process import NO_WINDOW

MPEGH_SUFFIXES = (".mhas",)
MPEGH_SAMPLE_ENTRIES = (b"mhm1", b"mhm2", b"mha1", b"mha2")

# Reorder decoded CICP 13 (22.2) raw testbench output to standard SoundWeaver / WalkMix order.
# Note: `ia_mpeghd_testbench -stream:1` applies this same permutation internally in C,
# whereas offline WAV output (`decode_mpegh`) needs it applied in Python.
CICP_22_2_REMAP: tuple[int, ...] = (
    6, 7, 2, 3, 4, 5, 0, 1, 8, 9, 10, 11,
    12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23,
)

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
    """Return path to `ia_mpeghd_testbench`, or None if unavailable."""
    return find_binary("ia_mpeghd_testbench", "MPEGH_DECODER_BIN")


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
            capture_output=True,
            text=True,
            timeout=15.0,
            creationflags=NO_WINDOW,
        )
    except OSError:
        return (48000, 0.0)
    if proc.returncode != 0:
        return (48000, 0.0)
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    sr = int(streams[0].get("sample_rate") or 48000) if streams else 48000
    dur = float((data.get("format") or {}).get("duration") or 0.0)
    return (sr, dur)


def probe_mpegh(path: Path, *, cicp: int = 13, binary: str | None = None) -> MpeghInfo:
    """Probe sample rate, target layout channel count, and duration of an MPEG-H file."""
    layout = cicp_layout(cicp)
    container = "MHAS" if path.suffix.lower() in MPEGH_SUFFIXES else "MP4 MPEG-H"
    sr, dur = _ffprobe_duration_rate(path)
    if sr <= 0 or dur <= 0.0:
        exe = binary or find_decoder()
        if exe and path.suffix.lower() in MPEGH_SUFFIXES:
            stream = stream_mpegh(path, cicp=cicp, binary=exe)
            try:
                sr = stream.sample_rate
                frames = 0
                while True:
                    chunk = stream.read_frames(4096)
                    if chunk.shape[0] == 0:
                        break
                    frames += chunk.shape[0]
                dur = frames / sr if sr > 0 else 0.0
            finally:
                stream.close()
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
) -> AudioData:
    """Decode `path` via `ia_mpeghd_testbench` to float32 `AudioData`."""
    exe = binary or find_decoder()
    if not exe:
        raise MpeghError(
            "MPEG-H decoder (`ia_mpeghd_testbench`) not found. "
            "Run `python scripts/build_native.py` or set MPEGH_DECODER_BIN."
        )
    layout = cicp_layout(cicp)
    tok = token or CancellationToken()
    with tempfile.TemporaryDirectory(prefix="soundweaver_mpegh_") as tmp:
        out_wav = Path(tmp) / "mpegh_out.wav"
        cmd = [
            exe,
            f"-ifile:{path}",
            f"-ofile:{out_wav}",
            "-pcmsz:24",
            f"-cicp:{layout.index}",
        ]
        run_native(cmd, token=tok, error_cls=MpeghError, timeout=600.0)
        audio = read_audio(out_wav, tok)
    if layout.index == 13 and audio.channels == 24:
        audio.samples[:] = audio.samples[list(CICP_22_2_REMAP), :]
    return audio


def stream_mpegh(
    path: Path,
    *,
    cicp: int = 13,
    binary: str | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> NativePcmStream:
    """Open a real-time float32 PCM stream from `ia_mpeghd_testbench -stream:1`."""
    exe = binary or find_decoder()
    if not exe:
        raise MpeghError(
            "MPEG-H decoder (`ia_mpeghd_testbench`) not found. "
            "Run `python scripts/build_native.py` or set MPEGH_DECODER_BIN."
        )
    cmd = [
        exe,
        f"-ifile:{path}",
        "-ofile:stream.wav",
        "-pcmsz:24",
        f"-cicp:{cicp_layout(cicp).index}",
        "-stream:1",
    ]
    return NativePcmStream(cmd, on_stderr=on_stderr)


__all__ = [
    "CICP_22_2_REMAP",
    "CICP_LAYOUTS",
    "CicpLayout",
    "CicpSpeaker",
    "MPEGH_SAMPLE_ENTRIES",
    "MPEGH_SUFFIXES",
    "MpeghError",
    "MpeghInfo",
    "SUPPORTED_CICP",
    "cicp_layout",
    "decode_mpegh",
    "find_decoder",
    "is_mpegh_file",
    "probe_mpegh",
    "stream_mpegh",
]
