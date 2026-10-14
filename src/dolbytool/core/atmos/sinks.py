"""What the decoder hands its outputs for every rendered syncframe, and the speaker feeds."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .oamd import Frame


@dataclass(frozen=True, slots=True)
class RenderedFrame:
    index: int  # syncframe number in the elementary stream
    metadata: Frame
    gains: np.ndarray  # (objects, samples, speakers), JOC order
    envelope: np.ndarray  # (objects, samples): each object's own linear gain
    essence: np.ndarray  # (objects, samples): rebuilt object audio before any gain
    lfe: np.ndarray  # (samples,): the core's LFE, which bypasses JOC
    # the part of this frame inside the container's timeline, as offsets into it
    span: slice


def container_span(index: int, samples: int, first: int, last: int) -> slice:
    """Which samples of syncframe `index` lie in the container's [first, last)."""
    a = index * samples
    lo, hi = max(a, first), min(a + samples, last)
    return slice(lo - a, max(lo, hi) - a)


def speaker_feeds(f: RenderedFrame, lfe: int | None = None) -> np.ndarray:
    """The render of the frame's span, (speakers, samples) as float32; `lfe` is the LFE's channel."""
    span = f.span
    if span.stop - span.start < f.essence.shape[1]:
        gains = f.gains[:, span, :]
        essence = f.essence[:, span]
        speakers = np.einsum("ots,ot->ts", gains, essence)
        if lfe is not None and 0 <= lfe < speakers.shape[1]:
            speakers[:, lfe] = f.lfe[span]
        return speakers.T.astype(np.float32)

    speakers = np.einsum("ots,ot->ts", f.gains, f.essence)
    if lfe is not None and 0 <= lfe < speakers.shape[1]:
        speakers[:, lfe] = f.lfe
    return speakers[span].T.astype(np.float32)
