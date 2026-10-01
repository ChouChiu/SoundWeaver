"""Object essences + OAMD -> 5.1.4 speaker feeds.

A room-cube panner: every speaker sits at a corner or an edge of the unit
room (TS 103 420 room-anchored coordinates), and an object is panned with
constant power along x within a row, along y between the front and back rows,
and along z between the floor and the ceiling.  In a 5.X layout the surrounds
take the back of the room (TS 103 420 table A.7, note 2).
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from .oamd import Frame, Render

# Output order: FL FR FC LFE SL SR TFL TFR TBL TBR (WAVE_FORMAT_EXTENSIBLE order)
SPEAKERS = ("L", "R", "C", "LFE", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr")
CH = {name: i for i, name in enumerate(SPEAKERS)}
POSITIONS = {
    "L": (0.0, 0.0, 0.0), "R": (1.0, 0.0, 0.0), "C": (0.5, 0.0, 0.0),
    "Ls": (0.0, 1.0, 0.0), "Rs": (1.0, 1.0, 0.0),
    "Tfl": (0.0, 0.0, 1.0), "Tfr": (1.0, 0.0, 1.0), "Tbl": (0.0, 1.0, 1.0), "Tbr": (1.0, 1.0, 1.0),
}  # fmt: skip
# Bed labels this layout has no speaker for, spread over the ones it has.
BED_FALLBACK = {
    "Lb": {"Ls": 1.0}, "Rb": {"Rs": 1.0},
    "Tsl": {"Tfl": math.sqrt(0.5), "Tbl": math.sqrt(0.5)},
    "Tsr": {"Tfr": math.sqrt(0.5), "Tbr": math.sqrt(0.5)},
    "Lw": {"L": math.sqrt(0.5), "Ls": math.sqrt(0.5)},
    "Rw": {"R": math.sqrt(0.5), "Rs": math.sqrt(0.5)},
    "LFE2": {"LFE": 1.0},
}  # fmt: skip


def _pair(t: float) -> tuple[float, float]:
    """Constant-power crossfade: t = 0 all on the first, t = 1 all on the second."""
    t = min(1.0, max(0.0, t)) * math.pi / 2
    return math.cos(t), math.sin(t)


def _row(x: float, names: tuple[str, ...]) -> dict[str, float]:
    """Pan along x over speakers spaced evenly across the room."""
    x = min(1.0, max(0.0, x))
    span = len(names) - 1
    i = min(span - 1, int(x * span))
    a, b = _pair(x * span - i)
    return {names[i]: a, names[i + 1]: b}


@lru_cache(maxsize=4096)
def object_gains(render: Render) -> np.ndarray:
    x, y, z = render.position
    g = np.zeros(len(SPEAKERS))
    if render.snap:
        nearest = min(
            POSITIONS, key=lambda s: sum((p - q) ** 2 for p, q in zip(POSITIONS[s], (x, y, z)))
        )
        g[CH[nearest]] = 1.0
        return g
    if not render.elevation:
        z = 0.0
    floor, top = _pair(z)
    front, back = _pair(y)
    for layer, rows in (
        (floor, ((front, ("L", "C", "R")), (back, ("Ls", "Rs")))),
        (top, ((front, ("Tfl", "Tfr")), (back, ("Tbl", "Tbr")))),
    ):
        for weight, names in rows:
            for name, w in _row(x, names).items():
                g[CH[name]] += layer * weight * w
    return g


def bed_gains(label: str) -> np.ndarray:
    g = np.zeros(len(SPEAKERS))
    for name, w in ({label: 1.0} if label in CH else BED_FALLBACK[label]).items():
        g[CH[name]] = w
    return g


# One more column than there are speakers: the object's own linear gain, which
# glides over the same ramps, for outputs that keep objects apart.
_WIDTH = len(SPEAKERS) + 1
_SILENT = np.zeros(_WIDTH)
_SILENT.flags.writeable = False


@lru_cache(maxsize=8192)
def _target(source: Render | str, gain_db: float) -> np.ndarray:
    """Speaker gains and the object's own gain for one update; shared, so read-only."""
    gain = 10 ** (gain_db / 20)
    g = np.empty(_WIDTH)
    g[:-1] = (bed_gains(source) if isinstance(source, str) else object_gains(source)) * gain
    g[-1] = gain
    g.flags.writeable = False
    return g


RendererState = tuple[np.ndarray, np.ndarray, np.ndarray]


class Renderer:
    """Turns each frame's updates into per-sample gains, gliding over each ramp."""

    def __init__(self, objects: int):
        self.current = np.zeros((objects, _WIDTH))
        self.target = np.zeros((objects, _WIDTH))
        self.remaining = np.zeros(objects, dtype=np.int64)
        self._steps = np.arange(0)

    def state(self) -> RendererState:
        return self.current.copy(), self.target.copy(), self.remaining.copy()

    @classmethod
    def restore(cls, state: RendererState) -> Renderer:
        renderer = cls(state[0].shape[0])
        renderer.current[:], renderer.target[:], renderer.remaining[:] = state
        return renderer

    def gains(self, frame: Frame, samples: int) -> np.ndarray:
        """Per-sample gains (objects, samples, speakers + 1) for the objects JOC rebuilds.

        The last column is each object's own gain, the rest its speaker gains.
        """
        out = np.empty((self.current.shape[0], samples, _WIDTH))
        self._frame(frame, samples, out)
        return out

    def advance(self, frame: Frame, samples: int) -> None:
        """Move the glides on as `gains` would, without producing any samples."""
        self._frame(frame, samples, None)

    def _frame(self, frame: Frame, samples: int, out: np.ndarray | None) -> None:
        labels = frame.program.bed_labels
        for o, meta_index in enumerate(joc_objects(frame)):
            source = labels[meta_index] if meta_index < len(labels) else None
            events = []
            for u in frame.updates[meta_index]:
                if u.active and u.gain_db != -math.inf:
                    g = _target(u.render if source is None else source, u.gain_db)
                else:
                    g = _SILENT
                events.append((u.start, u.ramp, g))
            self._envelope(o, events, samples, None if out is None else out[o])

    def _envelope(self, o: int, events, samples: int, env: np.ndarray | None) -> None:
        current, target = self.current[o], self.target[o]
        settled = self.remaining[o] == 0 or np.array_equal(current, target)
        if settled and all(np.array_equal(g, current) for _, _, g in events):
            # nothing moves this frame: every sample holds the current gains
            if env is not None:
                env[:] = current
            target[:] = current
            self.remaining[o] = 0
            return
        t = 0
        events = sorted(events, key=lambda e: e[0])
        for start, ramp, g in [*events, (samples, 0, None)]:
            start = min(start, samples)
            while t < start:
                remaining = int(self.remaining[o])
                if remaining > 0:
                    step = min(remaining, start - t)
                    if env is not None:
                        if self._steps.size <= step:
                            self._steps = np.arange(max(step + 1, 2 * self._steps.size))
                        frac = self._steps[1 : step + 1, None] / remaining
                        env[t : t + step] = current + frac * (target - current)
                        current[:] = env[t + step - 1]
                    else:
                        # the last row of the glide above, worked out alone
                        current[:] = current + (step / remaining) * (target - current)
                    self.remaining[o] = remaining - step
                    t += step
                else:
                    if env is not None:
                        env[t:start] = current
                    t = start
            if g is not None:
                if ramp > 0:
                    target[:] = g
                    self.remaining[o] = ramp
                else:
                    current[:] = g
                    target[:] = g
                    self.remaining[o] = 0


def joc_objects(frame: Frame) -> list[int]:
    """OAMD indices of the objects JOC carries: all but the LFE, which bypasses it."""
    labels = frame.program.bed_labels
    return [i for i in range(frame.object_count) if i >= len(labels) or labels[i] != "LFE"]
