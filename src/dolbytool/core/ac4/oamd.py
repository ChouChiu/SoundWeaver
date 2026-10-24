"""AC-4 object audio metadata (TS 103 190-2 clauses 6.2.8 and 6.3.9), in the renderer's terms.

AC-4 carries the same object properties as the OAMD of TS 103 420 in a syntax
of its own: the timing and the per-object updates come in separate elements,
and what kind each object is (bed, ISF, dynamic, LFE) is in the table of
contents.  Each frame's updates are turned into the `atmos.oamd.Frame` the
room renderer, the sound field trace and the ADM writer already take.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError
from dolbytool.core.atmos.oamd import (
    DEFAULT_TRIM,
    DISTANCE_FACTORS,
    ISF_RINGS,
    NUM_TRIM_CONFIGS,
    RAMP_DURATIONS,
    Frame,
    ObjectState,
    Program,
    Render,
    Trim,
    Update,
)

from .toc import BED, DYN, ISF

# ext_prec_pos3D_X, _Y and _Z (tables 123 to 125), in fifths of a coded step
_FINE_STEPS = (1, 2, -1, -2)
# trim_centre (table 113) and trim_surround and trim_height (tables 114, 115), in dB,
# as `atmos.oamd` holds them for TS 103 420, whose tables these are
_CENTRE_TRIMS = (6, 3, 1.5, 0.75, -0.75, -1.5, -3, -4.5, -6, -7.5, -9, -10.5, -12, -13.5, -15, -36)
_TRIMS = (None, None, None, None, *_CENTRE_TRIMS[4:])
_DEFAULT_TRIM, _DISABLE_TRIM, _CUSTOM_TRIM = 0, 1, 2


@dataclass(frozen=True, slots=True)
class Timing:
    """oamd_timing_data: where each block update starts in the frame, and its ramp."""

    blocks: tuple[tuple[int, int], ...]  # (update PCM sample, ramp_duration)


def read_timing(b: BitReader) -> Timing:
    if not b.flag():  # oa_sample_offset_type 0b0
        offset = 0
    elif not b.flag():  # 0b10: oa_sample_offset_code (table 93)
        offset = 16 if not b.flag() else (8 if not b.flag() else 24)
    else:
        offset = b.read(5)
    blocks = []
    for _ in range(b.read(3)):
        start = offset + 32 * b.read(6)  # clause 5.9.2
        code = b.read(2)
        if code < 3:
            ramp = (0, 512, 1536)[code]
        elif b.flag():  # b_use_ramp_table
            ramp = RAMP_DURATIONS[b.read(4)]
        else:
            ramp = b.read(11)
        blocks.append((start, ramp))
    return Timing(tuple(blocks))


def read_common(b: BitReader) -> Trim | None:
    """oamd_common_data; the trim it may carry, None without one."""
    if not b.flag():  # b_default_screen_size_ratio
        b.skip(5)
    b.skip(1)  # b_bed_object_chan_distribute
    if not b.flag():  # b_additional_data
        return None
    size = b.read(1) + 1
    if size == 2:
        size += b.variable_bits(2)
    end = b.pos + 8 * size
    trim = _trim(b)
    # bed_render_info and headphone follow; the renderer has no use for them
    if b.pos > end:
        raise BitstreamError("oamd_common_data overran its additional data")
    b.pos = end
    return trim


def _trim(b: BitReader) -> Trim | None:
    if not b.flag():  # b_trim_present
        return None
    warp = b.read(2) == 0b01
    b.skip(2)
    mode = b.read(2)
    if mode == _CUSTOM_TRIM:
        configs = tuple(_trim_config(b) for _ in range(NUM_TRIM_CONFIGS))
    elif mode in (_DEFAULT_TRIM, _DISABLE_TRIM):
        configs = (None if mode == _DISABLE_TRIM else DEFAULT_TRIM,) * NUM_TRIM_CONFIGS
    else:
        raise BitstreamError("reserved global_trim_mode")
    return Trim(warp, configs)


def _trim_config(b: BitReader):
    if b.flag():  # b_default_trim
        return DEFAULT_TRIM
    if b.flag():  # b_disable_trim
        return None
    # trim_balance_presence[4..0]: centre, surround, height, then the two balances
    present = b.read(5)
    levels = {}
    for flag, name, table in (
        (0b10000, "centre", _CENTRE_TRIMS),
        (0b01000, "surround", _TRIMS),
        (0b00100, "height", _TRIMS),
    ):
        if present & flag and (db := table[b.read(4)]) is not None:
            levels[name] = db
    for flag in (0b00010, 0b00001):  # a sign and an amount each
        if present & flag:
            b.skip(5)
    return replace(DEFAULT_TRIM, **levels)


@dataclass(slots=True)
class Objects:
    """The objects of one oamd_dyndata_single, and what their updates refer back to."""

    types: tuple[tuple[int, bool], ...]  # (obj_type, b_lfe) per object
    program: Program
    states: list[ObjectState] = field(default_factory=list)
    # the standard-precision coded position each update's differential coding builds on
    coded: list[tuple[float, float, float]] = field(default_factory=list)
    exempt: set[int] = field(default_factory=set)  # b_obj_trim_disable
    trim: Trim = field(default_factory=Trim)  # from the last oamd_common_data that carried one

    def __post_init__(self) -> None:
        self.states = [ObjectState() for _ in self.types]
        self.coded = [(31.0, 31.0, 0.0) for _ in self.types]


def program_for(
    types: list[tuple[int, bool]], beds: tuple[str, ...], isf: int | None, lfe: bool
) -> tuple[tuple[tuple[int, bool], ...], Program]:
    """The objects of a substream as `atmos.oamd` orders them: LFE, beds, ISF, dynamic.

    An A-JOC substream's b_lfe object is signal 0 (clause 6.2.3.4), before the
    signals bed_dyn_obj_assignment describes.
    """
    labels = (("LFE",) if lfe else ()) + beds
    full = ((BED, True),) * lfe + tuple(types)
    isf_count = sum(t == ISF for t, _ in types)
    rings = ISF_RINGS[isf] if isf is not None else ()
    dynamic = sum(t == DYN for t, _ in types)
    return full, Program(labels, isf_count, dynamic, rings)


def read_dyndata(
    b: BitReader, objects: Objects, timing: Timing, iframe: bool, alternative: bool
) -> Frame:
    """oamd_dyndata_single: one Frame of updates, timed by `timing`."""
    count = len(objects.types)
    frame = Frame(count, objects.program, [[] for _ in range(count)])
    for obj, (kind, lfe) in enumerate(objects.types):
        dynamic = kind == DYN and not lfe
        fixed = objects.program.isf_position(obj)
        for blk, (start, ramp) in enumerate(timing.blocks):
            frame.updates[obj].append(
                _info_block(b, objects, frame, obj, blk, iframe and blk == 0, dynamic, fixed,
                            start, ramp)
            )  # fmt: skip
    if alternative:
        _alternative(b, objects)
    frame.trim = replace(objects.trim, exempt=frozenset(objects.exempt))
    return frame


def _info_block(
    b: BitReader, objects: Objects, frame: Frame, obj: int, blk: int, no_delta: bool,
    dynamic: bool, fixed: tuple[float, float, float] | None, start: int, ramp: int,
) -> Update:  # fmt: skip
    state = objects.states[obj]
    inactive = b.flag()
    if inactive:
        state.gain_db = -math.inf
    elif no_delta or not b.flag():  # b_basic_info_reuse
        state.gain_db = _basic_info(b, frame, obj, blk)
    if inactive or not dynamic:
        state.render = Render()
        objects.coded[obj] = (31.0, 31.0, 0.0)
    elif no_delta:
        state.render = _render_info(b, objects, obj, state.render, True, True)
    elif not b.flag():  # b_render_info_reuse
        partial = b.flag()  # b_render_info_partial_reuse
        state.render = _render_info(b, objects, obj, state.render, not partial, False)
    if fixed is not None:
        x, y, z = fixed
        state.render = Render(x_bits=62 * x, y_bits=62 * y, z_bits=15 * z)
    render = state.render
    if b.flag():  # b_add_table_data
        size = b.read(4) + 1
        end = b.pos + 8 * size
        if b.flag():  # b_obj_trim_disable
            objects.exempt.add(obj)
        else:
            objects.exempt.discard(obj)
        if not inactive and dynamic and b.flag():  # b_ext_prec_pos
            present = b.read(3)
            fine = tuple(
                _FINE_STEPS[b.read(2)] if present & flag else 0 for flag in (0b100, 0b010, 0b001)
            )
            render = replace(render, fine=fine)
        if b.flag():  # b_headphone
            b.skip(3)
        if b.pos > end:
            raise BitstreamError("add_table_data overran its size")
        b.pos = end
    return Update(start, ramp, not inactive, state.gain_db, render)


def _basic_info(b: BitReader, frame: Frame, obj: int, blk: int) -> float:
    """object_basic_info: the object's gain in dB (tables 100 to 102)."""
    if b.flag():  # b_default_basic_info_md
        return 0.0
    if not b.flag():  # basic_info_md 0b0: gain, default priority
        gain, priority = True, False
    else:  # 0b10: gain and priority, 0b11: priority only
        gain = not b.flag()
        priority = True
    db = 0.0
    if gain:
        if not b.flag():  # object_gain_code 0b0
            g = b.read(6)
            db = float(15 - g if g < 15 else 14 - g)
        elif not b.flag():  # 0b10
            db = -math.inf
        else:  # 0b11: the previous object's in this block, 0 dB for the first
            db = frame.updates[obj - 1][blk].gain_db if obj else 0.0
    if priority:
        b.skip(5)
    return db


def _render_info(
    b: BitReader, objects: Objects, obj: int, prev: Render, all_new: bool, no_delta: bool
) -> Render:
    """object_render_info: an ALL_NEW update, or a PART_REUSE one with its mask."""
    if all_new:
        other = zone = position = True
    else:
        other, zone, position = b.flag(), b.flag(), b.flag()
    x, y, z = objects.coded[obj]
    distance, zone_mask, elevation, snap = prev.distance, prev.zone, prev.elevation, prev.snap
    size, screen = prev.size, prev.screen
    if position:
        if not no_delta and b.flag():  # b_diff_pos_coding
            x = max(0.0, min(62.0, x + b.signed(3)))
            y = max(0.0, min(62.0, y + b.signed(3)))
            z = max(-15.0, min(15.0, z + b.signed(3)))
        else:
            x, y = float(b.read(6)), float(b.read(6))
            sign = 1 if b.read(1) else -1
            z = float(sign * b.read(4))
        objects.coded[obj] = (x, y, z)
    if zone:
        zone_mask, elevation, snap = 0, True, False
        if not b.flag():  # b_grouped_zone_defaults
            flags = b.read(3)  # group_zone_flag[2..0]
            if flags & 0b100:
                zone_mask = b.read(3)
            elevation = not flags & 0b010
            snap = bool(flags & 0b001)
    if other:
        size, screen, distance = (0.0, 0.0, 0.0), False, None
        if not b.flag():  # b_grouped_other_defaults
            mask = b.read(4)
            if mask & 0b0001:
                if b.flag():  # object_width_mode
                    size = (b.read(5) / 31, b.read(5) / 31, b.read(5) / 31)
                else:
                    s = b.read(5) / 31
                    size = (s, s, s)
            if mask & 0b0010:
                screen = b.read(3) > 0  # object_screen_factor_code
                b.skip(2)  # object_depth_factor
            if mask & 0b0100:
                distance = math.inf if b.flag() else DISTANCE_FACTORS[b.read(4)]
            if mask & 0b1000:
                mode = b.read(2)
                if mode == 0:
                    b.skip(2)
                elif mode & 0b10:
                    b.skip(6)
    return Render(x, y, z, distance, zone_mask, elevation, size, screen, snap)


def _alternative(b: BitReader, objects: Objects) -> None:
    """The alternative property sets of oamd_dyndata_single, read past."""
    types = objects.types
    b.skip(1)  # b_ducking_disabled
    if b.read(2) == 3:
        b.variable_bits(2)
    sets = b.read(2)
    if sets == 3:
        sets += b.variable_bits(2)
    for _ in range(sets):
        keep = b.flag()
        if not keep:
            points = len(types)
            if (types and types[0][0] == ISF) or b.flag():  # b_common_data unless ISF
                points = 1
            for dp in range(points):
                kind, lfe = types[dp]
                if b.flag():  # b_alt_gain
                    b.skip(6)
                if kind == DYN and not lfe and b.flag():  # b_alt_position
                    b.skip(17)
        if b.flag():  # b_additional_data
            size = b.variable_bits(2) + 1
            end = b.pos + 8 * size
            if not keep:
                for kind, lfe in types:
                    if kind == DYN and not lfe and b.flag():  # b_ext_prec_alt_pos
                        present = b.read(3)
                        b.skip(2 * present.bit_count())
            b.pos = end
