"""The 64-band complex QMF bank of TS 103 420 clause 7, vectorised over time slots.

Complex products are done as real matrix products: a complex array is read as
interleaved real and imaginary parts (`view(np.float64)`), so each transform
is a single BLAS call over every channel and slot at once.
"""

from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .tables import QWIN

BANDS = 64
LENGTH = 640
# Analysis followed by synthesis delays the signal by this many samples.
DELAY = LENGTH - BANDS + 1
# synthesis weights the last ten slots, each by a 64-sample phase of the prototype
_TAPS = LENGTH // BANDS

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

# u (128 reals) -> Q, its output columns alternating real and imaginary parts
_ANALYSIS_REAL = np.empty((2 * BANDS, 2 * BANDS))
_ANALYSIS_REAL[:, 0::2], _ANALYSIS_REAL[:, 1::2] = _ANALYSIS.T.real, _ANALYSIS.T.imag
# Q read as 128 interleaved reals -> v
_SYNTHESIS_REAL = np.empty((2 * BANDS, 2 * BANDS))
_SYNTHESIS_REAL[0::2], _SYNTHESIS_REAL[1::2] = _SYNTHESIS.T.real, -_SYNTHESIS.T.imag
# The analysis window reads x[64 t : 64 t + 640] backwards; the reversed prototype absorbs that.
_WINDOW_REVERSED = QWIN[::-1].copy()
# pcm[t] = sum over k of V[t - k][half k % 2] * QWIN[64 k : 64 k + 64], split by the half
# read; the window of slots t .. t + 9 has slot t - k at position 9 - k
_PHASES = QWIN.reshape(_TAPS, BANDS)
_UPPER_PHASES = _PHASES[_TAPS - 1 :: -2].copy()  # k = 9, 7, 5, 3, 1
_LOWER_PHASES = _PHASES[_TAPS - 2 :: -2].copy()  # k = 8, 6, 4, 2, 0


class Analysis:
    """Real samples in, complex (slots, 64) subband samples out; keeps its history."""

    def __init__(self, channels: int):
        self.history = np.zeros((channels, LENGTH - BANDS))

    def __call__(self, pcm: np.ndarray) -> np.ndarray:
        """pcm: (channels, 64 * slots) -> (channels, slots, 64)."""
        x = np.concatenate([self.history, pcm], axis=1)
        self.history = x[:, -(LENGTH - BANDS) :]
        z = sliding_window_view(x, LENGTH, axis=1)[:, ::BANDS] * _WINDOW_REVERSED
        channels, slots = z.shape[:2]
        # the folded state vector, reversed back into the spec's order
        u = z.reshape(channels * slots, LENGTH // (2 * BANDS), 2 * BANDS).sum(axis=1)[:, ::-1]
        return (u @ _ANALYSIS_REAL).view(np.complex128).reshape(channels, slots, BANDS)


class Synthesis:
    """Complex (slots, 64) subband samples in, real samples out; keeps its history."""

    def __init__(self, channels: int):
        # the last nine slots' 128-sample vectors, oldest first
        self.history = np.zeros((channels, _TAPS - 1, 2 * BANDS))

    def __call__(self, q: np.ndarray) -> np.ndarray:
        """q: (channels, slots, 64) -> (channels, 64 * slots)."""
        channels, slots = q.shape[:2]
        v = np.ascontiguousarray(q).view(np.float64).reshape(-1, 2 * BANDS) @ _SYNTHESIS_REAL
        full = np.concatenate([self.history, v.reshape(channels, slots, 2 * BANDS)], axis=1)
        self.history = full[:, slots:]
        windows = sliding_window_view(full, _TAPS, axis=1).reshape(channels, slots, 2, BANDS, _TAPS)
        out = np.einsum("ctnm,mn->ctn", windows[:, :, 1, :, 0::2], _UPPER_PHASES)
        out += np.einsum("ctnm,mn->ctn", windows[:, :, 0, :, 1::2], _LOWER_PHASES)
        return out.reshape(channels, -1)
