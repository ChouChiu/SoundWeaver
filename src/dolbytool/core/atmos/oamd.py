"""Object audio metadata (TS 103 420 clause 5): who the objects are and where they go.

Flag arrays in the spec are indexed by bit weight: the first bit read is the
highest index (content_description[3], bed_channel_assignment[9], ...).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .bitstream import BitReader, BitstreamError

# Channel labels in the order the bed assignment flags are read (tables 12, 13).
BED_LABELS_NONSTD = (
    "L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb",
    "Tfl", "Tfr", "Tsl", "Tsr", "Tbl", "Tbr", "Lw", "Rw", "LFE2",
)  # fmt: skip
BED_GROUPS_STD = (
    ("L", "R"), ("C",), ("LFE",), ("Ls", "Rs"), ("Lb", "Rb"),
    ("Tfl", "Tfr"), ("Tsl", "Tsr"), ("Tbl", "Tbr"), ("Lw", "Rw"), ("LFE2",),
)  # fmt: skip
ISF_OBJECTS = (4, 8, 10, 14, 15, 30)
SAMPLE_OFFSETS = (8, 16, 18, 24)
RAMP_DURATIONS = (32, 64, 128, 256, 320, 480, 1000, 1001, 1024, 1600, 1601, 1602, 1920, 2000, 2002, 2048)  # fmt: skip
DISTANCE_FACTORS = (1.1, 1.3, 1.6, 2.0, 2.5, 3.2, 4.0, 5.0, 6.3, 7.9, 10.0, 12.6, 15.8, 20.0, 25.1, 50.1)  # fmt: skip

OBJECT_ELEMENT = 1


@dataclass(frozen=True, slots=True)
class Render:
    """Object properties that position it; the defaults are table 29's."""

    x_bits: float = 31.0  # pos3D_X = x_bits / 62, kept in bits for differential updates
    y_bits: float = 31.0
    z_bits: float = 0.0  # signed: pos3D_Z = z_bits / 15
    distance: float | None = None  # None inside the room, math.inf at infinity
    zone: int = 0
    elevation: bool = True
    size: tuple[float, float, float] = (0.0, 0.0, 0.0)
    screen: bool = False
    snap: bool = False

    @property
    def position(self) -> tuple[float, float, float]:
        return (min(1.0, self.x_bits / 62), min(1.0, self.y_bits / 62), self.z_bits / 15)


@dataclass(frozen=True, slots=True)
class Update:
    """One property update of one object (clause 5.3)."""

    start: int  # sample within the frame
    ramp: int  # samples to glide from the previous update
    active: bool
    gain_db: float
    render: Render


@dataclass(frozen=True, slots=True)
class Program:
    """Program assignment: which objects are beds, ISF or dynamic, in bitstream order."""

    bed_labels: tuple[str, ...]  # one speaker label per bed object
    isf_objects: int
    dynamic_objects: int

    @property
    def anchored(self) -> int:
        """Objects with no object_render_info: beds and ISF."""
        return len(self.bed_labels) + self.isf_objects


@dataclass(slots=True)
class Frame:
    object_count: int
    program: Program
    updates: list[list[Update]]  # per object, in time order


@dataclass(slots=True)
class ObjectState:
    gain_db: float = -math.inf
    priority: float = 0.0
    render: Render = field(default_factory=Render)


def _program(bits: BitReader) -> Program:
    labels: list[str] = []
    if bits.flag():  # b_dyn_object_only_program
        lfe = bits.flag()
        return Program(("LFE",) if lfe else (), 0, -1)
    content = bits.read(4)
    isf = dynamic = 0
    if content & 0b1000:  # bed objects
        bits.skip(1)  # b_bed_chan_distribute
        instances = bits.read(3) + 2 if bits.flag() else 1
        for _ in range(instances):
            if bits.flag():  # b_lfe_only
                labels.append("LFE")
            elif bits.flag():  # b_standard_chan_assign
                mask = bits.read(10)
                for index, group in enumerate(BED_GROUPS_STD):
                    if mask & (1 << (9 - index)):
                        labels.extend(group)
            else:
                mask = bits.read(17)
                labels.extend(
                    label
                    for index, label in enumerate(BED_LABELS_NONSTD)
                    if mask & (1 << (16 - index))
                )
    if content & 0b0100:
        isf = ISF_OBJECTS[bits.read(3)]
    if content & 0b0010:
        dynamic = bits.read(5)
        if dynamic == 0x1F:
            dynamic += bits.read(7)
        dynamic += 1
    if content & 0b0001:
        size = bits.read(4) + 1
        bits.skip(size * 8)  # reserved_data and its padding, in bytes
    return Program(tuple(labels), isf, dynamic)


class OamdDecoder:
    """Carries the state that differential and reused values refer back to."""

    def __init__(self, state: list[ObjectState] | None = None) -> None:
        self.state: list[ObjectState] = state if state is not None else []

    def snapshot(self) -> list[ObjectState]:
        return [replace(s) for s in self.state]

    def decode(self, payload: bytes) -> Frame:
        bits = BitReader(payload)
        version = bits.read(2)
        if version == 3:
            version += bits.read(3)
        count = bits.read(5)
        if count == 0x1F:
            count += bits.read(7)
        count += 1
        program = _program(bits)
        if program.dynamic_objects < 0:  # dynamic-only program: the rest are dynamic
            program = replace(program, dynamic_objects=count - len(program.bed_labels))
        while len(self.state) < count:
            self.state.append(ObjectState())
        alternate = bits.flag()
        elements = bits.read(4)
        if elements == 0xF:
            elements += bits.read(5)
        frame = Frame(count, program, [[] for _ in range(count)])
        for _ in range(elements):
            kind = bits.read(4)
            size = bits.variable_bits_max(4, 4) + 1
            end = bits.pos + size * 8
            if alternate:
                bits.skip(4)
            bits.skip(1)  # b_discard_unknown_element
            if kind == OBJECT_ELEMENT:
                self._object_element(bits, frame)
                if bits.pos > end:
                    raise BitstreamError("object_element overran its size")
            bits.pos = end
        return frame

    def _object_element(self, bits: BitReader, frame: Frame) -> None:
        code = bits.read(2)
        offset = (0, None, None, None)[code]
        if code == 1:
            offset = SAMPLE_OFFSETS[bits.read(2)]
        elif code == 2:
            offset = bits.read(5)
        elif code == 3:
            raise BitstreamError("reserved sample_offset_code")
        timing = []
        for _ in range(bits.read(3) + 1):
            start = offset + 32 * bits.read(6)
            ramp = (0, 512, 1536, None)[ramp_code := bits.read(2)]
            if ramp_code == 3:
                ramp = RAMP_DURATIONS[bits.read(4)] if bits.flag() else bits.read(11)
            timing.append((start, ramp))
        if not bits.flag():  # b_reserved_data_not_present
            bits.skip(5)
        for obj in range(frame.object_count):
            anchored = obj < frame.program.anchored
            for blk, (start, ramp) in enumerate(timing):
                frame.updates[obj].append(self._info_block(bits, obj, blk, anchored, start, ramp))

    def _info_block(
        self, bits: BitReader, obj: int, blk: int, anchored: bool, start: int, ramp: int
    ) -> Update:
        state = self.state[obj]
        inactive = bits.flag()
        basic = 0 if inactive else (1 if blk == 0 else bits.read(2))
        if basic == 0:
            state.gain_db, state.priority = -math.inf, 0.0
        elif basic in (1, 3):
            flags = 0b11 if basic == 1 else bits.read(2)
            if flags & 0b10:
                gain_idx = bits.read(2)
                if gain_idx == 0:
                    state.gain_db = 0.0
                elif gain_idx == 1:
                    state.gain_db = -math.inf
                elif gain_idx == 2:
                    g = bits.read(6)
                    state.gain_db = float(15 - g if g < 15 else 14 - g)
                else:  # same as the previous object in this update
                    state.gain_db = self.state[obj - 1].gain_db if obj else 0.0
            if flags & 0b01:
                state.priority = 1.0 if bits.flag() else bits.read(5) / 32
        render = 0 if inactive or anchored else (1 if blk == 0 else bits.read(2))
        if render == 0:
            state.render = Render()
        elif render in (1, 3):
            state.render = self._render_info(bits, state.render, blk, render)
        if bits.flag():  # b_additional_table_data_exists
            size = bits.read(4) + 1
            bits.skip(size * 8)
        return Update(start, ramp, not inactive, state.gain_db, state.render)

    @staticmethod
    def _render_info(bits: BitReader, prev: Render, blk: int, status: int) -> Render:
        flags = 0b1111 if status == 1 else bits.read(4)
        x, y, z = prev.x_bits, prev.y_bits, prev.z_bits
        distance, zone, elevation = prev.distance, prev.zone, prev.elevation
        size, screen = prev.size, prev.screen
        if flags & 0b1000:  # position
            if blk != 0 and bits.flag():  # b_differential_position_specified
                x = max(0.0, min(62.0, x + bits.signed(3)))
                y = max(0.0, min(62.0, y + bits.signed(3)))
                z = max(-15.0, min(15.0, z + bits.signed(3)))
            else:
                x, y = float(bits.read(6)), float(bits.read(6))
                sign = 1 if bits.read(1) else -1
                z = float(sign * bits.read(4))
            distance = None
            if bits.flag():  # b_object_distance_specified
                distance = math.inf if bits.flag() else DISTANCE_FACTORS[bits.read(4)]
        if flags & 0b0100:  # zone constraints
            zone, elevation = bits.read(3), bits.flag()
        if flags & 0b0010:  # size
            size_idx = bits.read(2)
            if size_idx == 0:
                size = (0.0, 0.0, 0.0)
            elif size_idx == 1:
                s = bits.read(5) / 31
                size = (s, s, s)
            elif size_idx == 2:
                size = (bits.read(5) / 31, bits.read(5) / 31, bits.read(5) / 31)
            else:
                raise BitstreamError("reserved object_size_idx")
        if flags & 0b0001:  # screen reference
            screen = bits.flag()
            if screen:
                bits.skip(5)  # screen_factor_bits, depth_factor_idx
        return Render(x, y, z, distance, zone, elevation, size, screen, bits.flag())
