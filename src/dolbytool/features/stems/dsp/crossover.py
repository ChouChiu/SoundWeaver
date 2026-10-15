"""The complementary crossover the band stems are split at.

Two independent filters do not add back up to what they were split from: a
Butterworth low-pass and a Butterworth high-pass at the same corner leave a dip
around it and a phase error between the pair, so the band stems of a song do
not reconstruct the song.  Here the low band is a second-order Butterworth
run forward and then backward: zero phase, with the magnitude of a
Linkwitz-Riley LR4 (-6 dB at the corner, 24 dB per octave).  Every other band
is a difference of such low-passes; the complement of a zero-phase |B|^2 is
the matching |B_hp|^2, so the high band falls at 24 dB per octave too, and
the bands add back up to the input sample for sample with no delay between
them.
"""

from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfilt, sosfilt_zi

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.pcm import BLOCK_FRAMES, AudioData

# Order of the Butterworth run in each direction; the pair doubles the slope.
ORDER = 2


def lowpass_sections(sample_rate: int, cutoff_hz: float) -> np.ndarray:
    """The second-order sections of the Butterworth low-pass run in each direction."""
    nyquist = sample_rate / 2.0
    # A corner at or above Nyquist describes no filter, and scipy rejects it:
    # a low sample rate must not fail a crossover that is valid at 48 kHz.
    corner = min(cutoff_hz, nyquist * 0.99)
    return butter(ORDER, corner, "lowpass", fs=sample_rate, output="sos")


def lowpass(
    source: AudioData,
    destination: AudioData,
    cutoff_hz: float,
    token: CancellationToken,
) -> None:
    """Zero-phase low-pass every channel of `source` into `destination`, in blocks.

    The forward pass runs front to back and the backward pass back to front
    over what it wrote, each carrying the filter state across block
    boundaries, so the result is the same as filtering the whole recording in
    one call without holding it in memory.  Each pass starts in the steady
    state of its first sample, so an edge that is not silent does not ring.
    """
    sections = lowpass_sections(source.sample_rate, cutoff_hz)
    steady = sosfilt_zi(sections)[:, None, :]
    frames = source.frames
    starts = range(0, frames, BLOCK_FRAMES)
    state = None
    for start in starts:
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, frames)
        block = np.asarray(source.samples[:, start:end], dtype=np.float64)
        if state is None:
            state = steady * block[None, :, :1]
        filtered, state = sosfilt(sections, block, axis=1, zi=state)
        destination.samples[:, start:end] = filtered
    state = None
    for start in reversed(starts):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, frames)
        block = np.asarray(destination.samples[:, start:end][:, ::-1], dtype=np.float64)
        if state is None:
            state = steady * block[None, :, :1]
        filtered, state = sosfilt(sections, block, axis=1, zi=state)
        destination.samples[:, start:end] = filtered[:, ::-1]


def difference(
    destination: AudioData,
    minuend: AudioData,
    subtrahend: AudioData,
    token: CancellationToken,
) -> None:
    """Write `minuend - subtrahend` into `destination`, which may be either of them."""
    frames = destination.frames
    for start in range(0, frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, frames)
        destination.samples[:, start:end] = (
            minuend.samples[:, start:end] - subtrahend.samples[:, start:end]
        )
