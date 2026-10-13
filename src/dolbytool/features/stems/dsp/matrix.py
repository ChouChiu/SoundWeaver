"""The channel matrices a split is built from: M/S, discrete channels, downmix.

Every function here writes one stem into a buffer the caller owns, in blocks,
so a long recording never becomes a resident array.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence

import numpy as np

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.pcm import BLOCK_FRAMES, AudioData

logger = logging.getLogger(__name__)

# Every layout of `core.speakers` starts this way, as WAVE_FORMAT_EXTENSIBLE
# orders it; the rest of its channels are found by name.
FRONT_LEFT, FRONT_RIGHT, FRONT_CENTER, LOW_FREQUENCY = range(4)
# channel indices on each side of the room, (left, right)
Sides = tuple[Sequence[int], Sequence[int]]
# ITU-R BS.775 folds the centre and the surrounds in at -3 dB.
DOWNMIX_COEFFICIENT = 0.70710678
# A 5.1 room plays the LFE 10 dB above a main channel at the same level.  Split
# over two speakers that add coherently at those frequencies, that is +4 dB in
# each, so the fold-down keeps the sub the master was mixed to have.
LFE_DOWNMIX_COEFFICIENT = 10.0 ** (10.0 / 20.0) / 2.0
# The band the LFE is compared with the mains in, and how far apart the two
# may be.  An encoder band-limits the LFE on its own, and the filter delays it
# against the mains - 3.7 ms on an E-AC-3 master, which puts 100 Hz, the body
# of a kick, in opposite phase, so adding it as it stands cancels the punch.
LFE_BAND_HZ = (20.0, 120.0)
LFE_MAX_LAG_SECONDS = 0.02
# Long enough to resolve the LFE band in a few bins; one block holds sixteen.
_LAG_FRAME = 16_384
# The level a stem is held under, so a stem that would pass full scale is
# attenuated rather than clipped by the writer.
CEILING = 0.999


def channel_peaks(source: AudioData, token: CancellationToken) -> np.ndarray:
    """The absolute peak of each channel, in one streaming pass."""
    peaks = np.zeros(source.channels, dtype=np.float64)
    for start in range(0, source.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, source.frames)
        block = np.abs(np.asarray(source.samples[:, start:end], dtype=np.float32))
        np.maximum(peaks, block.max(axis=1), out=peaks)
    return peaks


def apply_gain(audio: AudioData, gain: float, token: CancellationToken) -> None:
    """Scale a stem in place, in blocks."""
    for start in range(0, audio.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, audio.frames)
        audio.samples[:, start:end] *= gain


def fit_group(stems: Sequence[AudioData], token: CancellationToken) -> float:
    """Hold a group of stems that add back up to one signal under full scale.

    Normalising each stem on its own would break the decomposition: adding
    them would no longer return the mix.  A group is scaled by one shared
    factor or not at all, so the sum still holds.  A stem outside any group is
    left at unity, with the writer's clipping warning behind it.
    """
    peak = max(float(channel_peaks(stem, token).max()) for stem in stems)
    if peak <= CEILING:
        return 1.0
    gain = CEILING / peak
    logger.info("holding %d stems %.2f dB down to fit", len(stems), 20.0 * math.log10(gain))
    for stem in stems:
        apply_gain(stem, gain, token)
    return gain


def copy_channels_into(
    destination: AudioData,
    source: AudioData,
    indices: tuple[int, ...],
    token: CancellationToken,
) -> None:
    """Take discrete channels out of a surround master, in the order given."""
    for start in range(0, destination.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, destination.frames)
        for row, index in enumerate(indices):
            destination.samples[row, start:end] = source.samples[index, start:end]


def mid_into(destination: AudioData, stereo: AudioData, token: CancellationToken) -> None:
    """(L + R) / 2: the centred content, as a mono stem rather than a doubled one."""
    for start in range(0, destination.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, destination.frames)
        block = stereo.samples[:2, start:end]
        destination.samples[0, start:end] = 0.5 * (block[0] + block[1])


def side_into(destination: AudioData, stereo: AudioData, token: CancellationToken) -> None:
    """(L - R) / 2, written as the mono signal it is.

    Written as `[side, -side]` it would be two channels in opposite phase that
    collapse to silence the moment anything folds them to mono.
    """
    for start in range(0, destination.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, destination.frames)
        block = stereo.samples[:2, start:end]
        destination.samples[0, start:end] = 0.5 * (block[0] - block[1])


def lfe_lag(source: AudioData, token: CancellationToken) -> int:
    """How many samples the LFE runs behind the low end of the front pair.

    A cross-correlation over the whole recording, restricted to the band the
    LFE carries: the cross-spectrum is summed frame by frame, so the file is
    read once and never held.
    """
    rate = source.sample_rate
    frequencies = np.fft.rfftfreq(_LAG_FRAME, 1.0 / rate)
    band = (frequencies >= LFE_BAND_HZ[0]) & (frequencies <= LFE_BAND_HZ[1])
    window = np.hanning(_LAG_FRAME)
    cross = np.zeros(frequencies.size, dtype=np.complex128)
    usable = source.frames - source.frames % _LAG_FRAME
    for start in range(0, usable, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, usable)
        block = np.asarray(source.samples[:, start:end], dtype=np.float64)
        mains = (0.5 * (block[FRONT_LEFT] + block[FRONT_RIGHT])).reshape(-1, _LAG_FRAME)
        lfe = block[LOW_FREQUENCY].reshape(-1, _LAG_FRAME)
        cross += np.sum(
            np.fft.rfft(lfe * window, axis=1) * np.conj(np.fft.rfft(mains * window, axis=1)),
            axis=0,
        )
    if not np.any(cross[band]):
        return 0
    # Index k holds how well the LFE k samples on matches the mains now.
    correlation = np.fft.irfft(np.where(band, cross, 0.0), _LAG_FRAME)
    reach = int(LFE_MAX_LAG_SECONDS * rate)
    lags = np.arange(-reach, reach + 1)
    lag = int(lags[np.argmax(correlation[lags])])
    logger.info("the LFE runs %.2f ms behind the front pair", 1000.0 * lag / rate)
    return lag


def _shifted(row: np.ndarray, start: int, end: int, lag: int) -> np.ndarray:
    """`row[start + lag : end + lag]`, with silence where that runs off either end."""
    out = np.zeros(end - start, dtype=np.float32)
    first, last = max(start + lag, 0), min(end + lag, row.shape[0])
    if first < last:
        out[first - start - lag : last - start - lag] = row[first:last]
    return out


def height_into(
    destination: AudioData, source: AudioData, heights: Sides, token: CancellationToken
) -> None:
    """Fold the heights to a stereo pair, each at -3 dB when a side has more than one.

    An object panned halfway between two heights comes out at the level it was
    mixed at; the sum can pass full scale, which the caller holds under with
    `fit_group`.  A single pair is the stereo pair as it stands.
    """
    gain = DOWNMIX_COEFFICIENT if len(heights[0]) > 1 else 1.0
    for start in range(0, destination.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, destination.frames)
        block = source.samples[:, start:end]
        for row, side in enumerate(heights):
            destination.samples[row, start:end] = gain * block[list(side)].sum(axis=0)


def downmix_into(
    destination: AudioData,
    source: AudioData,
    sides: Sides,
    token: CancellationToken,
    lfe_lag: int = 0,
) -> None:
    """Fold a surround master to stereo: BS.775 for the mains, plus the LFE.

    Every channel in `sides` - the surrounds, and any backs, wides and heights -
    joins its side at -3 dB.

    BS.775 and a Lo/Ro fold-down leave the LFE out, which in a music master
    takes the kick and the sub with it: the front pair carries only what
    leaked into it, and every band split from the fold-down inherits the hole.
    The stems this feeds are for hearing what the master holds, so the LFE
    goes in at the level a 5.1 room plays it, read `lfe_lag` samples ahead so
    it lands in phase with the mains.  No gain is applied here - `fit_group`
    holds the fold-down and its bands under full scale by their measured peak,
    where a bound from the channel peaks would hold the whole group down for a
    coincidence that never happens.
    """
    _fold_into(destination, source, source.samples[FRONT_CENTER], sides, token, lfe_lag, front=True)


def downmix_instrumental_into(
    destination: AudioData,
    source: AudioData,
    sides: Sides,
    token: CancellationToken,
    lfe_lag: int = 0,
) -> None:
    """Fold a surround master to stereo instrumental (BS.775 without the centre lead vocal)."""
    _fold_into(destination, source, None, sides, token, lfe_lag, front=True)


def surround_into(
    destination: AudioData,
    source: AudioData,
    shared_centre: AudioData,
    surrounds: Sides,
    token: CancellationToken,
    lfe_lag: int = 0,
) -> None:
    """The surrounds (any backs among them) and the LFE, with what of the centre they share.

    The surrounds alone are ambience twenty decibels under the kick, and the
    LFE is where that kick is.  The centre carries the lead and the backing
    vocals in one channel; the backing vocals are the part a mix spreads to
    the rear as well, so the centre comes in as `shared_centre` - its share
    predicted from the surrounds - rather than as it stands.  The front pair
    stays out: it is where the lead leaks.  The sum can pass full scale; the
    caller holds it under with `fit_group`.
    """
    _fold_into(
        destination, source, shared_centre.samples[0], surrounds, token, lfe_lag, front=False
    )


def _fold_into(
    destination: AudioData,
    source: AudioData,
    centre: np.ndarray | None,
    sides: Sides,
    token: CancellationToken,
    lfe_lag: int,
    *,
    front: bool,
) -> None:
    left_side, right_side = list(sides[0]), list(sides[1])
    lfe = source.samples[LOW_FREQUENCY]
    for start in range(0, destination.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        end = min(start + BLOCK_FRAMES, destination.frames)
        block = source.samples[:, start:end]
        shared = LFE_DOWNMIX_COEFFICIENT * _shifted(lfe, start, end, lfe_lag)
        if centre is not None:
            shared = shared + DOWNMIX_COEFFICIENT * centre[start:end]
        left = shared + DOWNMIX_COEFFICIENT * block[left_side].sum(axis=0)
        right = shared + DOWNMIX_COEFFICIENT * block[right_side].sum(axis=0)
        if front:
            left += block[FRONT_LEFT]
            right += block[FRONT_RIGHT]
        destination.samples[0, start:end] = left
        destination.samples[1, start:end] = right

