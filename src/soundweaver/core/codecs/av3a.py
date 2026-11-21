"""Audio Vivid (AVS3-P3 / AV3A) detection, header parsing, decoding, streaming, and encoding."""

from __future__ import annotations

import binascii
import json
import os
import struct
import tempfile
from collections.abc import Callable, Generator
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from soundweaver.core import mp4
from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.codecs import av3a_input, av3a_routing
from soundweaver.core.codecs.native import NativePcmStream, NativeProcess, find_binary, run_native
from soundweaver.core.codecs.packet_io import ChunkReader, read_bits
from soundweaver.core.codecs.pcm_stream import BlockPcmStream, PcmStream, decode_pcm_stream
from soundweaver.core.paths import atomic_output
from soundweaver.core.pcm import BLOCK_FRAMES, AudioData, read_audio

AV3A_SUFFIXES = (".av3a", ".avs3a")
VVM1_MAGIC = 0x314D5656
VVM2_MAGIC = 0x324D5656
_VVM1_HEADER = struct.Struct("<6I")
_VVM1_FIELDS = 5
_VVM_FIELDS = {VVM1_MAGIC: _VVM1_FIELDS, VVM2_MAGIC: 6}
FRAME_SAMPLES = 1024
_WORKER = "soundweaver_av3a"
_WORKER_SETUP = struct.Struct("<2I")
_FLOAT_PROTOCOL = 0x80000000
_WORKER_FRAME = struct.Struct("<I9s")
_CRC_SEED = binascii.crc_hqx(b"\0\0", 0xFFFF)

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
    3: ("L", "R", "C", "LFE", "Ls", "Rs", "Lrs", "Rrs"),
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
_ENCODE_MC_CONFIGS = {
    "MC_5_1_0": 2, "MC_7_1_0": 3, "MC_4_0": 6,
    "MC_5_1_2": 7, "MC_5_1_4": 8, "MC_7_1_2": 9, "MC_7_1_4": 10,
}
_ENCODE_HOA_BITRATE = {1: 192000, 2: 384000, 3: 512000}


class Av3aError(RuntimeError):
    """Raised when Audio Vivid inspection, decoding or encoding fails."""


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
    """Metadata from the Audio Vivid AATF frame headers."""

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
    """Header of a versioned binary telemetry sidecar written by `stream_av3a`."""

    sample_rate: int
    bed_channels: int
    object_count: int
    config: int
    frame_samples: int
    fields: int = _VVM1_FIELDS


def find_decoder() -> str | None:
    """Locate the AVS3-P3 frame decoder."""
    return find_binary(_WORKER, "AV3A_DECODER_BIN")


def find_encoder() -> str | None:
    """Return path to `vivid_encode`, or None if unavailable."""
    return find_binary("vivid_encode", "AV3A_ENCODER_BIN")


def _bits(data: bytes, start: int, count: int) -> int:
    try:
        return read_bits(data, start, count)
    except ValueError as error:
        raise Av3aError("Truncated Audio Vivid AATF header.") from error


def parse_aatf_header(data: bytes) -> Av3aHeader:
    """Parse an AVS3-P3 AATF frame header."""
    return _parse_aatf_header(data)[0]


def _parse_aatf_header(data: bytes) -> tuple[Av3aHeader, int]:
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
            if order is None or config > 10 or (profile and config == 0):
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

    resolution = _bits(data, pos, 2)
    pos += 2
    if resolution > 2:
        raise Av3aError(f"Invalid Audio Vivid resolution index: {resolution}")

    if profile == 0 or profile == 2:
        br_idx = _bits(data, pos, 4)
        pos += 4
        table = _BITRATE_TABLES.get(config, ())
        bitrate = table[br_idx] if br_idx < len(table) else 0
        if bitrate <= 0:
            raise Av3aError(f"Unsupported Audio Vivid bitrate index: {br_idx}")

    crc_offset = pos
    header_bytes = (pos + 8 + 7) // 8
    if len(data) < header_bytes:
        raise Av3aError("Truncated Audio Vivid AATF header.")
    frame_bits = bitrate * FRAME_SAMPLES // sample_rate
    payload_bytes = max(0, (frame_bits + 7) // 8 - header_bytes)
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
    ), crc_offset


def is_av3a_file(path: Path) -> bool:
    """Detect raw `.av3a`/`.avs3a`, MPEG-TS with `0xFFF2` PES, or MP4 carrying an `av3a` track."""
    suf = path.suffix.lower()
    if suf in AV3A_SUFFIXES:
        with path.open("rb") as f:
            head = f.read(2)
        return head == b"\xff\xf2"
    if suf in (".ts", ".m2ts"):
        return mp4.has_ts_pes_sync(path, sync=b"\xff\xf2")
    if suf in (".mp4", ".m4a", ".m4v", ".mov"):
        return mp4.has_mp4_sample_entry(path, (b"av3a",))
    return False


def _require_decoder(binary: str | None) -> str:
    exe = binary or find_decoder()
    if not exe:
        raise Av3aError(
            "Audio Vivid decoder (`soundweaver_av3a`) not found. "
            "Run `python scripts/build_native.py` or set AV3A_DECODER_BIN."
        )
    return exe


def _aatf_crc(payload: bytes) -> int:
    # AVS3 feeds each byte into the low end of the 0x1021 CRC register.
    return binascii.crc_hqx(payload[:-2], _CRC_SEED) ^ int.from_bytes(payload[-2:], "big")


def _aatf_frames(
    path: Path, token: CancellationToken | None,
) -> Generator[tuple[Av3aHeader, bytes, bytes], None, None]:
    first = None
    try:
        with closing(ChunkReader(av3a_input.elementary_chunks(path, token))) as source:
            while head := source.read(7):
                if token is not None:
                    token.raise_if_cancelled()
                if len(head) < 7:
                    raise Av3aError("Truncated Audio Vivid AATF header.")
                if _bits(head, 20, 3) == 1:
                    head += source.read(1 if _bits(head, 35, 2) == 0 else 2)
                header, crc_offset = _parse_aatf_header(head)
                if header.sample_rate not in (44100, 48000) or header.nn_type > 1:
                    raise Av3aError("Audio Vivid decoding supports 44.1/48 kHz MAIN/LC audio.")
                if not 1 <= header.channels <= 16 or not 2 <= header.payload_bytes <= 12300:
                    raise Av3aError("Invalid Audio Vivid channel count or frame length.")
                payload = source.read(header.payload_bytes)
                if len(payload) != header.payload_bytes:
                    raise Av3aError("Incomplete Audio Vivid frame payload.")
                expected = (_bits(head, 27, 8) << 8) | _bits(head, crc_offset, 8)
                if _aatf_crc(payload) != expected:
                    raise Av3aError("Audio Vivid frame CRC mismatch.")
                config = (
                    header.sample_rate, header.config, header.profile, header.nn_type,
                    header.object_count,
                )
                if first is not None and first != config:
                    raise Av3aError("Audio Vivid configuration changes within the stream.")
                first = config
                yield header, head, payload
    except (Av3aError, ProcessingCancelled):
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise Av3aError(str(error)) from error
    if first is None:
        raise Av3aError("No complete Audio Vivid frames found.")


def probe_av3a(path: Path, binary: str | None = None, *, quick: bool = False) -> Av3aInfo:
    """Inspect the first AATF frame or validate and count the complete frame stream."""
    if binary is not None and Path(binary).stem != _WORKER:
        mode = "inspect" if quick else "probe"
        res = run_native([binary, mode, str(path)], token=None, error_cls=Av3aError, timeout=60.0)
        data = json.loads(res.stdout)
        return Av3aInfo(
            int(data["sampleRate"]), int(data["channels"]), int(data["configuration"]),
            int(data["bitrate"]), int(data["frames"]), float(data["duration"]),
            str(data.get("channelOrder") or ""), int(data.get("objectCount", 0)),
            int(data.get("hoaOrder", 0)), int(data.get("profile", 0)),
        )
    with closing(_aatf_frames(path, None)) as frames:
        header, _, _ = next(frames)
        count = 1 if quick else 1 + sum(1 for _ in frames)
    return Av3aInfo(
        header.sample_rate, header.channels, header.config, header.bitrate,
        count, count * FRAME_SAMPLES / header.sample_rate,
        ",".join(CONFIG_CHANNEL_ORDER.get(header.config, ())),
        header.object_count, header.hoa_order, header.profile,
    )


def decode_av3a(
    path: Path,
    token: CancellationToken | None = None,
    binary: str | None = None,
    target_order: str = "source",
) -> AudioData:
    """Decode Audio Vivid into a disk-backed float32 PCM buffer."""
    exe = _require_decoder(binary)
    tok = token or CancellationToken()
    if Path(exe).stem != _WORKER:
        with tempfile.TemporaryDirectory(prefix="soundweaver_av3a_") as tmp:
            wav = Path(tmp) / "decoded.wav"
            run_native([exe, "decode", str(path), target_order, str(wav)], tok, Av3aError)
            return read_audio(wav, tok)
    return decode_pcm_stream(
        stream_av3a(path, target_order, binary=exe, token=tok), tok, error_cls=Av3aError,
    )


def _decoded_blocks(
    exe: str,
    path: Path,
    target_order: str,
    timeline_path: Path | None,
    source_pcm_path: Path | None,
    on_stderr: Callable[[str], None] | None,
    token: CancellationToken | None,
) -> Generator[tuple[int, int] | np.ndarray, None, None]:
    try:
        with ExitStack() as stack:
            aatf = stack.enter_context(closing(_aatf_frames(path, token)))
            header, head, payload = next(aatf)
            names = CONFIG_CHANNEL_ORDER.get(header.config, ())
            matrix = None
            channels = header.channels
            if target_order != "source":
                if header.object_count or header.hoa_order:
                    raise Av3aError("Audio Vivid objects and HOA require source-channel output.")
                matrix = av3a_routing.speaker_matrix(names, target_order)
                channels = matrix.shape[1]

            worker = stack.enter_context(closing(
                NativeProcess([exe], token=token, on_stderr=on_stderr, writable=True),
            ))
            worker.send(_WORKER_SETUP.pack(header.channels | _FLOAT_PROTOCOL, header.bed_channels))
            timeline = source = None
            if timeline_path is not None and source_pcm_path is not None:
                timeline = stack.enter_context(timeline_path.open("wb"))
                source = stack.enter_context(source_pcm_path.open("wb"))
                timeline.write(_VVM1_HEADER.pack(
                    VVM2_MAGIC, header.sample_rate, header.bed_channels, header.object_count,
                    header.config & 0xFFFFFFFF, FRAME_SAMPLES,
                ))
                timeline.flush()
            bed_positions = av3a_routing.bed_positions(names)
            yield header.sample_rate, channels

            count = 0
            previous = None
            pcm_bytes = FRAME_SAMPLES * header.channels * 4
            meta_bytes = header.channels * _VVM_FIELDS[VVM2_MAGIC] * 4
            while True:
                if token is not None:
                    token.raise_if_cancelled()
                worker.send(_WORKER_FRAME.pack(len(payload), head) + payload)
                reply = worker.reply(pcm_bytes + meta_bytes)
                synth = np.frombuffer(reply, "<f4", count=pcm_bytes // 4)
                synth = synth.reshape(header.channels, FRAME_SAMPLES).T
                # Preserve the reference decoder's asymmetric PCM16 rounding.
                pcm = np.floor(synth + np.float32(0.5))
                np.clip(pcm, -32768, 32767, out=pcm)
                pcm *= np.float32(1 / 32768)
                metadata = np.frombuffer(reply, "<f4", offset=pcm_bytes).reshape(header.channels, -1)
                block, previous = av3a_routing.apply_object_ramp(
                    pcm, metadata, header.bed_channels, previous,
                )
                if source is not None and timeline is not None:
                    source_pcm = pcm * metadata[:, 3]
                    points = metadata.copy()
                    points[:header.bed_channels, :3] = bed_positions
                    points[:, 3] = np.max(np.abs(source_pcm), axis=0)
                    source.write(source_pcm.astype("<f4", copy=False).tobytes())
                    source.flush()
                    timeline.write(points.astype("<f4", copy=False).tobytes())
                    timeline.flush()
                count += 1
                if on_stderr is not None and count % 24 == 0:
                    on_stderr(f"frames={count}")
                yield block if matrix is None else block @ matrix
                frame = next(aatf, None)
                if frame is None:
                    worker.finish_input()
                    worker.check_exit()
                    if on_stderr is not None:
                        on_stderr(f"frames={count}")
                    break
                _, head, payload = frame
    except (Av3aError, ProcessingCancelled):
        raise
    except (RuntimeError, OSError, ValueError) as error:
        raise Av3aError(str(error)) from error


def stream_av3a(
    path: Path,
    target_order: str = "source",
    timeline_path: Path | None = None,
    source_pcm_path: Path | None = None,
    binary: str | None = None,
    on_stderr: Callable[[str], None] | None = None,
    token: CancellationToken | None = None,
) -> PcmStream:
    """Decode PCM incrementally with optional VVM2 and unmixed PCM sidecars."""
    exe = _require_decoder(binary)
    if Path(exe).stem == _WORKER:
        return BlockPcmStream(
            _decoded_blocks(exe, path, target_order, timeline_path, source_pcm_path, on_stderr, token),
        )
    cmd = [exe, "stream", str(path), target_order]
    if timeline_path is not None and source_pcm_path is not None:
        cmd.extend([str(timeline_path), str(source_pcm_path)])
    return NativePcmStream(cmd, on_stderr=on_stderr, token=token)


def read_vvm1_header(path: Path) -> VvmTimelineHeader | None:
    """Read the 24-byte header of a VVM1 or VVM2 binary telemetry file if complete."""
    try:
        with path.open("rb") as f:
            return _vvm1_header(f.read(_VVM1_HEADER.size))
    except FileNotFoundError:
        return None


def _vvm1_header(raw: bytes) -> VvmTimelineHeader | None:
    if len(raw) != _VVM1_HEADER.size:
        return None
    magic, rate, bed, objs, cfg, flen = _VVM1_HEADER.unpack(raw)
    fields = _VVM_FIELDS.get(magic)
    if fields is None or rate <= 0 or flen <= 0:
        return None
    return VvmTimelineHeader(rate, bed, objs, cfg, flen, fields)


def read_vvm1_frame(
    path: Path,
    header: VvmTimelineHeader,
    frame_index: int,
) -> np.ndarray | None:
    """Read `[x, y, z, peak, jump]` per channel, with a final Size field in VVM2."""
    channels = header.bed_channels + header.object_count
    stride = channels * header.fields * 4
    try:
        with path.open("rb") as f:
            f.seek(_VVM1_HEADER.size + max(0, frame_index) * stride)
            raw = f.read(stride)
    except FileNotFoundError:
        return None
    if len(raw) != stride:
        return None
    return np.frombuffer(raw, dtype="<f4").reshape(channels, header.fields).copy()


class Vvm1Reader:
    """One persistent handle over a decoder's VVM1/VVM2 sidecar, for the audio callback.

    The playback engine asks for a telemetry row every few milliseconds; keeping
    the file open and the stride precomputed keeps that off the open/seek/close
    path the one-shot reader pays per call.
    """

    def __init__(self, path: Path, header: VvmTimelineHeader) -> None:
        self.path, self.header = path, header
        self._stride = (header.bed_channels + header.object_count) * header.fields * 4
        self._fh = None

    def row(self, frame_index: int) -> np.ndarray | None:
        """`read_vvm1_frame` over the persistent handle; None past the written frames."""
        if self._fh is None:
            self._fh = self.path.open("rb")
        self._fh.seek(_VVM1_HEADER.size + max(0, frame_index) * self._stride)
        raw = self._fh.read(self._stride)
        if len(raw) != self._stride:
            return None
        channels = self.header.bed_channels + self.header.object_count
        return np.frombuffer(raw, dtype="<f4").reshape(channels, self.header.fields).copy()

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _write_aatf(
    payload_path: Path, output: Path, config: int, rate: int, bitrate: int,
    hoa_order: int, token: CancellationToken | None,
) -> None:
    bitrate_index = _BITRATE_TABLES[config].index(bitrate)
    profile = 2 if hoa_order else 0
    sample_rate_index = _SAMPLING_RATES.index(rate)
    with payload_path.open("rb") as source, output.open("wb") as dest:
        if source.read(4) != b"SWVP":
            raise Av3aError("Audio Vivid encoder returned an invalid payload stream.")
        while length := source.read(4):
            if token is not None:
                token.raise_if_cancelled()
            size = int.from_bytes(length, "little")
            if len(length) != 4 or not 2 <= size <= 12300:
                raise Av3aError("Invalid Audio Vivid encoded frame size.")
            payload = source.read(size)
            if len(payload) != size:
                raise Av3aError("Truncated Audio Vivid encoded frame.")
            crc = _aatf_crc(payload)
            # MAIN, no ancillary data, 16-bit source. Both supported profiles
            # produce seven header bytes after zero-padding to a byte boundary.
            fields = (
                (0xFFF2, 16), (0, 1), (0, 3), (profile, 3), (sample_rate_index, 4),
                (crc >> 8, 8), (hoa_order - 1, 4) if hoa_order else (config, 7),
                (1, 2), (bitrate_index, 4), (crc & 0xFF, 8),
            )
            header = bits = 0
            for value, width in fields:
                header = (header << width) | value
                bits += width
            dest.write((header << (56 - bits)).to_bytes(7, "big"))
            dest.write(payload)
        if dest.tell() == 0:
            raise Av3aError("Audio Vivid encoder produced no frames.")


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
        config = 10 + hoa_order
        expected = (hoa_order + 1) ** 2
        if ch != expected:
            raise Av3aError(f"HOA order {hoa_order} requires {expected} channels, got {ch}.")
        mode_args = ["-HOA", str(hoa_order)]
        default_br = _ENCODE_HOA_BITRATE.get(hoa_order, 384000)
    elif ch == 1:
        config = 0
        mode_args = ["-MONO"]
        default_br = 128000
    elif ch == 2:
        config = 1
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

        config = _ENCODE_MC_CONFIGS.get(mode_args[1], -1)

    br = int(bitrate_bps or default_br)
    explicit = binary or os.environ.get("AV3A_ENCODER_BIN", "").strip()
    payload_only = not explicit
    if payload_only and (
        config not in _BITRATE_TABLES or len(CONFIG_CHANNEL_ORDER[config]) != ch
        or br not in _BITRATE_TABLES[config] or audio.sample_rate not in _SAMPLING_RATES
    ):
        raise Av3aError("Unsupported Audio Vivid encoder configuration, bitrate or sample rate.")
    fs_khz = f"{audio.sample_rate / 1000:g}"

    with atomic_output(out_path) as output, tempfile.TemporaryDirectory(
        prefix="soundweaver_av3a_enc_",
    ) as tmp:
        in_wav = Path(tmp) / "input.wav"
        _write_pcm16_wav(in_wav, audio, token)
        payload = Path(tmp) / "payload.bin" if payload_only else output
        cmd = [
            exe,
            *(["--payload-only"] if payload_only else []),
            "-MAX_BAND",
            "FB",
            "-BITDEPTH",
            "16",
            *mode_args,
            str(br),
            fs_khz,
            str(in_wav),
            str(payload),
        ]
        run_native(cmd, token=token, error_cls=Av3aError, timeout=600.0)
        if not payload.is_file() or payload.stat().st_size == 0:
            raise Av3aError(f"Audio Vivid encoder produced empty output: {out_path}")
        if payload_only:
            _write_aatf(payload, output, config, audio.sample_rate, br, hoa_order, token)
        if token is not None:
            token.raise_if_cancelled()
    return out_path


def _write_pcm16_wav(
    path: Path, audio: AudioData, token: CancellationToken | None,
) -> None:
    """Write standard 44-byte canonical 16-bit integer WAV required by `Avs3WavReader`."""
    frames, channels, sample_rate = audio.frames, audio.channels, audio.sample_rate
    data_bytes = frames * channels * 2
    if data_bytes > 0xFFFFFFFF - 36:
        raise Av3aError("Audio Vivid encoder input exceeds the PCM RIFF size limit.")
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
        for start in range(0, frames, BLOCK_FRAMES):
            if token is not None:
                token.raise_if_cancelled()
            block = audio.samples[:, start : start + BLOCK_FRAMES].T
            pcm = np.clip(np.round(block * 32767.0), -32768, 32767).astype("<i2")
            f.write(pcm.tobytes())


def extract_elementary_stream(mp4_path: Path, out_av3a: Path) -> bool:
    """Extract raw `.av3a` frames from an MP4 `av3a` track using pure-Python ISOBMFF parsing."""
    if out_av3a.exists():
        out_av3a.unlink()
    return mp4.extract_elementary_stream(mp4_path, (b"av3a",), out_av3a)


__all__ = [
    "AV3A_SUFFIXES",
    "CONFIG_CHANNEL_ORDER",
    "FRAME_SAMPLES",
    "Av3aError",
    "Av3aHeader",
    "Av3aInfo",
    "VvmTimelineHeader",
    "decode_av3a",
    "encode_av3a",
    "extract_elementary_stream",
    "find_decoder",
    "find_encoder",
    "is_av3a_file",
    "parse_aatf_header",
    "probe_av3a",
    "read_vvm1_frame",
    "read_vvm1_header",
    "stream_av3a",
]
