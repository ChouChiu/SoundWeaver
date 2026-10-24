"""Stereo and multichannel processing in the MDCT domain (TS 103 190-1 clauses 4.2.10 and 5.3).

A chparam_info carries one 2x2 matrix [[a, b], [c, d]] per scale factor band;
`Matrix` holds it expanded to every spectral line of the frame.  The three,
four and five channel data elements chain such pairs (tables 178 and 179),
which `three`, `four` and `five` do one pair at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np

from dolbytool.core.atmos.bitstream import BitReader

from .asf import Layout
from .codes import huffman
from .tables import SFB_OFFSETS

SAP_IDENTITY, SAP_MS_SOME, SAP_MS_ALL, SAP_FULL = 0, 1, 2, 3


@dataclass(slots=True)
class ChParam:
    mode: int
    ms_used: list[list[bool]] | None = None
    coeff_used: list[list[bool]] | None = None
    delta_code_time: bool = False
    dpcm_alpha: list[list[int]] | None = None


def read_chparam(b: BitReader, layout: Layout) -> ChParam:
    """chparam_info (clause 4.2.10.1)."""
    mode = b.read(2)
    param = ChParam(mode)
    bands = [layout.band_count(g) for g in range(layout.groups)]
    if mode == SAP_MS_SOME:
        param.ms_used = [[b.flag() for _ in range(n)] for n in bands]
    elif mode == SAP_FULL:
        if b.flag():  # sap_coeff_all
            used = [[True] * n for n in bands]
        else:
            used = []
            for n in bands:
                row = []
                for _ in range(0, n, 2):
                    flag = b.flag()
                    row.extend([flag, flag])
                used.append(row[:n])
        param.coeff_used = used
        if layout.groups != 1:
            param.delta_code_time = b.flag()
        sf = huffman("ASF_HCB_SCALEFAC")
        param.dpcm_alpha = [
            [sf.decode(b) if s % 2 == 0 and used[g][s] else 0 for s in range(n)]
            for g, n in enumerate(bands)
        ]
    return param


class Matrix:
    """The [[a, b], [c, d]] of one chparam_info at every line of the frame."""

    __slots__ = ("a", "b", "c", "d")

    def __init__(self, a: np.ndarray, b: np.ndarray, c: np.ndarray, d: np.ndarray) -> None:
        self.a, self.b, self.c, self.d = a, b, c, d

    @classmethod
    def identity(cls, frame_len: int) -> Matrix:
        one, zero = np.ones(frame_len), np.zeros(frame_len)
        return cls(one, zero, zero, one)

    def apply(self, x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        return self.a * x + self.b * y, self.c * x + self.d * y


@cache
def _line_bands(frame_len: int, windows: tuple, lengths: tuple, bands: tuple) -> np.ndarray:
    """For each line in window order, the index of its (group, sfb) band; the last index past max_sfb."""
    total = sum(bands)
    out = np.full(frame_len, total, dtype=np.int64)
    start, first = 0, 0
    for count, n, max_sfb in zip(windows, lengths, bands, strict=True):
        sfb = SFB_OFFSETS[n]
        for _ in range(count):
            for s in range(max_sfb):
                out[start + sfb[s] : start + sfb[s + 1]] = first + s
            start += n
        first += max_sfb
    return out


def matrix(param: ChParam, layout: Layout) -> Matrix:
    """Pseudocode 59, expanded from bands to lines."""
    bands = [layout.band_count(g) for g in range(layout.groups)]
    total = sum(bands)
    a, b, c, d = np.ones(total + 1), np.zeros(total + 1), np.zeros(total + 1), np.ones(total + 1)
    i = 0
    alpha: list[list[int]] = []
    previous = bands[0] if bands else 0
    for g, n in enumerate(bands):
        row = [0] * n
        for s in range(n):
            if param.mode == SAP_MS_ALL or (param.mode == SAP_MS_SOME and param.ms_used[g][s]):
                a[i], b[i], c[i], d[i] = 1.0, 1.0, 1.0, -1.0
            elif param.mode == SAP_FULL and param.coeff_used[g][s]:
                if s % 2:
                    row[s] = row[s - 1]
                else:
                    delta = param.dpcm_alpha[g][s] - 60
                    in_time = g > 0 and n == previous and param.delta_code_time
                    if in_time:
                        row[s] = alpha[g - 1][s] + delta
                    elif s == 0:
                        row[s] = delta
                    else:
                        row[s] = row[s - 2] + delta
                gain = row[s] * 0.1
                a[i], b[i], c[i], d[i] = 1.0 + gain, 1.0, 1.0 - gain, -1.0
            i += 1
        alpha.append(row)
        previous = n
    index = _line_bands(
        layout.frame_len, tuple(layout.group_windows), tuple(layout.group_lengths), tuple(bands)
    )
    return Matrix(a[index], b[index], c[index], d[index])


def three(selector: int, m0: Matrix, m1: Matrix, x: list[np.ndarray]) -> list[np.ndarray]:
    """Table 178: two matrices over three tracks, the selector naming the pairs and the order."""
    i0, i1, i2 = x
    a0, b0, c0, d0 = m0.a, m0.b, m0.c, m0.d
    a1, b1, c1, d1 = m1.a, m1.b, m1.c, m1.d
    rows = {
        0: ((a0 * a1, b0 * a1, b1), (c0, d0, 0), (a0 * c1, b0 * c1, d1)),
        1: ((d0, c0, 0), (b0 * a1, a0 * a1, b1), (b0 * c1, a0 * c1, d1)),
        2: ((a0 * a1, b1, b0 * a1), (a0 * c1, d1, b0 * c1), (c0, 0, d0)),
        3: ((a1, c0 * b1, d0 * b1), (0, a0, b0), (c1, c0 * d1, d0 * d1)),
        4: ((a0, 0, b0), (c0 * b1, a1, d0 * b1), (c0 * d1, c1, d0 * d1)),
        5: ((a1, d0 * b1, c0 * b1), (c1, d0 * d1, c0 * d1), (0, b0, a0)),
        6: ((d0 * d1, c0 * d1, c1), (b0, a0, 0), (d0 * b1, c0 * b1, a1)),
        7: ((a0, b0, 0), (c0 * d1, d0 * d1, c1), (c0 * b1, d0 * b1, a1)),
        8: ((d0 * d1, c1, c0 * d1), (d0 * b1, a1, c0 * b1), (b0, 0, a0)),
        9: ((d1, b0 * c1, a0 * c1), (0, d0, c0), (b1, b0 * a1, a0 * a1)),
        10: ((d0, 0, c0), (b0 * c1, d1, a0 * c1), (b0 * a1, b1, a0 * a1)),
        11: ((d1, a0 * c1, b0 * c1), (b1, a0 * a1, b0 * a1), (0, c0, d0)),
    }.get(selector)
    if rows is None:
        from dolbytool.core.atmos.bitstream import BitstreamError

        raise BitstreamError(f"reserved chel_matsel {selector}")
    return [r[0] * i0 + r[1] * i1 + r[2] * i2 for r in rows]


def four(m: list[Matrix], x: list[np.ndarray]) -> list[np.ndarray]:
    """Clause 5.3.3.4: pairs (0, 1) and (2, 3), then their first and second halves paired."""
    p0, p1 = m[0].apply(x[0], x[1])
    q0, q1 = m[1].apply(x[2], x[3])
    o0, o2 = m[2].apply(p0, q0)
    o1, o3 = m[3].apply(p1, q1)
    return [o0, o1, o2, o3]


def five(selector: int, m: list[Matrix], x: list[np.ndarray]) -> list[np.ndarray]:
    """Table 179: the three channel matrix on tracks 0 to 2, a pair on 3 and 4, then two more pairs."""
    t0, t1, t2 = three(selector, m[0], m[1], x[:3])
    y0, y1 = m[2].apply(x[3], x[4])
    o0, o3 = m[3].apply(t0, y0)
    o1, o4 = m[4].apply(t1, y1)
    return [o0, o1, t2, o3, o4]
