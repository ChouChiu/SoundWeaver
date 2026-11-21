"""The ADM BWF container and metadata timelines shared by every ADM writer.

An ADM BWF file is a 24-bit WAV whose `axml` chunk carries ITU-R BS.2076
metadata, so a Dolby Atmos master (Dolby Atmos Master ADM Profile v1.1) and a
360RA restore differ only in what that metadata says.  The container work lives
here: a header with a JUNK chunk reserving room for `ds64` (a file past 4 GiB
becomes RF64, since section 7.1 of the Dolby profile rules out BW64), the PCM
appended in order, and the metadata written last.

The writers fill in `chna`, the packs, the objects and every block; the
helpers below turn a metadata timeline into the blocks the model wants, in the
coordinate and time units it wants them in.
"""

from __future__ import annotations

import math
import shutil
import struct
from collections.abc import Iterable
from itertools import chain, pairwise
from pathlib import Path

import numpy as np

from soundweaver.core.paths import output_workspace

BIT_DEPTH = 24
_FULL_SCALE = 2 ** (BIT_DEPTH - 1)
_RIFF_LIMIT = 0xFFFFFFFF
_DS64_SIZE = 28

# Table 2-15: the interpolationLength of every block but the first, which has 0.
INTERPOLATION = "0.005208"
# Section 2.5.2: a glide is sampled into blocks less than this far apart.
SAMPLE_PERIOD = 0.02
# Events closer than a timecode's last digit are one event: the later one wins.
SAME_TIME = 1e-5
# Timecode ticks per second: five decimals of a second.
TICKS_PER_SECOND = 100_000


def timecode(seconds: float) -> str:
    """Seconds as an ADM timecode, `hh:mm:ss.sssss`."""
    seconds = max(0.0, round(seconds, 5))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:08.5f}"


def number(value: float) -> str:
    """A position as a plain decimal with four places (section 2.1), and no negative zero."""
    return f"{value + 0.0:.4f}".replace("-0.0000", "0.0000")


def pcm24(block: np.ndarray) -> tuple[bytes, int]:
    """(channels, samples) as interleaved little-endian 24-bit PCM, and how many samples clipped."""
    scaled = np.round(block.T * _FULL_SCALE)
    over = (scaled > _FULL_SCALE - 1) | (scaled < -_FULL_SCALE)
    clipped = int(over.sum())
    if clipped:
        np.clip(scaled, -_FULL_SCALE, _FULL_SCALE - 1, out=scaled)
    raw = np.ascontiguousarray(scaled, dtype="<i4").view(np.uint8).reshape(-1, 4)[:, :3]
    return raw.tobytes(), clipped


def lerp(a: tuple[float, ...], b: tuple[float, ...], f: float) -> tuple[float, ...]:
    return tuple(x + (y - x) * f for x, y in zip(a, b, strict=True))


def events(
    keys: Iterable[tuple[float, float, tuple[float, ...]]],
    duration: float,
    default: tuple[float, ...],
) -> list[tuple[float, tuple[float, ...]]]:
    """Sample one metadata parameter timeline, starting at time 0.

    A key glides from where the object is to its position over its ramp, or
    until the next key takes over; the glide is sampled evenly, under
    `SAMPLE_PERIOD` apart.  Events before the container starts collapse onto
    its first sample, events past it go, and an event that leaves the value
    where it was is dropped.
    """
    timed: list[tuple[float, tuple[float, ...]]] = []
    here: tuple[float, ...] | None = None
    for (t, ramp, target), (end, _, _) in pairwise(chain(keys, [(math.inf, 0.0, default)])):
        if here is None or ramp <= 0:
            timed.append((t, target))
            here = target
            continue
        start = here
        here = lerp(start, target, min(1.0, (end - t) / ramp))
        steps = int(ramp / SAMPLE_PERIOD) + 1
        for k in range(1, steps + 1):
            at = t + ramp * k / steps
            if at > end + SAME_TIME:
                # cut short by the next key: hold where the glide got to
                timed.append((end, here))
                break
            timed.append((at, lerp(start, target, k / steps)))

    out: list[tuple[float, tuple[float, ...]]] = []
    for t, value in timed:
        t = max(0.0, t)
        if t >= duration:
            break
        if out and t <= out[-1][0] + SAME_TIME:
            out.pop()
        if not out or out[-1][1] != value:
            out.append((t, value))
    if not out:
        return [(0.0, timed[0][1] if timed else default)]
    out[0] = (0.0, out[0][1])
    return out


def quantize(
    timeline: list[tuple[float, tuple[float, ...]]], duration: float
) -> tuple[list[tuple[float, tuple[float, ...]]], float]:
    """Collapse a timeline onto whole timecode ticks, and report the quantized duration.

    Two events can round onto the same tick; the last one wins, so a block keeps
    the position its samples actually have.
    """
    end_tick = round(duration * TICKS_PER_SECOND)
    by_tick = {
        tick: value
        for t, value in timeline
        if (tick := round(t * TICKS_PER_SECOND)) < end_tick
    }
    return (
        [(tick / TICKS_PER_SECOND, value) for tick, value in by_tick.items()],
        end_tick / TICKS_PER_SECOND,
    )


class AdmBwf:
    """Writes the header up front, takes the PCM in order, and the metadata at `close`.

    `chna` is the 40-byte-per-track table the writers build once they know their
    packs; the metadata itself is passed to `close` or `prepare_readback`, whose
    callers build the `axml` from the timelines they collected on the way.
    """

    def __init__(
        self, target: Path, sample_rate: int, length: int, channels: int, chna: bytes
    ) -> None:
        self.target, self.sample_rate, self.length, self.channels = (
            target,
            sample_rate,
            length,
            channels,
        )
        self.data_bytes = 0
        align = channels * BIT_DEPTH // 8
        fmt = struct.pack(
            "<IHHIIHH", 16, 1, channels, sample_rate, sample_rate * align, align, BIT_DEPTH
        )
        header = (
            b"RIFF" + bytes(4) + b"WAVE"
            + b"JUNK" + struct.pack("<I", _DS64_SIZE) + bytes(_DS64_SIZE)
            + b"fmt " + fmt
            + b"chna" + struct.pack("<I", len(chna)) + chna
            + b"data" + bytes(4)
        )
        self._workspace = output_workspace(target)
        self.partial = Path(self._workspace.name) / target.name
        try:
            self.file = self.partial.open("w+b")
            try:
                self.file.write(header)
            except OSError:
                self.file.close()
                raise
        except OSError:
            self._workspace.cleanup()
            raise
        self.data_start = len(header)
        # a playback decode publishes the header mid-stream; the partial is gone by then
        self.published = False

    def append(self, part: Path) -> None:
        """Copy a run of PCM written by `pcm24` onto the end of the data chunk."""
        with part.open("rb") as source:
            # seek explicitly: a mid-stream axml rewrite moves the file position
            self.file.seek(self.data_start + self.data_bytes)
            shutil.copyfileobj(source, self.file, 1 << 22)
            self.data_bytes += source.tell()

    def write_pcm(self, data: bytes) -> None:
        self.file.seek(self.data_start + self.data_bytes)
        self.file.write(data)
        self.data_bytes += len(data)

    @property
    def frames_written(self) -> int:
        return self.data_bytes // (self.channels * BIT_DEPTH // 8)

    def prepare_readback(self, axml: bytes) -> Path:
        """Publish the header at the stable, private path owned by playback."""
        size = self.length * self.channels * BIT_DEPTH // 8
        self._write_footer(axml, size)
        self.file.close()
        self.partial.replace(self.target)
        self.published = True
        self._workspace.cleanup()
        self.file = self.target.open("r+b")
        self.file.seek(self.data_start)
        return self.target

    def close(self, axml: bytes) -> None:
        """Finish the file with `axml` and the PCM actually written."""
        try:
            self._write_footer(axml, self.data_bytes)
            self.file.close()
            if not self.published:
                self.partial.replace(self.target)
        finally:
            self.discard()

    def refresh(self, axml: bytes) -> None:
        """Rewrite the published axml footer in place while PCM keeps appending.

        The playback decode publishes early with a partial metadata timeline and
        rewrites it as the scan grows.  The axml sits at the declared final data
        offset, so a rewrite never moves the PCM the reader already sees; the
        file position after this call is undefined, and the writers seek before
        every PCM write.
        """
        if not self.published:
            return
        self._write_footer(axml, self.length * self.channels * BIT_DEPTH // 8)

    def _write_footer(self, axml: bytes, data: int) -> None:
        w = self.file
        w.seek(self.data_start + data)
        if data % 2:
            w.write(b"\0")
        w.write(b"axml" + struct.pack("<I", len(axml)) + axml)
        if len(axml) % 2:
            w.write(b"\0")
        riff = w.tell() - 8
        if riff > _RIFF_LIMIT or data > _RIFF_LIMIT:
            w.seek(0)
            w.write(b"RF64" + struct.pack("<I", _RIFF_LIMIT) + b"WAVE")
            w.write(b"ds64" + struct.pack("<I", _DS64_SIZE))
            frames = data // (self.channels * BIT_DEPTH // 8)
            w.write(struct.pack("<QQQI", riff, data, frames, 0))
            data = _RIFF_LIMIT
        else:
            w.seek(4)
            w.write(struct.pack("<I", riff))
        w.seek(self.data_start - 4)
        w.write(struct.pack("<I", data))
        w.flush()

    def discard(self) -> None:
        self.file.close()
        self._workspace.cleanup()
