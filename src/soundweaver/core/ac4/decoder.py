"""AC-4 as the rest of the app sees it: a file ffmpeg cannot decode, made readable once.

ffmpeg reads the container and copies the AC-4 frames out; everything past
that is ours (`frame.FrameDecoder`).  The decoded PCM is cached as a 24-bit
FLAC keyed on the source, and every feature that hands audio to ffmpeg or the
renderer reads that file in place of the source.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from soundweaver.core import ffmpeg
from soundweaver.core.atmos.bitstream import BitstreamError
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.i18n import t
from soundweaver.core.paths import atomic_output, cache_dir, content_key, file_stamp, work_root
from soundweaver.core.speakers import BY_NAME

from .frame import CHANNEL_ORDER, coded_channel_mode
from .parallel import Decoded, FrameError, decode_frames
from .toc import SYNC, SYNC_CRC, read_toc, sync_frames

logger = logging.getLogger(__name__)

CODEC_NAME = "ac4"
# bumped whenever decoding changes what a cached file holds
_CACHE_VERSION = 4
_CACHE_SUFFIX = ".ac4.flac"
_KEEP = 16
# Table 83: frames per second of each frame_rate_index, at 48 kHz
_FRAME_RATES = (
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
    Fraction(48000, 1001), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
    Fraction(100), Fraction(120000, 1001), Fraction(120), Fraction(48000, 2048),
)  # fmt: skip
# The labels of what each channel_mode decodes to (table 88)
_MODE_CHANNELS = {
    0: ("C",), 1: ("L", "R"), 2: ("L", "R", "C"), 3: ("L", "R", "C", "Ls", "Rs"),
    4: ("L", "R", "C", "LFE", "Ls", "Rs"),
    5: ("L", "R", "C", "Ls", "Rs", "Lb", "Rb"), 6: ("L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb"),
    7: ("L", "R", "C", "Ls", "Rs", "Lw", "Rw"), 8: ("L", "R", "C", "LFE", "Ls", "Rs", "Lw", "Rw"),
    9: ("L", "R", "C", "Ls", "Rs", "Tfl", "Tfr"),
    10: ("L", "R", "C", "LFE", "Ls", "Rs", "Tfl", "Tfr"),
}  # fmt: skip


# what readable() renders the objects of an A-JOC track to: the 5.1 an E-AC-3 JOC core is
OBJECTS_RENDER = BY_NAME["5.1"]


class Ac4Error(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Ac4Info:
    channels: tuple[str, ...]  # in output order
    sample_rate: int
    # presentation_version 2: Immersive Stereo, and the channel_mode it was made from
    ims: bool = False
    source_mode: int = 1
    # A-JOC coded objects (Dolby Atmos); `channels` are then those of the render readable() makes
    objects: bool = False
    # seconds, for a raw AC-4 file, which has no container to say
    duration: float | None = None


def is_ac4_stream(stream: dict) -> bool:
    return stream.get("codec_name") == CODEC_NAME


@lru_cache(maxsize=64)
def _is_ac4(stamp: tuple) -> bool:
    try:
        return is_ac4_stream(ffmpeg.first_audio_stream(ffmpeg.probe(stamp[0])))
    except (ffmpeg.FFmpegError, OSError):
        return False


def is_ac4(path: Path) -> bool:
    return _is_ac4(file_stamp(path))


def _output_channels(channel_mode: int) -> tuple[str, ...]:
    labels = _MODE_CHANNELS.get(channel_mode)
    if labels is None:
        raise Ac4Error(t("ac4.unsupported_channels", mode=channel_mode))
    return tuple(label for label in CHANNEL_ORDER if label in labels)


def _rates(toc) -> tuple[Fraction, int]:
    """(rate the frames are coded at, rate they are delivered at).

    At the video frame rates a frame holds a power-of-two-ish count of samples
    for one video frame, so the coded rate is off 48 kHz (46033.97 Hz at
    29.97 fps) and the output is resampled to 48 kHz (clause 6.2.15).
    """
    delivered = 48000 if toc.fs_index else 44100
    coded = toc.frame_length * _FRAME_RATES[toc.frame_rate_index]
    if not toc.fs_index:
        coded = Fraction(delivered)  # 44.1 kHz only exists at frame_rate_index 13
    ratio = Fraction(delivered) / coded
    if max(ratio.numerator, ratio.denominator) > 4096:
        raise Ac4Error(t("ac4.unsupported_rate", rate=float(coded)))
    return coded, delivered


def _is_raw(path: Path) -> bool:
    """Whether `path` is a bare AC-4 elementary stream (TS 103 190-1 Annex G), not a container."""
    with path.open("rb") as file:
        return int.from_bytes(file.read(2), "big") in (SYNC, SYNC_CRC)


def stream_bytes(
    path: Path, token: CancellationToken | None = None, *, first_only: bool = False
) -> bytes:
    """The AC-4 sync frames of `path`'s first audio track.

    ffmpeg copies them out of a container; a bare stream is read as it is,
    since ffmpeg cannot write one back without a sample rate it does not know.
    """
    if _is_raw(path):
        return path.read_bytes()
    limit = ["-frames:a", "1"] if first_only else []
    with tempfile.TemporaryDirectory(dir=work_root()) as scratch:
        stream = Path(scratch) / "stream.ac4"
        ffmpeg.run_ffmpeg(
            ["-i", str(path), "-map", "0:a:0", "-c", "copy", *limit, "-f", "ac4", str(stream)],
            token,
        )
        return stream.read_bytes()


def describe(path: Path) -> Ac4Info:
    """What the first frame says about the first presentation."""
    data = stream_bytes(path, first_only=True)
    first = next(sync_frames(data), None)
    if first is None:
        raise Ac4Error(t("ac4.no_frames"))
    info = _info(first)
    if _is_raw(path):
        toc = read_toc(first)
        frames = sum(1 for _ in sync_frames(data))
        info = replace(info, duration=float(frames * toc.frame_length / _rates(toc)[0]))
    return info


def _info(frame: bytes) -> Ac4Info:
    try:
        toc = read_toc(frame)
        presentation = toc.presentations[0]
        group = toc.groups[presentation.groups[0]]
    except (BitstreamError, IndexError) as error:
        raise Ac4Error(t("ac4.bad_toc", error=error)) from error
    if not group.channel_coded:
        if group.substreams[0].kind != "ajoc":
            raise Ac4Error(t("ac4.objects_unsupported"))
        return Ac4Info(OBJECTS_RENDER.channels, _rates(toc)[1], objects=True)
    mode = group.substreams[0].channel_mode
    coded = coded_channel_mode(presentation.version, mode)
    return Ac4Info(_output_channels(coded), _rates(toc)[1], coded != mode, mode)


def decoded_path(
    source: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None] = lambda _f: None,
) -> Path:
    """The cached FLAC of `source`'s first AC-4 track, decoding it first if need be."""
    directory = cache_dir("ac4")
    target = directory / f"{content_key(source)}.v{_CACHE_VERSION}{_CACHE_SUFFIX}"
    if target.is_file():
        on_fraction(1.0)
        return target
    data = stream_bytes(source, token)
    with atomic_output(target) as partial:
        _decode(data, partial, token, on_fraction)
    for stale in sorted(directory.glob("*" + _CACHE_SUFFIX), key=lambda p: p.stat().st_mtime)[
        :-_KEEP
    ]:
        stale.unlink(missing_ok=True)
    return target


def readable(
    path: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None] = lambda _f: None,
) -> Path:
    """`path`, or for an AC-4 track the decoded copy ffmpeg and the renderer can read."""
    return decoded_path(path, token, on_fraction) if is_ac4(path) else path


def _decode(
    data: bytes, target: Path, token: CancellationToken, on_fraction: Callable[[float], None]
) -> None:
    frames = list(sync_frames(data))
    if not frames:
        raise Ac4Error(t("ac4.no_frames"))
    info = _info(frames[0])
    if info.objects:
        _render(frames, target, token, on_fraction)
        return
    with sf.SoundFile(
        target, "w", samplerate=info.sample_rate, channels=len(info.channels),
        format="FLAC", subtype="PCM_24",
    ) as out:
        for block in _channel_blocks(frames, info, token, on_fraction):
            out.write(block)
    on_fraction(1.0)
    logger.info(
        "ac4: decoded %d frames, %s at %d Hz",
        len(frames), "/".join(info.channels), info.sample_rate,
    )


def playback_blocks(source: Path, token: CancellationToken) -> tuple[Ac4Info, Iterator[np.ndarray]]:
    frames = list(sync_frames(stream_bytes(source, token)))
    if not frames:
        raise Ac4Error(t("ac4.no_frames"))
    info = _info(frames[0])
    if info.objects:
        raise Ac4Error(t("ac4.layout_changed", frame=0))
    return info, _channel_blocks(frames, info, token, lambda _f: None, playback=True)


def playback_from_cache(
    source: Path, token: CancellationToken
) -> tuple[Ac4Info, sf.SoundFile] | None:
    """A cached-FLAC reader for plain AC-4 playback, or None when the stream must decode live.

    The export cache holds the same resampled PCM the playback path yields, so a
    track played before starts from disk instead of a full soft decode.  Object
    streams never use it: the player needs their raw frames for object metering.
    """
    target = cache_dir("ac4") / f"{content_key(source)}.v{_CACHE_VERSION}{_CACHE_SUFFIX}"
    if not target.is_file():
        return None
    info = describe(source)
    if info.objects:
        return None
    try:
        wav = sf.SoundFile(target)
    except sf.LibsndfileError:
        logger.warning("ac4: discarding unreadable playback cache %s", target, exc_info=True)
        target.unlink(missing_ok=True)
        return None
    token.raise_if_cancelled()
    return info, wav


def _channel_blocks(
    frames: list[bytes],
    info: Ac4Info,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    *,
    playback: bool = False,
) -> Iterator[np.ndarray]:
    """Yield interleaved PCM with the same delay compensation as the FLAC cache."""
    coded_rate, rate = _rates(read_toc(frames[0]))
    resampler = Resampler(
        Fraction(rate) / coded_rate, len(info.channels), 12000 if playback else 48000
    )
    trimmed = 0
    for k, decoded in enumerate(_checked_frames(frames, token, playback=playback)):
        if k % 64 == 0:
            token.raise_if_cancelled()
            on_fraction(k / len(frames))
        pcm = decoded.pcm
        if set(pcm) != set(info.channels):
            raise Ac4Error(t("ac4.layout_changed", frame=k))
        block = np.stack([pcm[name] for name in info.channels]) / 32768.0
        if trimmed < decoded.delay:
            cut = min(decoded.delay - trimmed, block.shape[1])
            block, trimmed = block[:, cut:], trimmed + cut
        ready = resampler.push(block)
        if ready.size:
            yield np.clip(ready, -1.0, 1.0).T
    resampler.push(np.zeros((len(info.channels), trimmed)))
    tail = resampler.finish()
    if tail.size:
        yield np.clip(tail, -1.0, 1.0).T


def _checked_frames(
    frames: list[bytes], token: CancellationToken, *, playback: bool = False,
) -> Iterator[Decoded]:
    """`parallel.decode_frames`, a frame that will not decode reported as an Ac4Error."""
    try:
        yield from decode_frames(frames, token, playback=playback)
    except FrameError as error:
        raise Ac4Error(t("ac4.bad_frame", frame=error.index, error=error.error)) from error


class Resampler:
    """`resample_poly` over a stream of (channels, samples) blocks, exactly as over the whole.

    Each stretch is resampled with a margin of input on either side and cut
    where the polyphase grid lines up; the filter is much shorter than the
    margin, so the cuts leave no trace.  A ratio of 1 passes blocks through.
    """

    def __init__(self, ratio: Fraction, channels: int, block_size: int = 48000) -> None:
        self.up, self.down = ratio.numerator, ratio.denominator
        self.margin = self.down * max(1, 64 // self.down)
        self.stretch = self.down * max(1, block_size // self.down)
        self.buffer = np.zeros((channels, 0), dtype=np.float32)
        self.start = 0  # input sample buffer[:, 0] is
        self.done = 0  # input samples resampled so far, a multiple of `down`
        self.total = 0  # input samples pushed

    def push(self, block: np.ndarray) -> np.ndarray:
        if self.up == self.down:
            return block
        self.buffer = np.concatenate([self.buffer, block.astype(np.float32)], axis=1)
        self.total += block.shape[1]
        out = []
        while self.total - self.done >= self.stretch + self.margin:
            out.append(self._run(self.done + self.stretch + self.margin, self.stretch))
            self.done += self.stretch
            keep = max(0, self.done - self.margin)
            self.buffer, self.start = self.buffer[:, keep - self.start :], keep
        return np.concatenate(out, axis=1) if out else np.zeros((self.buffer.shape[0], 0))

    def finish(self) -> np.ndarray:
        if self.up == self.down:
            return np.zeros((self.buffer.shape[0], 0))
        count = -(-self.total * self.up // self.down) - self.done * self.up // self.down
        return self._run(self.total, None)[:, :count]

    def _run(self, end: int, stretch: int | None) -> np.ndarray:
        begin = max(0, self.done - self.margin)
        y = resample_poly(
            self.buffer[:, begin - self.start : end - self.start], self.up, self.down, axis=1
        )
        first = (self.done - begin) * self.up // self.down
        last = None if stretch is None else first + stretch * self.up // self.down
        return y[:, first:last]


def _render(
    frames: list[bytes],
    target: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
) -> None:
    """The objects of an A-JOC stream rendered to `OBJECTS_RENDER`, as the readable copy."""
    from .objects import render_objects  # objects builds on this module

    _trace, audio = render_objects(frames, token, on_fraction, OBJECTS_RENDER, True, None)
    with audio:
        sf.write(target, np.clip(audio.samples.T, -1.0, 1.0), audio.sample_rate,
                 format="FLAC", subtype="PCM_24")  # fmt: skip
    logger.info("ac4: rendered %d A-JOC frames to %s", len(frames), OBJECTS_RENDER.name)
