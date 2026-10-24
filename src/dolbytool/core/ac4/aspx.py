"""Advanced spectral extension, A-SPX (TS 103 190-1 clauses 4.2.12, 4.3.10 and 5.7.6), and companding (5.7.5).

The syntax is read into an `AspxFrame` per channel; `AspxChannel` keeps what
one interval hands the next and turns a frame of QMF subband samples into the
extended spectrum.  QMF matrices here are indexed [subband, time slot].

Where the pseudocode contradicts itself this follows MacinDecode's readings,
which were checked against Dolby's decoder: the minus sign in the prediction
cross term of pseudocode 87, noise and tone cursors counted in QMF slots from
the interval start, the pre-flattening gain flattening rather than steepening,
noise only where no sinusoid starts, and a limiter-off interval left unboosted.
None of this has been checked against a real A-SPX stream here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import cache
from itertools import pairwise

import numpy as np

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError

from .codes import huffman
from .tables import ASPX_NOISE

FIXFIX, FIXVAR, VARFIX, VARVAR = range(4)
SIGNAL, NOISE = 0, 1
LEVEL, BALANCE = 0, 1
SUBBANDS = 64
# ts_offset_hfgen in A-SPX time slots of two QMF slots (table 192), and the HF adjuster's extra history
HFGEN_ATS = 3
HFADJ = 4

_TEMPLATE_LOW = (10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 22, 24, 26, 28, 30, 32, 35, 38, 42, 46)
_TEMPLATE_HIGH = (
    18,
    19,
    20,
    21,
    22,
    23,
    24,
    26,
    28,
    30,
    32,
    34,
    36,
    38,
    40,
    42,
    44,
    47,
    50,
    53,
    56,
    59,
    62,
)
# Table 194: FIXFIX borders by A-SPX slots and envelope count
_TAB_BORDER = {
    6: {1: (0, 6), 2: (0, 3, 6), 4: (0, 2, 3, 4, 6)},
    8: {1: (0, 8), 2: (0, 4, 8), 4: (0, 2, 4, 6, 8)},
    12: {1: (0, 12), 2: (0, 6, 12), 4: (0, 3, 6, 9, 12)},
    15: {1: (0, 15), 2: (0, 8, 15), 4: (0, 4, 8, 12, 15)},
    16: {1: (0, 16), 2: (0, 8, 16), 4: (0, 4, 8, 12, 16)},
}
# Table 195: new_chirp by [previous tna mode][current tna mode]
_NEW_CHIRP = (
    (0.0, 0.6, 0.9, 0.98),
    (0.6, 0.75, 0.9, 0.98),
    (0.0, 0.75, 0.9, 0.98),
    (0.0, 0.75, 0.9, 0.98),
)
_SINE_RE = (1.0, 0.0, -1.0, 0.0)
_SINE_IM = (0.0, 1.0, 0.0, -1.0)
# Annex A.2: cb_off of the delta codebooks
_DIFF_OFFSETS = {
    "ENV_LEVEL_15": 70, "ENV_BALANCE_15": 24, "ENV_LEVEL_30": 35, "ENV_BALANCE_30": 12,
    "NOISE_LEVEL": 29, "NOISE_BALANCE": 12,
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class AspxConfig:
    quant_mode_env: int
    start_freq: int
    stop_freq: int
    master_freq_scale: int
    interpolation: int
    preflat: int
    limiter: int
    noise_sbg: int
    num_env_bits_fixfix: int
    freq_res_mode: int

    @property
    def master(self) -> tuple[int, int, int]:
        return self.start_freq, self.stop_freq, self.master_freq_scale


def read_config(b: BitReader) -> AspxConfig:
    return AspxConfig(
        b.read(1), b.read(3), b.read(2), b.read(1), b.read(1), b.read(1), b.read(1), b.read(2),
        b.read(1), b.read(2),
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class Bands:
    """The subband group tables of clause 5.7.6.3.1."""

    master: tuple[int, ...]
    high: tuple[int, ...]
    low: tuple[int, ...]
    noise: tuple[int, ...]
    patch_count: tuple[int, ...]  # sbg_patch_num_sb
    patch_start: tuple[int, ...]  # sbg_patch_start_sb
    patches: tuple[int, ...]  # sbg_patches
    limiter: tuple[int, ...]

    @property
    def sba(self) -> int:
        return self.master[0]

    @property
    def sbx(self) -> int:
        return self.high[0]

    @property
    def width(self) -> int:
        return self.high[-1] - self.high[0]


@cache
def bands(config: tuple[int, int, int], noise_sbg: int, xover: int, base_48k: bool) -> Bands:
    start, stop, scale = config
    template, total = (_TEMPLATE_HIGH, 22) if scale else (_TEMPLATE_LOW, 20)
    count = total - 2 * start - 2 * stop
    if count <= 0:
        raise BitstreamError(f"A-SPX start {start} and stop {stop} leave no subband groups")
    master = template[2 * start : 2 * start + count + 1]
    sba, sbz = master[0], master[-1]
    if xover >= count:
        raise BitstreamError(f"A-SPX crossover offset {xover} past {count} groups")
    high = master[xover:]
    sbx, n_high = high[0], len(high) - 1
    n_low = n_high - n_high // 2
    low = (
        high[0],
        *(high[2 * s] if n_high % 2 == 0 else high[2 * s - 1] for s in range(1, n_low + 1)),
    )
    n_noise = max(1, math.floor(noise_sbg * math.log2(sbz / sbx) + 0.5))
    if n_noise > 5:
        raise BitstreamError(f"{n_noise} A-SPX noise groups")
    idx = [0]
    for s in range(1, n_noise + 1):
        idx.append(idx[-1] + (n_low - idx[-1]) // (n_noise + 1 - s))
    noise = tuple(low[i] for i in idx)
    # Pseudocode 71
    msb, usb = sba, sbx
    goal = 43 if base_48k else 46
    source_low = 4 if scale else 2
    width = high[-1] - sbx
    if goal < sbx + width:
        sbg = 0
        for i, value in enumerate(master):
            if value >= goal:
                break
            sbg = i + 1
    else:
        sbg = count
    counts, starts = [], []
    while True:
        j = sbg
        sb = master[j]
        odd = (sb - 2 + sba) % 2
        while sb > sba - source_low + msb - odd:
            j -= 1
            if j < 0:
                raise BitstreamError("A-SPX patches cannot be built")
            sb = master[j]
            odd = (sb - 2 + sba) % 2
        counts.append(max(sb - usb, 0))
        starts.append(sba - odd - max(sb - usb, 0))
        if counts[-1] > 0:
            usb = msb = sb
        else:
            counts.pop()
            starts.pop()
            msb = sbx
        if master[sbg] - sb < 3:
            sbg = count
        if sb == sbx + width:
            break
        if len(counts) > 8:
            raise BitstreamError("A-SPX patches do not reach the top")
    if len(counts) > 1 and counts[-1] < 3:
        counts.pop()
        starts.pop()
    patches = [sbx]
    for n in counts:
        patches.append(patches[-1] + n)
    # Pseudocode 72
    lim = sorted([*low, *patches[1:-1]])
    s = 1
    while s < len(lim):
        if math.log2(lim[s] / lim[s - 1]) < 0.245:
            if lim[s] == lim[s - 1]:
                del lim[s]
            elif lim[s] in patches:
                if lim[s - 1] in patches:
                    s += 1
                else:
                    del lim[s - 1]
            else:
                del lim[s]
        else:
            s += 1
    return Bands(master, high, low, noise, tuple(counts), tuple(starts), tuple(patches), tuple(lim))


@dataclass(slots=True)
class AspxFrame:
    """One channel's A-SPX data of one frame, with the borders it implies."""

    int_class: int = FIXFIX
    num_env: int = 1
    num_noise: int = 1
    tsg_ptr: int = -1
    qmode: int = 0
    sig_borders: list[int] = field(default_factory=list)  # atsg_sig
    noise_borders: list[int] = field(default_factory=list)  # atsg_noise
    freq_res: list[int] = field(default_factory=list)  # atsg_freqres
    sig_delta: list[int] = field(default_factory=list)
    noise_delta: list[int] = field(default_factory=list)
    tna: list[int] = field(default_factory=list)
    harmonics: list[int] = field(default_factory=list)
    fic: list[int] = field(default_factory=list)
    tic: list[int] = field(default_factory=list)
    sig: list[list[int]] = field(default_factory=list)
    noise: list[list[int]] = field(default_factory=list)
    balance: bool = False
    second: bool = False  # channel 1 of a balanced pair


class AspxChannel:
    """The state of one channel's A-SPX from interval to interval."""

    def __init__(self, frame_len: int) -> None:
        self.slots = frame_len // SUBBANDS
        self.ts_in_ats = 2 if frame_len >= 1536 else 1
        self.aspx_slots = self.slots // self.ts_in_ats
        self.hfgen = HFGEN_ATS * self.ts_in_ats
        self.config: AspxConfig | None = None
        self.xover = 0
        self.bands: Bands | None = None
        self.master_reset = True
        self.previous_stop = self.aspx_slots
        # what the previous interval leaves
        self.prev_num_env = 1
        self.prev_freq_res = [0]
        self.prev_sig = np.zeros(32)  # qscf of the last signal envelope, by high-res group
        self.prev_noise = np.zeros(8)
        self.prev_num_noise = 1
        self.prev_tna = [0] * 8
        self.prev_chirp = np.zeros(8)
        self.prev_tsg_ptr = -1
        self.prev_sine = np.zeros(SUBBANDS, dtype=bool)
        self.noise_cursor = 0
        self.sine_cursor = 0
        self.first = True
        self.q_prev = np.zeros((SUBBANDS, self.slots), dtype=complex)
        self.y_prev = np.zeros((SUBBANDS, self.slots + self.hfgen), dtype=complex)
        self.frame: AspxFrame | None = None

    # -- syntax ------------------------------------------------------------------------------

    def configure(self, config: AspxConfig, base_48k: bool, xover: int | None) -> None:
        """An I-frame's aspx_config and crossover offset (None keeps the offset)."""
        if self.config is None or self.config.master != config.master:
            self.master_reset = True
        self.config = config
        if xover is not None:
            self.xover = xover
        self.bands = bands(config.master, config.noise_sbg, self.xover, base_48k)

    def _framing(self, b: BitReader, iframe: bool) -> AspxFrame:
        config, f = self.config, AspxFrame()
        if not b.flag():
            f.int_class = FIXFIX
        elif not b.flag():
            f.int_class = FIXVAR
        else:
            f.int_class = VARVAR if b.flag() else VARFIX
        rel_bits = 2 if self.aspx_slots > 8 else 1
        left, right, var_left, var_right = [], [], 0, 0
        transmitted = []
        if f.int_class == FIXFIX:
            f.num_env = 1 << b.read(config.num_env_bits_fixfix + 1)
            if f.num_env > 4:
                raise BitstreamError("8 A-SPX envelopes")
            if config.freq_res_mode == 0:
                transmitted = [b.read(1)]
        else:
            if f.int_class in (VARFIX, VARVAR):
                if iframe:
                    var_left = b.read(2)
                left = [2 * b.read(rel_bits) + 2 for _ in range(b.read(rel_bits))]
            if f.int_class in (FIXVAR, VARVAR):
                var_right = b.read(2)
                right = [2 * b.read(rel_bits) + 2 for _ in range(b.read(rel_bits))]
            f.num_env = len(left) + len(right) + 1
            if f.num_env > 5:
                raise BitstreamError(f"{f.num_env} A-SPX envelopes")
            f.tsg_ptr = b.read(math.ceil(math.log2(f.num_env + 2))) - 1
            if config.freq_res_mode == 0:
                transmitted = [b.read(1) for _ in range(f.num_env)]
        f.num_noise = 2 if f.num_env > 1 else 1
        self._borders(f, iframe, left, right, var_left, var_right, transmitted)
        return f

    def _borders(
        self, f: AspxFrame, iframe: bool, left: list[int], right: list[int], var_left: int,
        var_right: int, transmitted: list[int],
    ) -> None:  # fmt: skip
        """Pseudocode 76."""
        n, slots = f.num_env, self.aspx_slots
        if f.int_class == FIXFIX:
            sig = list(_TAB_BORDER[slots][n])
            noise = list(_TAB_BORDER[slots][f.num_noise])
        else:
            sig = [0] * (n + 1)
            if f.int_class == FIXVAR:
                sig[0] = 0
            else:
                sig[0] = var_left if iframe else self.previous_stop - slots
            sig[n] = slots if f.int_class == VARFIX else var_right + slots
            for k, step in enumerate(left):
                sig[k + 1] = sig[k] + step
            for k, step in enumerate(right):
                sig[n - k - 1] = sig[n - k] - step
            noise = [sig[0]] * (f.num_noise + 1)
            noise[-1] = sig[n]
            if f.num_noise > 1:
                if f.tsg_ptr == -1:
                    mid = 1 if f.int_class == VARFIX else n - 1
                else:
                    mid = n - 1 if f.int_class == VARFIX else max(1, min(n - 1, f.tsg_ptr))
                noise[1] = sig[mid]
        if any(b2 <= b1 for b1, b2 in pairwise(sig)) or sig[0] < 0:
            raise BitstreamError(f"A-SPX borders {sig} do not rise")
        mode = self.config.freq_res_mode
        res = []
        for k in range(n):
            if mode == 0:
                res.append(transmitted[0] if f.int_class == FIXFIX else transmitted[k])
            elif mode == 1:
                res.append(0)
            elif mode == 2:
                long = sig[k + 1] - sig[k] > slots / 6.0 + 3.25
                res.append(int((k < f.tsg_ptr and slots > 8) or long))
            else:
                res.append(1)
        if f.int_class == FIXFIX:
            res = [res[0]] * n
        f.sig_borders, f.noise_borders, f.freq_res = sig, noise, res
        self.previous_stop = sig[n]

    def _hcb(self, data_type: int, qmode: int, stereo: int, kind: str):
        mode = "BALANCE" if stereo else "LEVEL"
        name = f"ENV_{mode}_{'30' if qmode else '15'}" if data_type == SIGNAL else f"NOISE_{mode}"
        return huffman(f"ASPX_HCB_{name}_{kind}"), (0 if kind == "F0" else _DIFF_OFFSETS[name])

    def _ec_data(self, b: BitReader, f: AspxFrame, data_type: int, stereo: int) -> list[list[int]]:
        bands_ = self.bands
        out = []
        count = f.num_env if data_type == SIGNAL else f.num_noise
        for env in range(count):
            if data_type == SIGNAL:
                num = len(bands_.high) - 1 if f.freq_res[env] else len(bands_.low) - 1
                direction = f.sig_delta[env]
            else:
                num = len(bands_.noise) - 1
                direction = f.noise_delta[env]
            values = []
            if direction == 0:
                hcb, off = self._hcb(data_type, f.qmode, stereo, "F0")
                values.append(hcb.decode(b) - off)
                hcb, off = self._hcb(data_type, f.qmode, stereo, "DF")
                values += [hcb.decode(b) - off for _ in range(num - 1)]
            else:
                hcb, off = self._hcb(data_type, f.qmode, stereo, "DT")
                values = [hcb.decode(b) - off for _ in range(num)]
            out.append(values)
        return out

    def _delta_dir(self, b: BitReader, f: AspxFrame) -> None:
        f.sig_delta = [b.read(1) for _ in range(f.num_env)]
        f.noise_delta = [b.read(1) for _ in range(f.num_noise)]


def _qmode(config: AspxConfig, f: AspxFrame) -> int:
    return 0 if f.int_class == FIXFIX and f.num_env == 1 else config.quant_mode_env


def _ready(ch: AspxChannel, config: AspxConfig | None, iframe: bool) -> None:
    if config is None or (not iframe and ch.bands is None):
        raise BitstreamError("A-SPX data before the I-frame that configures it")


def read_data_1ch(
    b: BitReader, ch: AspxChannel, config: AspxConfig | None, iframe: bool, base_48k: bool
) -> AspxFrame:
    """aspx_data_1ch (table 51)."""
    _ready(ch, config, iframe)
    if iframe:
        ch.configure(config, base_48k, b.read(3))
    nb = ch.bands
    f = ch._framing(b, iframe)
    f.qmode = _qmode(ch.config, f)
    ch._delta_dir(b, f)
    n_noise, n_high = len(nb.noise) - 1, len(nb.high) - 1
    f.tna = [b.read(2) for _ in range(n_noise)]
    f.harmonics = [b.read(1) for _ in range(n_high)] if b.flag() else [0] * n_high
    f.fic = [b.read(1) for _ in range(n_high)] if b.flag() else [0] * n_high
    f.tic = [b.read(1) for _ in range(ch.aspx_slots)] if b.flag() else [0] * ch.aspx_slots
    f.sig = ch._ec_data(b, f, SIGNAL, LEVEL)
    f.noise = ch._ec_data(b, f, NOISE, LEVEL)
    ch.frame = f
    return f


def read_data_2ch(
    b: BitReader,
    a: AspxChannel,
    c: AspxChannel,
    config: AspxConfig | None,
    iframe: bool,
    base_48k: bool,
) -> tuple[AspxFrame, AspxFrame]:
    """aspx_data_2ch (table 52)."""
    _ready(a, config, iframe)
    _ready(c, config, iframe)
    if iframe:
        xover = b.read(3)
        a.configure(config, base_48k, xover)
        c.configure(config, base_48k, xover)
    nb = a.bands
    f0 = a._framing(b, iframe)
    f0.qmode = _qmode(a.config, f0)
    balance = bool(b.read(1))
    if not balance:
        f1 = c._framing(b, iframe)
        f1.qmode = _qmode(c.config, f1)
    else:
        f1 = AspxFrame(
            f0.int_class, f0.num_env, f0.num_noise, f0.tsg_ptr, f0.qmode, list(f0.sig_borders),
            list(f0.noise_borders), list(f0.freq_res),
        )  # fmt: skip
        c.previous_stop = a.previous_stop
    f0.balance = f1.balance = balance
    f1.second = True
    a._delta_dir(b, f0)
    c._delta_dir(b, f1)
    n_noise, n_high = len(nb.noise) - 1, len(nb.high) - 1
    f0.tna = [b.read(2) for _ in range(n_noise)]
    f1.tna = list(f0.tna) if balance else [b.read(2) for _ in range(n_noise)]
    f0.harmonics = [b.read(1) for _ in range(n_high)] if b.flag() else [0] * n_high
    f1.harmonics = [b.read(1) for _ in range(n_high)] if b.flag() else [0] * n_high
    f0.fic, f1.fic = [0] * n_high, [0] * n_high
    if b.flag():
        if b.flag():
            f0.fic = [b.read(1) for _ in range(n_high)]
        if b.flag():
            f1.fic = [b.read(1) for _ in range(n_high)]
    f0.tic, f1.tic = [0] * a.aspx_slots, [0] * a.aspx_slots
    if b.flag():
        copy = b.read(1)
        left = right = 0
        if not copy:
            left, right = b.read(1), b.read(1)
        if copy or left:
            f0.tic = [b.read(1) for _ in range(a.aspx_slots)]
        if right:
            f1.tic = [b.read(1) for _ in range(a.aspx_slots)]
        if copy:
            f1.tic = list(f0.tic)
    f0.sig = a._ec_data(b, f0, SIGNAL, LEVEL)
    f1.sig = c._ec_data(b, f1, SIGNAL, BALANCE if balance else LEVEL)
    f0.noise = a._ec_data(b, f0, NOISE, LEVEL)
    f1.noise = c._ec_data(b, f1, NOISE, BALANCE if balance else LEVEL)
    a.frame, c.frame = f0, f1
    return f0, f1


# -- envelope decoding -------------------------------------------------------------------------


def _quantized(ch: AspxChannel, f: AspxFrame) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Pseudocodes 80 and 81: signal and noise scale factors, quantized, per envelope."""
    nb = ch.bands
    high, low = nb.high, nb.low
    high2low, low2high = [], [0]
    lo = 0
    for s in range(len(high) - 1):
        if lo + 1 < len(low) and low[lo + 1] == high[s]:
            lo += 1
            low2high.append(s)
        high2low.append(lo)
    delta = 2 if f.second and f.balance else 1
    sig: list[np.ndarray] = []
    for env in range(f.num_env):
        res = f.freq_res[env]
        prev_res = ch.prev_freq_res[-1] if env == 0 else f.freq_res[env - 1]
        prev = ch.prev_sig if env == 0 else sig[env - 1]
        num = len(high) - 1 if res else len(low) - 1
        data = np.asarray(f.sig[env], dtype=float)
        if f.sig_delta[env] == 0:
            values = delta * np.cumsum(data)
        else:
            if res == prev_res:
                index = list(range(num))
            elif res == 0:
                index = [low2high[s] for s in range(num)]
            else:
                index = [high2low[s] for s in range(num)]
            values = prev[index] + delta * data
        out = np.zeros(32)
        out[:num] = values
        sig.append(out)
    noise: list[np.ndarray] = []
    for env in range(f.num_noise):
        data = np.asarray(f.noise[env], dtype=float)
        n = len(data)
        out = np.zeros(8)
        if f.noise_delta[env] == 0:
            out[:n] = delta * np.cumsum(data)
        else:
            prev = ch.prev_noise if env == 0 else noise[env - 1]
            out[:n] = prev[:n] + delta * data
        noise.append(out)
    return sig, noise


def _dequantize(
    frames: list[tuple[AspxChannel, AspxFrame, list[np.ndarray], list[np.ndarray]]],
) -> list[tuple[list[np.ndarray], list[np.ndarray]]]:
    """Pseudocodes 82 to 84: scale factors, a balanced pair decoded jointly."""
    out = []
    first = frames[0]
    if len(frames) == 2 and first[1].balance:
        (_, fa, sa, na), (_, _, sb_, nb_) = frames
        a = 2 if fa.qmode == 0 else 1
        sig_a, sig_b, noise_a, noise_b = [], [], [], []
        for qa, qb in zip(sa, sb_):
            nom = 2.0 ** (qa / a + 1) * SUBBANDS
            sig_a.append(nom / (1 + 2.0 ** (12 - qb / a)))
            sig_b.append(nom / (1 + 2.0 ** (qb / a - 12)))
        for qa, qb in zip(na, nb_):
            nom = 2.0 ** (6 - qa + 1)
            noise_a.append(nom / (1 + 2.0 ** (12 - qb)))
            noise_b.append(nom / (1 + 2.0 ** (qb - 12)))
        return [(sig_a, noise_a), (sig_b, noise_b)]
    for ch, f, sig, noise in frames:
        a = 2 if f.qmode == 0 else 1
        s_out = []
        for env, q in enumerate(sig):
            s = SUBBANDS * 2.0 ** (q / a)
            s_out.append(s)
        out.append((s_out, [2.0 ** (6 - q) for q in noise]))
    return out


# -- companding --------------------------------------------------------------------------------

_ALPHA = 0.65


def compand(
    qs: list[np.ndarray], spans: list[tuple[int, int, int, int]], on: list[bool], avg: bool,
    sync: bool,
) -> None:  # fmt: skip
    """Clause 5.7.5 in place on QMF matrices at full scale 1; spans are (sb0, sb1, ts0, ts1)."""
    levels = []
    for q, (sb0, sb1, ts0, ts1) in zip(qs, spans):
        part = q[sb0:sb1, ts0:ts1]
        re, im = np.abs(part.real), np.abs(part.imag)
        e = np.maximum(re, im) + 0.5 * np.minimum(re, im)
        levels.append(0.9105 * e.mean(axis=0) if sb1 > sb0 else np.zeros(ts1 - ts0))
    boost = 2.0 ** (1 / _ALPHA)
    exponent = (1 - _ALPHA) / _ALPHA
    if sync:
        gains = np.mean([np.maximum(lv, 1e-12) ** exponent for lv in levels], axis=0)
        if on[0]:
            per = [gains] * len(qs)
        elif avg:
            per = [np.full_like(gains, gains.mean())] * len(qs)
        else:
            return
    else:
        per = []
        for k, lv in enumerate(levels):
            if on[min(k, len(on) - 1)]:
                per.append(np.maximum(lv, 1e-12) ** exponent)
            elif avg and lv.size:
                per.append(np.full_like(lv, max(lv.mean(), 1e-12) ** exponent))
            else:
                per.append(None)
    for q, g, (sb0, sb1, ts0, ts1) in zip(qs, per, spans):
        if g is not None:
            q[sb0:sb1, ts0:ts1] *= (g * boost)[None, :]


# -- HF reconstruction -------------------------------------------------------------------------


def _predictors(ext: np.ndarray, sba: int) -> tuple[np.ndarray, np.ndarray]:
    """Pseudocodes 86 and 87 (with the corrected cross-term sign) for subbands below sba."""
    n = ext.shape[1]
    ts = np.arange(HFADJ, n, 2)
    x0, x1, x2 = ext[:sba, ts], ext[:sba, ts - 2], ext[:sba, ts - 4]
    c01 = (x0 * x1.conj()).sum(1)
    c02 = (x0 * x2.conj()).sum(1)
    c11 = (x1 * x1.conj()).sum(1).real
    c12 = (x1 * x2.conj()).sum(1)
    c22 = (x2 * x2.conj()).sum(1).real
    eps = 2.0**-20
    denom = c22 * c11 - np.abs(c12) ** 2 / (1 + eps)
    with np.errstate(divide="ignore", invalid="ignore"):
        alpha1 = np.where(denom != 0, (c01 * c12 - c02 * c11) / denom, 0)
        alpha0 = np.where(c11 != 0, (-c01 - alpha1 * c12.conj()) / c11, 0)
    bad = (
        (np.abs(alpha0) >= 4) | (np.abs(alpha1) >= 4) | ~np.isfinite(alpha0) | ~np.isfinite(alpha1)
    )
    alpha0[bad] = 0
    alpha1[bad] = 0
    return alpha0, alpha1


def _preflatten(low: np.ndarray, sbx: int, ts0: int, ts1: int) -> np.ndarray:
    """Pseudocode 85, its gain turned the flattening way."""
    power = (np.abs(low[:sbx, ts0:ts1]) ** 2).sum(1) / max(ts1 - ts0, 1)
    env = 10 * np.log10(power + 1)
    x = np.arange(sbx)
    fit = np.polyval(np.polyfit(x, env, 3), x) if sbx > 3 else env
    return 10 ** ((fit - env.mean()) / 20)


@cache
def _patch_map(nb: Bands) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per subband the HF generator fills: where it copies from, itself, and its chirp group."""
    source, target = [], []
    sb_high = nb.sbx
    for count, start in zip(nb.patch_count, nb.patch_start):
        for i in range(count):
            if sb_high + i >= SUBBANDS:
                break
            source.append(start + i)
            target.append(sb_high + i)
        sb_high += count
    target = np.array(target, dtype=int)
    group = np.searchsorted(nb.noise, target, side="right") - 1
    return np.array(source, dtype=int), target, np.clip(group, 0, len(nb.noise) - 2)


@cache
def _groups(table: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
    """For subbands table[0] up to table[-1]: the group of each, and a [group, subband] sum."""
    index = np.repeat(np.arange(len(table) - 1), np.diff(table))
    return index, (index[None, :] == np.arange(len(table) - 1)[:, None]).astype(float)


def process(
    ch: AspxChannel, f: AspxFrame, q: np.ndarray, sig_scf: list[np.ndarray],
    noise_scf: list[np.ndarray],
) -> np.ndarray:  # fmt: skip
    """One frame of QMF input [64, slots] to the extended output, delayed by ts_offset_hfgen slots."""
    nb, config = ch.bands, ch.config
    slots, k, hfgen = ch.slots, ch.ts_in_ats, ch.hfgen
    sbx, sba, width = nb.sbx, nb.sba, nb.width
    history = np.concatenate([ch.q_prev, q], axis=1)  # previous frame then this one
    delayed = history[:, slots - hfgen : 2 * slots - hfgen]
    low = history[:, slots - hfgen :].copy()  # Q_low, slots + hfgen long
    low[sbx:] = 0
    ext = history[:, slots - hfgen - HFADJ :]  # Q_low_ext
    ts0, ts1 = f.sig_borders[0] * k, f.sig_borders[-1] * k
    n_env = f.num_env
    # HF generator
    alpha0, alpha1 = _predictors(ext, sba)
    chirps = np.zeros(len(nb.noise) - 1)
    for g in range(len(chirps)):
        new = _NEW_CHIRP[ch.prev_tna[g] if g < len(ch.prev_tna) else 0][f.tna[g]]
        prev = ch.prev_chirp[g]
        new = 0.75 * new + 0.25 * prev if new < prev else 0.90625 * new + 0.09375 * prev
        chirps[g] = 0.0 if new < 0.015625 else new
    gain_vec = _preflatten(low, sbx, ts0, ts1) if config.preflat else None
    high = np.zeros((SUBBANDS, slots + hfgen), dtype=complex)
    t = np.arange(ts0, ts1)
    source, target, group = _patch_map(nb)
    if source.size:
        c = chirps[group]
        rows = ext[source]
        n = t + HFADJ
        value = (
            rows[:, n]
            + (c * alpha0[source])[:, None] * rows[:, n - 2]
            + (c * c * alpha1[source])[:, None] * rows[:, n - 4]
        )
        if gain_vec is not None:
            value = value / gain_vec[source][:, None]
        high[target, ts0:ts1] = value
    # Pseudocode 90: estimated envelopes over the A-SPX range
    est = np.zeros((width, n_env))
    tables = [nb.high if r else nb.low for r in f.freq_res]
    for env in range(n_env):
        a, z = f.sig_borders[env] * k, f.sig_borders[env + 1] * k
        span = f.sig_borders[env + 1] - f.sig_borders[env]
        energy = (np.abs(high[:, a:z]) ** 2).sum(1)
        if config.interpolation:
            est[:, env] = energy[sbx : sbx + width] / span
        else:
            table = tables[env]
            index, sums = _groups(table)
            e = sums @ energy[table[0] : table[-1]] / np.diff(table) / span
            est[table[0] - sbx : table[-1] - sbx, env] = e[index]
    # Pseudocode 91: scale factors per subband
    scf_sig = np.zeros((width, n_env))
    scf_noise = np.zeros((width, n_env))
    noise_env = 0
    noise_index = _groups(nb.noise)[0]
    for env in range(n_env):
        table = tables[env]
        scf_sig[table[0] - sbx : table[-1] - sbx, env] = np.asarray(sig_scf[env])[_groups(table)[0]]
        if f.num_noise > 1 and f.sig_borders[env] == f.noise_borders[noise_env + 1]:
            noise_env += 1
        scf_noise[nb.noise[0] - sbx : nb.noise[-1] - sbx, env] = np.asarray(noise_scf[noise_env])[
            noise_index
        ]
    # Pseudocodes 92 and 93: sinusoids
    p_sine_at_end = 0 if ch.prev_tsg_ptr == ch.prev_num_env else -1
    sine = np.zeros((width, n_env), dtype=bool)
    high_borders = np.asarray(nb.high) - sbx
    mid = (high_borders[:-1] + high_borders[1:]) // 2
    sent = np.asarray(f.harmonics[: len(mid)], dtype=bool)
    if p_sine_at_end == 0:
        sine[mid] = sent[:, None]
    else:
        sine[mid] = sent[:, None] & ((np.arange(n_env) >= f.tsg_ptr) | ch.prev_sine[mid][:, None])
    area = np.zeros((width, n_env), dtype=bool)
    for env in range(n_env):
        table = tables[env]
        index, sums = _groups(table)
        area[table[0] - sbx : table[-1] - sbx, env] = (
            sums @ sine[table[0] - sbx : table[-1] - sbx, env] > 0
        )[index]
    # Pseudocode 94 and 95
    factor = scf_sig / (1 + scf_noise)
    sine_lev = np.sqrt(factor * sine)
    noise_lev = np.sqrt(factor * scf_noise)
    onset = np.array([env == f.tsg_ptr or env == p_sine_at_end for env in range(n_env)])
    denom = 1.0 + est
    denom_plain = np.where(onset[None, :], denom, denom * (1 + scf_noise))
    gain = np.where(
        area,
        np.sqrt(scf_sig * scf_noise / (denom * (1 + scf_noise))),
        np.sqrt(scf_sig / denom_plain),
    )
    noise_on = ~onset[None, :] & (sine_lev == 0)
    if config.limiter:
        # Pseudocodes 96 to 101
        lim = nb.limiter
        index, sums = _groups(lim)
        lo, hi = lim[0] - sbx, lim[-1] - sbx
        max_gain = np.zeros((width, n_env))
        ratio = np.sqrt(sums @ scf_sig[lo:hi] / (1e-12 + sums @ est[lo:hi])) * 1.41254
        max_gain[lo:hi] = np.minimum(ratio, 1e5)[index]
        with np.errstate(divide="ignore", invalid="ignore"):
            limited_noise = np.minimum(noise_lev, np.nan_to_num(noise_lev * max_gain / gain))
        gain_lim = np.minimum(gain, max_gain)
        boost = np.zeros((width, n_env))
        nom = 1e-12 + sums @ scf_sig[lo:hi]
        den = 1e-12 + sums @ (est[lo:hi] * gain_lim[lo:hi] ** 2) + sums @ (sine_lev[lo:hi] ** 2)
        den = den + sums @ np.where(noise_on[lo:hi], limited_noise[lo:hi] ** 2, 0)
        boost[lo:hi] = np.minimum(np.sqrt(nom / den), 1.584893192)[index]
        gain, noise_lev, sine_lev = gain_lim * boost, limited_noise * boost, sine_lev * boost
    # Pseudocodes 102 to 108
    y = np.zeros((SUBBANDS, slots + hfgen), dtype=complex)
    y[:, :ts0] = ch.y_prev[:, slots : slots + ts0]
    env_of_slot = (
        np.searchsorted(np.asarray(f.sig_borders) * k, np.arange(ts0, ts1), side="right") - 1
    )
    rows = np.arange(width)
    base = 0 if ch.master_reset else ch.noise_cursor
    noise_idx = (base + width * (t - ts0)[None, :] + rows[:, None] + 1) % 512
    sine_base = 1 if ch.first else (ch.sine_cursor + 1) % 4
    sine_idx = (sine_base + (t - ts0)) % 4
    parity = np.where((rows + sbx) % 2, -1.0, 1.0)
    g_slot = gain[:, env_of_slot]
    nl_slot = np.where(noise_on[:, env_of_slot], noise_lev[:, env_of_slot], 0.0)
    sl_slot = sine_lev[:, env_of_slot]
    tone = sl_slot * (
        np.array(_SINE_RE)[sine_idx][None, :]
        + 1j * parity[:, None] * np.array(_SINE_IM)[sine_idx][None, :]
    )
    y[sbx : sbx + width, ts0:ts1] = (
        g_slot * high[sbx : sbx + width, ts0:ts1] + nl_slot * ASPX_NOISE[noise_idx] + tone
    )
    if t.size:
        ch.noise_cursor = int(noise_idx[-1, -1])
        ch.sine_cursor = int(sine_idx[-1])
    # Clause 5.7.6.5: the low band delayed, the extension above it, waveform coding interleaved
    out = np.zeros((SUBBANDS, slots), dtype=complex)
    out[:sbx] = delayed[:sbx]
    out[sbx:] = y[sbx:, :slots]
    for g, used in enumerate(f.fic):
        if used:
            lo, hi = nb.high[g], nb.high[g + 1]
            out[lo:hi] += delayed[lo:hi]
    for n, used in enumerate(f.tic):
        if used:
            out[sbx:, n * k : (n + 1) * k] = delayed[sbx:, n * k : (n + 1) * k]
    # what the next interval needs
    ch.q_prev = q.copy()
    ch.y_prev = y
    ch.prev_num_env = n_env
    ch.prev_freq_res = list(f.freq_res)
    ch.prev_tsg_ptr = f.tsg_ptr
    ch.prev_tna = list(f.tna)
    ch.prev_chirp = np.zeros(8)
    ch.prev_chirp[: len(chirps)] = chirps
    ch.prev_sine = np.zeros(SUBBANDS, dtype=bool)
    ch.prev_sine[:width] = sine[:, -1]
    ch.master_reset = False
    ch.first = False
    return out


@dataclass(slots=True)
class Cursor:
    """Where a channel's noise and sine table indices stand (pseudocodes 103 and 105).

    They run on across I-frames, so a decoder started on a later one takes them
    from `advance`d copies that went through every frame before.
    """

    noise: int = 0
    sine: int = 0
    first: bool = True
    reset: bool = True

    def advance(self, width: int, slots: int) -> None:
        """One A-SPX interval of `slots` QMF slots over `width` subbands, as `process` steps."""
        if slots:
            self.noise = ((0 if self.reset else self.noise) + width * slots) % 512
            self.sine = ((1 if self.first else (self.sine + 1) % 4) + slots - 1) % 4
        self.first = self.reset = False

    def restore(self, ch: AspxChannel) -> None:
        ch.noise_cursor, ch.sine_cursor = self.noise, self.sine
        ch.first, ch.master_reset = self.first, self.reset


def interval(ch: AspxChannel, f: AspxFrame) -> tuple[int, int]:
    """The subbands and QMF slots `process` fills from noise and sines for this interval."""
    return ch.bands.width, (f.sig_borders[-1] - f.sig_borders[0]) * ch.ts_in_ats


def decode_channels(
    pairs: list[tuple[AspxChannel, AspxFrame]], qs: list[np.ndarray]
) -> list[np.ndarray]:
    """Envelopes decoded (jointly for a balanced pair), then each channel extended."""
    quantized = [(ch, f, *_quantized(ch, f)) for ch, f in pairs]
    scales = _dequantize(quantized)
    out = []
    for (ch, f, sig, noise), (sig_scf, noise_scf), q in zip(quantized, scales, qs, strict=True):
        out.append(process(ch, f, q, sig_scf, noise_scf))
        ch.prev_sig = sig[-1]
        ch.prev_noise = noise[-1]
        ch.prev_num_noise = f.num_noise
    return out


def skip_channel(ch: AspxChannel, q: np.ndarray) -> np.ndarray:
    """A channel without A-SPX data this frame: the same delay, nothing above the crossover."""
    history = np.concatenate([ch.q_prev, q], axis=1)
    ch.q_prev = q.copy()
    return history[:, ch.slots - ch.hfgen : 2 * ch.slots - ch.hfgen]
