"""Advanced joint object coding (TS 103 190-2 clauses 5.7, 6.2.3.4 to 6.2.5 and 6.3.6).

An A-JOC substream codes a few downmix signals (a 5.X channel element, or a
var_channel_element of dynamic objects) and the side information that rebuilds
the full set of objects from them in the QMF domain: per object a dry matrix
over the downmix and a wet one over decorrelated mixes of it.  The objects'
metadata for the full decode follows the A-JOC data; the downmix's own,
which a core decoder would render instead, is read past.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError
from dolbytool.core.atmos.oamd import Frame, Trim

from . import acpl, oamd
from .codes import huffman
from .elements import (
    ASPX,
    Element,
    Reader,
    Track,
    five_x_element,
    mono_data,
    three_channel_data,
    two_channel_data,
)
from .toc import SubstreamInfo

SUBBANDS = 64
NUM_BANDS = (23, 15, 12, 9, 7, 5, 3, 1)  # table 78
# Table 28: the parameter band of each QMF subband, per ajoc_num_bands
_SB_TO_PB = {
    23: (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22),
    15: (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13, 13, 14, 14, 14),
    12: (0, 1, 2, 3, 4, 4, 5, 5, 6, 6, 6, 7, 7, 8, 8, 9, 9, 10, 10, 10, 11, 11, 11),
    9: (0, 1, 2, 3, 3, 4, 4, 5, 5, 6, 6, 6, 6, 7, 7, 7, 7, 8, 8, 8, 8, 8, 8),
    7: (0, 1, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 4, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6),
    5: (0, 1, 1, 2, 2, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4),
    3: (0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2),
    1: (0,) * 23,
}  # fmt: skip
# the first subband of each row of table 28
_ROWS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16, 18, 20, 23, 26, 30, 35, 41, 48)
_ROW_OF_SUBBAND = np.searchsorted(_ROWS, np.arange(SUBBANDS), side="right") - 1
SB_TO_PB = {n: np.asarray(m)[_ROW_OF_SUBBAND] for n, m in _SB_TO_PB.items()}
# Dequantization (tables 29 to 32): the largest dry and wet value, reached at either end
_DRY_MAX, _WET_MAX = 5125 / 1024, 2050 / 1024
# the A-CPL decorrelator each A-JOC decorrelator instance is (clause 5.7.3.5)
_DECORRELATOR = (0, 2, 1, 0, 2, 1, 0)
DRY, WET = "DRY", "WET"


@dataclass(slots=True)
class AjocObject:
    bands: int
    coarse: bool
    sparse: bool
    dry_present: tuple[bool, ...]
    wet_present: tuple[bool, ...]


@dataclass(slots=True)
class AjocFrame:
    """ajoc(): the side information of one frame."""

    decorr: int
    points: list[tuple[int, int]]  # (ajoc_start_pos, ajoc_ramp_len) per data point
    objects: list[AjocObject | None]  # None: not present
    # per object, data point and input: (diff_type, values) or None where not sent
    dry: list[list[list[tuple[int, list[int]] | None]]]
    wet: list[list[list[tuple[int, list[int]] | None]]]


def read_ajoc(b: BitReader, n_dmx: int, n_umx: int) -> AjocFrame:
    decorr = b.read(3)
    enabled = [b.flag() for _ in range(decorr)]
    present = [b.flag() for _ in range(n_umx)]
    points = [(b.read(5), b.read(6) + 1) for _ in range(b.read(2))]
    objects: list[AjocObject | None] = [None] * n_umx
    if points:
        for o in range(n_umx):
            if not present[o]:
                continue
            bands, coarse, sparse = NUM_BANDS[b.read(3)], b.flag(), b.flag()
            dry = wet = None
            if sparse:
                dry = tuple(b.flag() for _ in range(n_dmx))
                wet = tuple(b.flag() if enabled[d] else False for d in range(decorr))
            objects[o] = AjocObject(
                bands, coarse, sparse, dry or (True,) * n_dmx, wet or (True,) * decorr
            )
    nodt = b.flag()
    dry_data, wet_data = [], []
    for o in range(n_umx):
        obj = objects[o]
        dry_o, wet_o = [], []
        for dp in range(len(points) if obj is not None else 0):
            dfonly = dp == 0 and nodt
            dry_o.append([
                _huff_data(b, DRY, obj.bands, obj.coarse, dfonly) if obj.dry_present[ch] else None
                for ch in range(n_dmx)
            ])  # fmt: skip
            wet_o.append([
                _huff_data(b, WET, obj.bands, obj.coarse, dfonly) if obj.wet_present[de] else None
                for de in range(decorr)
            ])  # fmt: skip
        dry_data.append(dry_o)
        wet_data.append(wet_o)
    return AjocFrame(decorr, points, objects, dry_data, wet_data)


def _huff_data(
    b: BitReader, kind: str, bands: int, coarse: bool, dfonly: bool
) -> tuple[int, list[int]]:
    """ajoc_huff_data: F0 and DF codes are indices (cb_off 0), DT codes centred."""
    quant = "COARSE" if coarse else "FINE"
    diff = 0 if dfonly else b.read(1)
    if diff == 0:
        values = [huffman(f"AJOC_HCB_{kind}_{quant}_F0").decode(b)]
        hcb = huffman(f"AJOC_HCB_{kind}_{quant}_DF")
        values += [hcb.decode(b) for _ in range(1, bands)]
        return 0, values
    centre = _nquant(kind, coarse) - 1  # the DT books span -centre to centre
    hcb = huffman(f"AJOC_HCB_{kind}_{quant}_DT")
    return 1, [hcb.decode(b) - centre for _ in range(bands)]


def _nquant(kind: str, coarse: bool) -> int:
    return (51 if coarse else 101) if kind == DRY else (21 if coarse else 41)


def read_dmx_de_data(b: BitReader, n_dmx: int, n_umx: int, state: AjocState, iframe: bool) -> None:
    """ajoc_dmx_de_data: dialogue enhancement is not applied, so this is read past."""
    cfg, keep = b.flag(), b.flag()
    if cfg:
        b.skip(2)  # de_max_gain
        state.dialogue = sum(b.flag() for _ in range(n_umx))
    if not keep or cfg or iframe:
        for _ in range(state.dialogue * n_dmx):
            # de_dlg_dmx_coeff_idx (table 82): 0b0, 0b1111 or five bits 0b1xxxx
            if b.flag() and b.read(3) != 0b111:
                b.skip(1)


@dataclass(slots=True)
class AjocState:
    """What an A-JOC substream's syntax refers back to across frames."""

    dmx_objects: oamd.Objects | None = None
    umx_objects: oamd.Objects | None = None
    dmx_timing: oamd.Timing = field(default_factory=lambda: oamd.Timing(()))
    umx_timing: oamd.Timing = field(default_factory=lambda: oamd.Timing(()))
    dialogue: int = 0  # num_dlg_obj
    # this frame's oamd_timing_data from the group's oamd_substream, which every
    # substream of the group falls back on (clause 6.3.9.3.1)
    group_timing: oamd.Timing | None = None
    trim: Trim | None = None  # from the last oamd_common_data


@dataclass(slots=True)
class AjocPayload:
    """What one frame's A-JOC substream hands the QMF stage and the renderer."""

    inputs: list[str]  # Qin_AJOC: the downmix tracks in A-JOC input order
    lfe: str | None
    frame: AjocFrame
    metadata: Frame
    upmix: int  # fullband objects A-JOC rebuilds
    # the downmix's own metadata, for core decoding (None for a static 5.X downmix)
    core: Frame | None = None


def audio_data_ajoc(
    r: Reader, info: SubstreamInfo, alternative: bool, state: AjocState
) -> tuple[Element, AjocPayload]:
    """audio_data_ajoc (clause 6.2.3.4) and the input preparation of clause 5.7.2.1."""
    b, iframe = r.b, r.iframe
    n_dmx, n_umx = info.dmx_signals, info.upmix_signals
    if state.umx_objects is None or iframe:
        types, program = oamd.program_for(
            info.upmix_objects, info.upmix_beds, info.isf_config, info.b_lfe
        )
        trim = state.umx_objects.trim if state.umx_objects is not None else Trim()
        state.umx_objects = oamd.Objects(types, program, trim=trim)
        dmx_types, dmx_program = oamd.program_for(info.dmx_objects, info.dmx_beds, None, info.b_lfe)
        state.dmx_objects = oamd.Objects(dmx_types, dmx_program)
    if info.oamd_common is not None:
        state.trim = info.oamd_common
    if state.trim is not None:
        state.umx_objects.trim = state.trim
    core = None
    if info.static_dmx:
        element = five_x_element(r, info.b_lfe)
        inputs = ["L", "R", "C", "Ls", "Rs"]
    else:
        if b.flag():  # b_some_signals_inactive
            b.skip(n_dmx)  # dmx_active_signals_mask
        element, inputs = _var_channel_element(r, n_dmx, info.b_lfe)
        if b.flag():  # b_dmx_timing
            state.dmx_timing = oamd.read_timing(b)
        elif state.group_timing is not None:
            state.dmx_timing = state.group_timing
        core = oamd.read_dyndata(b, state.dmx_objects, state.dmx_timing, iframe, alternative)
        if b.flag():  # b_oamd_extension_present
            size = b.variable_bits(3) + 1
            end = b.pos + 8 * size
            if b.flag():  # ajoc_bed_info: b_obj_without_bed_info_present
                b.skip(3)
            if b.pos > end:
                raise BitstreamError("ajoc_bed_info overran the OAMD extension")
            b.pos = end
    frame = read_ajoc(b, n_dmx, n_umx)
    read_dmx_de_data(b, n_dmx, n_umx, state, iframe)
    if b.flag():  # b_umx_timing
        state.umx_timing = oamd.read_timing(b)
    elif b.flag():  # b_derive_timing_from_dmx
        state.umx_timing = state.dmx_timing
    elif state.group_timing is not None:
        state.umx_timing = state.group_timing
    metadata = oamd.read_dyndata(b, state.umx_objects, state.umx_timing, iframe, alternative)
    lfe = "LFE" if info.b_lfe else None
    payload = AjocPayload(inputs, lfe, frame, metadata, n_umx, core)
    return element, payload


def oamd_substream(b: BitReader, state: AjocState) -> None:
    """oamd_substream (clause 6.2.2.4) of an A-JOC group: its trim and timing.

    Its oamd_dyndata_multi only covers objects that are not A-JOC coded, and an
    A-JOC substream group has none, so it is left unread.
    """
    state.group_timing = None
    if b.flag():  # b_oamd_common_data_present
        trim = oamd.read_common(b)
        if trim is not None:
            state.trim = trim
    if b.flag():  # b_oamd_timing_present
        state.group_timing = oamd.read_timing(b)


def _var_channel_element(r: Reader, n: int, lfe: bool) -> tuple[Element, list[str]]:
    """var_channel_element (clause 6.2.4.4): the tracks, and their A-JOC order (pseudocode 14a)."""
    b = r.b
    mode = ASPX if b.flag() else 0
    element = Element({}, mode)
    names = [f"d{i}" for i in range(n)]
    if mode == ASPX:
        r.aspx_config()
        if n <= 5:
            element.companding = r.companding(tuple(names))
    if lfe:
        element.channels["LFE"] = mono_data(r, True)
    tracks: list[Track] = []
    pairs = n // 2
    if n % 2:
        if n == 1:
            tracks.append(mono_data(r, False))
        else:
            for _ in range(pairs - 1):
                tracks += two_channel_data(r)
            if not b.flag():  # var_coding_config
                tracks += two_channel_data(r)
                tracks.append(mono_data(r, False))
            else:
                tracks += three_channel_data(r)
    else:
        for _ in range(pairs):
            tracks += two_channel_data(r)
    element.channels.update(zip(names, tracks, strict=True))
    if mode == ASPX:
        for p in range(pairs):
            r.aspx_2ch(element, names[2 * p], names[2 * p + 1])
        if n % 2:
            r.aspx_1ch(element, names[-1])
    if n > 3:
        offset = 2 + n % 2
        order = names[n - offset :] + names[: n - offset]
    else:
        order = names
    return element, order


@dataclass(slots=True)
class _Ramp:
    """The interpolation all matrix elements share (pseudocode 17)."""

    remaining: int = 0


class Reconstructor:
    """Pseudocodes 16 to 18: the objects from the downmix, frame by frame."""

    def __init__(self, n_dmx: int, n_umx: int) -> None:
        self.n_dmx, self.n_umx = n_dmx, n_umx
        self.decorr = 0
        self.q_dry: list[dict[int, np.ndarray]] = [{} for _ in range(n_umx)]
        self.q_wet: list[dict[int, np.ndarray]] = [{} for _ in range(n_umx)]
        # current interpolated values, per subband, and the step each takes
        self.dry = np.zeros((n_umx, n_dmx, SUBBANDS))
        self.wet = np.zeros((n_umx, 0, SUBBANDS))
        self.pre = np.zeros((0, n_dmx, SUBBANDS))
        self.steps: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self.final: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self.ramp = _Ramp()
        self.modules: list[acpl.Module] = []

    def _resize(self, decorr: int) -> None:
        if decorr == self.decorr:
            return
        wet = np.zeros((self.n_umx, decorr, SUBBANDS))
        pre = np.zeros((decorr, self.n_dmx, SUBBANDS))
        keep = min(decorr, self.decorr)
        wet[:, :keep], pre[:keep] = self.wet[:, :keep], self.pre[:keep]
        self.wet, self.pre, self.decorr = wet, pre, decorr
        self.modules = self.modules[:decorr] + [
            acpl.Module(_DECORRELATOR[d]) for d in range(len(self.modules), decorr)
        ]
        # a ramp under way was over the old decorrelators; the new ones start where they are
        self.steps = self.final = None
        self.ramp.remaining = 0

    def targets(self, frame: AjocFrame) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """The dequantized dry, wet and decorrelator input matrices of each data point, per subband."""
        out = []
        for dp in range(len(frame.points)):
            dry = np.zeros((self.n_umx, self.n_dmx, SUBBANDS))
            wet = np.zeros((self.n_umx, frame.decorr, SUBBANDS))
            for o, obj in enumerate(frame.objects):
                if obj is None:
                    continue
                pb = SB_TO_PB[obj.bands]
                for kind, data, prev, out_m, width in (
                    (DRY, frame.dry, self.q_dry[o], dry, self.n_dmx),
                    (WET, frame.wet, self.q_wet[o], wet, frame.decorr),
                ):
                    nq = _nquant(kind, obj.coarse)
                    centre = (nq - 1) // 2
                    top = _DRY_MAX if kind == DRY else _WET_MAX
                    for i in range(width):
                        sent = data[o][dp][i]
                        if sent is None:
                            q = np.full(obj.bands, centre)
                        elif sent[0] == 0:
                            q = np.cumsum(sent[1]) % nq
                        else:
                            before = prev.get(i)
                            if before is None or len(before) != obj.bands:
                                before = np.full(obj.bands, centre)
                            q = before + np.asarray(sent[1])
                        prev[i] = q
                        out_m[o, i] = ((q - centre) * (top / centre))[pb]
            # each object on its own band grid first, then summed (MacinDecode's finding)
            pre = np.einsum("ods,ocs->dcs", np.abs(wet), dry)
            out.append((dry, wet, pre))
        return out

    def __call__(self, frame: AjocFrame, x: np.ndarray) -> np.ndarray:
        """x: (n_dmx, 64, slots) -> the objects (n_umx, 64, slots)."""
        self._resize(frame.decorr)
        targets = self.targets(frame)
        slots = x.shape[2]
        starts = {start: dp for dp, (start, _) in enumerate(frame.points)}
        # runs of slots whose matrices are base + step * (1, 2, ...), or base alone
        runs = []
        ts = 0
        while ts < slots:
            dp = starts.get(ts)
            if dp is not None:
                # a new ramp starts here and takes its first step in this slot
                length = frame.points[dp][1]
                self.steps = tuple(
                    (t - c) / length for t, c in zip(targets[dp], (self.dry, self.wet, self.pre))
                )
                self.ramp.remaining = length
                self.final = targets[dp]
            # up to the next data point: the ramp's steps, then the values it ended on
            stop = min([s for s in starts if ts < s < slots], default=slots)
            ramp = min(self.ramp.remaining, stop - ts)
            current = (self.dry, self.wet, self.pre)
            if ramp:
                runs.append((ts, ts + ramp, current, self.steps))
                self.ramp.remaining -= ramp
                if self.ramp.remaining == 0:
                    current = tuple(m.copy() for m in self.final)
                else:
                    current = tuple(c + step * ramp for c, step in zip(current, self.steps))
                self.dry, self.wet, self.pre = current
            if ts + ramp < stop:
                runs.append((ts + ramp, stop, current, None))
            ts = stop
        # decorrelator inputs, the decorrelated and ducked signals, then the two matrices
        z = _mix(runs, 0, x)
        if self.decorr:
            u = _mix(runs, 2, x)
            y = np.stack([m.duck(m.decorrelate(u[d])) for d, m in enumerate(self.modules)])
            z += _mix(runs, 1, y)
        return z


def _mix(runs: list, which: int, x: np.ndarray) -> np.ndarray:
    """x (inputs, 64, slots) through matrix `which` (outputs, inputs, 64) of every run."""
    out = np.empty((runs[0][2][which].shape[0], *x.shape[1:]), dtype=complex)
    for start, stop, base, steps in runs:
        # real matrices on the real and imaginary parts side by side, a product per subband
        v = np.ascontiguousarray(x[:, :, start:stop]).view(float).transpose(1, 0, 2)
        z = np.matmul(base[which].transpose(2, 0, 1), v)
        if steps is not None:
            ramp = np.repeat(np.arange(1, stop - start + 1), 2)
            z += ramp * np.matmul(steps[which].transpose(2, 0, 1), v)
        out[:, :, start:stop] = np.ascontiguousarray(z.transpose(1, 0, 2)).view(complex)
    return out
