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


def speaker_feeds(f: RenderedFrame, lfe: int | None) -> np.ndarray:
    """The render of the frame's span, (speakers, samples) as float32.

    `lfe` is the layout's LFE channel, which takes the core's LFE as it is; a
    layout without one (2.0) leaves the LFE out, as a stereo downmix does.
    """
    span = f.span
    speakers = np.einsum("ots,ot->ts", f.gains[:, span], f.essence[:, span])
    if lfe is not None:
        speakers[:, lfe] = f.lfe[span]
    return speakers.T.astype(np.float32)
