"""Joint object coding (TS 103 420 clause 6): rebuild the objects from the channel downmix."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import tables
from .bitstream import BitReader, BitstreamError, Huffman
from .qmf import BANDS

SLOTS_PER_FRAME = 1536 // BANDS
DOWNMIX_CHANNELS = (5, 7, 7, 5, 7)  # table 48, by joc_dmx_config_idx
NUM_BANDS = (1, 3, 5, 7, 9, 12, 15, 23)  # table 50

# Table 54: the first QMF subband of each parameter band, per joc_num_bands.
_BAND_STARTS = {
    23: (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16, 18, 20, 23, 26, 30, 35, 41, 48),
    15: (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 14, 18, 23, 35),
    12: (0, 1, 2, 3, 4, 6, 8, 11, 14, 18, 23, 35),
    9: (0, 1, 2, 3, 5, 7, 9, 14, 23),
    7: (0, 1, 2, 4, 8, 14, 23),
    5: (0, 1, 3, 9, 23),
    3: (0, 3, 14),
    1: (0,),
}  # fmt: skip


def _band_map(bands: int) -> np.ndarray:
    """sb_to_pb for every subband."""
    starts = _BAND_STARTS[bands]
    assert len(starts) == bands
    return np.searchsorted(starts, np.arange(BANDS), side="right") - 1


BAND_MAPS = {n: _band_map(n) for n in NUM_BANDS}

_HUFF = {
    ("MTX", 0): Huffman(tables.JOC_HUFF_CODE_COARSE_GENERIC),
    ("MTX", 1): Huffman(tables.JOC_HUFF_CODE_FINE_GENERIC),
    ("VEC", 0): Huffman(tables.JOC_HUFF_CODE_COARSE_COEFF_SPARSE),
    ("VEC", 1): Huffman(tables.JOC_HUFF_CODE_FINE_COEFF_SPARSE),
    ("IDX", 5): Huffman(tables.JOC_HUFF_CODE_5CH_POS_INDEX_SPARSE),
    ("IDX", 7): Huffman(tables.JOC_HUFF_CODE_7CH_POS_INDEX_SPARSE),
}


@dataclass(slots=True)
class ObjectParams:
    bands: int
    sparse: bool
    quant_idx: int
    steep: bool
    offsets: tuple[int, ...]  # joc_offset_ts per data point (steep only)
    matrix_q: np.ndarray  # (dpoints, channels, bands) quantised


@dataclass(slots=True)
class JocFrame:
    dmx_config: int
    channels: int
    objects: int
    clipgain: float
    seq_count: int
    params: list[ObjectParams | None]  # None when the object carries no side information


def parse(payload: bytes) -> JocFrame:
    bits = BitReader(payload)
    dmx_config = bits.read(3)
    if dmx_config >= len(DOWNMIX_CHANNELS):
        raise BitstreamError(f"reserved joc_dmx_config_idx {dmx_config}")
    channels = DOWNMIX_CHANNELS[dmx_config]
    objects = bits.read(6) + 1
    ext_config = bits.read(3)
    clip_x, clip_y = bits.read(3), bits.read(5)
    clipgain = (1 + clip_y / 32) * 2.0 ** (clip_x - 4)
    seq_count = bits.read(10)
    if ext_config:
        raise BitstreamError(f"reserved joc_ext_config_idx {ext_config}")

    info = []
    for _ in range(objects):
        if not bits.flag():
            info.append(None)
            continue
        bands = NUM_BANDS[bits.read(3)]
        sparse = bits.flag()
        quant_idx = bits.read(1)
        steep = bits.flag()
        dpoints = bits.read(1) + 1
        offsets = tuple(bits.read(5) + 1 for _ in range(dpoints)) if steep else ()
        info.append((bands, sparse, quant_idx, steep, dpoints, offsets))

    params: list[ObjectParams | None] = []
    for entry in info:
        if entry is None:
            params.append(None)
            continue
        bands, sparse, quant_idx, steep, dpoints, offsets = entry
        nquant = 192 if quant_idx else 96
        if sparse:
            q = np.empty((dpoints, channels, bands), dtype=np.int64)
            for dp in range(dpoints):
                # Pseudocode 2: one active channel per band, both it and its
                # value coded as differences from the band below; the other
                # channels hold the offset (50 or 100, not the dequantiser's
                # centre: the spec's own value).  Checked line by line against
                # TS 103 420 V1.2.1; none of the masters at hand use sparse mode.
                idx = [bits.read(3)]
                if idx[0] >= channels:
                    raise BitstreamError(f"sparse channel {idx[0]} of a {channels}-channel downmix")
                idx += [_HUFF["IDX", channels].decode(bits) for _ in range(1, bands)]
                vec = [_HUFF["VEC", quant_idx].decode(bits) for _ in range(bands)]
                offset = 100 if quant_idx else 50
                q[dp] = offset
                ch = idx[0]
                q[dp, ch, 0] = (offset + vec[0]) % nquant
                for pb in range(1, bands):
                    ch = (ch + idx[pb]) % channels
                    q[dp, ch, pb] = (q[dp, ch, pb - 1] + vec[pb]) % nquant
        else:
            # Pseudocode 3: every channel, differentially coded across bands;
            # the running sum modulo nquant is the cumulative sum taken modulo once.
            deltas = _HUFF["MTX", quant_idx].decode_run(bits, dpoints * channels * bands)
            offset = 96 if quant_idx else 48
            steps = np.array(deltas, dtype=np.int64).reshape(dpoints, channels, bands)
            q = (offset + np.cumsum(steps, axis=2)) % nquant
        params.append(ObjectParams(bands, sparse, quant_idx, steep, offsets, q))
    return JocFrame(dmx_config, channels, objects, clipgain, seq_count, params)


def dequantise(p: ObjectParams) -> np.ndarray:
    """Pseudocode 5, expanded from parameter bands to subbands: (dpoints, channels, 64)."""
    nquant = 192 if p.quant_idx else 96
    dq = (p.matrix_q - nquant / 2) * 820 / (4096 * (1 + p.quant_idx))
    return dq[:, :, BAND_MAPS[p.bands]]


_RAMP = np.arange(1, SLOTS_PER_FRAME + 1)


class JocDecoder:
    """Holds joc_mix_mtx_prev across frames."""

    def __init__(self) -> None:
        self.prev: np.ndarray | None = None  # (objects, channels, 64)

    def reconstruct(self, frame: JocFrame, downmix: np.ndarray) -> np.ndarray:
        """Pseudocodes 6 and 7: downmix (channels, slots, 64) -> objects (objects, slots, 64).

        An object with one smooth data point, the common case, glides linearly
        from the previous matrix to the new one, so its output is prev . x plus
        a ramp times (new - prev) . x: two matrix products per subband instead
        of a matrix per slot.
        """
        prev = self._prev(frame)
        out = np.zeros((frame.objects, SLOTS_PER_FRAME, BANDS), dtype=np.complex128)
        smooth: list[int] = []
        targets: list[np.ndarray] = []
        for obj, p in enumerate(frame.params):
            if p is None:
                # No side information: the object is silent this frame.
                prev[obj] = 0.0
                continue
            dq = dequantise(p)
            if not p.steep and len(dq) == 1:
                smooth.append(obj)
                targets.append(dq[0])
                continue
            out[obj] = np.einsum("tcb,ctb->tb", _interpolated(p, dq, prev[obj]), downmix)
            prev[obj] = dq[-1]
        if smooth:
            # (subbands, 2 objects, channels) @ (subbands, channels, slots as real pairs)
            start, target = prev[smooth], np.stack(targets)
            weights = np.concatenate([start, target - start]).transpose(2, 0, 1)
            x = np.ascontiguousarray(downmix.transpose(2, 0, 1)).view(np.float64)
            y = (weights @ x).view(np.complex128)
            n = len(smooth)
            out[smooth] = (y[:, :n] + y[:, n:] * (_RAMP / SLOTS_PER_FRAME)).transpose(1, 2, 0)
            prev[smooth] = target
        return out

    def advance(self, frame: JocFrame) -> None:
        """Move joc_mix_mtx_prev past `frame` without rebuilding anything."""
        prev = self._prev(frame)
        for obj, p in enumerate(frame.params):
            prev[obj] = 0.0 if p is None else dequantise(p)[-1]

    def _prev(self, frame: JocFrame) -> np.ndarray:
        shape = (frame.objects, frame.channels, BANDS)
        if self.prev is None or self.prev.shape != shape:
            self.prev = np.zeros(shape)
        return self.prev


def _interpolated(p: ObjectParams, dq: np.ndarray, prev: np.ndarray) -> np.ndarray:
    """Pseudocode 6 for a steep or two-point object: its matrix per slot, (slots, channels, 64)."""
    n = SLOTS_PER_FRAME
    ramp = _RAMP[:, None, None]
    if not p.steep:
        half = n // 2
        first = prev + ramp[:half] * (dq[0] - prev) / half
        second = dq[0] + (ramp[half:] - half) * (dq[1] - dq[0]) / (n - half)
        return np.concatenate([first, second])
    out = np.repeat(prev[None], n, axis=0)
    out[p.offsets[0] :] = dq[0]
    if len(dq) > 1:
        out[p.offsets[1] :] = dq[1]
    return out
