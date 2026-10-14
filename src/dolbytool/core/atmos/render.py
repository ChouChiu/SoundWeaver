"""Object essences + OAMD -> speaker feeds, in any of the layouts of `core.speakers`.

A room panner: every speaker sits in a row of the unit room (TS 103 420
room-anchored coordinates), and an object is panned with constant power along
x within a row, along y between the two rows around it, and along z between
the floor and the ceiling.  This is the allocentric panner of ITU-R BS.2127
clause 7.3.6 for a point source.  A layout with a single row of heights takes
every elevated object on that row; one without heights folds them to the
floor.

To a layout without heights the stream's trims apply (`_trim`), as they do
when Dolby's encoder renders the objects to the 5.1 core.
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from dolbytool.core.speakers import BED_POSITIONS, SIDE_POSITIONS, Row, SpeakerLayout

from .oamd import Frame, Levels, Render

_SPREAD = math.sqrt(0.5)


def _pair(t: float) -> tuple[float, float]:
    """Constant-power crossfade: t = 0 all on the first, t = 1 all on the second."""
    t = min(1.0, max(0.0, t)) * math.pi / 2
    return math.cos(t), math.sin(t)


def _row(x: float, names: tuple[str, ...]) -> dict[str, float]:
    """Pan along x over speakers spaced evenly across the room."""
    if len(names) == 1:
        return {names[0]: 1.0}
    x = min(1.0, max(0.0, x))
    span = len(names) - 1
    i = min(span - 1, int(x * span))
    a, b = _pair(x * span - i)
    return {names[i]: a, names[i + 1]: b}


def _rows(y: float, rows: tuple[Row, ...]) -> list[tuple[float, tuple[str, ...]]]:
    """Pan along y between the two rows around `y`; one row takes everything."""
    if len(rows) == 1:
        return [(1.0, rows[0][1])]
    y = min(rows[-1][0], max(rows[0][0], y))
    i = next(i for i in range(len(rows) - 1) if y <= rows[i + 1][0])
    (near, a_names), (far, b_names) = rows[i], rows[i + 1]
    a, b = _pair((y - near) / (far - near))
    return [(a, a_names), (b, b_names)]


def _place(layout: SpeakerLayout, x: float, y: float, z: float) -> np.ndarray:
    g = np.zeros(len(layout.channels))
    floor, top = _pair(z) if layout.top else (1.0, 0.0)
    for layer, rows in ((floor, layout.floor), (top, layout.top)):
        if not layer:
            continue
        for weight, names in _rows(y, rows):
            for name, w in _row(x, names).items():
                g[layout.index[name]] += layer * weight * w
    return g


def _trim(layout: SpeakerLayout, g: np.ndarray, z: float, trim: Levels | None) -> np.ndarray:
    """The gains `g` of a source at height `z` in a layout without heights, trimmed.

    The surround trim lowers the speakers behind the front row and the centre
    trim the centre; the height trim lowers what folds down from the ceiling
    onto the others, in proportion to its height.  The two do not stack: what
    folds onto a surround takes the surround trim alone.  Rendering the objects
    of four masters to 5.1 against their 5.1 core bears this out, with Dolby's
    automatic -3 dB for both where the stream leaves the trim on (the surrounds
    then match to 23 dB instead of 7) and nothing where it turns it off.
    """
    if trim is None:
        return g
    factor = np.full(len(g), 10 ** (trim.height * min(1.0, max(0.0, z)) / 20))
    factor[list(layout.surrounds)] = 10 ** (trim.surround / 20)
    if layout.has("C"):
        factor[layout.index["C"]] *= 10 ** (trim.centre / 20)
    return g * factor


@lru_cache(maxsize=64)
def _zone_mask(layout: SpeakerLayout, zone: int) -> np.ndarray:
    """Allowed speaker mask for zone_constraints_idx (ETSI TS 103 420 Table 20 & Table A.7)."""
    mask = np.ones(len(layout.channels), dtype=bool)
    if zone == 0:
        mask.flags.writeable = False
        return mask
    backs = layout.has("Lb")
    screen = {"L", "R", "C"}
    side = ({"Ls", "Rs"} if backs else set()) | {"Lw", "Rw"}
    back = {"Lb", "Rb"} | ({"Ls", "Rs"} if not backs else set())
    surround = {"Ls", "Rs", "Lb", "Rb"}
    lfe_set = {"LFE"} if layout.has("LFE") else set()

    if zone == 1:  # Back excluded
        for name in back:
            if name in layout.index:
                mask[layout.index[name]] = False
    elif zone == 2:  # Side excluded
        for name in side:
            if name in layout.index:
                mask[layout.index[name]] = False
    elif zone == 3:  # Centre-and-back included
        allowed = {"C"} | back | set(layout.heights) | lfe_set
        for name in layout.channels:
            if name not in allowed:
                mask[layout.index[name]] = False
    elif zone == 4:  # Screen only
        allowed = screen | set(layout.heights) | lfe_set
        for name in layout.channels:
            if name not in allowed:
                mask[layout.index[name]] = False
    elif zone == 5:  # Surround only
        allowed = surround | set(layout.heights) | lfe_set
        for name in layout.channels:
            if name not in allowed:
                mask[layout.index[name]] = False
    mask.flags.writeable = False
    return mask


def _place_spread(
    layout: SpeakerLayout, x: float, y: float, z: float, size: tuple[float, float, float]
) -> np.ndarray:
    w, d, h = size
    if not (w > 0 or d > 0 or h > 0):
        return _place(layout, x, y, z)
    # ITU-R BS.2127 clause 7.3.7: distribute energy across the extent of the object
    hx, hy, hz = w * 0.5, d * 0.5, h * 0.5
    offsets = []
    if hx > 0:
        offsets.extend([(max(0.0, x - hx), y, z), (min(1.0, x + hx), y, z)])
    if hy > 0:
        offsets.extend([(x, max(0.0, y - hy), z), (x, min(1.0, y + hy), z)])
    if hz > 0:
        offsets.extend([(x, y, max(0.0, z - hz)), (x, y, min(1.0, z + hz))])
    g_center = _place(layout, x, y, z)
    if not offsets:
        return g_center
    spread_factor = min(1.0, max(w, d, h))
    w_center = 1.0 - 0.5 * spread_factor
    w_periph = (0.5 * spread_factor) / len(offsets)
    energy = w_center * (g_center**2)
    for px, py, pz in offsets:
        energy += w_periph * (_place(layout, px, py, pz) ** 2)
    total = energy.sum()
    return np.sqrt(energy / total) if total > 0 else g_center


@lru_cache(maxsize=4096)
def object_gains(layout: SpeakerLayout, render: Render, trim: Levels | None = None) -> np.ndarray:
    x, y, z = render.position
    if not render.elevation:
        z = 0.0
    mask = _zone_mask(layout, render.zone)
    if render.snap:
        positions = layout.positions
        candidates = [s for s in positions if mask[layout.index[s]]] or list(positions.keys())
        nearest = min(
            candidates, key=lambda s: sum((p - q) ** 2 for p, q in zip(positions[s], (x, y, z)))
        )
        g = np.zeros(len(layout.channels))
        g[layout.index[nearest]] = 1.0
    else:
        g = _place_spread(layout, x, y, z, render.size)
        if render.zone != 0:
            g = g * mask
            power = np.sum(g**2)
            if power > 0:
                g = g / np.sqrt(power)
    return _trim(layout, g, z, trim)


@lru_cache(maxsize=256)
def bed_gains(
    layout: SpeakerLayout, label: str, backs: bool, trim: Levels | None = None
) -> np.ndarray:
    """A bed channel on its own speaker, or where it stands when the layout lacks one.

    `backs`: the bed has Lb and Rb, so its Ls and Rs are the side pair.  A 5.X
    bed's surrounds in a layout with backs are spread over the side and the back.
    """
    g = np.zeros(len(layout.channels))
    z = 0.0
    if label in ("LFE", "LFE2"):
        if layout.has("LFE"):
            g[layout.index["LFE"]] = 1.0
        g.flags.writeable = False
        return g
    if label in ("Ls", "Rs") and not backs and layout.has("Lb"):
        g[layout.index[label]] = g[layout.index[label[0] + "b"]] = _SPREAD
    elif label in layout.index:
        g[layout.index[label]] = 1.0
    else:
        x, y, z = (SIDE_POSITIONS if backs else {}).get(label, BED_POSITIONS[label])
        g = _place(layout, x, y, z)
    trimmed = _trim(layout, g, z, trim)
    trimmed.flags.writeable = False
    return trimmed


@lru_cache(maxsize=8192)
def _target(
    layout: SpeakerLayout, source: Render | str, gain_db: float, backs: bool, trim: Levels | None
) -> np.ndarray:
    """Speaker gains and the object's own gain for one update; shared, so read-only.

    One more column than there are speakers: the object's own linear gain, which
    glides over the same ramps, for outputs that keep objects apart.
    """
    gain = 10 ** (gain_db / 20)
    g = np.empty(len(layout.channels) + 1)
    g[:-1] = (
        bed_gains(layout, source, backs, trim)
        if isinstance(source, str)
        else object_gains(layout, source, trim)
    ) * gain
    g[-1] = gain
    g.flags.writeable = False
    return g


RendererState = tuple[np.ndarray, np.ndarray, np.ndarray]


class Renderer:
    """Turns each frame's updates into per-sample gains, gliding over each ramp."""

    def __init__(self, objects: int, layout: SpeakerLayout):
        self.layout = layout
        width = len(layout.channels) + 1
        self.current = np.zeros((objects, width))
        self.target = np.zeros((objects, width))
        self.silent = np.zeros(width)
        self.silent.flags.writeable = False
        self.remaining = np.zeros(objects, dtype=np.int64)
        self._steps = np.arange(0)

    def state(self) -> RendererState:
        return self.current.copy(), self.target.copy(), self.remaining.copy()

    @classmethod
    def restore(cls, state: RendererState, layout: SpeakerLayout) -> Renderer:
        renderer = cls(state[0].shape[0], layout)
        renderer.current[:], renderer.target[:], renderer.remaining[:] = state
        return renderer

    def gains(self, frame: Frame, samples: int) -> np.ndarray:
        """Per-sample gains (objects, samples, speakers + 1) for the objects JOC rebuilds.

        The last column is each object's own gain, the rest its speaker gains.
        """
        out = np.empty((self.current.shape[0], samples, self.current.shape[1]))
        self._frame(frame, samples, out)
        return out

    def advance(self, frame: Frame, samples: int) -> None:
        """Move the glides on as `gains` would, without producing any samples."""
        self._frame(frame, samples, None)

    def _frame(self, frame: Frame, samples: int, out: np.ndarray | None) -> None:
        labels = frame.program.bed_labels
        backs = "Lb" in labels
        for o, meta_index in enumerate(joc_objects(frame)):
            source = labels[meta_index] if meta_index < len(labels) else None
            trim = None if self.layout.top else frame.trim.levels(self.layout.name, meta_index)
            events = []
            for u in frame.updates[meta_index]:
                if u.active and u.gain_db != -math.inf:
                    render = u.render.warped(frame.trim) if source is None else source
                    g = _target(self.layout, render, u.gain_db, backs, trim)
                else:
                    g = self.silent
                events.append((u.start, u.ramp, g))
            self._envelope(o, events, samples, None if out is None else out[o])

    def _envelope(self, o: int, events, samples: int, env: np.ndarray | None) -> None:
        current, target = self.current[o], self.target[o]
        settled = self.remaining[o] == 0 or (current is target or np.array_equal(current, target))
        if settled and all(g is current or np.array_equal(g, current) for _, _, g in events):
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
    """OAMD indices of the objects JOC carries: all but the LFE channels, which bypass it."""
    labels = frame.program.bed_labels
    return [i for i in range(frame.object_count) if i >= len(labels) or not labels[i].startswith("LFE")]
