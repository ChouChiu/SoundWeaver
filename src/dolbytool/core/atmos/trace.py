"""Object trajectories and levels, for drawing the sound field rather than rendering it.

Only dynamic objects are traced: beds sit on their speakers and ISF objects
carry no position.  Times are seconds on the container's timeline, the same
one the rendered 5.1.4 audio and every video are cut to.

The positions follow from the metadata alone and are collected in syncframe
order by `Tracer`; the levels need the rebuilt audio, so each decode worker
`measure`s its own frames and the tracer puts them in place.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass

import numpy as np

from .oamd import Frame, Program
from .render import CH, SPEAKERS, joc_objects
from .sinks import RenderedFrame

# Level reported for a frame with no signal at all.
SILENCE_DB = -120.0

Key = tuple[float, float, float, float, float, bool]
# per dynamic object of one frame: (OAMD index, peak dBFS, share of energy per speaker)
Measure = list[tuple[int, float, np.ndarray | None]]


@dataclass(slots=True)
class ObjectPath:
    index: int  # OAMD object index
    # (start s, ramp s, x, y, z, active): glide from the previous key over `ramp`
    keys: list[Key]
    # peak dBFS after the object's gain, one per syncframe
    levels: np.ndarray
    # share of the object's energy each speaker gets, one row per syncframe
    shares: np.ndarray


@dataclass(frozen=True, slots=True)
class ObjectTrace:
    sample_rate: int
    level_start: float  # container time of the first level
    level_step: float  # seconds per level
    duration: float
    program: Program
    objects: tuple[ObjectPath, ...]

    def to_json(self, speakers: Collection[str] | None = None) -> dict:
        """The scene for the renderer.

        With `speakers`, each object also gets `trackLevels`: its level counting
        only the energy that reaches those speakers.
        """
        columns = [CH[s] for s in speakers] if speakers is not None else None
        return {
            "sampleRate": self.sample_rate,
            "levelStart": self.level_start,
            "levelStep": self.level_step,
            "duration": self.duration,
            "objects": [
                {
                    "id": o.index,
                    "keys": [
                        [round(t, 6), round(r, 6), round(x, 4), round(y, 4), round(z, 4), int(a)]
                        for t, r, x, y, z, a in o.keys
                    ],
                    "levels": o.levels.tolist(),
                    **({} if columns is None else {"trackLevels": _within(o, columns)}),
                }
                for o in self.objects
            ],
        }


def _within(path: ObjectPath, columns: list[int]) -> list[float]:
    share = path.shares[:, columns].sum(axis=1)
    with np.errstate(divide="ignore"):
        db = path.levels + 10 * np.log10(share)
    return [round(float(v), 1) for v in np.maximum(SILENCE_DB, np.nan_to_num(db, nan=SILENCE_DB))]


def measure(f: RenderedFrame) -> Measure:
    """Peak level and speaker shares of every dynamic object in one rendered frame."""
    anchored = f.metadata.program.anchored
    rows, indices = [], []
    for o, index in enumerate(joc_objects(f.metadata)):
        if index >= anchored:
            rows.append(o)
            indices.append(index)
    if not rows:
        return []
    essence = f.essence[rows]
    peaks = np.max(np.abs(essence * f.envelope[rows]), axis=1)
    energy = np.einsum("ot,ots->os", np.square(essence), np.square(f.gains[rows]))
    totals = energy.sum(axis=1)
    out: Measure = []
    for index, peak, row, total in zip(indices, peaks, energy, totals, strict=True):
        level = 20 * math.log10(peak) if peak > 0 else SILENCE_DB
        share = (row / total).astype(np.float32) if total > 0 else None
        out.append((index, round(max(SILENCE_DB, level), 1), share))
    return out


class Tracer:
    """Collects every dynamic object's position keys, fed the metadata in syncframe order."""

    def __init__(self, sample_rate: int, first: int, length: int, samples: int) -> None:
        self.sample_rate, self.first, self.length, self.samples = sample_rate, first, length, samples
        self.keys: dict[int, list[Key]] = {}
        self.program: Program | None = None

    def metadata(self, k: int, meta: Frame) -> None:
        if self.program is None:
            self.program = meta.program
        anchored = meta.program.anchored
        for index in joc_objects(meta):
            if index < anchored:
                continue
            keys = self.keys.setdefault(index, [])
            for u in meta.updates[index]:
                x, y, z = u.render.position
                if not u.render.elevation:
                    z = 0.0
                active = u.active and u.gain_db != -math.inf
                t = (k * self.samples + u.start - self.first) / self.sample_rate
                key = (t, u.ramp / self.sample_rate, x, y, z, active)
                if not keys or keys[-1][2:] != key[2:]:
                    keys.append(key)

    def result(self, measured: Sequence[Measure]) -> ObjectTrace:
        """The trace, with `measured` holding one entry per syncframe."""
        if self.program is None:
            raise ValueError("no frame was traced")
        frames = len(measured)
        paths = {
            index: ObjectPath(
                index,
                keys,
                np.full(frames, SILENCE_DB),
                np.zeros((frames, len(SPEAKERS)), dtype=np.float32),
            )
            for index, keys in sorted(self.keys.items())
        }
        for k, entries in enumerate(measured):
            for index, level, share in entries:
                path = paths[index]
                path.levels[k] = level
                if share is not None:
                    path.shares[k] = share
        return ObjectTrace(
            sample_rate=self.sample_rate,
            level_start=-self.first / self.sample_rate,
            level_step=self.samples / self.sample_rate,
            duration=self.length / self.sample_rate,
            program=self.program,
            objects=tuple(paths.values()),
        )
