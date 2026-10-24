"""IMDCT, windowing and overlap/add with block switching (TS 103 190-1 clause 5.5)."""

from __future__ import annotations

from functools import cache

import numpy as np

# Table 186: the KBD alpha of each transform length at 44.1 or 48 kHz
_ALPHA = {
    2048: 3.0, 1920: 3.0, 1536: 3.0, 1024: 4.0, 960: 4.0, 768: 4.0,
    512: 4.5, 480: 4.5, 384: 4.5, 256: 5.0, 240: 5.0, 192: 5.0, 128: 6.0, 120: 6.0, 96: 6.0,
}  # fmt: skip


@cache
def kbd_left(n: int) -> np.ndarray:
    """KBD_LEFT(N, n) for n < N (clause 5.5.3); KBD_RIGHT(N, m) is this reversed."""
    alpha = _ALPHA[n]
    p = np.arange(n + 1)
    kernel = np.i0(np.pi * alpha * np.sqrt(1.0 - (2.0 * p / n - 1.0) ** 2)) / np.i0(np.pi * alpha)
    return np.sqrt(np.cumsum(kernel)[:n] / kernel.sum())


@cache
def _twiddles(n: int) -> np.ndarray:
    k = np.arange(n // 2)
    return -np.exp(1j * 2 * np.pi * (8 * k + 1) / (16 * n))


@cache
def _left_window(n: int, previous: int) -> np.ndarray:
    nw = min(n, previous)
    skip = (n - nw) // 2
    return np.concatenate([np.zeros(skip), kbd_left(nw), np.ones(skip)])


@cache
def _right_window(n: int, previous: int) -> np.ndarray:
    """The window over the previous block's second half when a block of n follows."""
    nw = min(n, previous)
    skip = (previous - nw) // 2
    return np.concatenate([np.ones(skip), kbd_left(nw)[::-1], np.zeros(skip)])


def unfold(spectrum: np.ndarray) -> np.ndarray:
    """Steps 1 to 5 without the window: N spectral lines to 2N time samples."""
    n = len(spectrum)
    tw = _twiddles(n)
    z = (spectrum[n - 1 :: -2] + 1j * spectrum[0::2]) * tw
    y = np.fft.ifft(z) * (n / 2) * tw / n
    yr, yi = y.real, y.imag
    q = n // 4
    x = np.empty(2 * n)
    i = np.arange(q)
    x[2 * i] = yi[q + i]
    x[2 * i + 1] = -yr[q - i - 1]
    x[n // 2 + 2 * i] = yr[i]
    x[n // 2 + 2 * i + 1] = -yi[n // 2 - i - 1]
    x[n + 2 * i] = yr[q + i]
    x[n + 2 * i + 1] = -yi[q - i - 1]
    x[3 * n // 2 + 2 * i] = -yi[i]
    x[3 * n // 2 + 2 * i + 1] = yr[n // 2 - i - 1]
    return x


class Imdct:
    """One channel's transform state: the overlap buffer and the previous block length."""

    __slots__ = ("frame_len", "overlap", "previous")

    def __init__(self, frame_len: int) -> None:
        self.frame_len = frame_len
        self.overlap = np.zeros(frame_len)
        self.previous = frame_len

    def __call__(self, spectrum: np.ndarray, windows: list[int]) -> np.ndarray:
        """A frame of spectral lines, window after window, to frame_len samples (pseudocode 64)."""
        full, overlap = self.frame_len, self.overlap
        pcm = np.empty(full)
        start = 0
        for n in windows:
            x = unfold(spectrum[start : start + n])
            x[:n] *= _left_window(n, self.previous)
            skip, skip_prev = (full - n) // 2, (full - self.previous) // 2
            overlap[skip_prev : skip_prev + self.previous] *= _right_window(n, self.previous)
            overlap[skip : skip + n] += x[:n]
            pcm[start : start + n] = overlap[:n]
            overlap[:skip] = overlap[n : n + skip].copy()
            overlap[skip : skip + n] = x[n:]
            self.previous = n
            start += n
        return pcm
