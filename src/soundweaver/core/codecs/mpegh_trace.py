"""Where a 360RA track's objects are, how loud they are, and how a layout shares them.

The decode hands back, frame by frame, the object signals and the metadata they
are panned with; walking them once gives the same `ObjectTrace` the Atmos tracer
builds, so the sound field video, the monitor's object meters and anything else
that reads a trace work on 360RA without a second decode.

Positions are the room coordinates the trace and the renderer share (0..1, front
0, ear level 0); the shares of an object's energy come from the same placement
the speaker render uses.  A trace is cached per track and layout the way the
Atmos one is, so a video and a monitor view of the same file cost one decode.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from pathlib import Path

import numpy as np

from soundweaver.core.atmos.adm_reader import adm_to_room
from soundweaver.core.atmos.render import room_gains
from soundweaver.core.atmos.trace import (
    SILENCE_DB,
    Key,
    ObjectPath,
    ObjectTrace,
    load_cached_trace,
    store_cached_trace,
)
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.mpegh import MpeghError, decoded_blocks, decoder_input, probe_mpegh
from soundweaver.core.codecs.mpegh_adm import position
from soundweaver.core.paths import cache_dir, content_key
from soundweaver.core.speakers import DEFAULT_ATMOS, SpeakerLayout

logger = logging.getLogger(__name__)

_TRACE_SUFFIX = ".mpegh-trace.pickle"
# bumped when the trace's own content changes, so older ones go unread
_TRACE_VERSION = 1
# the decoder renders to this layout on the way; the trace does not depend on it
_TRACE_CICP = 2
# the frame length the worker reports, which sets the step between level columns
_FRAME = 1024
# keys repeat the same position every frame; this is where they collapse
_PLACES = 4


def _trace_path(source: Path, layout: SpeakerLayout) -> Path:
    return cache_dir() / f"{content_key(source)}.{layout.name}.mpegh{_TRACE_VERSION}{_TRACE_SUFFIX}"


def _cached_trace(source: Path, layout: SpeakerLayout) -> ObjectTrace | None:
    return load_cached_trace(_trace_path(source, layout), layout.channels, component="mpegh")


def _store_trace(source: Path, layout: SpeakerLayout, trace: ObjectTrace) -> None:
    store_cached_trace(trace, _trace_path(source, layout), suffix=_TRACE_SUFFIX)


def trace_mpegh(
    source: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None] = lambda _f: None,
    layout: SpeakerLayout = DEFAULT_ATMOS,
) -> ObjectTrace:
    """Where each of `source`'s objects is over time, its level, and how `layout` shares it."""
    cached = _cached_trace(source, layout)
    if cached is not None:
        on_fraction(1.0)
        return cached
    trace = _collect(source, token, on_fraction, layout)
    _store_trace(source, layout, trace)
    return trace


def _collect(
    source: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    layout: SpeakerLayout,
) -> ObjectTrace:
    """One streaming pass over the objects, measuring what the trace needs."""
    token.raise_if_cancelled()
    exe, mp4_source = decoder_input(source, None)
    if Path(exe).stem != "soundweaver_mpegh":
        raise MpeghError(
            "Tracing 360RA objects needs the bundled decoder: run `python scripts/build_native.py`."
        )
    try:
        duration = probe_mpegh(source, cicp=_TRACE_CICP, binary=exe, measure_duration=False).duration
    except MpeghError:
        duration = 0.0

    blocks = decoded_blocks(exe, source, _TRACE_CICP, mp4_source, None, token, objects=True)
    rate, _channels = next(blocks)
    keys: dict[int, list[Key]] = {}
    levels: list[np.ndarray] = []
    shares: list[np.ndarray] = []
    objects = 0
    samples = 0
    try:
        for frame in blocks:
            token.raise_if_cancelled()
            span = len(frame.samples)
            if frame.objects and frame.signals is not None:
                objects = max(objects, len(frame.objects))
                row = np.full(max(objects, len(frame.objects)), SILENCE_DB, np.float32)
                share = np.zeros((len(row), len(layout.channels)), np.float32)
                here = samples / rate
                for meta in frame.objects:
                    signal = frame.signals[meta.index, :span]
                    peak = float(np.max(np.abs(signal))) * meta.gain if signal.size else 0.0
                    row[meta.index] = 20 * math.log10(peak) if peak > 0 else SILENCE_DB
                    place = adm_to_room(*position(meta.azimuth, meta.elevation))
                    share[meta.index] = room_gains(layout, *place) ** 2
                    path = keys.setdefault(meta.index, [])
                    key = (here, 0.0, *place, meta.audible)
                    if not path or _places(path[-1]) != _places(key):
                        path.append(key)
                levels.append(row)
                shares.append(share)
            samples += span
            if duration > 0:
                on_fraction(min(1.0, samples / rate / duration))
    finally:
        blocks.close()

    if not levels:
        raise MpeghError("The 360RA stream carries no objects to trace.")
    stacked_levels = np.stack(levels)
    stacked_shares = np.stack(shares)
    if stacked_levels.shape[1] < objects:
        pad = np.full((len(levels), objects - stacked_levels.shape[1]), SILENCE_DB, np.float32)
        stacked_levels = np.concatenate((stacked_levels, pad), axis=1)
        stacked_shares = np.concatenate(
            (stacked_shares, np.zeros((len(levels), objects - stacked_shares.shape[1],
                                       len(layout.channels)), np.float32)), axis=1,
        )  # fmt: skip
    paths = tuple(
        ObjectPath(
            index,
            keys.get(index, [(0.0, 0.0, 0.5, 0.5, 0.0, True)]),
            stacked_levels[:, index],
            stacked_shares[:, index],
        )
        for index in range(objects)
    )
    on_fraction(1.0)
    return ObjectTrace(
        sample_rate=rate,
        level_start=0.0,
        level_step=_FRAME / rate,
        duration=samples / rate,
        objects=paths,
        speakers=layout.channels,
    )


def _places(key: Key) -> tuple:
    """The part of a key a repeat would duplicate: where the object is and whether it sounds."""
    return tuple(round(value, _PLACES) for value in key[2:])


__all__ = ["trace_mpegh"]
