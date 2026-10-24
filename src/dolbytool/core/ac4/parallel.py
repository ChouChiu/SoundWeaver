"""Decoding the frames of an AC-4 track on several processes at once.

A decoder that starts on an I-frame needs little from before it: what the last
frames leave in its transforms, filter banks, smoothers and held-back tool data
dies away within a few frames, so the frames are cut into stretches that each
begin some frames past an I-frame, and a worker decodes a stretch from that
I-frame on and keeps only the stretch's own frames.  The one thing that does
not die away is where A-SPX stands in its noise and sine tables, which runs on
from the start of the stream; it follows from how many slots each earlier
interval filled, so every stretch is first only parsed, and the positions are
added up in order before the stretches after it are decoded.  The parsing also
hands over each frame's object metadata, for what runs on in this process
alongside (objects.py: the trace, and the renderer's glides).
"""

from __future__ import annotations

import multiprocessing
import os
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace

import numpy as np

from dolbytool.core.atmos.bitstream import BitstreamError
from dolbytool.core.atmos.chunk import init_worker
from dolbytool.core.atmos.oamd import Frame
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.i18n import get_language

from . import aspx
from .frame import ALIGNMENT, FrameDecoder
from .toc import read_toc

# frames per stretch, frames decoded before it from an I-frame, and stretches a
# worker may hold decoded but not yet taken
_STRETCH = 96
_WARMUP = 16
_AHEAD = 2
_WORKERS = max(1, min((os.process_cpu_count() or 1) - 1, 12))


@dataclass(slots=True)
class Decoded:
    """One frame's samples per channel (or per object and "LFE"), and what goes with them."""

    pcm: dict[str, np.ndarray]
    metadata: Frame | None  # the A-JOC objects' metadata, None for channels
    delay: int  # FrameDecoder.delay after this frame


class FrameError(BitstreamError):
    """A frame that would not decode, by its index in the track."""

    def __init__(self, index: int, error: BitstreamError) -> None:
        super().__init__(str(error))
        self.index, self.error = index, error

    def __reduce__(self):
        return FrameError, (self.index, self.error)


@dataclass(frozen=True, slots=True)
class Stretch:
    warm: int  # first frame decoded, an I-frame
    start: int  # first frame kept
    stop: int  # one past the last frame kept


def plan(frames: list[bytes]) -> list[Stretch]:
    """The stretches `frames` is decoded in; a short track is one."""
    if len(frames) < 4 * _STRETCH:
        return [Stretch(0, 0, len(frames))]
    warmup = max(_WARMUP, ALIGNMENT.get(read_toc(frames[0]).frame_length, (0, 4))[1] + 2)
    starts = [0]
    k = _STRETCH
    while k < len(frames) - _STRETCH // 2:
        if _iframe(frames[k - warmup]):
            starts.append(k)
            k += _STRETCH
        else:
            k += 1
    stops = [*starts[1:], len(frames)]
    return [Stretch(max(0, s - warmup), s, e) for s, e in zip(starts, stops, strict=True)]


@contextmanager
def worker_pool(stretches: int) -> Iterator[multiprocessing.pool.Pool | _Inline]:
    """A pool for `stretches` stretches; for one, everything runs here, as it is called."""
    if stretches == 1:
        yield _Inline()
        return
    # spawn: the caller may be a thread of a Qt application, which must not fork
    pool = multiprocessing.get_context("spawn").Pool(
        min(_WORKERS, stretches), init_worker, (get_language(),)
    )
    try:
        yield pool
        pool.close()
    finally:
        pool.terminate()
        pool.join()


class _Inline:
    """Pool.apply_async run on the spot; what it raises, it raises right away."""

    def apply_async(self, fn: Callable, args: tuple) -> _Done:
        return _Done(fn(*args))


@dataclass(slots=True)
class _Done:
    value: object

    def ready(self) -> bool:
        return True

    def wait(self, _timeout: float) -> None:
        pass

    def get(self):
        return self.value


def done(value: object) -> _Done:
    """A result worked out already, to stand among a pool's."""
    return _Done(value)


def wait(result, token: CancellationToken):
    """An AsyncResult's value, watching for cancellation meanwhile."""
    while not result.ready():
        token.raise_if_cancelled()
        result.wait(0.1)
    return result.get()


class Parse:
    """The parse pass over every stretch, taken in frame order in this process.

    `take` goes through the frames up to a point: their A-SPX intervals into the
    noise and sine positions, snapshotted at each stretch's first decoded frame
    for `resume`, and their object metadata into `metadata`.
    """

    def __init__(
        self, pool, frames: list[bytes], stretches: list[Stretch], token, here: int = 0
    ) -> None:
        """The first `here` stretches are parsed in this process, once the rest are handed out."""
        self.stretches, self.token = stretches, token
        jobs = [(_parse, (frames[s.warm : s.stop], s.start - s.warm)) for s in stretches]
        self.results = [pool.apply_async(*job) for job in jobs[here:]]
        self.results[:0] = [_Done(fn(*args)) for fn, args in jobs[:here]]
        self.ctrl_delay = ALIGNMENT.get(read_toc(frames[0]).frame_length, (0, 0))[1]
        self.cursors: dict[str, aspx.Cursor] = {}
        self.snapshots: dict[int, dict[str, aspx.Cursor]] = {}
        self.marks = {s.warm for s in stretches}
        # each frame's own object metadata, None for channels; `delayed` gives decoding's
        self.metadata: list[Frame | None] = []
        self.broken = False  # a frame would not parse, so nothing after it is known

    def take(self, until: int, block: bool) -> bool:
        """Go through the frames before `until`; False if the parsing is not there yet
        (and not `block`) or never will be."""
        while len(self.metadata) < until:
            k = len(self.metadata)
            if k in self.marks:
                self.snapshots[k] = {name: replace(c) for name, c in self.cursors.items()}
            index = next(i for i, s in enumerate(self.stretches) if s.start <= k < s.stop)
            result = self.results[index]
            if self.broken or (not block and not result.ready()):
                return False
            parsed = wait(result, self.token)
            if k - self.stretches[index].start >= len(parsed):
                self.broken = True
                return False
            intervals, metadata = parsed[k - self.stretches[index].start]
            # the first frame goes through d_ctrl + 1 times (frame.py)
            for _ in range(self.ctrl_delay + 1 if k == 0 else 1):
                for name, (width, slots) in intervals.items():
                    self.cursors.setdefault(name, aspx.Cursor()).advance(width, slots)
            self.metadata.append(metadata)
        if until in self.marks and until not in self.snapshots:
            self.snapshots[until] = {name: replace(c) for name, c in self.cursors.items()}
        return True

    def resume(self, stretch: Stretch) -> dict[str, aspx.Cursor] | None:
        """The noise and sine positions a decoder starting on `stretch.warm` takes."""
        return (
            {n: replace(c) for n, c in self.snapshots[stretch.warm].items()}
            if stretch.warm
            else None
        )

    def delayed(self, k: int) -> Frame | None:
        """The metadata that comes out with frame k: d_ctrl frames older (frame.py)."""
        return self.metadata[max(0, k - self.ctrl_delay)]

    def error(self) -> FrameError:
        """What to report when the parsing stopped short and no decoding said why."""
        return FrameError(len(self.metadata), BitstreamError("the frame would not parse"))


def decode_frames(frames: list[bytes], token: CancellationToken) -> Iterator[Decoded]:
    """Every frame of `frames` decoded, in order, as one FrameDecoder would decode them."""
    stretches = plan(frames)
    if len(stretches) == 1:
        yield from _decode(frames, 0, 0, None)
        return
    with worker_pool(len(stretches)) as pool:
        parse = Parse(pool, frames, stretches, token)
        pending: deque = deque()
        queued = 1

        def submit(block: bool) -> None:
            """Hand the next stretches to the pool, as far as the parsing has got unless `block`."""
            nonlocal queued
            while queued < len(stretches) and len(pending) < _AHEAD * _WORKERS:
                s = stretches[queued]
                if not parse.take(s.warm, block):
                    return
                pending.append(
                    pool.apply_async(
                        _stretch,
                        (frames[s.warm : s.stop], s.start - s.warm, s.warm, parse.resume(s)),
                    )
                )
                queued += 1

        # the first stretch needs no positions: it is decoded here while the workers start
        for k, decoded in enumerate(_decode(frames[: stretches[0].stop], 0, 0, None)):
            if k % 8 == 0:
                submit(block=False)
            yield decoded
        while pending or (queued < len(stretches) and not parse.broken):
            submit(block=True)
            if pending:
                yield from wait(pending.popleft(), token)
        if queued < len(stretches):
            raise parse.error()


def _iframe(frame: bytes) -> bool:
    try:
        toc = read_toc(frame)
        group = toc.groups[toc.presentations[0].groups[0]]
    except (BitstreamError, IndexError):
        return False
    return toc.iframe_global or all(info.iframe for info in group.substreams)


def _parse(frames: list[bytes], skip: int) -> list[tuple[dict[str, tuple[int, int]], Frame | None]]:
    """Per frame from `skip` on, parsed only: its A-SPX intervals as aspx.interval
    gives them, and its own object metadata.

    A frame that will not parse ends the list; decoding it reports the error.
    """
    decoder = FrameDecoder()
    out = []
    for k, frame in enumerate(frames):
        try:
            element = decoder.element(frame)
        except BitstreamError:
            break
        if k >= skip:
            tools = decoder.tools
            intervals = {n: aspx.interval(tools.channel(n), f) for n, f in element.aspx.items()}
            payload = decoder.payload
            out.append((intervals, payload.metadata if payload is not None else None))
    return out


def _stretch(
    frames: list[bytes], skip: int, first: int, resume: dict[str, aspx.Cursor] | None
) -> list[Decoded]:
    return list(_decode(frames, skip, first, resume))


def _decode(
    frames: list[bytes], skip: int, first: int, resume: dict[str, aspx.Cursor] | None
) -> Iterator[Decoded]:
    decoder = FrameDecoder()
    decoder.resume = resume
    for k, frame in enumerate(frames):
        try:
            pcm = decoder.decode(frame)
        except BitstreamError as error:
            raise FrameError(first + k, error) from error
        if k >= skip:
            payload = decoder.payload
            yield Decoded(pcm, payload.metadata if payload is not None else None, decoder.delay)
