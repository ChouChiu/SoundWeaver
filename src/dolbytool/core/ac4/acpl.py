"""Advanced coupling, A-CPL (TS 103 190-1 clauses 4.2.13 and 5.7.7).

QMF matrices are indexed [subband, time slot].  A `Module` holds the state of
one coupling module (its decorrelator, transient ducker and the parameters the
next frame interpolates from); `read_config_*` and `read_data_*` follow the
syntax tables.  Not checked against a real A-CPL stream here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.signal import lfilter

from dolbytool.core.atmos.bitstream import BitReader

from .codes import huffman

FULL, PARTIAL = 0, 1
ALPHA, BETA, BETA3, GAMMA = "ALPHA", "BETA", "BETA3", "GAMMA"
SUBBANDS = 64
NUM_BANDS = (15, 12, 9, 7)
# Table 197: the first QMF subband of each band group, and the parameter band per id
_GROUP_START = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 14, 18, 23, 35)
_GROUP_BAND = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14),
    (0, 1, 2, 3, 4, 4, 5, 5, 6, 6, 7, 8, 9, 10, 11),
    (0, 1, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8),
    (0, 1, 2, 2, 3, 3, 3, 3, 4, 4, 4, 5, 5, 6, 6),
)
# Annex A.3: cb_off of the F0, DF and DT codebooks, by data type and coarse
_OFFSETS = {
    (ALPHA, 0): (0, 32, 32), (ALPHA, 1): (0, 16, 16), (BETA, 0): (0, 8, 8), (BETA, 1): (0, 4, 4),
    (BETA3, 0): (0, 16, 16), (BETA3, 1): (0, 8, 8), (GAMMA, 0): (20, 40, 40), (GAMMA, 1): (10, 20, 20),
}  # fmt: skip
# Tables 203 to 206
_ALPHA_FINE = (
    -2.0, -1.809375, -1.6375, -1.484375, -1.35, -1.234375, -1.1375, -1.059375, -1.0,
    -0.940625, -0.8625, -0.765625, -0.65, -0.515625, -0.3625, -0.190625, 0.0,
    0.190625, 0.3625, 0.515625, 0.65, 0.765625, 0.8625, 0.940625, 1.0,
    1.059375, 1.1375, 1.234375, 1.35, 1.484375, 1.6375, 1.809375, 2.0,
)  # fmt: skip
_IBETA_FINE = (
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    7,
    6,
    5,
    4,
    3,
    2,
    1,
    0,
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    7,
    6,
    5,
    4,
    3,
    2,
    1,
    0,
)
_BETA_FINE = np.array([
    [0.0] * 9,
    [0.2375, 0.2035449, 0.1729297, 0.1456543, 0.1217188, 0.101123, 0.0838672, 0.0699512, 0.059375],
    [0.55, 0.4713672, 0.4004688, 0.3373047, 0.281875, 0.2341797, 0.1942188, 0.1619922, 0.1375],
    [0.9375, 0.8034668, 0.6826172, 0.5749512, 0.4804688, 0.3991699, 0.3310547, 0.276123, 0.234375],
    [1.4, 1.199844, 1.019375, 0.8585938, 0.7175, 0.5960938, 0.494375, 0.4123438, 0.35],
    [1.9375, 1.660498, 1.410742, 1.1882319, 0.9929688, 0.8249512, 0.6841797, 0.5706543, 0.484375],
    [2.55, 2.18543, 1.856719, 1.563867, 1.306875, 1.085742, 0.9004688, 0.7510547, 0.6375],
    [3.2375, 2.7746389, 2.357305, 1.985498, 1.659219, 1.378467, 1.143242, 0.9535449, 0.809375],
    [4.0, 3.4281249, 2.9124999, 2.453125, 2.05, 1.703125, 1.4125, 1.178125, 1.0],
])  # fmt: skip
_ALPHA_COARSE = (
    -2.0, -1.6375, -1.35, -1.1375, -1.0, -0.8625, -0.65, -0.3625, 0.0,
    0.3625, 0.65, 0.8625, 1.0, 1.1375, 1.35, 1.6375, 2.0,
)  # fmt: skip
_IBETA_COARSE = (0, 1, 2, 3, 4, 3, 2, 1, 0, 1, 2, 3, 4, 3, 2, 1, 0)
_BETA_COARSE = np.array([
    [0.0] * 5,
    [0.55, 0.4004688, 0.281875, 0.1942188, 0.1375],
    [1.4, 1.019375, 0.7175, 0.494375, 0.35],
    [2.55, 1.856719, 1.306875, 0.9004688, 0.6375],
    [4.0, 2.9124999, 2.05, 1.4125, 1.0],
])  # fmt: skip
# Tables 199 to 201: decorrelator a[i] per region (subbands 0-6, 7-22, 23-63) and decorrelator
_REGIONS = ((0, 7, 7), (7, 23, 10), (23, 64, 12))  # first, end, delay
_DECORRELATORS = (
    (
        (1.0, 0.5306, -0.4533, -0.6248, 0.0424, 0.4237, 0.4311, 0.1688),
        (1.0, 0.5561, -0.3039, -0.5024, -0.1850),
        (1.0, 0.5773, 0.3321),
    ),
    (
        (1.0, -0.4178, 0.1082, -0.2368, -0.1014, -0.1052, -0.3528, 0.4665),
        (1.0, 0.0425, 0.3235, -0.1556, 0.4958),
        (1.0, 0.2327, -0.3901),
    ),
    (
        (1.0, 0.4007, 0.4747, 0.2611, -0.1211, -0.4248, -0.2989, -0.1932),
        (1.0, -0.4361, 0.0345, 0.5215, -0.4178),
        (1.0, -0.6057, 0.3804),
    ),
)
_GAMMA_STEP = (1638 / 16384, 3276 / 16384)
_BETA3_STEP = (0.125, 0.25)


def band_of_subband(bands_id: int) -> np.ndarray:
    """sb_to_pb: the parameter band of every QMF subband (table 197)."""
    group = np.searchsorted(_GROUP_START, np.arange(SUBBANDS), side="right") - 1
    return np.asarray(_GROUP_BAND[bands_id])[group]


# the ducker's 15 bands as a [band, subband] sum
_DUCK_BANDS = (band_of_subband(0)[None, :] == np.arange(15)[:, None]).astype(float)


@dataclass(frozen=True, slots=True)
class AcplConfig:
    bands_id: int
    quant: tuple[int, int]  # quant mode of alpha/beta, and of gamma
    qmf_band: int = 0
    param_band: int = 0

    @property
    def bands(self) -> int:
        return NUM_BANDS[self.bands_id]


def read_config_1ch(b: BitReader, mode: int) -> AcplConfig:
    bands_id, quant = b.read(2), b.read(1)
    if mode == PARTIAL:
        qmf_band = b.read(3) + 1
        return AcplConfig(
            bands_id, (quant, quant), qmf_band, int(band_of_subband(bands_id)[qmf_band])
        )
    return AcplConfig(bands_id, (quant, quant))


def read_config_2ch(b: BitReader) -> AcplConfig:
    bands_id = b.read(2)
    return AcplConfig(bands_id, (b.read(1), b.read(1)))


@dataclass(slots=True)
class AcplData:
    interpolation: int
    sets: int
    timeslots: list[int]
    # per parameter name: (diff types, values) of each set
    params: dict[str, tuple[list[int], list[list[int]]]] = field(default_factory=dict)


def _huff_data(
    b: BitReader, kind: str, quant: int, bands: int, start: int
) -> tuple[int, list[int]]:
    coarse = "COARSE" if quant else "FINE"
    f0, df, dt = _OFFSETS[(kind, quant)]
    values = [0] * bands
    diff = b.read(1)
    if diff == 0:
        values[start] = huffman(f"ACPL_HCB_{kind}_{coarse}_F0").decode(b) - f0
        hcb = huffman(f"ACPL_HCB_{kind}_{coarse}_DF")
        for i in range(start + 1, bands):
            values[i] = hcb.decode(b) - df
    else:
        hcb = huffman(f"ACPL_HCB_{kind}_{coarse}_DT")
        for i in range(start, bands):
            values[i] = hcb.decode(b) - dt
    return diff, values


def _data(b: BitReader, config: AcplConfig, names: tuple[tuple[str, str, int], ...]) -> AcplData:
    interpolation, sets = b.read(1), b.read(1) + 1
    slots = [b.read(5) for _ in range(sets)] if interpolation else []
    data = AcplData(interpolation, sets, slots)
    for name, kind, which in names:
        diffs, values = [], []
        for _ in range(sets):
            diff, v = _huff_data(b, kind, config.quant[which], config.bands, config.param_band)
            diffs.append(diff)
            values.append(v)
        data.params[name] = (diffs, values)
    return data


def read_data_1ch(b: BitReader, config: AcplConfig) -> AcplData:
    return _data(b, config, (("alpha1", ALPHA, 0), ("beta1", BETA, 0)))


def read_data_2ch(b: BitReader, config: AcplConfig) -> AcplData:
    names = [("alpha1", ALPHA, 0), ("alpha2", ALPHA, 0), ("beta1", BETA, 0), ("beta2", BETA, 0),
             ("beta3", BETA3, 0)]  # fmt: skip
    names += [(f"gamma{i}", GAMMA, 1) for i in range(1, 7)]
    return _data(b, config, tuple(names))


class Module:
    """One coupling module's state: decorrelator history, ducker, last quantized and dequantized sets."""

    def __init__(self, decorrelator: int) -> None:
        self.decorrelator = decorrelator
        self.zi = [None, None, None]
        self.peak = np.zeros(15)
        self.smooth = np.zeros(15)
        self.diff = np.zeros(15)
        self.q_prev: dict[str, np.ndarray] = {}
        self.dq_prev: dict[str, np.ndarray] = {}

    def quantized(self, data: AcplData, name: str, bands: int) -> list[np.ndarray]:
        """Pseudocode 121."""
        diffs, values = data.params[name]
        prev = self.q_prev.get(name, np.zeros(bands))
        out = []
        for diff, v in zip(diffs, values):
            v = np.asarray(v, dtype=float)
            q = np.cumsum(v) if diff == 0 else prev[: len(v)] + v
            out.append(q)
            prev = q
        self.q_prev[name] = prev
        return out

    def decorrelate(self, x: np.ndarray) -> np.ndarray:
        """Pseudocode 111: delay then all-pass IIR, per region, the state kept across frames."""
        y = np.zeros_like(x)
        for r, (first, end, delay) in enumerate(_REGIONS):
            a = np.asarray(_DECORRELATORS[self.decorrelator][r])
            b = np.concatenate([np.zeros(delay), a[::-1]])
            if self.zi[r] is None:
                self.zi[r] = np.zeros((end - first, max(len(a), len(b)) - 1), dtype=complex)
            y[first:end], self.zi[r] = lfilter(b, a, x[first:end], axis=1, zi=self.zi[r])
        return y

    def duck(self, u: np.ndarray) -> np.ndarray:
        """Pseudocodes 112 to 114: every slot's band energies at once, then the
        peak held slot by slot and the two smoothers run as one-pole filters."""
        if not u.shape[1]:
            return u.copy()
        energy = _DUCK_BANDS @ (u.real**2 + u.imag**2)  # [band, slot]
        peak = np.empty_like(energy)
        held = self.peak
        for ts in range(energy.shape[1]):
            held = peak[:, ts] = np.maximum(0.76592833836465 * held, energy[:, ts])
        smooth, _ = lfilter([0.25], [1, -0.75], energy, axis=1, zi=0.75 * self.smooth[:, None])
        diff, _ = lfilter([0.25], [1, -0.75], peak - energy, axis=1, zi=0.75 * self.diff[:, None])
        gain = np.where(1.5 * diff > smooth, smooth / (1.5 * (diff + 1e-9)), 1.0)
        self.peak, self.smooth, self.diff = peak[:, -1].copy(), smooth[:, -1], diff[:, -1]
        return u * gain[band_of_subband(0)]


def _interpolate(
    data: AcplData, sets: list[np.ndarray], prev: np.ndarray, bands_id: int, slots: int
) -> np.ndarray:
    """Pseudocode 109 for every subband and slot: [64, slots]; `sets` are per parameter band."""
    pb = band_of_subband(bands_id)
    per_sb = [np.asarray(s)[np.minimum(pb, len(s) - 1)] for s in sets]
    ts = np.arange(slots)[None, :]
    if data.interpolation == 0:
        if len(sets) == 1:
            return prev[:, None] + (ts + 1) * (per_sb[0] - prev)[:, None] / slots
        half = slots // 2
        first = prev[:, None] + (ts + 1) * (per_sb[0] - prev)[:, None] / half
        second = per_sb[0][:, None] + (ts - half + 1) * (per_sb[1] - per_sb[0])[:, None] / (
            slots - half
        )
        return np.where(ts < half, first, second)
    out = np.where(ts < data.timeslots[0], prev[:, None], per_sb[0][:, None])
    if len(sets) == 2:
        out = np.where(ts >= data.timeslots[1], per_sb[1][:, None], out)
    return out


def _alpha_beta(module: Module, data: AcplData, config: AcplConfig, alpha: str, beta: str):
    """Dequantized alpha and beta sets (tables 203 to 206), with their values from the last frame."""
    qa = module.quantized(data, alpha, config.bands)
    qb = module.quantized(data, beta, config.bands)
    coarse = config.quant[0]
    a_tab, i_tab, b_tab, top = (
        (_ALPHA_COARSE, _IBETA_COARSE, _BETA_COARSE, 16) if coarse else (_ALPHA_FINE, _IBETA_FINE, _BETA_FINE, 32)
    )  # fmt: skip
    alphas, betas = [], []
    for a, b in zip(qa, qb):
        ai = np.clip(a, 0, top).astype(int)
        bi = np.clip(b, 0, b_tab.shape[0] - 1).astype(int)
        da, db = np.asarray(a_tab)[ai], b_tab[bi, np.asarray(i_tab)[ai]]
        da[: config.param_band] = 0
        db[: config.param_band] = 0
        alphas.append(da)
        betas.append(db)
    return alphas, betas


def couple(
    module: Module, data: AcplData, config: AcplConfig, x0: np.ndarray, x1: np.ndarray | None,
    names: tuple[str, str] = ("alpha1", "beta1"),
) -> tuple[np.ndarray, np.ndarray]:  # fmt: skip
    """Pseudocodes 115 and 116 for one module: x0 the coupled channel, x1 a residual or None."""
    slots = x0.shape[1]
    x0in = 2 * x0
    x1in = 2 * x1 if x1 is not None else np.zeros_like(x0)
    y = module.duck(module.decorrelate(x0in))
    alphas, betas = _alpha_beta(module, data, config, *names)
    pb = band_of_subband(config.bands_id)
    prev_a = module.dq_prev.get("a", np.zeros(SUBBANDS))
    prev_b = module.dq_prev.get("b", np.zeros(SUBBANDS))
    ia = _interpolate(data, alphas, prev_a, config.bands_id, slots)
    ib = _interpolate(data, betas, prev_b, config.bands_id, slots)
    module.dq_prev["a"] = alphas[-1][np.minimum(pb, len(alphas[-1]) - 1)]
    module.dq_prev["b"] = betas[-1][np.minimum(pb, len(betas[-1]) - 1)]
    z0 = 0.5 * (x0in * (1 + ia) + y * ib)
    z1 = 0.5 * (x0in * (1 - ia) - y * ib)
    low = slice(0, config.qmf_band)
    z0[low] = 0.5 * (x0in[low] + x1in[low])
    z1[low] = 0.5 * (x0in[low] - x1in[low])
    return z0, z1
