"""Stereo monitoring for MPEG-H when solo or mute bypasses the native stereo render."""

from __future__ import annotations

import numpy as np

from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.mpegh import CicpLayout
from soundweaver.core.pcm import BLOCK_FRAMES, AudioData

_MONITOR_CEILING = 10 ** (-0.5 / 20)


def cicp_fold_matrix(layout: CicpLayout, channels: int) -> np.ndarray:
    """Use CICP positions to keep left and right surrounds on the correct side."""
    if channels == 1:
        return np.ones((1, 2), dtype=np.float32)
    if layout.index == 2 and channels == 2:
        return np.eye(2, dtype=np.float32)

    matrix = np.zeros((channels, 2), dtype=np.float32)
    for i, speaker in enumerate(layout.speakers[:channels]):
        if speaker.is_lfe:
            continue
        azimuth = speaker.azimuth_deg
        if azimuth == 0 or abs(azimuth) == 180:
            matrix[i] = 0.7071
        else:
            matrix[i, int(azimuth < 0)] = (
                1.0 if speaker.name in {"L", "R", "Lc", "Rc", "Lv", "Rv"} else 0.7071
            )
    return matrix


def fold_peak_bound(audio: AudioData, matrix: np.ndarray, token: CancellationToken) -> float:
    """Find the worst sample peak over all possible mute/solo combinations."""
    peak = 0.0
    weights = np.abs(matrix)
    for start in range(0, audio.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        block = audio.samples[:, start : start + BLOCK_FRAMES].T
        # Keeping only the positive or only the negative contributions yields
        # the largest possible magnitude for each sample and output side.
        total = block @ matrix
        magnitude = np.abs(block) @ weights
        bound = (magnitude + np.abs(total)) * 0.5
        peak = max(peak, float(np.max(bound)))
    return peak


def stereo_peak(audio: AudioData, token: CancellationToken) -> float:
    peak = 0.0
    for start in range(0, audio.frames, BLOCK_FRAMES):
        token.raise_if_cancelled()
        peak = max(peak, float(np.max(np.abs(audio.samples[:, start : start + BLOCK_FRAMES]))))
    return peak


def monitor_gain(volume: float, peak: float) -> float:
    """Keep a fixed gain for the track, capped before full scale."""
    return min(volume, _MONITOR_CEILING / peak) if peak > 0 else volume


def cap_stream_peak(stereo: np.ndarray, volume: float) -> np.ndarray:
    """Linked soft knee over -0.92 dBFS for stereo whose full-track peak is unknown."""
    stereo = stereo * volume
    peak = np.max(np.abs(stereo), axis=1)
    knee = 0.9
    over = peak > knee
    width = _MONITOR_CEILING - knee
    limited = knee + width * np.tanh((peak[over] - knee) / width)
    stereo[over] *= (limited / peak[over])[:, None]
    return stereo
