"""The audio spectral frontend (TS 103 190-1 clauses 4.2.7, 4.2.8 and 5.1).

`sf_info` becomes a `Layout`: how a frame is cut into transform windows and
which scale factor bands each window group carries.  `read_spectrum` then
decodes one `sf_data` against it into the frame's spectral lines, ordered by
window and ascending within each (clause 5.1.5), ready for the stereo tool and
the IMDCT.
"""

from __future__ import annotations

from functools import cache

import numpy as np

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError

from .codes import huffman
from .tables import CODEBOOKS, RANDOM_NOISE_TABLE, SFB_OFFSETS, SFB_SIDE

# Tables 100 and 103: the transform lengths transf_length 0 to 3 stand for, per frame length
TRANSFORM_LENGTHS = {
    2048: (128, 256, 512, 1024),
    1920: (120, 240, 480, 960),
    1536: (96, 192, 384, 768),
    1024: (128, 256, 512, 1024),
    960: (120, 240, 480, 960),
    768: (96, 192, 384, 768),
    512: (128, 256, 512),
    384: (96, 192, 384),
}
# Table B.1: scale factor bands at 48 kHz
NUM_SFB_48 = {
    2048: 63, 1920: 61, 1536: 55, 1024: 49, 960: 49, 768: 43, 512: 36, 480: 36,
    384: 33, 256: 20, 240: 20, 192: 18, 128: 14, 120: 14, 96: 12,
}  # fmt: skip
# Table 109: n_grp_bits by transf_length[0] and [1] for frames of 1 536 samples and more
_GRP_BITS_LONG = ((15, 10, 8, 7), (10, 7, 4, 3), (8, 4, 3, 1), (7, 3, 1, 1))
# Table 110: by transf_length for 1 024, 960 and 768, then for 512 and 384
_GRP_BITS_MID = (7, 3, 1, 0)
_GRP_BITS_SHORT = (3, 1, 0)
# Tables A.3 to A.13: (dimension, unsigned, cb_mod, cb_off) of spectral codebooks 1 to 11
_CODEBOOKS = (
    None,
    (4, False, 3, 1), (4, False, 3, 1), (4, True, 3, 0), (4, True, 3, 0),
    (2, False, 9, 4), (2, False, 9, 4), (2, True, 8, 0), (2, True, 8, 0),
    (2, True, 13, 0), (2, True, 13, 0), (2, True, 17, 0),
)  # fmt: skip
ESCAPE_CODEBOOK = 11
LONG = 4  # the transform index of a long frame's one block


def msfb_bits(length: int) -> int:
    """Table 106: n_msfb_bits."""
    return 6 if length >= 384 else 5 if length >= 192 else 4


def side_bits(length: int) -> int:
    """Table 106: n_side_bits."""
    return 5 if length >= 480 else 4 if length >= 240 else 3


def msfbl_bits(frame_len: int) -> int:
    """Table 106: n_msfbl_bits of an LFE's long block."""
    return 3 if frame_len >= 1536 else 2


class Layout:
    """sf_info: the windows of one frame and the scale factor bands each group transmits."""

    __slots__ = (
        "_first_half_group",
        "different_framing",
        "dual_maxsfb",
        "frame_len",
        "group_lengths",
        "group_transform",
        "group_windows",
        "long_frame",
        "max_sfb",
        "max_sfb_side",
        "side_limited",
        "transform",
        "window_lengths",
        "window_to_group",
    )  # fmt: skip

    def __init__(
        self, frame_len: int, long_frame: bool, transform: tuple[int, int], max_sfb: list[int],
        max_sfb_side: list[int], grouping: list[int], *, side_limited: bool = False,
        dual_maxsfb: bool = False,
    ) -> None:  # fmt: skip
        self.frame_len, self.long_frame, self.transform = frame_len, long_frame, transform
        self.max_sfb, self.max_sfb_side = max_sfb, max_sfb_side
        self.side_limited, self.dual_maxsfb = side_limited, dual_maxsfb
        lengths = TRANSFORM_LENGTHS[frame_len]
        self.different_framing = (
            frame_len >= 1536 and not long_frame and transform[0] != transform[1]
        )
        # Pseudocode 3
        to_group = [0]
        if long_frame:
            windows = 1
        else:
            windows = len(grouping) + 1
            grouping = list(grouping)
            if self.different_framing:
                first = 1 << (3 - transform[0])
                grouping.insert(first - 1, 0)
                windows += 1
            groups = 1
            for bit in grouping[: windows - 1]:
                if bit == 0:
                    groups += 1
                to_group.append(groups - 1)
        self.window_to_group = to_group
        count = to_group[-1] + 1
        self.group_windows = [to_group.count(g) for g in range(count)]
        self._first_half_group = (
            to_group[1 << (3 - transform[0])] if frame_len >= 1536 and not long_frame else count
        )
        self.group_transform = [self._transform_index(g) for g in range(count)]
        self.group_lengths = [frame_len if t == LONG else lengths[t] for t in self.group_transform]
        self.window_lengths = [self.group_lengths[g] for g in to_group]
        if sum(self.window_lengths) != frame_len:
            raise BitstreamError(
                f"windows of {self.window_lengths} do not fill a frame of {frame_len}"
            )

    def _transform_index(self, g: int) -> int:
        """get_transf_length (pseudocode 2): the transform index of group g, LONG for a long frame."""
        if self.frame_len >= 1536:
            if self.long_frame:
                return LONG
            return self.transform[0] if g < self._first_half_group else self.transform[1]
        return self.transform[0]

    @property
    def groups(self) -> int:
        return len(self.group_windows)

    def band_count(self, g: int, side: bool = False) -> int:
        """get_max_sfb (pseudocode 5)."""
        idx = 1 if self.different_framing and g >= self._first_half_group else 0
        if self.side_limited or (self.dual_maxsfb and side):
            return self.max_sfb_side[idx]
        return self.max_sfb[idx]


def read_layout(
    b: BitReader, frame_len: int, *, dual_maxsfb: bool = False, side_limited: bool = False
) -> Layout:
    """sf_info for the ASF: asf_transform_info then asf_psy_info."""
    lengths = TRANSFORM_LENGTHS[frame_len]
    if frame_len >= 1536:
        long_frame = b.flag()
        transform = (LONG, LONG) if long_frame else (b.read(2), b.read(2))
    else:
        long_frame = False
        t = b.read(2)
        if t >= len(lengths):
            raise BitstreamError(f"transf_length {t} for a frame of {frame_len}")
        transform = (t, t)
    first_len = frame_len if long_frame else lengths[transform[0]]
    different = frame_len >= 1536 and not long_frame and transform[0] != transform[1]
    max_sfb, max_side = [0, 0], [0, 0]

    def counts(i: int, length: int) -> None:
        if side_limited:
            max_side[i] = b.read(side_bits(length))
        else:
            max_sfb[i] = b.read(msfb_bits(length))
            if dual_maxsfb:
                max_side[i] = b.read(msfb_bits(length))

    counts(0, first_len)
    if different:
        counts(1, lengths[transform[1]])
    if frame_len >= 1536:
        grp = 0 if long_frame else _GRP_BITS_LONG[transform[0]][transform[1]]
    elif frame_len > 512:
        grp = _GRP_BITS_MID[transform[0]]
    else:
        grp = _GRP_BITS_SHORT[transform[0]]
    grouping = [b.read(1) for _ in range(grp)]
    layout = Layout(
        frame_len, long_frame, transform, max_sfb, max_side, grouping,
        side_limited=side_limited, dual_maxsfb=dual_maxsfb,
    )  # fmt: skip
    _check_bands(layout)
    return layout


def read_lfe_layout(b: BitReader, frame_len: int) -> Layout:
    """sf_info_lfe: one long block."""
    layout = Layout(frame_len, True, (LONG, LONG), [b.read(msfbl_bits(frame_len)), 0], [0, 0], [])
    _check_bands(layout)
    return layout


def _check_bands(layout: Layout) -> None:
    for g in range(layout.groups):
        n = layout.group_lengths[g]
        for side in (False, True):
            if layout.band_count(g, side) >= len(SFB_OFFSETS[n]):
                raise BitstreamError(
                    f"max_sfb {layout.band_count(g, side)} for a {n}-line transform"
                )


def read_master(b: BitReader, layout: Layout) -> Layout:
    """max_sfb_master (clause 4.3.5.13) and the layout the two sf_data after it use.

    The value counts bands of the largest transform in `layout`; blocks of another
    length take their count from tables B.8 to B.19.
    """
    largest = max(layout.group_lengths)
    master = b.read(side_bits(largest))

    def count(n: int) -> int:
        if n == largest:
            return master
        column = SFB_SIDE.get(largest, {}).get(n)
        if column is None or master >= len(column):
            raise BitstreamError(f"max_sfb_master {master} has no mapping from {largest} to {n}")
        return column[master]

    first, last = layout.group_lengths[0], layout.group_lengths[-1]
    copy = Layout.__new__(Layout)
    for name in Layout.__slots__:
        setattr(copy, name, getattr(layout, name))
    copy.max_sfb = copy.max_sfb_side = [count(first), count(last)]
    copy.side_limited = copy.dual_maxsfb = False
    _check_bands(copy)
    return copy


class Noise:
    """The random number generator of clause 5.2.8.3, started per frame as pseudocode 24 does."""

    __slots__ = ("a", "b", "current", "state")

    def __init__(self, sequence_counter: int) -> None:
        start = 255 * (sequence_counter % 256)
        a, b = start % 255, (start // 255) % 256
        state = (a + 1) * ((a + 2) + 2 * b) // 2 + 255 * b * (255 + b) // 2
        if start % 130560 >= 65280:
            state += 128
        self.a, self.b = a, b
        self.state = state % 256
        self.current = (a * (a + 1) // 2 + b * 32386) % 256

    def take(self, count: int) -> np.ndarray:
        out = np.empty(count)
        a, b, current, state = self.a, self.b, self.current, self.state
        table = RANDOM_NOISE_TABLE
        for i in range(count):
            out[i] = table[current] + table[state]
            a = (a + 1) & 0xFF
            state = (state + 1) & 0xFF
            if a == 255:
                current = (current + 1) & 0xFF
                b = (b + 1) & 0xFF
                a = 0
            current = (current + a) & 0xFF
            state = (state + b + a) & 0xFF
        self.a, self.b, self.current, self.state = a, b, current, state
        return out


@cache
def _symbols(cb: int) -> tuple[tuple[int, ...], ...]:
    """The quantized lines each symbol of spectral codebook cb stands for (pseudocode 19)."""
    dim, _, mod, off = _CODEBOOKS[cb]
    size = len(CODEBOOKS[f"ASF_HCB_{cb}"][0])
    out = []
    for idx in range(size):
        if dim == 4:
            q1 = idx // (mod * mod * mod) - off
            r = idx - (q1 + off) * mod * mod * mod
            q2 = r // (mod * mod) - off
            r -= (q2 + off) * mod * mod
            q3 = r // mod - off
            r -= (q3 + off) * mod
            out.append((q1, q2, q3, r - off))
        else:
            q1 = idx // mod - off
            out.append((q1, idx - (q1 + off) * mod - off))
    return tuple(out)


def _ext_decode(b: BitReader) -> int:
    """Pseudocode 20."""
    n = 0
    while b.read(1):
        n += 1
    return (1 << (n + 4)) + b.read(n + 4)


def _read_lines(b: BitReader, cb: int, out: np.ndarray, start: int, stop: int) -> None:
    dim, unsigned, _, _ = _CODEBOOKS[cb]
    hcb = huffman(f"ASF_HCB_{cb}")
    symbols = _symbols(cb)
    k = start
    if not unsigned:
        for sym in hcb.decode_run(b, (stop - start) // dim):
            out[k : k + dim] = symbols[sym]
            k += dim
        return
    if cb != ESCAPE_CODEBOOK:
        count = -(-(stop - start) // dim)
        out[start : start + count * dim] = hcb.decode_signed(b, count, symbols)
        return
    decode, read = hcb.decode, b.read
    while k < stop:
        lines = symbols[decode(b)]
        for i, q in enumerate(lines):
            if q and read(1):
                lines = lines[:i] + (-q,) + lines[i + 1 :]
        lines = tuple(
            (_ext_decode(b) if q > 0 else -_ext_decode(b)) if abs(q) == 16 else q for q in lines
        )
        out[k : k + dim] = lines
        k += dim


@cache
def _grouped_order(layout_key: tuple) -> tuple[np.ndarray, list[list[int]]]:
    """Where each line of the grouped (bitstream) order goes, and the group band offsets.

    Returns the destination of every grouped line in the frame's window order and,
    per group, sect_sfb_offset (pseudocode 4) relative to the start of the grouped array.
    """
    _, windows, lengths, bands = layout_key
    dest: list[np.ndarray] = []
    offsets: list[list[int]] = []
    group_offset = 0
    win_start = 0
    for count, n, max_sfb in zip(windows, lengths, bands, strict=True):
        sfb = SFB_OFFSETS[n]
        offsets.append([group_offset + sfb[s] * count for s in range(max_sfb + 1)])
        starts = win_start + n * np.arange(count)
        for s in range(max_sfb):
            lines = np.arange(sfb[s], sfb[s + 1])
            dest.append((starts[:, None] + lines[None, :]).ravel())
        group_offset += sfb[max_sfb] * count
        win_start += n * count
    order = np.concatenate(dest) if dest else np.zeros(0, dtype=np.int64)
    return order, offsets


def read_spectrum(b: BitReader, layout: Layout, noise: Noise, side: bool = False) -> np.ndarray:
    """One sf_data(ASF): the scaled spectral lines of the frame, in window order (clause 5.1)."""
    groups = layout.groups
    bands = [layout.band_count(g, side) for g in range(groups)]
    key = (layout.frame_len, tuple(layout.group_windows), tuple(layout.group_lengths), tuple(bands))
    order, offsets = _grouped_order(key)
    total = offsets[-1][-1] if offsets else 0
    # asf_section_data
    sections: list[list[tuple[int, int, int]]] = []
    band_cb: list[list[int]] = []
    for g in range(groups):
        bits = 3 if layout.group_transform[g] <= 2 else 5
        escape = (1 << bits) - 1
        k, found, cbs = 0, [], []
        while k < bands[g]:
            cb = b.read(4)
            if cb > 11:
                raise BitstreamError(f"reserved section codebook {cb}")
            length = 1
            incr = b.read(bits)
            while incr == escape:
                length += escape
                incr = b.read(bits)
            length += incr
            if k + length > bands[g]:
                raise BitstreamError("a section runs past max_sfb")
            found.append((cb, k, k + length))
            cbs.extend([cb] * length)
            k += length
        sections.append(found)
        band_cb.append(cbs)
    # asf_spectral_data, only below the 48 kHz bands (the rest is the HSF substream's)
    quant = np.zeros(total, dtype=np.int64)
    for g in range(groups):
        num48 = NUM_SFB_48[layout.group_lengths[g]]
        for cb, s0, s1 in sections[g]:
            s1 = min(s1, num48)
            if cb and s0 < s1:
                _read_lines(b, cb, quant, offsets[g][s0], offsets[g][s1])
    # max_quant_idx per band, in the order bands follow each other in the grouped array
    starts = [offsets[g][s] for g in range(groups) for s in range(bands[g])]
    peak = np.maximum.reduceat(np.abs(quant), starts) if starts else np.zeros(0, np.int64)
    widths = np.diff([*starts, total]) if starts else np.zeros(0, np.int64)
    # asf_scalefac_data
    gains = np.zeros(len(starts))
    scale = b.read(8)
    first = True
    i = 0
    for g in range(groups):
        num48 = NUM_SFB_48[layout.group_lengths[g]]
        for s in range(bands[g]):
            if s < num48 and band_cb[g][s] and peak[i]:
                if first:
                    first = False
                else:
                    scale += huffman("ASF_HCB_SCALEFAC").decode(b) - 60
                gains[i] = 2.0 ** (0.25 * (scale - 100))
            i += 1
    # asf_snf_data
    snf: dict[int, int] = {}
    if b.flag():
        i = 0
        for g in range(groups):
            num48 = NUM_SFB_48[layout.group_lengths[g]]
            for s in range(bands[g]):
                if s < num48 and (band_cb[g][s] == 0 or peak[i] == 0):
                    snf[i] = huffman("ASF_HCB_SNF").decode(b)
                i += 1
    # clause 5.1.3: rec_spec = sign(q) |q|^(4/3), scaled by the band's gain
    band_of_line = np.repeat(np.arange(len(starts)), widths) if starts else np.zeros(0, int)
    scaled = np.sign(quant) * np.abs(quant) ** (4.0 / 3.0) * gains[band_of_line]
    if snf:
        _noise_fill(scaled, starts, widths, layout, bands, snf, noise)
    spectrum = np.zeros(layout.frame_len)
    spectrum[order] = scaled
    return spectrum


def _noise_fill(
    scaled: np.ndarray, starts: list[int], widths: np.ndarray, layout: Layout, bands: list[int],
    snf: dict[int, int], noise: Noise,
) -> None:  # fmt: skip
    """Pseudocodes 22 and 23: fill silent bands with noise relative to the band below."""
    energy = [float(np.dot(scaled[s : s + w], scaled[s : s + w])) for s, w in zip(starts, widths)]
    previous = -1000.0
    for e, w in zip(energy, widths):
        if e > 0:
            previous = 1.44269504 * np.log(e / w)
            break
    for i, (s, w) in enumerate(zip(starts, widths)):
        if energy[i] > 0:
            previous = 1.44269504 * np.log(energy[i] / w)
        elif i in snf and snf[i] != 0:
            level = previous + snf[i] - 17
            previous = level
            scaled[s : s + w] = noise.take(int(w)) * 2.0 ** (0.5 * level)
