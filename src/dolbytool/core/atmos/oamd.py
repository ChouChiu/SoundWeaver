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
TRIM_ELEMENT = 2
# global_trim_mode (table 33)
_DEFAULT_TRIM, _DISABLE_TRIM, _CUSTOM_TRIM = 0, 1, 2
# trim_centre (table 35), and trim_surround and trim_height (tables 36, 37), in dB
_CENTRE_TRIMS = (6, 3, 1.5, 0.75, -0.75, -1.5, -3, -4.5, -6, -7.5, -9, -10.5, -12, -13.5, -16, -36)
_TRIMS = (None, None, None, None, *_CENTRE_TRIMS[4:])  # 0 to 3 reserved
NUM_TRIM_CONFIGS = 9
# Which trim configuration a layout without heights takes.  The standard
# leaves the order of the nine to the renderer; rendering the objects of four
# masters to 5.1 against their 5.1 core matches only with 1 as 5.1, whose
# flags differ between them; 0 and 2 follow in the usual 2.0, 5.1, 7.1 order.
TRIM_CONFIGS = {"2.0": 0, "5.1": 1, "7.1": 2}


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

    def warped(self, trim: Trim) -> Render:
        """The position the renderer takes: warp_mode 1 doubles y (table 32)."""
        return replace(self, y_bits=min(62.0, 2 * self.y_bits)) if trim.warp else self


@dataclass(frozen=True, slots=True)
class Levels:
    """The trims of one trim configuration, in dB; the defaults are Dolby's automatic trim."""

    centre: float = 0.0
    surround: float = -3.0
    height: float = -3.0


DEFAULT_TRIM = Levels()


@dataclass(frozen=True, slots=True)
class Trim:
    """The trim_element (clause 5.6.5): how to render to a layout with fewer speakers.

    The balance controls are read past but not applied.
    """

    warp: bool = False  # warp_mode 0b01
    # per trim configuration, None where trimming is disabled
    configs: tuple[Levels | None, ...] = (DEFAULT_TRIM,) * NUM_TRIM_CONFIGS
    # OAMD indices of the objects trimming leaves alone
    exempt: frozenset[int] = frozenset()

    def levels(self, layout: str, obj: int) -> Levels | None:
        """The trims of object `obj` rendered to `layout`, None if it is not trimmed."""
        config = TRIM_CONFIGS.get(layout)
        return None if config is None or obj in self.exempt else self.configs[config]


def _trim_element(bits: BitReader, objects: int) -> Trim:
    warp = bits.read(2) == 0b01
    bits.skip(2)  # reserved
    mode = bits.read(2)
    if mode == _CUSTOM_TRIM:
        configs = tuple(_trim_config(bits) for _ in range(NUM_TRIM_CONFIGS))
    elif mode in (_DEFAULT_TRIM, _DISABLE_TRIM):
        configs = (None if mode == _DISABLE_TRIM else DEFAULT_TRIM,) * NUM_TRIM_CONFIGS
    else:
        raise BitstreamError("reserved global_trim_mode")
    exempt = frozenset()
    if bits.flag():  # b_disable_trim_per_obj
        exempt = frozenset(obj for obj in range(objects) if bits.flag())
    return Trim(warp, configs, exempt)


def _trim_config(bits: BitReader) -> Levels | None:
    if bits.flag():  # b_default_trim
        return DEFAULT_TRIM
    if bits.flag():  # b_disable_trim
        return None
    present = bits.read(5)  # trim_balance_presence[4..0]
    levels: dict[str, float] = {}
    for flag, name, table in (
        (0b10000, "centre", _CENTRE_TRIMS),
        (0b01000, "surround", _TRIMS),
        (0b00100, "height", _TRIMS),
    ):
        if present & flag and (db := table[bits.read(4)]) is not None:
            levels[name] = db
    for flag in (0b00010, 0b00001):  # the balances: a direction bit and an amount
        if present & flag:
            bits.skip(5)
    return replace(DEFAULT_TRIM, **levels)


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
    trim: Trim = Trim()


@dataclass(slots=True)
class ObjectState:
    gain_db: float = -math.inf
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
        isf_idx = bits.read(3)
        if isf_idx >= len(ISF_OBJECTS):
            raise BitstreamError(f"reserved intermediate_spatial_format_idx {isf_idx}")
        isf = ISF_OBJECTS[isf_idx]
    if content & 0b0010:
        dynamic = bits.read(5)
        if dynamic == 0x1F:
            dynamic += bits.read(7)
        dynamic += 1
    if content & 0b0001:
        size = bits.read(4) + 1
        bits.skip(size * 8)  # reserved_data and its padding, in bytes
    return Program(tuple(labels), isf, dynamic)


OamdState = tuple[list[ObjectState], Trim]


class OamdDecoder:
    """Carries the state that differential and reused values refer back to.

    The trim settings hold until a trim_element changes them.
    """

    def __init__(self, state: OamdState | None = None) -> None:
        self.state: list[ObjectState] = state[0] if state is not None else []
        self.trim = state[1] if state is not None else Trim()

    def snapshot(self) -> OamdState:
        return [replace(s) for s in self.state], self.trim

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
        if program.anchored > count:
            raise BitstreamError(f"{program.anchored} bed and ISF objects in a program of {count}")
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
            elif kind == TRIM_ELEMENT:
                self.trim = _trim_element(bits, count)
            if bits.pos > end:
                raise BitstreamError("oa_element overran its size")
            bits.pos = end
        frame.trim = self.trim
        return frame

    def _object_element(self, bits: BitReader, frame: Frame) -> None:
        code = bits.read(2)
        if code == 0:
            offset = 0
        elif code == 1:
            offset = SAMPLE_OFFSETS[bits.read(2)]
        elif code == 2:
            offset = bits.read(5)
        else:
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
            state.gain_db = -math.inf
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
            if flags & 0b01 and not bits.flag():  # object_priority: the renderer has no use for it
                bits.skip(5)
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
