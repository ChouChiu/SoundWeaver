"""Device-independent acoustic monitoring, stereo mixing, and metering."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from soundweaver.core.atmos.adm_reader import PreparedADM, adm_to_room
from soundweaver.core.codecs.av3a import VvmTimelineHeader
from soundweaver.core.codecs.mpegh import CicpLayout
from soundweaver.core.speakers import SpeakerLayout

SILENCE_DB = -96.0
_MONITOR_CEILING = 10 ** (-0.5 / 20)


@dataclass(frozen=True, slots=True)
class ObjectSnapshot:
    index: int
    x: float
    y: float
    z: float
    level_db: float
    active: bool
    size: float = 0.0


def fold_matrix(channel_names: tuple[str, ...] | list[str]) -> np.ndarray:
    """Stereo fold-down matrix of shape `(len(channel_names), 2)`."""
    n = len(channel_names)
    mat = np.zeros((n, 2), dtype=np.float32)
    for i, name in enumerate(channel_names):
        if name in ("LFE", "LFE2"):
            continue
        if name == "ACN0":
            mat[i, 0] = 0.7071
            mat[i, 1] = 0.7071
            continue
        if name == "ACN1":  # Y axis (+left / -right) -> stereo difference
            mat[i, 0] = 0.7071
            mat[i, 1] = -0.7071
            continue
        if name.startswith("ACN"):
            continue
        low = name.lower()
        if name in ("C", "Cb", "Cv", "Cvr", "Cs", "Ts", "Tfc", "Tsc", "Tbc", "Bfc"):
            mat[i, 0] = 0.7071
            mat[i, 1] = 0.7071
        elif low.startswith(("l", "tl", "tf", "ts", "tb", "bf")) and "r" not in low:
            mat[i, 0] = 1.0 if name in ("L", "Lfc", "Lc", "Lv") else 0.7071
        elif low.startswith(("r", "tr", "bf")) or "r" in low:
            mat[i, 1] = 1.0 if name in ("R", "Rfc", "Rc", "Rv") else 0.7071
        else:
            mat[i, 0] = 0.5
            mat[i, 1] = 0.5
    return mat


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


def _peak_dbfs(peak: float) -> float:
    return max(SILENCE_DB, 20.0 * math.log10(peak)) if peak > 1e-5 else SILENCE_DB


def peaks_dbfs(block: np.ndarray) -> list[float]:
    """Per-channel peak for a non-empty frame block."""
    return [_peak_dbfs(float(peak)) for peak in np.max(np.abs(block), axis=0)]


def _audible(index: int, muted: set[int], soloed: set[int]) -> bool:
    return index not in muted and (not soloed or index in soloed)


def channel_mask(channels: int, muted: set[int], soloed: set[int]) -> np.ndarray:
    return np.fromiter(
        (_audible(i, muted, soloed) for i in range(channels)),
        dtype=np.float32,
        count=channels,
    )


def mix_adm_block(
    adm: PreparedADM,
    layout: SpeakerLayout,
    start: int,
    frames: int,
    muted: set[int],
    soloed: set[int],
) -> tuple[np.ndarray, list[ObjectSnapshot]]:
    # the playback decode keeps rewriting the axml as its scan grows; pick up new blocks
    adm.refresh()
    raw = adm.read_frames(start, frames)
    time_s = (start + frames * 0.5) / adm.sample_rate
    bed = np.zeros((frames, len(layout.channels)), dtype=np.float32)
    objects: list[ObjectSnapshot] = []
    for track in adm.tracks:
        channel = raw[:, track.channel_index]
        if track.is_object:
            index = len(objects)
            active = _audible(index, muted, soloed)
            point = adm.block_at(track, time_s)
            pos = (point.x, point.y, point.z) if point else (0.5, 0.5, 0.0)
            level = _peak_dbfs(float(np.max(np.abs(channel)))) if active else SILENCE_DB
            size = point.size_at(time_s) if point else 0.0
            objects.append(ObjectSnapshot(index, *pos, level, active, size))
        else:
            active = not soloed
        if active:
            gains = adm.track_gains_at(track, time_s, layout)
            if np.any(gains):
                bed += channel[:, None] * gains
    return bed, objects


def vivid_snapshots(
    header: VvmTimelineHeader,
    row: np.ndarray,
    muted: set[int],
    soloed: set[int],
) -> list[ObjectSnapshot]:
    points = row[header.bed_channels : header.bed_channels + header.object_count]
    objects: list[ObjectSnapshot] = []
    for index, point in enumerate(points):
        x, y, z, peak = point[:4]
        position = adm_to_room(float(x), float(y), float(z))
        active = _audible(index, muted, soloed)
        level = _peak_dbfs(float(peak)) if active else SILENCE_DB
        size = max(0.0, min(float(point[5]), 1.0)) if header.fields > 5 else 0.0
        objects.append(ObjectSnapshot(index, *position, level, active, size))
    return objects


def stereo_mix(
    bed: np.ndarray,
    mask: np.ndarray,
    fold: np.ndarray,
    cicp_fold: np.ndarray | None,
    streamed: np.ndarray | None,
    volume: float,
) -> np.ndarray:
    """Return device PCM from the masked bed or the available native stereo render."""
    channels = bed.shape[1]
    gain = volume
    if streamed is not None:
        return cap_stream_peak(streamed, gain)
    if cicp_fold is not None:
        stereo = bed @ cicp_fold
        if channels <= 2:
            return cap_stream_peak(stereo, gain)
        bound = float((np.abs(cicp_fold) * mask[:, None]).sum(axis=0).max())
        gain = monitor_gain(gain, bound)
    elif channels == 1:
        stereo = np.repeat(bed, 2, axis=1)
    elif channels > 2 and channels == fold.shape[0]:
        stereo = bed @ fold
    else:
        stereo = bed[:, :2]
    return np.clip(stereo * gain, -1.0, 1.0).astype(np.float32, copy=False)
