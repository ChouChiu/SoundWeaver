"""Audio Vivid (AVS3-P3 / AV3A) detection, header parsing, decoding, streaming, encoding, and binaural rendering."""

from __future__ import annotations

import json
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import soundfile as sf

from soundweaver.core import mp4
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.native import NativePcmStream, find_binary, run_native
from soundweaver.core.pcm import AudioData, read_audio

AV3A_SUFFIXES = (".av3a", ".avs3a")
VVM1_MAGIC = 0x314D5656
FRAME_SAMPLES = 1024

_SAMPLING_RATES = (
    192000,
    96000,
    48000,
    44100,
    32000,
    24000,
    22050,
    16000,
    8000,
)

# Canonical bed channel order per AVS3 configuration index.
CONFIG_CHANNEL_ORDER: dict[int, tuple[str, ...]] = {
    0: ("C",),
    1: ("L", "R"),
    2: ("L", "R", "C", "LFE", "Ls", "Rs"),
    3: ("L", "R", "C", "LFE", "Lrs", "Rrs", "Ls", "Rs"),
    6: ("L", "R", "Ls", "Rs"),
    7: ("L", "R", "C", "LFE", "Ls", "Rs", "Ltf", "Rtf"),
    8: ("L", "R", "C", "LFE", "Ls", "Rs", "Ltf", "Rtf", "Ltr", "Rtr"),
    9: ("L", "R", "C", "LFE", "Lrs", "Rrs", "Ls", "Rs", "Ltf", "Rtf"),
    10: ("L", "R", "C", "LFE", "Lrs", "Rrs", "Ls", "Rs", "Ltf", "Rtf", "Ltr", "Rtr"),
    11: tuple(f"ACN{i}" for i in range(4)),
    12: tuple(f"ACN{i}" for i in range(9)),
    13: tuple(f"ACN{i}" for i in range(16)),
}

# Bitrate tables (bps) indexed by configuration id, matching AVS3-P3 / avs3_rom_com.c.
_BITRATE_TABLES: dict[int, tuple[int, ...]] = {
    0: (16000, 32000, 44000, 56000, 64000, 72000, 80000, 96000, 128000, 144000, 164000, 192000),
    1: (24000, 32000, 48000, 64000, 80000, 96000, 128000, 144000, 192000, 256000, 320000),
    2: (192000, 256000, 320000, 384000, 448000, 512000, 640000, 720000, 144000, 96000, 128000, 160000),
    3: (192000, 480000, 256000, 384000, 576000, 640000, 128000, 160000),
    6: (48000, 96000, 128000, 192000, 256000),
    7: (152000, 320000, 480000, 576000),
    8: (176000, 384000, 576000, 704000, 256000, 448000),
    9: (216000, 480000, 576000, 384000, 768000),
    10: (240000, 608000, 384000, 512000, 832000),
    11: (48000, 96000, 128000, 192000, 256000),
    12: (192000, 256000, 320000, 384000, 480000, 512000, 640000),
    13: (256000, 320000, 384000, 512000, 640000, 896000),
}

# MC config strings accepted by `vivid_encode -MC`.
_ENCODE_MC_BY_CHANNELS: dict[int, tuple[str, int]] = {
    4: ("MC_4_0", 192000),
    6: ("MC_5_1_0", 384000),
    8: ("MC_7_1_0", 480000),
    10: ("MC_5_1_4", 576000),
    12: ("MC_7_1_4", 608000),
}
_ENCODE_HOA_BITRATE = {1: 192000, 2: 384000, 3: 512000}


class Av3aError(RuntimeError):
    """Raised when Audio Vivid inspection, decoding, encoding, or binaural rendering fails."""


@dataclass(frozen=True)
class Av3aHeader:
    """Parsed fields from one AVS3-P3 AATF frame header."""

    profile: int
    nn_type: int
    sample_rate: int
    config: int
    bed_channels: int
    object_count: int
    hoa_order: int
    channels: int
    bitrate: int
    payload_bytes: int
    header_bytes: int


@dataclass(frozen=True)
class Av3aInfo:
    """Metadata returned by `vivid probe` or `vivid inspect`."""

    sample_rate: int
    channels: int
    configuration: int
    bitrate: int
    frames: int
    duration: float
    channel_order: str
    object_count: int
    hoa_order: int = 0
    profile: int = 0


@dataclass(frozen=True)
class VvmTimelineHeader:
    """Header of a `.vvm1` binary telemetry sidecar produced by `vivid stream`."""

    sample_rate: int
    bed_channels: int
    object_count: int
    config: int
    frame_samples: int


def find_decoder() -> str | None:
    """Return path to `vivid`, or None if unavailable."""
    return find_binary("vivid", "AV3A_DECODER_BIN")


def find_encoder() -> str | None:
    """Return path to `vivid_encode`, or None if unavailable."""
    return find_binary("vivid_encode", "AV3A_ENCODER_BIN")


def find_binaural_renderer() -> str | None:
    """Return path to `vivid_binaural`, or None if unavailable."""
    return find_binary("vivid_binaural", "AV3A_BINAURAL_BIN")


def _bits(data: bytes, start: int, count: int) -> int:
    value = 0
    for i in range(count):
        pos = start + i
        value = (value << 1) | ((data[pos >> 3] >> (7 - (pos & 7))) & 1)
    return value


def parse_aatf_header(data: bytes) -> Av3aHeader:
    """Parse an AVS3-P3 AATF frame header (minimum 9 bytes)."""
    if len(data) < 9:
        raise Av3aError("AATF header requires at least 9 bytes.")
    if _bits(data, 0, 16) != 0xFFF2 or _bits(data, 16, 1) != 0:
        raise Av3aError("Invalid Audio Vivid AATF syncword.")
    nn_type = _bits(data, 17, 3)
    profile = _bits(data, 20, 3)
    fs_idx = _bits(data, 23, 4)
    if profile > 2:
        raise Av3aError(f"Unsupported Audio Vivid profile: {profile}")
    if fs_idx >= len(_SAMPLING_RATES):
        raise Av3aError(f"Invalid Audio Vivid sample rate index: {fs_idx}")
    sample_rate = _SAMPLING_RATES[fs_idx]
    pos = 35
    config = -1
    bed_channels = 0
    object_count = 0
    hoa_order = 0
    bitrate = 0

    if profile == 2:
        hoa_order = _bits(data, pos, 4) + 1
        pos += 4
        if not 1 <= hoa_order <= 3:
            raise Av3aError(f"Unsupported Audio Vivid HOA order: {hoa_order}")
        config = 10 + hoa_order
        bed_channels = (hoa_order + 1) ** 2
    else:
        sound_bed = _bits(data, pos, 2) if profile else 1
        if profile:
            pos += 2
        if sound_bed > 1:
            raise Av3aError(f"Invalid Audio Vivid sound bed type: {sound_bed}")
        if sound_bed:
            config = _bits(data, pos, 7)
            pos += 7
            order = CONFIG_CHANNEL_ORDER.get(config)
            if order is None or (profile and config == 0):
                raise Av3aError(f"Unsupported Audio Vivid bed config: {config}")
            bed_channels = len(order)
            if profile:
                br_idx = _bits(data, pos, 4)
                pos += 4
                table = _BITRATE_TABLES.get(config, ())
                bitrate = table[br_idx] if br_idx < len(table) else 0
                if bitrate <= 0:
                    raise Av3aError(f"Unsupported Audio Vivid bed bitrate index: {br_idx}")
        if profile:
            object_count = _bits(data, pos, 7) + 1
            pos += 7
            obj_idx = _bits(data, pos, 4)
            pos += 4
            obj_table = _BITRATE_TABLES[0]
            obj_br = obj_table[obj_idx] if obj_idx < len(obj_table) else 0
            if obj_br <= 0:
                raise Av3aError(f"Unsupported Audio Vivid object bitrate index: {obj_idx}")
            bitrate += obj_br * object_count

    if profile == 0 or profile == 2:
        br_idx = _bits(data, pos, 4)
        pos += 4
        table = _BITRATE_TABLES.get(config, ())
        bitrate = table[br_idx] if br_idx < len(table) else 0
        if bitrate <= 0:
            raise Av3aError(f"Unsupported Audio Vivid bitrate index: {br_idx}")

    resolution = _bits(data, pos, 2)
    pos += 2
    if resolution > 2:
        raise Av3aError(f"Invalid Audio Vivid resolution index: {resolution}")
    pos += 8 if profile else 12  # low-level / NN / frame metadata bits
    header_bytes = (pos + 7) // 8
    payload_bytes = max(0, int((bitrate * FRAME_SAMPLES + sample_rate * 4) // (sample_rate * 8)) - header_bytes)
    return Av3aHeader(
        profile=profile,
        nn_type=nn_type,
        sample_rate=sample_rate,
        config=config,
        bed_channels=bed_channels,
        object_count=object_count,
        hoa_order=hoa_order,
        channels=bed_channels + object_count,
        bitrate=bitrate,
        payload_bytes=payload_bytes,
        header_bytes=header_bytes,
    )


def is_av3a_file(path: Path) -> bool:
    """Detect raw `.av3a`/`.avs3a`, MPEG-TS with `0xFFF2` PES, or MP4 carrying an `av3a` track."""
    suf = path.suffix.lower()
    if suf in AV3A_SUFFIXES:
        with path.open("rb") as f:
            head = f.read(2)
        return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xFE) == 0xF2
    if suf in (".ts", ".m2ts"):
        return mp4.has_ts_pes_sync(path, sync=b"\xff\xf2")
    if suf in (".mp4", ".m4a", ".m4v", ".mov"):
        return mp4.has_mp4_sample_entry(path, (b"av3a",))
    return False


def _require_decoder(binary: str | None) -> str:
    exe = binary or find_decoder()
    if not exe:
        raise Av3aError(
            "Audio Vivid decoder (`vivid`) not found. "
            "Run `python scripts/build_native.py` or set AV3A_DECODER_BIN."
        )
    return exe


def probe_av3a(path: Path, binary: str | None = None, *, quick: bool = False) -> Av3aInfo:
    """Probe an Audio Vivid file via `vivid probe` (full frame count) or `vivid inspect` (first frame)."""
    exe = _require_decoder(binary)
    mode = "inspect" if quick else "probe"
    res = run_native([exe, mode, str(path)], token=None, error_cls=Av3aError, timeout=60.0)
    data = json.loads(res.stdout)
    return Av3aInfo(
        sample_rate=int(data["sampleRate"]),
        channels=int(data["channels"]),
        configuration=int(data["configuration"]),
        bitrate=int(data["bitrate"]),
        frames=int(data["frames"]),
        duration=float(data["duration"]),
        channel_order=str(data.get("channelOrder") or ""),
        object_count=int(data.get("objectCount", 0)),
        hoa_order=int(data.get("hoaOrder", 0)),
        profile=int(data.get("profile", 0)),
    )


def decode_av3a(
    path: Path,
    token: CancellationToken | None = None,
    binary: str | None = None,
    target_order: str = "source",
) -> AudioData:
    """Decode `path` via `vivid decode` to float32 `AudioData`."""
    exe = _require_decoder(binary)
    tok = token or CancellationToken()
    with tempfile.TemporaryDirectory(prefix="soundweaver_av3a_") as tmp:
        out_wav = Path(tmp) / "av3a_out.wav"
        run_native([exe, "decode", str(path), target_order, str(out_wav)], token=tok, error_cls=Av3aError)
        return read_audio(out_wav, tok)


def stream_av3a(
    path: Path,
    target_order: str = "source",
    timeline_path: Path | None = None,
    source_pcm_path: Path | None = None,
    binary: str | None = None,
    on_stderr: Callable[[str], None] | None = None,
) -> NativePcmStream:
    """Open a real-time float32 PCM stream from `vivid stream` (with optional VVM1 telemetry sidecar)."""
    exe = _require_decoder(binary)
    cmd = [exe, "stream", str(path), target_order]
    if timeline_path is not None and source_pcm_path is not None:
        cmd.extend([str(timeline_path), str(source_pcm_path)])
    return NativePcmStream(cmd, on_stderr=on_stderr)


def read_vvm1_header(path: Path) -> VvmTimelineHeader | None:
    """Read the 24-byte header of a `.vvm1` binary telemetry file if complete."""
    if not path.exists() or path.stat().st_size < 24:
        return None
    with path.open("rb") as f:
        raw = f.read(24)
    if len(raw) < 24:
        return None
    magic, rate, bed, objs, cfg, flen = struct.unpack("<6I", raw)
    if magic != VVM1_MAGIC:
        return None
    return VvmTimelineHeader(rate, bed, objs, cfg, flen)


def read_vvm1_frame(
    path: Path,
    header: VvmTimelineHeader,
    frame_index: int,
) -> np.ndarray | None:
    """Return shape `(bed + objects, 5)` float32 `[x, y, z, peak, jump]` for `frame_index`, or None."""
    total_ch = header.bed_channels + header.object_count
    stride = total_ch * 5 * 4
    offset = 24 + max(0, frame_index) * stride
    if not path.exists() or path.stat().st_size < offset + stride:
        return None
    with path.open("rb") as f:
        f.seek(offset)
        raw = f.read(stride)
    if len(raw) < stride:
        return None
    return np.frombuffer(raw, dtype="<f4").reshape(total_ch, 5).copy()


def encode_av3a(
    audio: AudioData,
    out_path: Path,
    *,
    bitrate_bps: int | None = None,
    hoa_order: int = 0,
    mc_config: str | None = None,
    token: CancellationToken | None = None,
    binary: str | None = None,
) -> Path:
    """Encode an `AudioData` buffer into a raw `.av3a` bitstream via `vivid_encode`."""
    exe = binary or find_encoder()
    if not exe:
        raise Av3aError(
            "Audio Vivid encoder (`vivid_encode`) not found. "
            "Run `python scripts/build_native.py` or set AV3A_ENCODER_BIN."
        )
    ch = audio.channels
    if hoa_order > 0:
        expected = (hoa_order + 1) ** 2
        if ch != expected:
            raise Av3aError(f"HOA order {hoa_order} requires {expected} channels, got {ch}.")
        mode_args = ["-HOA", str(hoa_order)]
        default_br = _ENCODE_HOA_BITRATE.get(hoa_order, 384000)
    elif ch == 1:
        mode_args = ["-MONO"]
        default_br = 128000
    elif ch == 2:
        mode_args = ["-STEREO"]
        default_br = 192000
    else:
        resolved = _ENCODE_MC_BY_CHANNELS.get(ch)
        if mc_config:
            mode_args = ["-MC", mc_config]
            default_br = resolved[1] if resolved else 480000
        elif resolved:
            mode_args = ["-MC", resolved[0]]
            default_br = resolved[1]
        else:
            raise Av3aError(
                f"No default Audio Vivid MC config for {ch} channels (supported: 1, 2, 4, 6, 8, 10, 12, or HOA)."
            )

    br = int(bitrate_bps or default_br)
    fs_khz = f"{audio.sample_rate / 1000:g}"
    pcm_i16 = np.clip(np.round(audio.samples.T * 32767.0), -32768, 32767).astype("<i2")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="soundweaver_av3a_enc_") as tmp:
        in_wav = Path(tmp) / "input.wav"
        _write_pcm16_wav(in_wav, pcm_i16, audio.sample_rate)
        cmd = [
            exe,
            "-MAX_BAND",
            "FB",
            "-BITDEPTH",
            "16",
            *mode_args,
            str(br),
            fs_khz,
            str(in_wav),
            str(out_path),
        ]
        run_native(cmd, token=token, error_cls=Av3aError, timeout=600.0)
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise Av3aError(f"vivid_encode produced empty output: {out_path}")
    return out_path


def _write_pcm16_wav(path: Path, pcm_i16: np.ndarray, sample_rate: int) -> None:
    """Write standard 44-byte canonical 16-bit integer WAV required by `Avs3WavReader`."""
    frames, channels = pcm_i16.shape
    data_bytes = frames * channels * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_bytes,
        b"WAVE",
        b"fmt ",
        16,
        1,  # PCM
        channels,
        sample_rate,
        sample_rate * channels * 2,
        channels * 2,
        16,
        b"data",
        data_bytes,
    )
    with path.open("wb") as f:
        f.write(header)
        f.write(np.ascontiguousarray(pcm_i16).tobytes())


def render_binaural_file(
    in_wav: Path,
    out_wav: Path,
    *,
    speakers: list[str] | tuple[str, ...] | None = None,
    hoa_order: int = 0,
    token: CancellationToken | None = None,
    binary: str | None = None,
) -> Path:
    """Render an ADM BW64, multichannel speaker WAV, or HOA WAV to stereo binaural via `vivid_binaural`."""
    exe = binary or find_binaural_renderer()
    if not exe:
        raise Av3aError(
            "Audio Vivid binaural renderer (`vivid_binaural`) not found. "
            "Run `python scripts/build_native.py` or set AV3A_BINAURAL_BIN."
        )
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-i", str(in_wav), "-o", str(out_wav)]
    if hoa_order > 0:
        cmd.extend(["--hoa", str(hoa_order)])
    elif speakers:
        cmd.extend(["--speakers", ",".join(speakers)])
    run_native(cmd, token=token, error_cls=Av3aError, timeout=600.0)
    return out_wav


def render_binaural_audio(
    audio: AudioData,
    *,
    speakers: list[str] | tuple[str, ...] | None = None,
    hoa_order: int = 0,
    token: CancellationToken | None = None,
    binary: str | None = None,
) -> AudioData:
    """Render an in-memory multichannel or HOA `AudioData` to 2-ch stereo HRTF binaural `AudioData`."""
    tok = token or CancellationToken()
    with tempfile.TemporaryDirectory(prefix="soundweaver_bin_") as tmp:
        tmp_dir = Path(tmp)
        in_wav = tmp_dir / "in.wav"
        out_wav = tmp_dir / "out.wav"
        sf.write(in_wav, audio.samples.T, audio.sample_rate, subtype="PCM_24", format="WAV")
        render_binaural_file(
            in_wav,
            out_wav,
            speakers=speakers,
            hoa_order=hoa_order,
            token=tok,
            binary=binary,
        )
        return read_audio(out_wav, tok)


def extract_elementary_stream(mp4_path: Path, out_av3a: Path) -> bool:
    """Extract raw `.av3a` frames from an MP4 `av3a` track using pure-Python ISOBMFF parsing."""
    if out_av3a.exists():
        out_av3a.unlink()
    return mp4.extract_elementary_stream(mp4_path, (b"av3a",), out_av3a)


__all__ = [
    "AV3A_SUFFIXES",
    "Av3aError",
    "Av3aHeader",
    "Av3aInfo",
    "CONFIG_CHANNEL_ORDER",
    "FRAME_SAMPLES",
    "VvmTimelineHeader",
    "decode_av3a",
    "encode_av3a",
    "extract_elementary_stream",
    "find_binaural_renderer",
    "find_decoder",
    "find_encoder",
    "is_av3a_file",
    "parse_aatf_header",
    "probe_av3a",
    "read_vvm1_frame",
    "read_vvm1_header",
    "render_binaural_audio",
    "render_binaural_file",
    "stream_av3a",
]
