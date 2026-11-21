"""Audio Vivid speaker mapping, object gain interpolation and VVM2 telemetry."""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

_SPEAKER_ANGLES = {
    "L": (-30, 0), "R": (30, 0), "C": (0, 0), "LFE": (0, 0),
    "Ls": (-110, 0), "Rs": (110, 0), "Lrs": (-150, 0), "Rrs": (150, 0),
    "Lw": (-60, 0), "Rw": (60, 0), "Ltf": (-30, 35), "Rtf": (30, 35),
    "Ltm": (-90, 45), "Rtm": (90, 45), "Ltr": (-135, 35), "Rtr": (135, 35),
    "Cs": (180, 0), "Ts": (0, 90),
}
_FOLD_GAIN = np.float32(0.70710678)


def _direction(name: str) -> tuple[float, float, float]:
    az, el = map(math.radians, _SPEAKER_ANGLES[name])
    return math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)


def speaker_matrix(source: tuple[str, ...], target_order: str) -> np.ndarray:
    target = tuple(target_order.replace(",", " ").split())
    if not target or any(name not in _SPEAKER_ANGLES for name in target):
        raise ValueError("Invalid Audio Vivid target speaker layout.")
    if len(set(target)) != len(target):
        raise ValueError("Duplicate Audio Vivid target speaker.")
    if target != ("C",) and any(name not in source for name in target):
        raise ValueError("Target layout adds speakers absent from the Audio Vivid source.")

    matrix = np.zeros((len(source), len(target)), np.float32)
    for channel, name in enumerate(source):
        if name in target:
            matrix[channel, target.index(name)] = 1
        elif target == ("L", "R"):
            azimuth, _ = _SPEAKER_ANGLES[name]
            if name == "LFE":
                matrix[channel] = 0.5
            elif azimuth < 0:
                matrix[channel, 0] = _FOLD_GAIN
            elif 0 < azimuth < 180:
                matrix[channel, 1] = _FOLD_GAIN
            else:
                matrix[channel] = _FOLD_GAIN
        elif name != "LFE":
            candidates = [index for index, dest in enumerate(target) if dest != "LFE"]
            if candidates:
                direction = _direction(name)
                nearest = max(
                    candidates,
                    key=lambda index: sum(a * b for a, b in zip(direction, _direction(target[index]), strict=False)),
                )
                matrix[channel, nearest] = _FOLD_GAIN
    return matrix


def bed_positions(names: Sequence[str]) -> np.ndarray:
    positions = np.array(
        [(0.0, 1.0, 0.0) if name.startswith("ACN") else _direction(name) for name in names],
        dtype=np.float64,
    ).reshape(-1, 3)
    positions[:, 2] = (positions[:, 2] + 1) * 0.5
    return positions.astype(np.float32)


def apply_object_ramp(
    pcm: np.ndarray, metadata: np.ndarray, bed_channels: int, previous: np.ndarray | None,
) -> tuple[np.ndarray, np.ndarray]:
    target = metadata[:, 3]
    if not np.all(np.isfinite(target[bed_channels:])):
        raise ValueError("Invalid Audio Vivid object gain.")
    output = pcm * target
    if previous is not None and bed_channels < len(target):
        previous = previous[bed_channels:]
        current = target[bed_channels:]
        start = np.where(metadata[bed_channels:, 4] != 0, current, previous)
        fraction = np.arange(1, len(pcm) + 1, dtype=np.float32)[:, None] / len(pcm)
        output[:, bed_channels:] = pcm[:, bed_channels:] * (start + (current - start) * fraction)
    return output, target
