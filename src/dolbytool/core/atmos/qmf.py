"""The 64-band complex QMF bank of TS 103 420 clause 7, vectorised over time slots."""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .tables import QWIN

BANDS = 64
LENGTH = 640
# Analysis followed by synthesis delays the signal by this many samples.
DELAY = LENGTH - BANDS + 1

_j = np.arange(2 * BANDS)
_k = np.arange(BANDS)
# Q = M . u (clause 7.2 step 5)
_ANALYSIS = np.exp(1j * np.pi * (_k[:, None] + 0.5) * (_j[None, :] - 0.5) / BANDS)
# v = Re(N . Q) (clause 7.3 step 2), with the 1/n folded in.  The text of the
# spec reads (2j - 2n - 1) here, which does not invert the analysis above;
# (2j - 4n + 1) does, to within rounding, with the delay of DELAY samples.
_SYNTHESIS = (
    np.exp(1j * np.pi / (4 * BANDS) * (2 * _k[None, :] + 1) * (2 * _j[:, None] - 4 * BANDS + 1))
    / BANDS
)


class Analysis:
    """Real samples in, complex (slots, 64) subband samples out; keeps its history."""

    def __init__(self, channels: int):
        self.history = np.zeros((channels, LENGTH - BANDS))

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        """pcm: (channels, 64 * slots) -> (channels, slots, 64)."""
        x = np.concatenate([self.history, pcm], axis=1)
        self.history = x[:, -(LENGTH - BANDS) :]
        # Window t holds x[64 t : 64 t + 640]; the state buffer is that window reversed.
        windows = sliding_window_view(x, LENGTH, axis=1)[:, ::BANDS, ::-1]
        z = windows * QWIN
        u = z.reshape(*z.shape[:2], LENGTH // (2 * BANDS), 2 * BANDS).sum(axis=2)
        return u @ _ANALYSIS.T


class Synthesis:
    """Complex (slots, 64) subband samples in, real samples out; keeps its history."""

    def __init__(self, channels: int):
        # the last nine slots' 128-sample vectors, oldest first
        self.history = np.zeros((channels, LENGTH // BANDS - 1, 2 * BANDS))

    def __call__(self, q: np.ndarray) -> np.ndarray:
        """q: (channels, slots, 64) -> (channels, 64 * slots)."""
        v = (q @ _SYNTHESIS.T).real  # (channels, slots, 128)
        slots = v.shape[1]
        full = np.concatenate([self.history, v], axis=1)
        self.history = full[:, -(LENGTH // BANDS - 1) :]
        out = np.zeros((v.shape[0], slots, BANDS))
        base = LENGTH // BANDS - 1
        for j in range(LENGTH // (2 * BANDS)):
            # pcm[t] += V[t-2j][:64] * QWIN[128j:128j+64] + V[t-2j-1][64:] * QWIN[128j+64:128j+128]
            even = full[:, base - 2 * j : base - 2 * j + slots, :BANDS]
            odd = full[:, base - 2 * j - 1 : base - 2 * j - 1 + slots, BANDS:]
            out += (
                even * QWIN[128 * j : 128 * j + BANDS] + odd * QWIN[128 * j + BANDS : 128 * (j + 1)]
            )
        return out.reshape(v.shape[0], -1)
