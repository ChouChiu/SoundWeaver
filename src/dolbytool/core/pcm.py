"""Planar float32 audio kept in disk-backed buffers, read and written in blocks.

A long multichannel master never becomes one resident array: buffers are
memory-mapped scratch files, and every loop walks them `BLOCK_FRAMES` at a time.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Self

import numpy as np
import soundfile as sf

from dolbytool.core import ffmpeg
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.paths import atomic_output, work_root

# Anything decoded from a wider or floating-point source is written at 24
# bits, the practical ceiling for a delivery file.
DEFAULT_BIT_DEPTH = 24
# Every streaming loop reads, writes and copies audio in blocks of this many frames.
BLOCK_FRAMES = 262_144
# Formats libsndfile reads itself, keeping their bit depth; the rest go through ffmpeg.
SOUNDFILE_SUFFIXES = {".wav", ".flac", ".aiff", ".aif", ".ogg"}


@dataclass(frozen=True, slots=True)
class AudioData:
    """Planar float32 audio, shape [channels, frames], nominally within -1..1.

    A buffer made by `create_pcm_audio` owns its scratch file and removes it
    on `cleanup` (or leaving its `with` block); views made from it own nothing.
    """

    samples: np.ndarray
    sample_rate: int
    bit_depth: int = DEFAULT_BIT_DEPTH
    backing_path: Path | None = None

    @property
    def channels(self) -> int:
        return int(self.samples.shape[0])

    @property
    def frames(self) -> int:
        return int(self.samples.shape[1])

    def stereo(self) -> AudioData:
        """The first two channels; a mono source is duplicated onto both."""
        rows = np.repeat(self.samples, 2, axis=0) if self.channels == 1 else self.samples[:2]
        return AudioData(rows, self.sample_rate, self.bit_depth)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        # the mapping goes with the last array referring to it; the name goes now
        if self.backing_path is not None:
            self.backing_path.unlink(missing_ok=True)


def _scratch() -> Path:
    fd, name = tempfile.mkstemp(prefix="pcm-", suffix=".f32", dir=work_root())
    os.close(fd)
    return Path(name)


def create_pcm_audio(
    channels: int, frames: int, sample_rate: int, bit_depth: int = DEFAULT_BIT_DEPTH
) -> AudioData:
    """A silent planar float32 buffer, mapped from a scratch file under the work root."""
    path = _scratch()
    try:
        samples = np.memmap(path, mode="w+", dtype=np.float32, shape=(channels, frames))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return AudioData(samples, sample_rate, bit_depth, path)


def _bit_depth(subtype: str) -> int:
    """8- and 16-bit PCM keeps 16 bits; wider PCM, float and every lossy format get 24."""
    return 16 if subtype in {"PCM_S8", "PCM_U8", "PCM_16"} else DEFAULT_BIT_DEPTH


def load_audio(path: Path, token: CancellationToken) -> AudioData:
    """The first audio stream of any file ffmpeg or libsndfile reads, as a scratch buffer."""
    if path.suffix.lower() in SOUNDFILE_SUFFIXES:
        try:
            return read_audio(path, token)
        except sf.LibsndfileError:
            pass  # a variant libsndfile does not know; ffmpeg may
    return decode_audio(path, token)


def decode_audio(path: Path, token: CancellationToken) -> AudioData:
    """Decode with ffmpeg straight into a scratch file, used in place as interleaved float32."""
    stream = ffmpeg.first_audio_stream(ffmpeg.probe(path))
    channels, rate = int(stream["channels"]), int(stream["sample_rate"])
    scratch = _scratch()
    try:
        ffmpeg.run_ffmpeg(
            ["-i", str(path), "-map", "0:a:0", "-c:a", "pcm_f32le", "-f", "f32le", str(scratch)],
            token,
        )
        frames = scratch.stat().st_size // (4 * channels)
        if frames <= 0:
            raise ValueError(f"音频不含任何采样：{path.name}")
        interleaved = np.memmap(scratch, dtype=np.float32, mode="r", shape=(frames, channels))
    except BaseException:
        scratch.unlink(missing_ok=True)
        raise
    return AudioData(interleaved.T, rate, DEFAULT_BIT_DEPTH, scratch)


def read_audio(path: Path, token: CancellationToken) -> AudioData:
    """Decode a file libsndfile reads into a scratch buffer; ffmpeg converts anything else first."""
    with sf.SoundFile(path) as source:
        if source.frames <= 0:
            raise ValueError(f"音频不含任何采样：{path.name}")
        audio = create_pcm_audio(
            source.channels, source.frames, source.samplerate, _bit_depth(source.subtype)
        )
        try:
            start = 0
            for block in source.blocks(blocksize=BLOCK_FRAMES, dtype="float32", always_2d=True):
                token.raise_if_cancelled()
                audio.samples[:, start : start + block.shape[0]] = block.T
                start += block.shape[0]
        except BaseException:
            audio.cleanup()
            raise
    return audio


def write_wav(path: Path, audio: AudioData, token: CancellationToken) -> float:
    """Write `audio` at its own rate and depth, appearing only once complete; returns its peak."""
    subtype = "PCM_24" if audio.bit_depth == 24 else "PCM_16"
    peak = 0.0
    interleaved = audio.samples.T
    with atomic_output(path) as partial:
        with sf.SoundFile(
            partial, "w", audio.sample_rate, audio.channels, subtype=subtype, format="WAV"
        ) as output:
            for start in range(0, audio.frames, BLOCK_FRAMES):
                token.raise_if_cancelled()
                # scrubbed per block: over the whole mapping it would all become resident
                block = np.ascontiguousarray(interleaved[start : start + BLOCK_FRAMES])
                np.nan_to_num(block, copy=False)
                peak = max(peak, float(np.max(np.abs(block), initial=0.0)))
                output.write(block)
        token.raise_if_cancelled()
    return peak
