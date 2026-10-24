"""Decoding the frames of an AC-4 track on several processes at once.

A decoder that starts on an I-frame needs little from before it: what the last
frames leave in its transforms, filter banks, smoothers and held-back tool data
dies away within a few frames, so the frames are cut into stretches that each
begin some frames past an I-frame, and a worker decodes a stretch from that
I-frame on and keeps only the stretch's own frames.  The one thing that does
not die away is where A-SPX stands in its noise and sine tables, which runs on
from the start of the stream; it follows from how many slots each earlier
interval filled, so every stretch is first only parsed, and the positions are
added up in order before the stretches after it are decoded.
"""

from __future__ import annotations

import multiprocessing
import os
from collections import deque
from collections.abc import Iterator
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
_WORKERS = max(1, min((os.cpu_count() or 1) - 1, 8))


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


def decode_frames(frames: list[bytes], token: CancellationToken) -> Iterator[Decoded]:
    """Every frame of `frames` decoded, in order, as one FrameDecoder would decode them."""
    plan = _plan(frames)
    if len(plan) == 1:
        yield from _decode(frames, 0, 0, None)
        return
    # spawn: the caller may be a thread of a Qt application, which must not fork
    pool = multiprocessing.get_context("spawn").Pool(
        min(_WORKERS, len(plan)), init_worker, (get_language(),)
    )
    try:
        parsed = [
            pool.apply_async(_intervals, (frames[warm:stop], start - warm))
            for warm, start, stop in plan
        ]
        ctrl_delay = ALIGNMENT.get(read_toc(frames[0]).frame_length, (0, 0))[1]
        cursors: dict[str, aspx.Cursor] = {}
        counted = 0  # frames whose intervals `cursors` went through
        pending: deque = deque()
        queued = 0
        while queued < len(plan) or pending:
            while queued < len(plan) and len(pending) < _AHEAD * _WORKERS:
                warm, start, stop = plan[queued]
                resume = None
                if queued:
                    # the cursors as decoding from frame 0 leaves them before frame `warm`,
                    # the first frame 0 going through d_ctrl + 1 times (frame.py)
                    while counted < warm:
                        index = next(i for i, (_, s, e) in enumerate(plan) if s <= counted < e)
                        frame = _wait(parsed[index], token)[counted - plan[index][1]]
                        for _ in range(ctrl_delay + 1 if counted == 0 else 1):
                            for name, (width, slots) in frame.items():
                                cursors.setdefault(name, aspx.Cursor()).advance(width, slots)
                        counted += 1
                    resume = {name: replace(c) for name, c in cursors.items()}
                pending.append(
                    pool.apply_async(_stretch, (frames[warm:stop], start - warm, warm, resume))
                )
                queued += 1
            yield from _wait(pending.popleft(), token)
        pool.close()
    finally:
        pool.terminate()
        pool.join()


def _wait(result, token: CancellationToken):
    while not result.ready():
        token.raise_if_cancelled()
        result.wait(0.1)
    return result.get()


def _plan(frames: list[bytes]) -> list[tuple[int, int, int]]:
    """(first frame decoded, first frame kept, stop) per stretch."""
    if len(frames) < 4 * _STRETCH:
        return [(0, 0, len(frames))]
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
    return [(max(0, s - warmup), s, e) for s, e in zip(starts, stops, strict=True)]


def _iframe(frame: bytes) -> bool:
    try:
        toc = read_toc(frame)
        group = toc.groups[toc.presentations[0].groups[0]]
    except (BitstreamError, IndexError):
        return False
    return toc.iframe_global or all(info.iframe for info in group.substreams)


def _intervals(frames: list[bytes], skip: int) -> list[dict[str, tuple[int, int]]]:
    """Per frame from `skip` on, its A-SPX intervals as aspx.interval gives them, parsed only.

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
            out.append({n: aspx.interval(tools.channel(n), f) for n, f in element.aspx.items()})
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
