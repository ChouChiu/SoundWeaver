"""Short-time Fourier transform and its inverse, the one spectral implementation in the tree."""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy.fft import irfft, rfft
from scipy.signal import get_window


@lru_cache(maxsize=16)
def _hann_window(size: int, dtype_string: str) -> np.ndarray:
    return get_window("hann", size, fftbins=True).astype(np.dtype(dtype_string))


def _frames(values: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """View `values` as the frames an STFT reads, without copying any of them."""
    stride = values.strides[0]
    count = (values.size - n_fft) // hop + 1
    return np.lib.stride_tricks.as_strided(
        values, shape=(count, n_fft), strides=(hop * stride, stride), writeable=False
    )


def stft(signal: np.ndarray, n_fft: int = 2048, hop: int = 512) -> np.ndarray:
    """Centred STFT, reflect-padded by half a frame at each end, as [frames, bins]."""
    source = np.asarray(signal)
    real_dtype = np.dtype(np.float32 if source.dtype == np.float32 else np.float64)
    values = np.asarray(source, dtype=real_dtype)
    padded = np.pad(values, (n_fft // 2, n_fft // 2), mode="reflect")
    remainder = (padded.size - n_fft) % hop
    if remainder:
        padded = np.pad(padded, (0, hop - remainder), mode="reflect")
    # Stride straight to the hop positions.  Framing every offset and keeping
    # every `hop`-th row afterwards is the same result, but the intermediate is
    # `hop` times taller for nothing.
    frames = _frames(padded, n_fft, hop)
    window = _hann_window(n_fft, real_dtype.str)
    return rfft(frames * window, axis=1, workers=-1)


def _overlap_add(frames: np.ndarray, hop: int) -> np.ndarray:
    """Sum frames placed `hop` apart.

    Each frame is cut into hop-sized blocks and every block offset is added in
    one array operation, latest frame first, which is the order a loop over
    the frames adds them to any one output block.
    """
    count, size = frames.shape
    if size % hop:
        raise ValueError("the frame length must be a multiple of the hop")
    taps = size // hop
    blocks = frames.reshape(count, taps, hop)
    output = np.zeros((count + taps - 1, hop), dtype=frames.dtype)
    for tap in reversed(range(taps)):
        output[tap : tap + count] += blocks[:, tap]
    return output.reshape(-1)


@lru_cache(maxsize=16)
def _window_sum(count: int, n_fft: int, hop: int, dtype_string: str) -> np.ndarray:
    squared = _hann_window(n_fft, dtype_string) ** 2
    return _overlap_add(np.broadcast_to(squared, (count, n_fft)).copy(), hop)


def istft(spectra: np.ndarray, hop: int = 512, length: int | None = None) -> np.ndarray:
    """Inverse of `stft`, overlap-added and window-normalised; `length` trims or pads the result."""
    frames = np.asarray(spectra)
    real_dtype = np.dtype(np.float32 if frames.dtype == np.complex64 else np.float64)
    n_fft = 2 * (frames.shape[1] - 1)
    window = _hann_window(n_fft, real_dtype.str)
    output = _overlap_add(irfft(frames, n=n_fft, axis=1, workers=-1) * window, hop)
    normalizer = _window_sum(frames.shape[0], n_fft, hop, real_dtype.str)
    valid = normalizer > np.finfo(real_dtype).eps
    output[valid] /= normalizer[valid]
    output = output[n_fft // 2 :]
    if length is None:
        return output[: hop * (frames.shape[0] - 1)]
    if output.size < length:
        output = np.pad(output, (0, length - output.size))
    return output[:length]
