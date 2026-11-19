"""Stabilize and restore Dolby Atmos JOC / A-JOC dynamic objects beyond 15 slots.

Dolby Atmos streaming codecs (E-AC-3 JOC and AC-4 A-JOC) pack up to 128 studio
master tracks into at most 15 dynamic coding slots plus LFE.  Encoders achieve
this by:
  1. Time-multiplexing different spatial sources onto shared slots (causing
     single-object slot-hopping and fragmented trajectories).
  2. Co-clustering simultaneously active nearby sources onto a single slot
     whose decoded position oscillates near their energy-weighted barycenter.

This module recovers coherent, stable dynamic objects via four stages:
  1. Kinematic + acoustic change-point segmentation with cross-slot handoff
     detection and co-clustered ping-pong preservation.
  2. Global identity stabilization (Hungarian temporal chaining + global
     non-overlapping Re-ID merging) without a 15-object cap.
  3. Co-clustered dual-trajectory decomposition via Moving-Frenet multi-init
     banded-spline barycenter EM, 5-factor confidence gating, and exact-sum
     barycenter-guided STFT Wiener masking.
  4. Post-split Re-ID, quiet-fragment packing, trajectory key decimation,
     `ObjectTrace` construction, and streaming 24-bit ADM BWF synthesis.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy import ndimage, optimize, signal, sparse
from scipy.sparse.linalg import spsolve

from soundweaver.core.cancel import CancellationToken
from soundweaver.core.speakers import DEFAULT_ATMOS, SpeakerLayout

from .adm import AdmFile, _BED_CHANNELS, _BED_SETS, _Track, _bed, pcm24
from .adm_reader import PreparedADM
from .oamd import Program, Render
from .render import object_gains
from .trace import SILENCE_DB, Key, ObjectPath, ObjectTrace

logger = logging.getLogger(__name__)

_BIG_COST = 1e6


@dataclass(frozen=True, slots=True)
class RestoreConfig:
    """Parameters governing object segmentation, global Re-ID, and splitting."""

    hop_samples: int = 512
    n_fft: int = 1024
    sep_n_fft: int = 2048
    n_bands: int = 24
    silence_db: float = -60.0
    silence_gap_s: float = 0.14
    teleport_dist: float = 0.18
    teleport_innov: float = 0.13
    handoff_dist: float = 0.055
    handoff_window_frames: int = 3
    timbre_split_sim: float = 0.52
    link_max_gap_s: float = 1.5
    stationary_radius: float = 0.045
    reid_moving_sim: float = 0.80
    enable_split: bool = True
    split_confidence: float = 0.52
    split_min_sep: float = 0.045
    split_min_duration_s: float = 0.30
    split_max_depth: int = 2
    crossfade_ms: float = 5.0
    max_dynamic_objects: int = 118
    min_object_peak_db: float = -72.0
    quiet_fragment_peak_db: float = -30.0
    quiet_fragment_duration_s: float = 2.0


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Summary returned after restoring an ADM BWF file."""

    output_path: Path
    input_objects: int
    restored_objects: int
    bed_channels: int
    splits_applied: int
    max_sum_error: float


@dataclass(slots=True)
class SlotFeatures:
    """Hop-aligned spatial and loudness-invariant acoustic features for M slots."""

    sample_rate: float
    hop_samples: int
    hop_start_sample: int
    bark_power: np.ndarray  # (M, T, 24) float32
    fingerprint: np.ndarray  # (M, T, 32) float32
    rms: np.ndarray  # (M, T) float32
    peak: np.ndarray  # (M, T) float32
    pos: np.ndarray  # (M, T, 3) float32
    active: np.ndarray  # (M, T) bool


@dataclass(frozen=True, slots=True)
class SplitNode:
    """One accepted Stage-3 decomposition of a stretch into two branches."""

    split_id: int
    start_hop: int
    end_hop: int
    weights: np.ndarray  # (end_hop - start_hop,) float32 for branch 0
    parent_spans: tuple[RawSpan, ...]


@dataclass(slots=True)
class RawSpan:
    """Contiguous hop range sourced from a raw slot or a separated SplitNode branch."""

    raw_slot: int
    start_hop: int
    end_hop: int
    pos: np.ndarray  # (end_hop - start_hop, 3) float32
    active: np.ndarray  # (end_hop - start_hop,) bool
    split_source: tuple[SplitNode, int] | None = None
    bark: np.ndarray | None = None  # (end_hop - start_hop, 24) float32 for split spans
    rms: np.ndarray | None = None  # (end_hop - start_hop,) float32 for split spans
    peak: np.ndarray | None = None  # (end_hop - start_hop,) float32 for split spans


@dataclass(slots=True)
class RestoredObjectPlan:
    """Fully planned trajectory, spectral summary, and audio spans for one output object."""

    index: int
    spans: list[RawSpan]
    keys: list[Key]
    pos: np.ndarray  # (T, 3) float32
    active: np.ndarray  # (T,) bool
    bark_power: np.ndarray  # (T, 24) float32
    rms: np.ndarray  # (T,) float32
    peak: np.ndarray  # (T,) float32
    is_stationary: bool
    total_energy: float


@dataclass(frozen=True, slots=True)
class RestorationPlan:
    """Complete object restoration plan ready for ADM synthesis and tracing."""

    objects: tuple[RestoredObjectPlan, ...]
    input_objects: int
    splits_applied: int
    hop_samples: int
    sample_rate: float
    hop_start_sample: int


# ---------------------------------------------------------------------------
# 1.2 Shared acoustic feature extractor & key evaluation
# ---------------------------------------------------------------------------


def _hz_to_erb(hz: np.ndarray | float) -> np.ndarray | float:
    return 21.4 * np.log10(1.0 + 0.00437 * np.asarray(hz))


def _erb_to_hz(erb: np.ndarray | float) -> np.ndarray | float:
    return (10.0 ** (np.asarray(erb) / 21.4) - 1.0) / 0.00437


@lru_cache(maxsize=16)
def _erb_filterbank(
    sample_rate: float, n_fft: int, n_bands: int
) -> tuple[np.ndarray, tuple[slice, slice, slice], slice]:
    """24-band ERB triangular filterbank normalized per covered bin to preserve power."""
    n_bins = n_fft // 2 + 1
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sample_rate)
    f_min = 40.0
    f_max = min(18000.0, 0.48 * sample_rate)
    erb_pts = np.linspace(_hz_to_erb(f_min), _hz_to_erb(f_max), n_bands + 2)
    hz_pts = _erb_to_hz(erb_pts)
    fb = np.zeros((n_bands, n_bins), dtype=np.float64)
    for b in range(n_bands):
        lo, mid, hi = hz_pts[b], hz_pts[b + 1], hz_pts[b + 2]
        up = (freqs - lo) / max(mid - lo, 1e-9)
        dn = (hi - freqs) / max(hi - mid, 1e-9)
        fb[b] = np.maximum(0.0, np.minimum(up, dn))
        if fb[b].sum() <= 1e-9:
            k = int(np.argmin(np.abs(freqs - mid)))
            fb[b, k] = 1.0
    col_sum = fb.sum(axis=0)
    covered = col_sum > 1e-9
    fb[:, covered] /= col_sum[covered]
    fb_f32 = fb.astype(np.float32)
    fb_f32.flags.writeable = False

    k_low_a = int(np.searchsorted(freqs, 40.0, side="left"))
    k_low_b = max(k_low_a + 1, int(np.searchsorted(freqs, 400.0, side="right")))
    k_mid_b = max(k_low_b + 1, int(np.searchsorted(freqs, 3500.0, side="right")))
    k_high_b = max(k_mid_b + 1, int(np.searchsorted(freqs, f_max, side="right")))
    k_harm_a = int(np.searchsorted(freqs, 150.0, side="left"))
    k_harm_b = max(k_harm_a + 2, int(np.searchsorted(freqs, 5000.0, side="right")))
    sub_slices = (
        slice(k_low_a, k_low_b),
        slice(k_low_b, k_mid_b),
        slice(k_mid_b, min(n_bins, k_high_b)),
    )
    return fb_f32, sub_slices, slice(k_harm_a, min(n_bins, k_harm_b))


class HopFeatureExtractor:
    """Streaming vectorized FFT + ERB-band feature extractor over fixed hops."""

    def __init__(
        self,
        n_objects: int,
        sample_rate: float,
        hop_samples: int = 512,
        n_fft: int = 1024,
        n_bands: int = 24,
    ) -> None:
        if hop_samples <= 0 or n_fft < hop_samples:
            raise ValueError(f"invalid hop_samples={hop_samples} for n_fft={n_fft}")
        self.n_objects = n_objects
        self.sample_rate = float(sample_rate)
        self.hop_samples = int(hop_samples)
        self.n_fft = int(n_fft)
        self.n_bands = int(n_bands)
        self._pad = self.n_fft - self.hop_samples
        self._tail = np.zeros((n_objects, self._pad), dtype=np.float32)
        self._win = np.hanning(self.n_fft).astype(np.float32)
        self._win_norm = float(np.sum(self._win**2)) + 1e-12
        self._fb, self._sub_slices, self._harm_slice = _erb_filterbank(
            self.sample_rate, self.n_fft, self.n_bands
        )

    def seed(self, tail: np.ndarray) -> None:
        """Seed the overlap tail buffer from warm-up samples `(n_objects, >=pad)`."""
        if self._pad <= 0 or tail.size == 0 or self.n_objects == 0:
            return
        take = min(self._pad, tail.shape[1])
        self._tail[:, -take:] = tail[:, -take:].astype(np.float32, copy=False)

    def process(self, audio: np.ndarray) -> dict[str, np.ndarray]:
        """Process `(M, N)` where `N` is a multiple of `hop_samples`."""
        m, n_samples = audio.shape
        if m != self.n_objects:
            raise ValueError(f"expected {self.n_objects} objects, got {m}")
        if n_samples == 0 or n_samples % self.hop_samples != 0:
            raise ValueError(f"chunk length {n_samples} not divisible by hop {self.hop_samples}")
        n_hops = n_samples // self.hop_samples
        if m == 0:
            return {
                "bark_power": np.zeros((0, n_hops, self.n_bands), dtype=np.float32),
                "rms": np.zeros((0, n_hops), dtype=np.float32),
                "peak": np.zeros((0, n_hops), dtype=np.float32),
                "flatness_3": np.zeros((0, n_hops, 3), dtype=np.float32),
                "harmonicity": np.zeros((0, n_hops), dtype=np.float32),
            }
        a32 = np.asarray(audio, dtype=np.float32)
        hop_view = a32.reshape(m, n_hops, self.hop_samples)
        rms = np.sqrt(np.mean(np.square(hop_view, dtype=np.float32), axis=2))
        peak = np.max(np.abs(hop_view), axis=2)

        buf = np.concatenate([self._tail, a32], axis=1) if self._pad else a32
        if self._pad:
            self._tail[:] = buf[:, -self._pad :]
        frames = np.lib.stride_tricks.sliding_window_view(buf, self.n_fft, axis=1)[
            :, :: self.hop_samples, :
        ]
        spec = np.fft.rfft(frames * self._win[None, None, :], axis=2)
        power = (spec.real**2 + spec.imag**2) / self._win_norm  # (M, H, F)
        bark_power = np.einsum("mhf,bf->mhb", power, self._fb).astype(np.float32)

        flatness = np.empty((m, n_hops, 3), dtype=np.float32)
        for idx, sl in enumerate(self._sub_slices):
            sub = power[:, :, sl]
            if sub.shape[2] == 0:
                flatness[:, :, idx] = 0.0
            else:
                gmean = np.exp(np.mean(np.log(sub + 1e-18), axis=2))
                amean = np.mean(sub, axis=2) + 1e-18
                flatness[:, :, idx] = np.clip(gmean / amean, 0.0, 1.0)

        h_sub = power[:, :, self._harm_slice]
        if h_sub.shape[2] >= 2:
            med = np.median(h_sub, axis=2) + 1e-18
            ratio = np.max(h_sub, axis=2) / med
            lp = np.log1p(ratio)
            harmonicity = (lp / (lp + 3.0)).astype(np.float32)
        else:
            harmonicity = np.zeros((m, n_hops), dtype=np.float32)

        return {
            "bark_power": bark_power,
            "rms": rms.astype(np.float32),
            "peak": peak.astype(np.float32),
            "flatness_3": flatness,
            "harmonicity": harmonicity,
        }


def build_fingerprints(
    bark_power: np.ndarray,
    flatness_3: np.ndarray,
    harmonicity: np.ndarray,
) -> np.ndarray:
    """Construct loudness-invariant 32-D L2-normalized acoustic fingerprints."""
    m, t_len, n_bands = bark_power.shape
    if m == 0 or t_len == 0:
        return np.zeros((m, t_len, 32), dtype=np.float32)
    bp = bark_power.astype(np.float64, copy=False)
    total_bp = bp.sum(axis=-1, keepdims=True)
    mean_bp = bp.mean(axis=-1, keepdims=True)
    log_bark = np.log1p(bp / (mean_bp + 1e-12))
    bark_shape = log_bark - log_bark.mean(axis=-1, keepdims=True)

    p_norm = bp / (total_bp + 1e-12)
    band_idx = np.linspace(0.0, 1.0, n_bands, dtype=np.float64)
    centroid = (p_norm * band_idx).sum(axis=-1)
    spread = np.sqrt(np.maximum(0.0, ((band_idx - centroid[..., None]) ** 2 * p_norm).sum(axis=-1)))

    flux = np.zeros((m, t_len), dtype=np.float64)
    if t_len > 1:
        diff = np.maximum(0.0, log_bark[:, 1:] - log_bark[:, :-1])
        flux[:, 1:] = np.sqrt(np.mean(diff**2, axis=-1))

    tilt = (bp[..., :8].sum(axis=-1) - bp[..., 16:].sum(axis=-1)) / (total_bp[..., 0] + 1e-12)

    fp = np.empty((m, t_len, 32), dtype=np.float32)
    fp[:, :, :24] = bark_shape.astype(np.float32)
    fp[:, :, 24:27] = 0.35 * flatness_3
    fp[:, :, 27] = (0.35 * (2.0 * centroid - 1.0)).astype(np.float32)
    fp[:, :, 28] = (0.35 * (4.0 * spread - 1.0)).astype(np.float32)
    fp[:, :, 29] = (0.35 * np.tanh(flux)).astype(np.float32)
    fp[:, :, 30] = (0.35 * (2.0 * harmonicity - 1.0)).astype(np.float32)
    fp[:, :, 31] = (0.35 * tilt).astype(np.float32)

    norms = np.linalg.norm(fp, axis=-1, keepdims=True)
    silent = total_bp <= 1e-12
    fp = np.where((norms > 1e-9) & (~silent), fp / np.maximum(norms, 1e-9), 0.0).astype(np.float32)
    return fp


def eval_keys_on_grid(keys: Sequence[Key], times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Evaluate OAMD/ADM trajectory keys at sorted timestamps `times` in O(T + |K|)."""
    t_len = len(times)
    if t_len == 0:
        return np.zeros((0, 3), dtype=np.float32), np.zeros(0, dtype=bool)
    if not keys:
        pos = np.tile(np.array([0.5, 0.5, 0.0], dtype=np.float32), (t_len, 1))
        return pos, np.ones(t_len, dtype=bool)

    n_k = len(keys)
    starts = np.empty(n_k, dtype=np.float64)
    ramps = np.empty(n_k, dtype=np.float64)
    to_pos = np.empty((n_k, 3), dtype=np.float64)
    from_pos = np.empty((n_k, 3), dtype=np.float64)
    key_active = np.empty(n_k, dtype=bool)

    for i, (kt, kr, kx, ky, kz, ka) in enumerate(keys):
        starts[i] = kt
        ramps[i] = kr
        to_pos[i] = (kx, ky, kz)
        key_active[i] = bool(ka)
        if i == 0:
            from_pos[i] = to_pos[i]
        else:
            prev_r = ramps[i - 1]
            f = min(1.0, max(0.0, (kt - starts[i - 1]) / prev_r)) if prev_r > 0.0 else 1.0
            from_pos[i] = from_pos[i - 1] + (to_pos[i - 1] - from_pos[i - 1]) * f

    idx = np.searchsorted(starts, times, side="right") - 1
    before = idx < 0
    safe_idx = np.maximum(0, idx)
    r = ramps[safe_idx]
    dt = times - starts[safe_idx]
    f = np.where(before, 1.0, np.where(r > 0.0, np.clip(dt / np.maximum(r, 1e-12), 0.0, 1.0), 1.0))
    pos = from_pos[safe_idx] + (to_pos[safe_idx] - from_pos[safe_idx]) * f[:, None]
    pos[before] = to_pos[0]
    active = np.where(before, key_active[0], key_active[safe_idx])
    return pos.astype(np.float32), active


def extract_slot_features(
    audio: np.ndarray,
    keys_per_slot: Sequence[Sequence[Key]],
    sample_rate: float,
    cfg: RestoreConfig,
    hop_start_sample: int = 0,
) -> SlotFeatures:
    """Compute `SlotFeatures` from in-memory `(M, N)` audio and per-slot trajectory keys."""
    m, n_samples = audio.shape
    hop = cfg.hop_samples
    n_hops = math.ceil(n_samples / hop) if n_samples else 1
    pad = n_hops * hop - n_samples
    padded = np.pad(audio, ((0, 0), (0, pad))) if pad > 0 else audio
    ext = HopFeatureExtractor(m, sample_rate, hop, cfg.n_fft, cfg.n_bands)
    raw = ext.process(padded)
    fp = build_fingerprints(raw["bark_power"], raw["flatness_3"], raw["harmonicity"])
    times = (np.arange(n_hops, dtype=np.float64) * hop + 0.5 * hop + hop_start_sample) / sample_rate
    pos = np.empty((m, n_hops, 3), dtype=np.float32)
    active = np.empty((m, n_hops), dtype=bool)
    for slot_idx in range(m):
        klist = keys_per_slot[slot_idx] if slot_idx < len(keys_per_slot) else ()
        pos[slot_idx], active[slot_idx] = eval_keys_on_grid(klist, times)
    return SlotFeatures(
        sample_rate=float(sample_rate),
        hop_samples=hop,
        hop_start_sample=hop_start_sample,
        bark_power=raw["bark_power"],
        fingerprint=fp,
        rms=raw["rms"],
        peak=raw["peak"],
        pos=pos,
        active=active,
    )


# ---------------------------------------------------------------------------
# 1.3 Stage 1: Kinematic + Acoustic Change-Point Segmentation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Tracklet:
    raw_slot: int
    core_start: int
    core_end: int
    span_start: int
    span_end: int
    pos: np.ndarray  # (span_end - span_start, 3) float32
    active: np.ndarray  # (span_end - span_start,) bool
    start_pos: np.ndarray
    end_pos: np.ndarray
    anchor_pos: np.ndarray
    pos_std: float
    is_stationary: bool
    start_vel: np.ndarray
    end_vel: np.ndarray
    head_feat: np.ndarray
    tail_feat: np.ndarray
    mean_feat: np.ndarray
    total_energy: float
    peak_amp: float


def _norm_vec(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return (v / n).astype(np.float32) if n > 1e-9 else np.zeros_like(v, dtype=np.float32)


def _mean_feat_window(fp: np.ndarray, active: np.ndarray, lo: int, hi: int) -> np.ndarray:
    sub_a = active[lo:hi]
    if not np.any(sub_a):
        return np.zeros(fp.shape[1], dtype=np.float32)
    return _norm_vec(fp[lo:hi][sub_a].mean(axis=0))


def _is_ping_pong(
    p: np.ndarray,
    p_bar_wide: np.ndarray,
    fp: np.ndarray,
    act: np.ndarray,
    i_start: int,
    i_end: int,
    t: int,
    handoff_flag: bool,
    pre_idx: int | None = None,
    post_idx: int | None = None,
) -> bool:
    """Detect oscillating dual-source alternation on a single slot without cross-slot handoffs."""
    if handoff_flag:
        return False
    a_idx = max(i_start, t - 1) if pre_idx is None else pre_idx
    b_idx = min(i_end - 1, t) if post_idx is None else post_idx
    p_a, p_b = p[a_idx], p[b_idx]
    if float(np.linalg.norm(p_b - p_a)) < 0.04:
        return False
    r_a = p_a - p_bar_wide[a_idx]
    f_a = fp[a_idx]
    f_b = fp[b_idx]
    has_fa = bool(np.any(f_a))
    has_fb = bool(np.any(f_b))

    def _matches_a(k: int) -> bool:
        if not act[k]:
            return False
        dist_b = float(np.linalg.norm(p[k] - p[b_idx]))
        if dist_b < 0.05:
            return False
        r_k = p[k] - p_bar_wide[k]
        spatial_ret = float(np.linalg.norm(r_k - r_a)) <= 0.09 or float(np.linalg.norm(p[k] - p_a)) <= 0.06
        if has_fa and has_fb and np.any(fp[k]):
            sim_a = float(np.dot(f_a, fp[k]))
            sim_b = float(np.dot(f_b, fp[k]))
            if sim_a >= 0.80 and sim_a > sim_b + 0.12 and float(np.linalg.norm(r_k - r_a)) <= 0.14:
                return True
        return spatial_ret and (not has_fa or not np.any(fp[k]) or float(np.dot(f_a, fp[k])) >= 0.65)

    for k in range(b_idx + 1, min(i_end, b_idx + 105)):
        if _matches_a(k):
            return True

    # Also check symmetric lookback near the tail of an island:
    # if state A (at a_idx) already matches an earlier peak before b_idx's counterpart
    if i_end - t <= 105:
        r_b = p_b - p_bar_wide[b_idx]
        saw_b_prev = False
        for k in range(a_idx - 1, max(i_start - 1, a_idx - 105), -1):
            if not act[k]:
                continue
            r_k = p[k] - p_bar_wide[k]
            if float(np.linalg.norm(r_k - r_b)) <= 0.09 or (
                has_fb and np.any(fp[k]) and float(np.dot(f_b, fp[k])) >= 0.80 and float(np.dot(f_a, fp[k])) < float(np.dot(f_b, fp[k])) - 0.12
            ):
                saw_b_prev = True
            elif saw_b_prev and _matches_a(k):
                return True
    return False


def _detect_handoffs(feats: SlotFeatures, active_core: np.ndarray, cfg: RestoreConfig) -> np.ndarray:
    m, t_len = active_core.shape
    handoff = np.zeros((m, t_len), dtype=bool)
    if m <= 1 or t_len <= 1:
        return handoff
    p = feats.pos
    fp = feats.fingerprint
    win = cfg.handoff_window_frames
    step_dists = np.linalg.norm(p[:, 1:] - p[:, :-1], axis=2)
    pair_active = active_core[:, :-1] & active_core[:, 1:]
    cand_m, cand_t = np.nonzero(pair_active & (step_dists >= cfg.handoff_dist))
    for mi, ti_prev in zip(cand_m, cand_t, strict=True):
        t = int(ti_prev) + 1
        lo = max(0, t - win)
        hi = min(t_len, t + win + 1)
        p_from = p[mi, t - 1]
        p_to = p[mi, t]
        f_from = fp[mi, t - 1]
        f_to = fp[mi, t]
        t_bwd = max(0, t - 2)
        t_fwd = min(t_len - 1, t + 1)

        bwd_ok = (~active_core[:, t_bwd]) | (np.sum((p[:, t_bwd] - p_from) ** 2, axis=1) > 0.08**2)
        bwd_ok[mi] = False
        if np.any(bwd_ok):
            d1 = np.sum((p[bwd_ok, t - 1 : hi] - p_from) ** 2, axis=2) <= 0.06**2
            s1 = (fp[bwd_ok, t - 1 : hi] @ f_from) >= 0.55
            if np.any(active_core[bwd_ok, t - 1 : hi] & d1 & s1):
                handoff[mi, t] = True
                continue

        fwd_ok = (~active_core[:, t_fwd]) | (np.sum((p[:, t_fwd] - p_to) ** 2, axis=1) > 0.08**2)
        fwd_ok[mi] = False
        if np.any(fwd_ok):
            d2 = np.sum((p[fwd_ok, lo : t + 1] - p_to) ** 2, axis=2) <= 0.06**2
            s2 = (fp[fwd_ok, lo : t + 1] @ f_to) >= 0.55
            if np.any(active_core[fwd_ok, lo : t + 1] & d2 & s2):
                handoff[mi, t] = True
    return handoff


def _bridge_and_collect_islands(
    active_core: np.ndarray,
    active_tail: np.ndarray,
    max_gap_hops: int,
) -> list[tuple[int, int]]:
    """Find contiguous core islands (bridging gaps < max_gap_hops) plus quiet standalone islands."""
    t_len = len(active_core)
    if t_len == 0:
        return []
    bridged = active_core.copy()
    idx = np.flatnonzero(active_core)
    for a, b in zip(idx[:-1], idx[1:], strict=True):
        if 1 < b - a <= max_gap_hops:
            bridged[a + 1 : b] = True

    islands: list[tuple[int, int]] = []
    padded = np.concatenate([[False], bridged, [False]])
    starts = np.flatnonzero(~padded[:-1] & padded[1:])
    ends = np.flatnonzero(padded[:-1] & ~padded[1:])
    covered = np.zeros(t_len, dtype=bool)
    for s, e in zip(starts, ends, strict=True):
        islands.append((int(s), int(e)))
        covered[s:e] = True

    # Recover very quiet standalone regions (between min_object_peak_db and silence_db)
    quiet_only = active_tail & ~covered
    if np.any(quiet_only):
        q_idx = np.flatnonzero(quiet_only)
        for a, b in zip(q_idx[:-1], q_idx[1:], strict=True):
            if 1 < b - a <= max_gap_hops and np.all(~covered[a + 1 : b]):
                quiet_only[a + 1 : b] = True
        qp = np.concatenate([[False], quiet_only, [False]])
        for s, e in zip(np.flatnonzero(~qp[:-1] & qp[1:]), np.flatnonzero(qp[:-1] & ~qp[1:]), strict=True):
            if e - s >= 3:
                islands.append((int(s), int(e)))
        islands.sort()
    return islands


def _fold_transition_stubs(
    bounds: list[tuple[int, int]],
    p: np.ndarray,
    fp: np.ndarray,
    act: np.ndarray,
    stub_hops: int = 3,
) -> list[tuple[int, int]]:
    """Absorb <=3-hop transition stubs into the better-matching adjacent segment."""
    segs = list(bounds)
    changed = True
    while changed and len(segs) > 1:
        changed = False
        for i, (s, e) in enumerate(segs):
            if e - s > stub_hops:
                continue
            stub_p = p[s:e].mean(axis=0)
            stub_f = _mean_feat_window(fp, act, s, e)
            best_side = None
            best_score = math.inf
            if i > 0:
                ps, pe = segs[i - 1]
                pp = p[max(ps, pe - 4) : pe].mean(axis=0)
                pf = _mean_feat_window(fp, act, max(ps, pe - 6), pe)
                best_score = 3.0 * float(np.linalg.norm(stub_p - pp)) + (1.0 - float(np.dot(stub_f, pf)))
                best_side = "left"
            if i + 1 < len(segs):
                ns, ne = segs[i + 1]
                np_ = p[ns : min(ne, ns + 4)].mean(axis=0)
                nf = _mean_feat_window(fp, act, ns, min(ne, ns + 6))
                score = 3.0 * float(np.linalg.norm(stub_p - np_)) + (1.0 - float(np.dot(stub_f, nf)))
                if score < best_score:
                    best_side = "right"
            if best_side == "left":
                segs[i - 1] = (segs[i - 1][0], e)
                segs.pop(i)
                changed = True
                break
            if best_side == "right":
                segs[i + 1] = (s, segs[i + 1][1])
                segs.pop(i)
                changed = True
                break
    return segs


def segment_slots(feats: SlotFeatures, cfg: RestoreConfig) -> list[_Tracklet]:
    """Split each raw slot at silence gaps, handoffs, teleports, and sustained timbre shifts."""
    m, t_len = feats.rms.shape
    if m == 0 or t_len == 0:
        return []
    dt = feats.hop_samples / feats.sample_rate
    silence_lin = 10.0 ** (cfg.silence_db / 20.0)
    min_peak_lin = 10.0 ** (cfg.min_object_peak_db / 20.0)
    max_gap_hops = max(1, round(cfg.silence_gap_s / dt))

    active_core = feats.active & (feats.rms >= silence_lin)
    active_tail = feats.active & (feats.peak >= min_peak_lin)
    nonzero_mask = feats.active & (feats.peak > 1e-7)
    handoffs = _detect_handoffs(feats, active_core, cfg)

    tracklets: list[_Tracklet] = []
    for slot in range(m):
        p = feats.pos[slot]
        fp = feats.fingerprint[slot]
        act_c = active_core[slot]
        act_t = active_tail[slot]
        act_nz = nonzero_mask[slot]
        rms_s = feats.rms[slot]
        peak_s = feats.peak[slot]

        islands = _bridge_and_collect_islands(act_c, act_t, max_gap_hops)
        if not islands:
            continue

        slot_cores: list[tuple[int, int]] = []
        wide_win = max(7, round(1.20 / dt) | 1)
        for i_start, i_end in islands:
            isl_len = i_end - i_start
            p_isl = p[i_start:i_end].astype(np.float64)
            fp_isl = fp[i_start:i_end]
            act_isl = act_nz[i_start:i_end]

            w_eff = min(isl_len if isl_len % 2 == 1 else isl_len - 1, wide_win)
            if w_eff >= 5:
                p_bar_wide = signal.savgol_filter(
                    p_isl, window_length=w_eff, polyorder=2, axis=0, mode="interp"
                ).astype(np.float32)
            else:
                p_bar_wide = np.repeat(p_isl.mean(axis=0, keepdims=True), isl_len, axis=0).astype(
                    np.float32
                )

            # Vectorized bidirectional local acoustic similarity via cumulative sums
            fp_masked = np.where(act_isl[:, None], fp_isl.astype(np.float64), 0.0)
            cs_fp = np.zeros((isl_len + 1, fp_isl.shape[1]), dtype=np.float64)
            np.cumsum(fp_masked, axis=0, out=cs_fp[1:])
            idx_arr = np.arange(isl_len)
            pre_sum = cs_fp[idx_arr] - cs_fp[np.maximum(0, idx_arr - 8)]
            post_sum = cs_fp[np.minimum(isl_len, idx_arr + 8)] - cs_fp[idx_arr]
            pre_n = np.linalg.norm(pre_sum, axis=1)
            post_n = np.linalg.norm(post_sum, axis=1)
            s_local = np.where(
                (pre_n > 1e-9) & (post_n > 1e-9),
                np.sum(pre_sum * post_sum, axis=1) / np.maximum(pre_n * post_n, 1e-12),
                1.0,
            ).astype(np.float32)
            s_local[:2] = 1.0
            s_local[-1:] = 1.0

            d_arr = np.zeros(isl_len, dtype=np.float64)
            if isl_len > 1:
                d_arr[1:] = np.linalg.norm(p_isl[1:] - p_isl[:-1], axis=1)
            r_arr = d_arr.copy()
            if isl_len > 2:
                r_arr[2:] = np.linalg.norm(p_isl[2:] - (2.0 * p_isl[1:-1] - p_isl[:-2]), axis=1)

            max_lag_d = np.zeros(isl_len, dtype=np.float64)
            for lag in (2, 3, 4):
                if isl_len > lag + 4:
                    ld = np.linalg.norm(
                        p_isl[lag + 2 : isl_len - 2] - p_isl[2 : isl_len - lag - 2], axis=1
                    )
                    max_lag_d[lag + 2 : isl_len - 2] = np.maximum(
                        max_lag_d[lag + 2 : isl_len - 2], ld
                    )

            cs_p = np.zeros((isl_len + 1, 3), dtype=np.float64)
            np.cumsum(p_isl, axis=0, out=cs_p[1:])
            lo_p = np.maximum(0, idx_arr - 6)
            hi_p = np.minimum(isl_len, idx_arr + 6)
            pre_p_arr = (cs_p[idx_arr] - cs_p[lo_p]) / np.maximum(1, idx_arr - lo_p)[:, None]
            post_p_arr = (cs_p[hi_p] - cs_p[idx_arr]) / np.maximum(1, hi_p - idx_arr)[:, None]
            shift_arr = np.linalg.norm(pre_p_arr - post_p_arr, axis=1)
            s_min_win = ndimage.minimum_filter1d(s_local, size=9, mode="nearest")
            # Small slot jumps can still identify a source switch when the timbre
            # changes sharply. Require a break in motion near the audio boundary.
            local_innovation = ndimage.maximum_filter1d(r_arr, size=5, mode="nearest")
            timbre_switch = (shift_arr >= 0.045) | (
                (shift_arr >= 0.02)
                & (local_innovation >= 0.025)
                & (s_local < 0.25)
            )
            timbre_cand = (
                (s_local < cfg.timbre_split_sim)
                & (s_local <= s_min_win + 1e-6)
                & timbre_switch
            )
            timbre_cand[:2] = False
            timbre_cand[-1:] = False

            ho_isl = handoffs[slot, i_start:i_end]
            cand_mask = (
                ho_isl
                | ((d_arr >= cfg.teleport_dist) & (r_arr >= cfg.teleport_innov))
                | (max_lag_d >= 0.22)
                | timbre_cand
            )
            cand_mask[0] = False

            split_points: list[int] = [i_start]
            p_isl_f32 = p[i_start:i_end]
            for off_np in np.flatnonzero(cand_mask):
                off = int(off_np)
                t = i_start + off
                if t <= split_points[-1]:
                    continue
                d_t = float(d_arr[off])
                r_t = float(r_arr[off])
                ho_t = bool(ho_isl[off])
                pingpong = d_t >= cfg.handoff_dist and _is_ping_pong(
                    p_isl_f32, p_bar_wide, fp_isl, act_isl, 0, isl_len, off, ho_t
                )

                do_split_at: int | None = None
                if ho_t:
                    do_split_at = t
                elif d_t >= cfg.teleport_dist and r_t >= cfg.teleport_innov and not pingpong:
                    do_split_at = t
                elif max_lag_d[off] >= 0.22:
                    for lag in (2, 3, 4):
                        if off - lag < 2 or off + 2 >= isl_len:
                            continue
                        cum_d = float(np.linalg.norm(p_isl[off] - p_isl[off - lag]))
                        if cum_d < 0.22:
                            continue
                        pre_vel = (p_isl[off - lag] - p_isl[off - lag - 2]) / 2.0
                        extrap = p_isl[off - lag] + pre_vel * lag
                        r_extrap = float(np.linalg.norm(p_isl[off] - extrap))
                        pre_std = float(np.std(p_isl[max(0, off - lag - 3) : off - lag + 1], axis=0).sum())
                        post_std = float(np.std(p_isl[off : min(isl_len, off + 4)], axis=0).sum())
                        if (
                            r_extrap >= 0.16
                            and pre_std < 0.06
                            and post_std < 0.06
                            and not _is_ping_pong(
                                p_isl_f32,
                                p_bar_wide,
                                fp_isl,
                                act_isl,
                                0,
                                isl_len,
                                off,
                                False,
                                pre_idx=off - lag,
                                post_idx=off,
                            )
                        ):
                            do_split_at = t - lag // 2
                            break

                if do_split_at is None and timbre_cand[off] and not pingpong and not _is_ping_pong(
                    p_isl_f32,
                    p_bar_wide,
                    fp_isl,
                    act_isl,
                    0,
                    isl_len,
                    off,
                    ho_t,
                    pre_idx=max(0, off - 3),
                    post_idx=min(isl_len - 1, off + 3),
                ):
                    do_split_at = t

                if do_split_at is not None and do_split_at > split_points[-1]:
                    split_points.append(do_split_at)
            split_points.append(i_end)
            raw_bounds = list(zip(split_points[:-1], split_points[1:], strict=True))
            folded = _fold_transition_stubs(raw_bounds, p, fp, act_nz, stub_hops=3)
            slot_cores.extend(folded)

        if not slot_cores:
            continue

        # Expand each segment outward into any adjacent non-zero tail (up to midpoint
        # between consecutive segments on this slot) so 100% of slot audio is conserved.
        n_sc = len(slot_cores)
        spans: list[tuple[int, int]] = []
        for idx_sc, (cs, ce) in enumerate(slot_cores):
            if idx_sc == 0:
                lo_lim = 0
                s_exp = cs
                while s_exp > lo_lim and act_nz[s_exp - 1]:
                    s_exp -= 1
            else:
                prev_ce = slot_cores[idx_sc - 1][1]
                s_exp = prev_ce if cs <= prev_ce else prev_ce + (cs - prev_ce) // 2
            if idx_sc + 1 == n_sc:
                e_exp = ce
                while e_exp < t_len and act_nz[e_exp]:
                    e_exp += 1
            else:
                next_cs = slot_cores[idx_sc + 1][0]
                e_exp = ce if next_cs <= ce else ce + (next_cs - ce) // 2
            # Ensure span encompasses core
            s_exp = min(s_exp, cs)
            e_exp = max(e_exp, ce)
            spans.append((s_exp, e_exp))

        # Partition any residual non-zero regions on this slot to nearest span midpoint
        # if a huge non-zero gap exceeded max_gap_hops? Already handled by while loops on act_nz!
        # Wait: ensure consecutive spans on this slot touch seamlessly across non-zero valleys.
        for idx_sc in range(n_sc - 1):
            e_prev = spans[idx_sc][1]
            s_next = spans[idx_sc + 1][0]
            if np.any(act_nz[e_prev:s_next]):
                mid = e_prev + (s_next - e_prev) // 2
                # Find lowest-RMS hop in the valley
                valley = rms_s[slot_cores[idx_sc][1] : slot_cores[idx_sc + 1][0]]
                if valley.size > 0:
                    mid = slot_cores[idx_sc][1] + int(np.argmin(valley))
                spans[idx_sc] = (spans[idx_sc][0], mid)
                spans[idx_sc + 1] = (mid, spans[idx_sc + 1][1])

        for (cs, ce), (ss, se) in zip(slot_cores, spans, strict=True):
            peak_amp = float(np.max(peak_s[ss:se])) if se > ss else 0.0
            if peak_amp < min_peak_lin:
                continue
            w_e = np.square(rms_s[cs:ce], dtype=np.float64)
            tot_e = float(np.sum(w_e))
            w_norm = w_e / tot_e if tot_e > 1e-18 else np.full(ce - cs, 1.0 / max(1, ce - cs))
            p_core = p[cs:ce]
            anchor = (p_core * w_norm[:, None]).sum(axis=0).astype(np.float32)
            pos_var = np.sum(np.square(p_core - anchor[None, :]) * w_norm[:, None])
            pos_std = float(np.sqrt(max(0.0, pos_var)))

            head_k = min(6, ce - cs)
            tail_k = min(6, ce - cs)
            start_pos = p[cs].astype(np.float32)
            end_pos = p[ce - 1].astype(np.float32)
            start_vel = ((p[cs + head_k - 1] - p[cs]) / max(1e-6, (head_k - 1) * dt)).astype(np.float32) if head_k >= 2 else np.zeros(3, dtype=np.float32)
            end_vel = ((p[ce - 1] - p[ce - tail_k]) / max(1e-6, (tail_k - 1) * dt)).astype(np.float32) if tail_k >= 2 else np.zeros(3, dtype=np.float32)

            act_span = act_nz[ss:se].copy()
            head_feat = _mean_feat_window(fp, act_nz, cs, min(ce, cs + 8))
            tail_feat = _mean_feat_window(fp, act_nz, max(cs, ce - 8), ce)
            mean_feat = _mean_feat_window(fp, act_nz, cs, ce)
            tot_span_e = float(np.sum(np.square(rms_s[ss:se], dtype=np.float64)))

            tracklets.append(
                _Tracklet(
                    raw_slot=slot,
                    core_start=cs,
                    core_end=ce,
                    span_start=ss,
                    span_end=se,
                    pos=p[ss:se].copy(),
                    active=act_span,
                    start_pos=start_pos,
                    end_pos=end_pos,
                    anchor_pos=anchor,
                    pos_std=pos_std,
                    is_stationary=pos_std < cfg.stationary_radius,
                    start_vel=start_vel,
                    end_vel=end_vel,
                    head_feat=head_feat,
                    tail_feat=tail_feat,
                    mean_feat=mean_feat,
                    total_energy=tot_span_e,
                    peak_amp=peak_amp,
                )
            )
    return tracklets


# ---------------------------------------------------------------------------
# 1.4 Stage 2: Global Identity Stabilization (Pass 2A + Pass 2B)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _LogicalChain:
    spans: list[RawSpan]
    core_intervals: list[tuple[int, int]]  # sorted non-overlapping [core_start, core_end)
    last_core_end: int
    end_pos: np.ndarray
    end_vel: np.ndarray
    tail_feat: np.ndarray
    prototypes: list[tuple[float, np.ndarray]]  # (energy, L2-normalized feat)
    anchor_pos: np.ndarray
    pos_std: float
    is_stationary: bool
    total_energy: float
    peak_amp: float
    start_pos: np.ndarray
    first_core_start: int

    def mean_feat(self) -> np.ndarray:
        acc = np.zeros_like(self.tail_feat)
        for e, f in self.prototypes:
            acc += max(e, 1e-12) * f
        return _norm_vec(acc)


def _tracklet_to_chain(tr: _Tracklet) -> _LogicalChain:
    span = RawSpan(
        raw_slot=tr.raw_slot,
        start_hop=tr.span_start,
        end_hop=tr.span_end,
        pos=tr.pos.copy(),
        active=tr.active.copy(),
        split_source=None,
    )
    return _LogicalChain(
        spans=[span],
        core_intervals=[(tr.core_start, tr.core_end)],
        last_core_end=tr.core_end,
        end_pos=tr.end_pos.copy(),
        end_vel=tr.end_vel.copy(),
        tail_feat=tr.tail_feat.copy(),
        prototypes=[(tr.total_energy, tr.mean_feat.copy())],
        anchor_pos=tr.anchor_pos.copy(),
        pos_std=tr.pos_std,
        is_stationary=tr.is_stationary,
        total_energy=tr.total_energy,
        peak_amp=tr.peak_amp,
        start_pos=tr.start_pos.copy(),
        first_core_start=tr.core_start,
    )


def _link_cost(chain: _LogicalChain, seg: _Tracklet, dt: float, cfg: RestoreConfig) -> float | None:
    if seg.core_start + 1 < chain.last_core_end:
        return None
    gap_s = max(0.0, (seg.core_start - chain.last_core_end) * dt)
    if gap_s > cfg.link_max_gap_s:
        return None
    tau_v = 0.25
    eff_dt = tau_v * (1.0 - math.exp(-gap_s / tau_v)) if gap_s > 0.0 else 0.0
    p_fwd = chain.end_pos + chain.end_vel * eff_dt
    p_bwd = seg.start_pos - seg.start_vel * eff_dt
    d_kin = 0.5 * (
        float(np.linalg.norm(p_fwd - seg.start_pos))
        + float(np.linalg.norm(chain.end_pos - p_bwd))
    )
    d_raw = float(np.linalg.norm(chain.end_pos - seg.start_pos))
    d = min(d_kin, d_raw)
    s_tail = float(np.dot(chain.tail_feat, seg.head_feat))
    s_mean = float(np.dot(chain.mean_feat(), seg.mean_feat))
    s_audio = max(s_tail, s_mean)

    if d <= 0.035:
        if s_audio < 0.30:
            return None
    elif d <= 0.12:
        req = 0.30 + 0.48 * (d - 0.035) / 0.085
        if s_audio < req:
            return None
    elif d <= 0.24:
        vn_a = float(np.linalg.norm(chain.end_vel))
        vn_b = float(np.linalg.norm(seg.start_vel))
        if vn_a <= 0.05 or vn_b <= 0.05:
            return None
        cos_v = float(np.dot(chain.end_vel, seg.start_vel)) / (vn_a * vn_b)
        if d_kin > 0.09 or cos_v <= 0.2 or s_audio < 0.76:
            return None
    else:
        return None

    return 3.0 * d + 1.2 * (1.0 - s_audio) + 0.15 * gap_s


def _append_tracklet(chain: _LogicalChain, tr: _Tracklet, cfg: RestoreConfig) -> None:
    new_span = RawSpan(
        raw_slot=tr.raw_slot,
        start_hop=tr.span_start,
        end_hop=tr.span_end,
        pos=tr.pos.copy(),
        active=tr.active.copy(),
        split_source=None,
    )
    prev = chain.spans[-1]
    if new_span.start_hop < prev.end_hop:
        mid = (prev.end_hop + new_span.start_hop) // 2
        cut_prev = max(prev.start_hop + 1, mid)
        if cut_prev < prev.end_hop:
            keep = cut_prev - prev.start_hop
            prev.end_hop = cut_prev
            prev.pos = prev.pos[:keep]
            prev.active = prev.active[:keep]
        if new_span.start_hop < cut_prev:
            drop = cut_prev - new_span.start_hop
            new_span.start_hop = cut_prev
            new_span.pos = new_span.pos[drop:]
            new_span.active = new_span.active[drop:]
    if new_span.end_hop > new_span.start_hop:
        chain.spans.append(new_span)

    prev_cs, prev_ce = chain.core_intervals[-1]
    cs, ce = tr.core_start, tr.core_end
    if cs < prev_ce:
        cs = prev_ce
    if ce > cs:
        chain.core_intervals.append((cs, ce))
    chain.last_core_end = max(chain.last_core_end, tr.core_end)

    w0 = max(chain.total_energy, 1e-12)
    w1 = max(tr.total_energy, 1e-12)
    w_tot = w0 + w1
    new_anchor = (chain.anchor_pos * w0 + tr.anchor_pos * w1) / w_tot
    d0 = float(np.sum((chain.anchor_pos - new_anchor) ** 2))
    d1 = float(np.sum((tr.anchor_pos - new_anchor) ** 2))
    new_var = (w0 * (chain.pos_std**2 + d0) + w1 * (tr.pos_std**2 + d1)) / w_tot

    chain.anchor_pos = new_anchor.astype(np.float32)
    chain.pos_std = float(np.sqrt(max(0.0, new_var)))
    chain.is_stationary = chain.pos_std < cfg.stationary_radius
    chain.end_pos = tr.end_pos.copy()
    chain.end_vel = tr.end_vel.copy()
    chain.tail_feat = tr.tail_feat.copy()
    chain.prototypes.append((tr.total_energy, tr.mean_feat.copy()))
    chain.total_energy += tr.total_energy
    chain.peak_amp = max(chain.peak_amp, tr.peak_amp)


def _intervals_overlap(a: list[tuple[int, int]], b: list[tuple[int, int]], allow_touch: int = 1) -> bool:
    ia = ib = 0
    while ia < len(a) and ib < len(b):
        as0, ae0 = a[ia]
        bs0, be0 = b[ib]
        if min(ae0, be0) - max(as0, bs0) > allow_touch:
            return True
        if ae0 <= be0:
            ia += 1
        else:
            ib += 1
    return False


def _top_prototypes(chain: _LogicalChain) -> list[np.ndarray]:
    out = [chain.mean_feat()]
    top = sorted(chain.prototypes, key=lambda x: -x[0])[:2]
    for _, f in top:
        if np.any(f):
            out.append(f)
    return out


def _reid_merge_cost(
    a: _LogicalChain, b: _LogicalChain, dt: float, cfg: RestoreConfig
) -> float | None:
    if _intervals_overlap(a.core_intervals, b.core_intervals, allow_touch=1):
        return None
    pa = _top_prototypes(a)
    pb = _top_prototypes(b)
    s_proto = max(float(np.dot(fa, fb)) for fa in pa for fb in pb)
    d_anchor = float(np.linalg.norm(a.anchor_pos - b.anchor_pos))

    if a.is_stationary and b.is_stationary:
        ok = (
            (d_anchor <= 0.030 and s_proto >= 0.30)
            or (d_anchor <= 0.055 and s_proto >= 0.55)
            or (d_anchor <= 0.085 and s_proto >= 0.78)
        )
        if not ok:
            return None
        return 3.0 * d_anchor + 1.0 * (1.0 - s_proto)

    # Order earlier -> later to evaluate endpoint continuation
    first, second = (a, b) if a.last_core_end <= b.first_core_start else (b, a)
    gap_s = max(0.0, (second.first_core_start - first.last_core_end) * dt)
    d_end = float(np.linalg.norm(first.end_pos - second.start_pos))
    ok_global = (
        s_proto >= cfg.reid_moving_sim
        and d_anchor <= 0.15
        and abs(float(a.anchor_pos[2]) - float(b.anchor_pos[2])) <= 0.12
    )
    ok_cont = gap_s <= 4.0 and d_end <= 0.08 and s_proto >= 0.72
    if not (ok_global or ok_cont):
        return None
    d_eff = min(d_anchor, d_end) if ok_cont else d_anchor
    return 2.5 * d_eff + 1.2 * (1.0 - s_proto)


def _merge_chains(a: _LogicalChain, b: _LogicalChain, cfg: RestoreConfig) -> _LogicalChain:
    spans = sorted(a.spans + b.spans, key=lambda s: (s.start_hop, s.end_hop))
    cleaned: list[RawSpan] = []
    for sp in spans:
        if cleaned and sp.start_hop < cleaned[-1].end_hop:
            prev = cleaned[-1]
            mid = (prev.end_hop + sp.start_hop) // 2
            if mid > prev.start_hop:
                keep = mid - prev.start_hop
                prev.end_hop = mid
                prev.pos = prev.pos[:keep]
                prev.active = prev.active[:keep]
                if prev.bark is not None:
                    prev.bark = prev.bark[:keep]
                if prev.rms is not None:
                    prev.rms = prev.rms[:keep]
                if prev.peak is not None:
                    prev.peak = prev.peak[:keep]
            else:
                cleaned.pop()
            if sp.start_hop < mid:
                drop = mid - sp.start_hop
                sp = RawSpan(
                    sp.raw_slot,
                    mid,
                    sp.end_hop,
                    sp.pos[drop:],
                    sp.active[drop:],
                    sp.split_source,
                    None if sp.bark is None else sp.bark[drop:],
                    None if sp.rms is None else sp.rms[drop:],
                    None if sp.peak is None else sp.peak[drop:],
                )
        if sp.end_hop > sp.start_hop:
            cleaned.append(sp)

    raw_cores = sorted(a.core_intervals + b.core_intervals)
    cores: list[tuple[int, int]] = []
    for cs, ce in raw_cores:
        if cores and cs < cores[-1][1]:
            cs = cores[-1][1]
        if ce > cs:
            cores.append((cs, ce))

    first, second = (a, b) if a.first_core_start <= b.first_core_start else (b, a)
    tail_src = a if a.last_core_end >= b.last_core_end else b
    w0 = max(a.total_energy, 1e-12)
    w1 = max(b.total_energy, 1e-12)
    wt = w0 + w1
    anchor = ((a.anchor_pos * w0 + b.anchor_pos * w1) / wt).astype(np.float32)
    d0 = float(np.sum((a.anchor_pos - anchor) ** 2))
    d1 = float(np.sum((b.anchor_pos - anchor) ** 2))
    var = (w0 * (a.pos_std**2 + d0) + w1 * (b.pos_std**2 + d1)) / wt
    pos_std = float(np.sqrt(max(0.0, var)))

    return _LogicalChain(
        spans=cleaned,
        core_intervals=cores,
        last_core_end=tail_src.last_core_end,
        end_pos=tail_src.end_pos.copy(),
        end_vel=tail_src.end_vel.copy(),
        tail_feat=tail_src.tail_feat.copy(),
        prototypes=a.prototypes + b.prototypes,
        anchor_pos=anchor,
        pos_std=pos_std,
        is_stationary=pos_std < cfg.stationary_radius,
        total_energy=a.total_energy + b.total_energy,
        peak_amp=max(a.peak_amp, b.peak_amp),
        start_pos=first.start_pos.copy(),
        first_core_start=first.first_core_start,
    )


def _agglomerative_reid(
    chains: list[_LogicalChain], dt: float, cfg: RestoreConfig
) -> list[_LogicalChain]:
    n = len(chains)
    if n <= 1:
        return chains
    alive = [True] * n
    # Cache pairwise costs
    pair_costs: dict[tuple[int, int], float] = {}
    for i in range(n):
        for j in range(i + 1, n):
            c = _reid_merge_cost(chains[i], chains[j], dt, cfg)
            if c is not None:
                pair_costs[(i, j)] = c

    while pair_costs:
        (bi, bj), _ = min(pair_costs.items(), key=lambda kv: kv[1])
        merged = _merge_chains(chains[bi], chains[bj], cfg)
        chains[bi] = merged
        alive[bj] = False
        # Remove entries involving bi or bj and recompute for bi
        stale = [k for k in pair_costs if bi in k or bj in k]
        for k in stale:
            del pair_costs[k]
        for k in range(n):
            if k == bi or not alive[k]:
                continue
            lo, hi = (bi, k) if bi < k else (k, bi)
            c = _reid_merge_cost(chains[bi], chains[k], dt, cfg)
            if c is not None:
                pair_costs[(lo, hi)] = c

    return [c for flag, c in zip(alive, chains, strict=True) if flag]


def stitch_tracklets(
    tracklets: list[_Tracklet], dt: float, cfg: RestoreConfig
) -> list[_LogicalChain]:
    """Pass 2A Hungarian chaining followed by Pass 2B global non-overlapping Re-ID."""
    if not tracklets:
        return []
    ordered = sorted(tracklets, key=lambda tr: (tr.core_start, tr.raw_slot))
    chains: list[_LogicalChain] = []
    batch_tol = 2
    idx = 0
    while idx < len(ordered):
        t0 = ordered[idx].core_start
        batch: list[_Tracklet] = []
        while idx < len(ordered) and ordered[idx].core_start <= t0 + batch_tol:
            batch.append(ordered[idx])
            idx += 1

        if not chains:
            for tr in batch:
                chains.append(_tracklet_to_chain(tr))
            continue

        n_cl = len(chains)
        n_seg = len(batch)
        cost_mat = np.full((n_cl, n_seg), _BIG_COST, dtype=np.float64)
        for ci, ch in enumerate(chains):
            for sj, tr in enumerate(batch):
                c = _link_cost(ch, tr, dt, cfg)
                if c is not None:
                    cost_mat[ci, sj] = c

        row_ind, col_ind = optimize.linear_sum_assignment(cost_mat)
        matched_segs: set[int] = set()
        for r, c_idx in zip(row_ind, col_ind, strict=True):
            if cost_mat[r, c_idx] < _BIG_COST * 0.5:
                _append_tracklet(chains[r], batch[c_idx], cfg)
                matched_segs.add(int(c_idx))
        for sj, tr in enumerate(batch):
            if sj not in matched_segs:
                chains.append(_tracklet_to_chain(tr))

    return _agglomerative_reid(chains, dt, cfg)


# ---------------------------------------------------------------------------
# 1.5 Stage 3: Co-Clustered Dual-Trajectory Decomposition
# ---------------------------------------------------------------------------


def _chain_arrays(
    chain: _LogicalChain,
    feats: SlotFeatures,
    t_len: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Assemble full-timeline `(pos, active, bark_power, rms, peak)` for a chain."""
    pos = np.tile(np.array([0.5, 0.5, 0.0], dtype=np.float32), (t_len, 1))
    active = np.zeros(t_len, dtype=bool)
    bark = np.zeros((t_len, feats.bark_power.shape[2]), dtype=np.float32)
    rms = np.zeros(t_len, dtype=np.float32)
    peak = np.zeros(t_len, dtype=np.float32)

    for sp in chain.spans:
        s0, s1 = sp.start_hop, sp.end_hop
        if s1 <= s0:
            continue
        pos[s0:s1] = sp.pos
        active[s0:s1] = sp.active
        m = sp.raw_slot
        if sp.split_source is None or sp.bark is None or sp.rms is None or sp.peak is None:
            bark[s0:s1] = feats.bark_power[m, s0:s1]
            rms[s0:s1] = feats.rms[m, s0:s1]
            peak[s0:s1] = feats.peak[m, s0:s1]
        else:
            bark[s0:s1] = sp.bark
            rms[s0:s1] = sp.rms
            peak[s0:s1] = sp.peak
    return pos, active, bark, rms, peak


@lru_cache(maxsize=64)
def _smoothness_penalty(length: int, l1: float = 0.04, l2: float = 6.0, l3: float = 120.0) -> sparse.csc_matrix:
    """Build sparse 1st+2nd+3rd-order difference Gram matrix S for length `L`."""
    mat = sparse.eye(length, format="csc", dtype=np.float64) * 1e-6
    if length >= 2 and l1 > 0.0:
        d1 = sparse.diags([-1.0, 1.0], [0, 1], shape=(length - 1, length), format="csc")
        mat = mat + l1 * (d1.T @ d1)
    if length >= 3 and l2 > 0.0:
        d2 = sparse.diags([1.0, -2.0, 1.0], [0, 1, 2], shape=(length - 2, length), format="csc")
        mat = mat + l2 * (d2.T @ d2)
    if length >= 4 and l3 > 0.0:
        d3 = sparse.diags([-1.0, 3.0, -3.0, 1.0], [0, 1, 2, 3], shape=(length - 3, length), format="csc")
        mat = mat + l3 * (d3.T @ d3)
    return mat.tocsc()


def _solve_barycenter_trajectories(
    p3: np.ndarray,
    w: np.ndarray,
    alpha: np.ndarray,
    smooth: sparse.csc_matrix,
) -> tuple[np.ndarray, np.ndarray]:
    """Solve coupled sparse barycenter normal equations for `(p1, p2)` of shape `(L, 3)`."""
    length = len(w)
    w1 = w.astype(np.float64)
    w2 = 1.0 - w1
    d11 = sparse.diags(alpha * w1 * w1, 0, format="csc")
    d22 = sparse.diags(alpha * w2 * w2, 0, format="csc")
    d12 = sparse.diags(alpha * w1 * w2, 0, format="csc")
    a_mat = sparse.bmat([[smooth + d11, d12], [d12, smooth + d22]], format="csc")
    rhs = np.vstack([(alpha * w1)[:, None] * p3, (alpha * w2)[:, None] * p3])
    sol = spsolve(a_mat, rhs)
    p1 = sol[:length].copy()
    p2 = sol[length:].copy()
    p1[:, :2] = np.clip(p1[:, :2], 0.0, 1.0)
    p1[:, 2] = np.clip(p1[:, 2], -1.0, 1.0)
    p2[:, :2] = np.clip(p2[:, :2], 0.0, 1.0)
    p2[:, 2] = np.clip(p2[:, 2], -1.0, 1.0)
    return p1, p2


def _deterministic_2means_euclid(x: np.ndarray, iters: int = 10) -> np.ndarray:
    """Deterministic farthest-point 2-means++ returning labels in {0, 1}."""
    n = len(x)
    if n < 2:
        return np.zeros(n, dtype=np.int64)
    center = x.mean(axis=0)
    i1 = int(np.argmax(np.sum((x - center) ** 2, axis=1)))
    c1 = x[i1].copy()
    i2 = int(np.argmax(np.sum((x - c1) ** 2, axis=1)))
    c2 = x[i2].copy()
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        d1 = np.sum((x - c1) ** 2, axis=1)
        d2 = np.sum((x - c2) ** 2, axis=1)
        labels = (d2 < d1).astype(np.int64)
        if np.any(labels == 0):
            c1 = x[labels == 0].mean(axis=0)
        if np.any(labels == 1):
            c2 = x[labels == 1].mean(axis=0)
    return labels


def _deterministic_2means_cosine(x: np.ndarray, iters: int = 10) -> np.ndarray:
    """Deterministic cosine 2-means++ on L2-normalized vectors returning labels in {0, 1}."""
    n = len(x)
    if n < 2:
        return np.zeros(n, dtype=np.int64)
    mean = _norm_vec(x.mean(axis=0))
    i1 = int(np.argmin(x @ mean))
    c1 = _norm_vec(x[i1])
    i2 = int(np.argmin(x @ c1))
    c2 = _norm_vec(x[i2])
    labels = np.zeros(n, dtype=np.int64)
    for _ in range(iters):
        s1 = x @ c1
        s2 = x @ c2
        labels = (s2 > s1).astype(np.int64)
        if np.any(labels == 0):
            c1 = _norm_vec(x[labels == 0].mean(axis=0))
        if np.any(labels == 1):
            c2 = _norm_vec(x[labels == 1].mean(axis=0))
    return labels


def _nnls_2col(p_norm: np.ndarray, p1_tpl: np.ndarray, p2_tpl: np.ndarray) -> np.ndarray:
    """Exact vectorized 2-variable NNLS returning w_aud = c1 / (c1 + c2)."""
    g11 = float(np.dot(p1_tpl, p1_tpl))
    g22 = float(np.dot(p2_tpl, p2_tpl))
    g12 = float(np.dot(p1_tpl, p2_tpl))
    b1 = p_norm @ p1_tpl
    b2 = p_norm @ p2_tpl
    det = g11 * g22 - g12 * g12
    if det <= 1e-12 or g11 <= 1e-12 or g22 <= 1e-12:
        return np.full(len(p_norm), 0.5, dtype=np.float64)

    c1_u = (g22 * b1 - g12 * b2) / det
    c2_u = (-g12 * b1 + g11 * b2) / det
    interior = (c1_u >= 0.0) & (c2_u >= 0.0)

    c1_b1 = np.maximum(0.0, b1 / g11)
    c2_b2 = np.maximum(0.0, b2 / g22)
    e_b1 = (c1_b1**2) * g11
    e_b2 = (c2_b2**2) * g22

    c1 = np.where(interior, c1_u, np.where(e_b1 >= e_b2, c1_b1, 0.0))
    c2 = np.where(interior, c2_u, np.where(e_b1 >= e_b2, 0.0, c2_b2))
    return np.clip(c1 / (c1 + c2 + 1e-12), 0.0, 1.0)


@dataclass(slots=True)
class _SplitCandidate:
    confidence: float
    p1: np.ndarray
    p2: np.ndarray
    w: np.ndarray
    p1_tpl: np.ndarray
    p2_tpl: np.ndarray


def _run_em_candidate(
    p3: np.ndarray,
    p_bar: np.ndarray,
    act: np.ndarray,
    alpha: np.ndarray,
    bark_norm: np.ndarray,
    fp: np.ndarray,
    w_init: np.ndarray,
    smooth: sparse.csc_matrix,
    cfg: RestoreConfig,
) -> _SplitCandidate | None:
    w = w_init.copy()
    p1 = p3.copy()
    p2 = p3.copy()
    p1_tpl = np.full(bark_norm.shape[1], 1.0 / bark_norm.shape[1], dtype=np.float64)
    p2_tpl = p1_tpl.copy()
    w_pos = w.copy()
    w_aud = w.copy()
    act_f = act.astype(np.float64)

    for _ in range(8):
        p1, p2 = _solve_barycenter_trajectories(p3, w, alpha, smooth)
        u1 = np.maximum(0.0, 2.0 * w - 1.0) ** 2 * act_f
        u2 = np.maximum(0.0, 1.0 - 2.0 * w) ** 2 * act_f
        if float(u1.sum()) < 1e-6:
            u1 = (w**2) * act_f
        if float(u2.sum()) < 1e-6:
            u2 = ((1.0 - w) ** 2) * act_f
        p1_tpl = (u1[:, None] * bark_norm).sum(axis=0)
        p1_tpl /= float(p1_tpl.sum()) + 1e-12
        p2_tpl = (u2[:, None] * bark_norm).sum(axis=0)
        p2_tpl /= float(p2_tpl.sum()) + 1e-12

        f1_tpl = _norm_vec((u1[:, None] * fp).sum(axis=0))
        f2_tpl = _norm_vec((u2[:, None] * fp).sum(axis=0))

        d = p1 - p2
        d_sq = np.sum(d**2, axis=1)
        w_pos = np.clip(np.sum((p3 - p2) * d, axis=1) / (d_sq + 1e-9), 0.0, 1.0)
        w_aud = _nnls_2col(bark_norm, p1_tpl, p2_tpl)

        gamma_pos = np.clip(d_sq / (0.06**2), 0.2, 1.0)
        gamma_aud = float(np.clip((1.0 - float(np.dot(f1_tpl, f2_tpl))) / 0.25, 0.0, 1.0))
        w = (gamma_pos * w_pos + 0.6 * gamma_aud * w_aud) / (gamma_pos + 0.6 * gamma_aud)

    # 5-Factor Confidence Gate evaluated on active frames
    n_act = int(act.sum())
    if n_act < 12:
        return None
    p3_a = p3[act]
    p_bar_a = p_bar[act]
    p1_a = p1[act]
    p2_a = p2[act]
    w_a = w[act]
    w_pos_a = w_pos[act]
    w_aud_a = w_aud[act]
    fp_a = fp[act]

    # 1. Spatial variance reduction
    e1 = float(np.mean(np.sum((p3_a - p_bar_a) ** 2, axis=1)))
    p_rec_a = w_a[:, None] * p1_a + (1.0 - w_a[:, None]) * p2_a
    e2 = float(np.mean(np.sum((p_rec_a - p3_a) ** 2, axis=1)))
    r2_spatial = 1.0 - e2 / (e1 + 1e-9)
    if r2_spatial < 0.45:
        return None

    # 2. Median 3D separation
    sep_vec = p1_a - p2_a
    sep_a = np.linalg.norm(sep_vec, axis=1)
    d_sep = float(np.median(sep_a))
    if d_sep < cfg.split_min_sep:
        return None

    # 3. Balance & support
    w_mean = float(np.mean(w_a))
    if min(w_mean, 1.0 - w_mean) < 0.12:
        return None
    if int((w_a > 0.65).sum()) < 6 or int((w_a < 0.35).sum()) < 6:
        return None

    # 4. Position-audio coupling (pure position-defined prototypes)
    hi_mask = w_pos_a > 0.70
    lo_mask = w_pos_a < 0.30
    if int(hi_mask.sum()) < 3:
        hi_mask = w_pos_a >= float(np.percentile(w_pos_a, 75))
    if int(lo_mask.sum()) < 3:
        lo_mask = w_pos_a <= float(np.percentile(w_pos_a, 25))
    f1_pos = _norm_vec(fp_a[hi_mask].mean(axis=0))
    f2_pos = _norm_vec(fp_a[lo_mask].mean(axis=0))
    d_spec = float(np.clip(1.0 - float(np.dot(f1_pos, f2_pos)), 0.0, 1.0))

    std_p = float(np.std(w_pos_a))
    std_a = float(np.std(w_aud_a))
    if std_p > 1e-6 and std_a > 1e-6:
        rho_pa = float(np.corrcoef(w_pos_a, w_aud_a)[0, 1])
        rho_pa = float(np.clip(0.0 if math.isnan(rho_pa) else rho_pa, 0.0, 1.0))
    else:
        rho_pa = 0.0

    # 5. Segment-line alignment & spatial bimodality
    u_axis = sep_vec / (sep_a[:, None] + 1e-9)
    q = p3_a - 0.5 * (p1_a + p2_a)
    proj = np.sum(q * u_axis, axis=1)
    r_line = float(np.sum(proj**2) / (np.sum(q**2) + 1e-9))
    bimod = float(np.mean((w_pos_a - 0.5) ** 2) * 4.0)

    s_r2 = float(np.clip((r2_spatial - 0.45) / 0.40, 0.0, 1.0))
    s_sep = float(np.clip((d_sep - cfg.split_min_sep) / 0.10, 0.0, 1.0))
    s_spec = float(np.clip(d_spec / 0.18, 0.0, 1.0))
    s_corr = float(np.clip(rho_pa / 0.45, 0.0, 1.0))
    s_line = float(np.clip((r_line - 0.55) / 0.35, 0.0, 1.0))
    s_bimod = float(np.clip((bimod - 0.45) / 0.40, 0.0, 1.0))

    c_ac = 0.30 * s_r2 + 0.20 * s_sep + 0.25 * s_spec + 0.25 * s_corr
    c_sp = 0.35 * s_r2 + 0.25 * s_sep + 0.20 * s_line + 0.20 * s_bimod
    confidence = max(c_ac, 0.85 * c_sp)
    if confidence < cfg.split_confidence:
        return None
    return _SplitCandidate(
        confidence=confidence,
        p1=p1.astype(np.float32),
        p2=p2.astype(np.float32),
        w=w.astype(np.float32),
        p1_tpl=p1_tpl.astype(np.float64),
        p2_tpl=p2_tpl.astype(np.float64),
    )


def _try_split_stretch(
    pos: np.ndarray,
    act: np.ndarray,
    bark: np.ndarray,
    rms: np.ndarray,
    fp: np.ndarray,
    dt: float,
    cfg: RestoreConfig,
) -> _SplitCandidate | None:
    length = len(pos)
    act_idx = np.flatnonzero(act)
    if len(act_idx) < 12:
        return None

    # Linearly interpolate active positions across inactive gaps for SavGol pre-filter
    p3_interp = np.empty((length, 3), dtype=np.float64)
    all_h = np.arange(length, dtype=np.float64)
    for ax in range(3):
        p3_interp[:, ax] = np.interp(all_h, act_idx.astype(np.float64), pos[act_idx, ax])
    p3 = np.where(act[:, None], pos.astype(np.float64), p3_interp)

    max_odd = length if length % 2 == 1 else length - 1
    win = min(max_odd, max(7, round(0.65 / dt) | 1))
    if win >= 5 and max_odd >= 5:
        poly = min(3, win - 2)
        p_bar = signal.savgol_filter(
            p3_interp, window_length=win, polyorder=poly, axis=0, mode="interp"
        )
    else:
        p_bar = p3_interp

    r_all = p3 - p_bar
    r_act = r_all[act]
    r_rms = float(np.sqrt(np.mean(np.sum(r_act**2, axis=1))))
    if r_rms < 0.018:
        return None

    # 2nd-difference roughness on active sequence (95th percentile detects localized jumps too)
    p_act = pos[act_idx].astype(np.float64)
    if len(p_act) >= 3:
        d2 = p_act[2:] - 2.0 * p_act[1:-1] + p_act[:-2]
        if float(np.percentile(np.linalg.norm(d2, axis=1), 95)) < 0.028:
            return None

    # Moving Frenet residual frame for horizontal curved paths
    v_xy = np.gradient(p_bar[:, :2], dt, axis=0)
    speed = np.linalg.norm(v_xy, axis=1)
    moving = speed > 0.05
    r_local = r_all.copy()
    if np.any(moving):
        t_hat = v_xy[moving] / speed[moving, None]
        n_hat = np.column_stack([-t_hat[:, 1], t_hat[:, 0]])
        r_local[moving, 0] = np.sum(r_all[moving, :2] * t_hat, axis=1)
        r_local[moving, 1] = np.sum(r_all[moving, :2] * n_hat, axis=1)

    r_local_norm = r_local[act] / max(r_rms, 1e-4)
    fp_act = fp[act].astype(np.float64)
    fp_centered = fp_act - fp_act.mean(axis=0, keepdims=True)
    if fp_centered.shape[0] >= 3:
        _, _, vt = np.linalg.svd(fp_centered, full_matrices=False)
        fp_pca = fp_centered @ vt[:3].T
    else:
        fp_pca = fp_centered[:, :3]
    feat_joint = np.hstack([r_local_norm, 0.5 * fp_pca])

    labels1 = _deterministic_2means_euclid(feat_joint)
    labels2 = _deterministic_2means_cosine(fp_act)

    e_raw = np.square(rms.astype(np.float64))
    e_med = max(float(np.median(e_raw[act])), 1e-12)
    alpha = np.where(act, np.clip(e_raw / e_med, 0.2, 3.0), 0.0)
    bark_sum = bark.sum(axis=1, keepdims=True).astype(np.float64)
    bark_norm = bark.astype(np.float64) / np.maximum(bark_sum, 1e-12)
    smooth = _smoothness_penalty(length)

    best: _SplitCandidate | None = None
    for labels in (labels1, labels2):
        w_init = np.full(length, 0.5, dtype=np.float64)
        w_init[act] = np.where(labels == 0, 0.88, 0.12)
        cand = _run_em_candidate(p3, p_bar, act, alpha, bark_norm, fp, w_init, smooth, cfg)
        if cand is not None and (best is None or cand.confidence > best.confidence):
            best = cand
    return best


def _subslice_spans(
    spans: list[RawSpan],
    s0: int,
    s1: int,
) -> list[RawSpan]:
    out: list[RawSpan] = []
    for sp in spans:
        ov0 = max(sp.start_hop, s0)
        ov1 = min(sp.end_hop, s1)
        if ov1 <= ov0:
            continue
        off0 = ov0 - sp.start_hop
        off1 = ov1 - sp.start_hop
        out.append(
            RawSpan(
                raw_slot=sp.raw_slot,
                start_hop=ov0,
                end_hop=ov1,
                pos=sp.pos[off0:off1].copy(),
                active=sp.active[off0:off1].copy(),
                split_source=sp.split_source,
                bark=None if sp.bark is None else sp.bark[off0:off1].copy(),
                rms=None if sp.rms is None else sp.rms[off0:off1].copy(),
                peak=None if sp.peak is None else sp.peak[off0:off1].copy(),
            )
        )
    return out


def _stretch_to_chain(
    spans: list[RawSpan],
    pos: np.ndarray,
    active: np.ndarray,
    rms: np.ndarray,
    peak: np.ndarray,
    fp_slice: np.ndarray,
    s0: int,
    s1: int,
    dt: float,
    cfg: RestoreConfig,
) -> _LogicalChain | None:
    act_idx = np.flatnonzero(active)
    if len(act_idx) == 0:
        return None
    cs = s0 + int(act_idx[0])
    ce = s0 + int(act_idx[-1]) + 1
    w_e = np.square(rms[act_idx], dtype=np.float64)
    tot_e = float(np.sum(w_e))
    w_n = w_e / tot_e if tot_e > 1e-18 else np.full(len(act_idx), 1.0 / len(act_idx))
    p_act = pos[act_idx]
    anchor = (p_act * w_n[:, None]).sum(axis=0).astype(np.float32)
    var = float(np.sum(np.square(p_act - anchor[None, :]) * w_n[:, None]))
    pos_std = float(np.sqrt(max(0.0, var)))

    head_k = min(6, len(act_idx))
    tail_k = min(6, len(act_idx))
    h_idx = act_idx[:head_k]
    t_idx = act_idx[-tail_k:]
    start_vel = (
        ((pos[h_idx[-1]] - pos[h_idx[0]]) / max(1e-6, (h_idx[-1] - h_idx[0]) * dt)).astype(np.float32)
        if head_k >= 2 and h_idx[-1] > h_idx[0]
        else np.zeros(3, dtype=np.float32)
    )
    end_vel = (
        ((pos[t_idx[-1]] - pos[t_idx[0]]) / max(1e-6, (t_idx[-1] - t_idx[0]) * dt)).astype(np.float32)
        if tail_k >= 2 and t_idx[-1] > t_idx[0]
        else np.zeros(3, dtype=np.float32)
    )

    head_feat = _mean_feat_window(fp_slice, active, int(act_idx[0]), min(len(active), int(act_idx[0]) + 8))
    tail_feat = _mean_feat_window(fp_slice, active, max(0, int(act_idx[-1]) - 7), int(act_idx[-1]) + 1)
    mean_feat = _mean_feat_window(fp_slice, active, 0, len(active))

    # Update spans' pos and active from decomposed stretch
    updated_spans: list[RawSpan] = []
    for sp in spans:
        off0 = sp.start_hop - s0
        off1 = sp.end_hop - s0
        updated_spans.append(
            RawSpan(
                raw_slot=sp.raw_slot,
                start_hop=sp.start_hop,
                end_hop=sp.end_hop,
                pos=pos[off0:off1].copy(),
                active=active[off0:off1].copy(),
                split_source=sp.split_source,
                bark=None if sp.bark is None else sp.bark.copy(),
                rms=None if sp.rms is None else sp.rms.copy(),
                peak=None if sp.peak is None else sp.peak.copy(),
            )
        )

    return _LogicalChain(
        spans=updated_spans,
        core_intervals=[(cs, ce)],
        last_core_end=ce,
        end_pos=pos[act_idx[-1]].copy(),
        end_vel=end_vel,
        tail_feat=tail_feat,
        prototypes=[(tot_e, mean_feat)],
        anchor_pos=anchor,
        pos_std=pos_std,
        is_stationary=pos_std < cfg.stationary_radius,
        total_energy=tot_e,
        peak_amp=float(np.max(peak[act_idx])),
        start_pos=pos[act_idx[0]].copy(),
        first_core_start=cs,
    )


def decompose_coclustered_objects(
    chains: list[_LogicalChain],
    feats: SlotFeatures,
    cfg: RestoreConfig,
) -> tuple[list[_LogicalChain], int]:
    """Detect and split co-clustered overlapping trajectories into separate objects."""
    if not cfg.enable_split or not chains:
        return chains, 0

    t_len = feats.rms.shape[1]
    dt = feats.hop_samples / feats.sample_rate
    bridge_hops = max(1, round(0.5 / dt))
    min_stretch_hops = max(12, round(cfg.split_min_duration_s / dt))
    min_peak_lin = 10.0 ** (cfg.min_object_peak_db / 20.0)

    next_split_id = 0
    total_splits = 0
    current = list(chains)

    for _depth in range(cfg.split_max_depth):
        if len(current) >= cfg.max_dynamic_objects:
            break
        depth_splits = 0
        next_round: list[_LogicalChain] = []

        for ch in current:
            if len(current) + depth_splits >= cfg.max_dynamic_objects:
                next_round.append(ch)
                continue
            pos, act, bark, rms, peak = _chain_arrays(ch, feats, t_len)
            # Compute local fingerprints from chain's bark & parent slot auxiliary dims
            aux_flat = np.zeros((1, t_len, 3), dtype=np.float32)
            aux_harm = np.zeros((1, t_len), dtype=np.float32)
            for sp in ch.spans:
                # Reconstruct auxiliary timbral cues from parent raw slot
                s0, s1 = sp.start_hop, sp.end_hop
                m = sp.raw_slot
                aux_flat[0, s0:s1] = feats.fingerprint[m, s0:s1, 24:27] / 0.35
                aux_harm[0, s0:s1] = (feats.fingerprint[m, s0:s1, 30] / 0.35 + 1.0) * 0.5
            fp_chain = build_fingerprints(bark[None, :, :], aux_flat, aux_harm)[0]

            # Find stretches bridging <= 0.5s gaps
            act_bridged = act.copy()
            idx = np.flatnonzero(act)
            for a, b in zip(idx[:-1], idx[1:], strict=True):
                if 1 < b - a <= bridge_hops:
                    act_bridged[a + 1 : b] = True
            padded = np.concatenate([[False], act_bridged, [False]])
            s_starts = np.flatnonzero(~padded[:-1] & padded[1:])
            s_ends = np.flatnonzero(padded[:-1] & ~padded[1:])

            split_children: list[_LogicalChain] = []
            did_split_chain = False

            for ss, se in zip(s_starts, s_ends, strict=True):
                s0, s1 = int(ss), int(se)
                # Expand stretch edges slightly to span boundaries when consecutive
                spans_sub = _subslice_spans(ch.spans, s0, s1)
                if not spans_sub:
                    continue
                if s1 - s0 < min_stretch_hops or int(act[s0:s1].sum()) < 12:
                    sub_ch = _stretch_to_chain(
                        spans_sub, pos[s0:s1], act[s0:s1], rms[s0:s1], peak[s0:s1], fp_chain[s0:s1], s0, s1, dt, cfg
                    )
                    if sub_ch is not None:
                        split_children.append(sub_ch)
                    continue

                cand = _try_split_stretch(
                    pos[s0:s1], act[s0:s1], bark[s0:s1], rms[s0:s1], fp_chain[s0:s1], dt, cfg
                )
                if cand is None:
                    sub_ch = _stretch_to_chain(
                        spans_sub, pos[s0:s1], act[s0:s1], rms[s0:s1], peak[s0:s1], fp_chain[s0:s1], s0, s1, dt, cfg
                    )
                    if sub_ch is not None:
                        split_children.append(sub_ch)
                    continue

                did_split_chain = True
                depth_splits += 1
                total_splits += 1
                node = SplitNode(
                    split_id=next_split_id,
                    start_hop=s0,
                    end_hop=s1,
                    weights=cand.w,
                    parent_spans=tuple(spans_sub),
                )
                next_split_id += 1

                w_col = cand.w[:, None].astype(np.float64)
                w1_col = 1.0 - w_col
                denom = w_col * cand.p1_tpl[None, :] + w1_col * cand.p2_tpl[None, :] + 1e-12
                m1 = (w_col * cand.p1_tpl[None, :]) / denom
                m2 = 1.0 - m1
                b_sub = bark[s0:s1].astype(np.float64)
                b_tot = b_sub.sum(axis=1, keepdims=True)
                b1 = b_sub * m1
                b2 = b_sub * m2
                e_r1 = np.where(b_tot[:, 0] > 1e-12, b1.sum(axis=1) / np.maximum(b_tot[:, 0], 1e-12), cand.w)
                e_r2 = 1.0 - e_r1

                r1 = rms[s0:s1] * np.sqrt(np.clip(e_r1, 0.0, 1.0)).astype(np.float32)
                r2 = rms[s0:s1] * np.sqrt(np.clip(e_r2, 0.0, 1.0)).astype(np.float32)
                pk1 = peak[s0:s1] * np.sqrt(np.clip(e_r1, 0.0, 1.0)).astype(np.float32)
                pk2 = peak[s0:s1] * np.sqrt(np.clip(e_r2, 0.0, 1.0)).astype(np.float32)
                a1 = act[s0:s1] & (cand.w > 0.03)
                a2 = act[s0:s1] & ((1.0 - cand.w) > 0.03)

                for br, (c_pos, c_act, c_bark, c_rms, c_pk) in enumerate(
                    ((cand.p1, a1, b1, r1, pk1), (cand.p2, a2, b2, r2, pk2))
                ):
                    if float(np.max(c_pk)) < min_peak_lin or not np.any(c_act):
                        continue
                    sp_c = [
                        RawSpan(
                            raw_slot=spans_sub[0].raw_slot,
                            start_hop=s0,
                            end_hop=s1,
                            pos=c_pos.copy(),
                            active=c_act.copy(),
                            split_source=(node, br),
                            bark=c_bark.astype(np.float32),
                            rms=c_rms.astype(np.float32),
                            peak=c_pk.astype(np.float32),
                        )
                    ]
                    c_fp = build_fingerprints(
                        c_bark[None, :, :].astype(np.float32),
                        aux_flat[:, s0:s1],
                        aux_harm[:, s0:s1],
                    )[0]
                    c_ch = _stretch_to_chain(sp_c, c_pos, c_act, c_rms, c_pk, c_fp, s0, s1, dt, cfg)
                    if c_ch is not None:
                        split_children.append(c_ch)

            if did_split_chain and split_children:
                next_round.extend(split_children)
            else:
                next_round.append(ch)

        if depth_splits == 0:
            break
        # Post-split Re-ID reconnects split stretches with standalone segments
        current = _agglomerative_reid(next_round, dt, cfg)

    return current, total_splits


# ---------------------------------------------------------------------------
# 1.6 Stage 4: Key Decimation, Plan, Trace & Streaming ADM Synthesis
# ---------------------------------------------------------------------------


def _dp_decimate(
    points: np.ndarray, times: np.ndarray, i0: int, i1: int, tol: float, keep: list[int]
) -> None:
    if i1 <= i0 + 1:
        return
    t0, t1 = float(times[i0]), float(times[i1])
    dt_span = t1 - t0
    if dt_span <= 1e-12:
        return
    p0, p1 = points[i0], points[i1]
    u = ((times[i0 + 1 : i1] - t0) / dt_span)[:, None]
    pred = p0[None, :] + (p1 - p0)[None, :] * u
    errs = np.linalg.norm(points[i0 + 1 : i1] - pred, axis=1)
    rel = int(np.argmax(errs))
    if float(errs[rel]) > tol:
        mid = i0 + 1 + rel
        _dp_decimate(points, times, i0, mid, tol, keep)
        keep.append(mid)
        _dp_decimate(points, times, mid, i1, tol, keep)


def build_trajectory_keys(
    pos: np.ndarray, active: np.ndarray, times: np.ndarray, tol: float = 0.004
) -> list[Key]:
    """Build compact OAMD/ADM trajectory keys with snap on reactivation and DP decimation."""
    t_len = len(pos)
    if t_len == 0:
        return [(0.0, 0.0, 0.5, 0.5, 0.0, True)]

    keys: list[Key] = []
    padded = np.concatenate([[False], active, [False]])
    starts = np.flatnonzero(~padded[:-1] & padded[1:])
    ends = np.flatnonzero(padded[:-1] & ~padded[1:])

    if len(starts) == 0:
        x, y, z = (float(v) for v in pos[0])
        return [(0.0, 0.0, x, y, z, False)]

    if starts[0] > 0:
        x, y, z = (float(v) for v in pos[starts[0]])
        keys.append((0.0, 0.0, x, y, z, False))

    for s, e in zip(starts, ends, strict=True):
        t_center = float(times[s]) if s else 0.0
        # Snap at the PCM hop boundary, so a reused track has the new position
        # as soon as its next fragment starts.
        t_start = float(0.5 * (times[s - 1] + t_center)) if s else 0.0
        x0, y0, z0 = (float(v) for v in pos[s])
        keys.append((max(0.0, t_start), 0.0, x0, y0, z0, True))
        if e - s > 1:
            keep = [int(s)]
            _dp_decimate(pos.astype(np.float64), times, int(s), int(e) - 1, tol, keep)
            keep.append(int(e) - 1)
            prev_t = max(0.0, t_center)
            for k in keep[1:]:
                tk = max(prev_t, float(times[k]))
                xk, yk, zk = (float(v) for v in pos[k])
                keys.append((tk, max(0.0, tk - prev_t), xk, yk, zk, True))
                prev_t = tk
        if e < t_len:
            te = float(times[e])
            xe, ye, ze = (float(v) for v in pos[e - 1])
            keys.append((max(0.0, te), 0.0, xe, ye, ze, False))
    return keys


def _pack_quiet_fragments(
    chains: list[_LogicalChain], feats: SlotFeatures, cfg: RestoreConfig
) -> list[_LogicalChain]:
    """Reuse tracks for sparse, quiet chains whose audio spans do not overlap."""
    dt = feats.hop_samples / feats.sample_rate
    peak_limit = 10.0 ** (cfg.quiet_fragment_peak_db / 20.0)
    rms_floor = 10.0 ** (cfg.min_object_peak_db / 20.0)
    # Allow both crossfade tails and leave space for an inactive trajectory key.
    gap = max(1, math.ceil(cfg.crossfade_ms / (1000.0 * dt)))
    objects: list[_LogicalChain] = []
    packed: list[tuple[_LogicalChain, list[tuple[int, int]]]] = []

    for chain in sorted(chains, key=lambda ch: -ch.total_energy):
        if chain.peak_amp >= peak_limit:
            objects.append(chain)
            continue
        audible_hops = 0
        for span in chain.spans:
            rms = span.rms
            if rms is None:
                rms = feats.rms[span.raw_slot, span.start_hop : span.end_hop]
            audible_hops += np.count_nonzero(span.active & (rms >= rms_floor))
        if audible_hops * dt >= cfg.quiet_fragment_duration_s:
            objects.append(chain)
            continue

        intervals = [(sp.start_hop, sp.end_hop) for sp in chain.spans]
        for i, (track, occupied) in enumerate(packed):
            if not _intervals_overlap(occupied, intervals, allow_touch=-gap):
                packed[i] = (
                    _merge_chains(track, chain, cfg),
                    sorted(occupied + intervals),
                )
                break
        else:
            packed.append((chain, intervals))
    return objects + [track for track, _ in packed]


def plan_object_restoration(feats: SlotFeatures, cfg: RestoreConfig | None = None) -> RestorationPlan:
    """Run Stages 1–4 planning on `SlotFeatures` and return deterministic `RestorationPlan`."""
    c = cfg or RestoreConfig()
    m, t_len = feats.rms.shape
    dt = feats.hop_samples / feats.sample_rate
    times = (
        np.arange(t_len, dtype=np.float64) * feats.hop_samples
        + 0.5 * feats.hop_samples
        + feats.hop_start_sample
    ) / feats.sample_rate

    tracklets = segment_slots(feats, c)
    chains = stitch_tracklets(tracklets, dt, c)
    chains, splits = decompose_coclustered_objects(chains, feats, c)
    chains = _pack_quiet_fragments(chains, feats, c)

    min_peak_lin = 10.0 ** (c.min_object_peak_db / 20.0)
    kept: list[tuple[_LogicalChain, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
    for ch in chains:
        pos, act, bark, rms, peak = _chain_arrays(ch, feats, t_len)
        if not np.any(act) or float(np.max(peak)) < min_peak_lin:
            continue
        kept.append((ch, pos, act, bark, rms, peak))

    # Enforce hard dynamic object ceiling if a pathological track still exceeds max_dynamic_objects
    if len(kept) > c.max_dynamic_objects:
        kept.sort(key=lambda item: item[0].total_energy, reverse=True)
        kept = kept[: c.max_dynamic_objects]

    kept.sort(key=lambda item: -item[0].total_energy)
    objects: list[RestoredObjectPlan] = []
    for out_idx, (ch, pos, act, bark, rms, peak) in enumerate(kept):
        keys = build_trajectory_keys(pos, act, times)
        objects.append(
            RestoredObjectPlan(
                index=out_idx,
                spans=ch.spans,
                keys=keys,
                pos=pos,
                active=act,
                bark_power=bark,
                rms=rms,
                peak=peak,
                is_stationary=ch.is_stationary,
                total_energy=ch.total_energy,
            )
        )

    return RestorationPlan(
        objects=tuple(objects),
        input_objects=m,
        splits_applied=splits,
        hop_samples=feats.hop_samples,
        sample_rate=feats.sample_rate,
        hop_start_sample=feats.hop_start_sample,
    )


def build_restored_trace(
    plan: RestorationPlan,
    program: Program,
    layout: SpeakerLayout,
    n_syncframes: int,
    syncframe_samples: int,
    first_sample: int,
    total_length: int,
    exact_levels: np.ndarray | None = None,
) -> ObjectTrace:
    """Build `ObjectTrace` on the syncframe timeline for 3D soundfield video & playback."""
    sr = plan.sample_rate
    level_start = -first_sample / sr
    level_step = syncframe_samples / sr
    frame_times = (
        np.arange(n_syncframes, dtype=np.float64) * syncframe_samples
        + 0.5 * syncframe_samples
        - first_sample
    ) / sr
    hop_times = (
        np.arange(plan.objects[0].pos.shape[0] if plan.objects else 0, dtype=np.float64)
        * plan.hop_samples
        + 0.5 * plan.hop_samples
        + plan.hop_start_sample
    ) / sr

    n_spk = len(layout.channels)
    paths: list[ObjectPath] = []
    anchored = program.anchored
    new_program = replace(program, dynamic_objects=len(plan.objects))

    for i, obj in enumerate(plan.objects):
        if exact_levels is not None and i < exact_levels.shape[0]:
            levels = exact_levels[i].astype(np.float64, copy=False)
        elif len(hop_times) > 0:
            pk_interp = np.interp(frame_times, hop_times, obj.peak.astype(np.float64))
            levels = np.where(pk_interp > 1e-6, np.maximum(SILENCE_DB, 20.0 * np.log10(np.maximum(pk_interp, 1e-12))), SILENCE_DB)
        else:
            levels = np.full(n_syncframes, SILENCE_DB, dtype=np.float64)

        pos_f, act_f = eval_keys_on_grid(obj.keys, frame_times)
        shares = np.zeros((n_syncframes, n_spk), dtype=np.float32)
        for f_idx in range(n_syncframes):
            if not act_f[f_idx] or levels[f_idx] <= SILENCE_DB + 0.1:
                continue
            x, y, z = float(pos_f[f_idx, 0]), float(pos_f[f_idx, 1]), float(pos_f[f_idx, 2])
            r = Render(x_bits=62.0 * x, y_bits=62.0 * y, z_bits=15.0 * z)
            g = object_gains(layout, r, None)
            e = g * g
            tot = float(np.sum(e))
            if tot > 0.0:
                shares[f_idx] = (e / tot).astype(np.float32)

        paths.append(
            ObjectPath(
                index=anchored + i,
                keys=list(obj.keys),
                levels=np.round(np.maximum(SILENCE_DB, levels), 1),
                shares=shares,
            )
        )

    return ObjectTrace(
        sample_rate=int(round(sr)),
        level_start=level_start,
        level_step=level_step,
        duration=total_length / sr,
        program=new_program,
        objects=tuple(paths),
        speakers=layout.channels,
    )


def separate_stretch_audio(
    audio_1d: np.ndarray,
    sr: float,
    hop_samples: float,
    w_curve: np.ndarray,
    sep_n_fft: int = 2048,
    *,
    hop_origin: float = 0.0,
    token: CancellationToken | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Estimate source spectra from spatial energy shares and apply a soft Wiener mask."""
    x = np.asarray(audio_1d, dtype=np.float32)
    n_samples = len(x)
    if not n_samples or not np.any(x):
        return np.zeros_like(x), x.copy()

    hop_centers = (np.arange(len(w_curve), dtype=np.float64) + 0.5) * hop_samples + hop_origin
    nperseg = min(sep_n_fft, n_samples)
    if nperseg < 64 or float(np.max(np.abs(x))) <= 1e-12:
        w = np.interp(np.arange(n_samples), hop_centers, w_curve).astype(np.float32)
        s1 = x * w
        return s1, x - s1

    hop_eff = min(max(1, round(hop_samples)), nperseg // 2)
    noverlap = nperseg - hop_eff
    _, times, mix = signal.stft(
        x, fs=sr, window="hann", nperseg=nperseg, noverlap=noverlap, boundary="zeros", padded=True
    )
    centers = times * sr
    shares = np.interp(centers, hop_centers, w_curve).astype(np.float32)
    power = mix.real**2 + mix.imag**2
    frame_power = power.sum(axis=0)
    # A partially padded window and a near-silent window are poor observations of timbre.
    valid = (centers >= nperseg / 2) & (centers <= n_samples - nperseg / 2)
    valid &= frame_power > float(frame_power.max()) * 1e-5
    if not np.any(valid):
        valid = frame_power > 0.0
    np.divide(power, frame_power, out=power, where=frame_power > 0)
    # Dominant-source frames are more informative than an even mix, especially
    # for instruments whose spectra change quickly between notes or attacks.
    confidence = valid * (0.1 + 0.9 * (2.0 * shares - 1.0)**4)
    power *= confidence

    # Fit normalized mixture power as P_mix = w * P1 + (1 - w) * P2.
    # Extrapolating to each endpoint removes leakage from observations where both sources play.
    weight_sum = float(confidence.sum())
    mean_share = float(np.dot(confidence, shares) / weight_sum)
    centered = shares - mean_share
    prior_variance = 0.0025
    global_slope_scale = np.dot(confidence, centered**2) / weight_sum + prior_variance
    # Include enough context around transients to observe both sources' timbres.
    window = max(3, round(2.5 * sr / hop_eff) | 1)
    support = ndimage.uniform_filter1d(confidence, window, mode="constant")
    safe_support = np.maximum(support, 1e-6)
    local_share = ndimage.uniform_filter1d(shares * confidence, window, mode="constant") / safe_support
    local_var = ndimage.uniform_filter1d(shares**2 * confidence, window, mode="constant")
    local_var = np.maximum(local_var / safe_support - local_share**2, 0.0)
    # Use local timbre only where the fit has enough distinct, audible observations.
    local_blend = np.minimum(1.0, 4.0 * support) * local_var / (local_var + 0.01)
    for start in range(0, len(mix), 32):
        if token is not None:
            token.raise_if_cancelled()
        bins = slice(start, start + 32)
        p = power[bins]
        avg = p.sum(axis=1, keepdims=True) / weight_sum
        slope = (p @ centered)[:, None] / (weight_sum * global_slope_scale)
        global_1 = np.maximum(avg + (1.0 - mean_share) * slope, 0.0)
        global_2 = np.maximum(avg - mean_share * slope, 0.0)

        local_avg = ndimage.uniform_filter1d(p, window, axis=1, mode="constant") / safe_support
        local_cov = ndimage.uniform_filter1d(p * shares, window, axis=1, mode="constant")
        local_cov = local_cov / safe_support - local_avg * local_share
        local_slope = local_cov / (local_var + prior_variance)
        local_1 = np.maximum(local_avg + (1.0 - local_share) * local_slope, 0.0)
        local_2 = np.maximum(local_avg - local_share * local_slope, 0.0)

        # Keep a small shared spectral floor to limit holes from imperfect spatial shares.
        floor = 0.005 * avg
        src1 = ((1.0 - local_blend) * global_1 + local_blend * local_1 + floor) * shares
        src2 = ((1.0 - local_blend) * global_2 + local_blend * local_2 + floor) * (1.0 - shares)
        total = src1 + src2
        mask = np.broadcast_to(shares, total.shape).copy()
        np.divide(src1, total, out=mask, where=total > 0.0)
        mix[bins] *= mask

    _, rec = signal.istft(
        mix, fs=sr, window="hann", nperseg=nperseg, noverlap=noverlap, boundary=True
    )
    s1 = rec[:n_samples].astype(np.float32, copy=False)
    # The residual keeps the two outputs complementary through overlap-add.
    return s1, x - s1


def _hop_to_sample(hop_idx: int, hop_samples_out: float, hop_start_out: float, length: int) -> int:
    return max(0, min(length, round(hop_idx * hop_samples_out + hop_start_out)))


def _adm_layout_from_program(
    program: Program, n_dynamic: int
) -> tuple[str, tuple[_Track, ...], tuple[_Track, ...]]:
    labels = program.bed_labels
    name, assigned = _bed(labels)
    by_channel = {channel: index for index, channel in assigned.items()}
    channels = dict(_BED_SETS).get(name, ())
    bed = tuple(
        _Track(by_channel.get(channel), _BED_CHANNELS[channel][0], channel) for channel in channels
    )
    from soundweaver.core.speakers import BED_POSITIONS

    pinned = tuple(
        _Track(index, f"Bed {label}", pinned=BED_POSITIONS[label])
        for index, label in enumerate(labels)
        if index not in assigned
    )
    isf = tuple(
        _Track(index, f"ISF {index}", pinned=pos)
        for index in range(len(labels), program.anchored)
        if (pos := program.isf_position(index)) is not None
    )
    dynamic = tuple(
        _Track(program.anchored + i, f"Object {i + 1}") for i in range(n_dynamic)
    )
    return name, bed, pinned + isf + dynamic


def write_restored_adm(
    raw_memmap: np.ndarray,
    raw_bed_indices: Sequence[int],
    raw_pinned_tracks: Sequence[tuple[int, str, tuple[float, float, float]]],
    raw_dynamic_indices: Sequence[int],
    plan: RestorationPlan,
    program: Program,
    target: Path,
    sample_rate: int,
    length: int,
    cfg: RestoreConfig | None = None,
    *,
    adm_layout: tuple[str, tuple[_Track, ...], tuple[_Track, ...]] | None = None,
    hop_samples_out: float | None = None,
    hop_start_out: float | None = None,
    syncframe_samples: int = 1536,
    first_sample: int = 0,
    n_syncframes: int = 0,
    token: CancellationToken | None = None,
    remove_bed_placeholders: bool = False,
) -> tuple[np.ndarray | None, float]:
    """Stream restored 24-bit ADM BWF to `target` and return `(exact_levels, max_sum_error)`."""
    c = cfg or RestoreConfig()
    hop_out = float(plan.hop_samples) if hop_samples_out is None else float(hop_samples_out)
    start_out = float(plan.hop_start_sample) if hop_start_out is None else float(hop_start_out)
    half_fade = max(0, round(c.crossfade_ms * sample_rate / 2000.0))

    # Pre-analyze internal cuts on each raw slot (including SplitNode ancestors)
    raw_spans_by_slot: dict[int, list[tuple[int, int]]] = {}
    seen_nodes: set[int] = set()

    def _collect_raw_spans(span_list: Sequence[RawSpan]) -> None:
        for sp in span_list:
            if sp.split_source is None:
                s0 = _hop_to_sample(sp.start_hop, hop_out, start_out, length)
                s1 = _hop_to_sample(sp.end_hop, hop_out, start_out, length)
                if s1 > s0:
                    raw_spans_by_slot.setdefault(sp.raw_slot, []).append((s0, s1))
            else:
                node, _ = sp.split_source
                if node.split_id not in seen_nodes:
                    seen_nodes.add(node.split_id)
                    _collect_raw_spans(node.parent_spans)

    for obj in plan.objects:
        _collect_raw_spans(obj.spans)

    cuts_by_slot: dict[int, set[int]] = {}
    for slot, intervals in raw_spans_by_slot.items():
        intervals.sort()
        cuts: set[int] = set()
        for a, b in zip(intervals[:-1], intervals[1:], strict=True):
            if a[1] == b[0]:
                cuts.add(a[1])
        cuts_by_slot[slot] = cuts

    # Evaluate SplitNode audio cache recursively (on demand, bounded to split stretches)
    split_cache: dict[int, tuple[int, np.ndarray, np.ndarray]] = {}

    def _node_bounds(node: SplitNode) -> tuple[int, int]:
        n0 = _hop_to_sample(node.start_hop, hop_out, start_out, length)
        n1 = _hop_to_sample(node.end_hop, hop_out, start_out, length)
        # If an outer boundary of a raw-sourced node meets a neighboring span on the same slot,
        # extend by half_fade so complementary crossfading conserves sample sums exactly.
        ext0, ext1 = n0, n1
        if node.parent_spans:
            first_p, last_p = node.parent_spans[0], node.parent_spans[-1]
            if first_p.split_source is None and n0 in cuts_by_slot.get(first_p.raw_slot, ()):
                ext0 = max(0, n0 - half_fade)
            if last_p.split_source is None and n1 in cuts_by_slot.get(last_p.raw_slot, ()):
                ext1 = min(length, n1 + half_fade)
        return ext0, ext1

    def _eval_split(node: SplitNode) -> tuple[int, np.ndarray, np.ndarray]:
        if node.split_id in split_cache:
            return split_cache[node.split_id]
        ext0, ext1 = _node_bounds(node)
        parent_buf = np.zeros(max(0, ext1 - ext0), dtype=np.float32)
        for p_sp in node.parent_spans:
            _accum_span(parent_buf, ext0, ext1, p_sp, cuts_by_slot)
        s1, s2 = separate_stretch_audio(
            parent_buf,
            sample_rate,
            hop_out,
            node.weights,
            c.sep_n_fft,
            hop_origin=node.start_hop * hop_out + start_out - ext0,
            token=token,
        )
        split_cache[node.split_id] = (ext0, s1, s2)
        return ext0, s1, s2

    def _accum_span(
        dst: np.ndarray,
        blk_start: int,
        blk_end: int,
        sp: RawSpan,
        slot_cuts: dict[int, set[int]],
    ) -> None:
        s0 = _hop_to_sample(sp.start_hop, hop_out, start_out, length)
        s1 = _hop_to_sample(sp.end_hop, hop_out, start_out, length)
        if s1 <= s0:
            return
        if sp.split_source is not None:
            node, br = sp.split_source
            ext0, b0, b1 = _eval_split(node)
            buf = b0 if br == 0 else b1
            n0 = _hop_to_sample(node.start_hop, hop_out, start_out, length)
            n1 = _hop_to_sample(node.end_hop, hop_out, start_out, length)
            sp_lo = ext0 if sp.start_hop == node.start_hop else max(ext0, s0)
            sp_hi = ext0 + len(buf) if sp.end_hop == node.end_hop else min(ext0 + len(buf), s1)
            if sp_hi <= sp_lo and n1 > n0:
                sp_lo = max(ext0, s0)
                sp_hi = min(ext0 + len(buf), s1)
            ov0 = max(blk_start, sp_lo)
            ov1 = min(blk_end, sp_hi)
            if ov1 <= ov0:
                return
            dst[ov0 - blk_start : ov1 - blk_start] += buf[ov0 - ext0 : ov1 - ext0]
            return

        raw_ch = raw_dynamic_indices[sp.raw_slot]
        cuts = slot_cuts.get(sp.raw_slot, set())
        cut_in = half_fade if s0 in cuts else 0
        cut_out = half_fade if s1 in cuts else 0
        ext0 = max(0, s0 - cut_in)
        ext1 = min(length, s1 + cut_out)
        ov0 = max(blk_start, ext0)
        ov1 = min(blk_end, ext1)
        if ov1 <= ov0:
            return
        seg = np.array(raw_memmap[raw_ch, ov0:ov1], dtype=np.float32)
        if cut_in > 0:
            fi0 = s0 - cut_in
            fi1 = s0 + cut_in
            f0 = max(ov0, fi0)
            f1 = min(ov1, fi1)
            if f1 > f0:
                u = (np.arange(f0, f1, dtype=np.float64) - fi0 + 0.5) / (2.0 * cut_in)
                g_in = (0.5 * (1.0 - np.cos(math.pi * np.clip(u, 0.0, 1.0)))).astype(np.float32)
                seg[f0 - ov0 : f1 - ov0] *= g_in
        if cut_out > 0:
            fo0 = s1 - cut_out
            fo1 = s1 + cut_out
            f0 = max(ov0, fo0)
            f1 = min(ov1, fo1)
            if f1 > f0:
                u = (np.arange(f0, f1, dtype=np.float64) - fo0 + 0.5) / (2.0 * cut_out)
                g_out = (0.5 * (1.0 + np.cos(math.pi * np.clip(u, 0.0, 1.0)))).astype(np.float32)
                seg[f0 - ov0 : f1 - ov0] *= g_out
        dst[ov0 - blk_start : ov1 - blk_start] += seg

    # Pre-materialize any split nodes so block streaming stays clean and fast
    for obj in plan.objects:
        for sp in obj.spans:
            if sp.split_source is not None:
                _eval_split(sp.split_source[0])

    n_dyn = len(plan.objects)
    new_program = replace(program, dynamic_objects=n_dyn)
    if adm_layout is None:
        adm_layout = _adm_layout_from_program(new_program, n_dyn)
    bed_name, bed_tracks, other_tracks = adm_layout
    adm = AdmFile(
        target,
        sample_rate,
        length,
        new_program,
        new_program.anchored + n_dyn,
        layout=(bed_name, bed_tracks, other_tracks),
        remove_bed_placeholders=remove_bed_placeholders,
    )

    # Map each AdmFile track index to its raw source or restored object index
    exact_levels = (
        np.full((n_dyn, n_syncframes), SILENCE_DB, dtype=np.float64) if n_syncframes > 0 else None
    )
    keys_map: dict[int, list[Key]] = {}
    for i, obj in enumerate(plan.objects):
        keys_map[new_program.anchored + i] = obj.keys

    static_raw_by_track: dict[int, int] = {}
    static_raw_list = list(raw_bed_indices) + [p[0] for p in raw_pinned_tracks]
    static_it = iter(static_raw_list)
    n_static_adm = len(adm.tracks) - n_dyn
    for ch_out in range(n_static_adm):
        tr = adm.tracks[ch_out]
        if tr.source is not None:
            val = next(static_it, None)
            if val is not None:
                static_raw_by_track[ch_out] = val

    chunk_frames = 65536
    max_sum_error = 0.0
    try:
        for start in range(0, length, chunk_frames):
            if token is not None:
                token.raise_if_cancelled()
            end = min(length, start + chunk_frames)
            width = end - start
            block = np.zeros((adm.channels, width), dtype=np.float32)

            for ch_out, raw_idx in static_raw_by_track.items():
                block[ch_out] = raw_memmap[raw_idx, start:end]

            rest_sum = np.zeros(width, dtype=np.float64)
            for oi, obj in enumerate(plan.objects):
                ch_out = n_static_adm + oi
                row = block[ch_out]
                for sp in obj.spans:
                    _accum_span(row, start, end, sp, cuts_by_slot)
                rest_sum += row.astype(np.float64)

                if exact_levels is not None and syncframe_samples > 0:
                    # Update syncframe peak levels overlapping [start, end)
                    f_lo = max(0, (start + first_sample) // syncframe_samples)
                    f_hi = min(n_syncframes, (end + first_sample - 1) // syncframe_samples + 1)
                    for f_idx in range(f_lo, f_hi):
                        fs = f_idx * syncframe_samples - first_sample
                        fe = fs + syncframe_samples
                        ov0 = max(start, fs)
                        ov1 = min(end, fe)
                        if ov1 > ov0:
                            pk = float(np.max(np.abs(row[ov0 - start : ov1 - start])))
                            if pk > 1e-12:
                                db = max(SILENCE_DB, 20.0 * math.log10(pk))
                                if db > exact_levels[oi, f_idx]:
                                    exact_levels[oi, f_idx] = db

            if raw_dynamic_indices:
                orig_sum = np.sum(
                    raw_memmap[list(raw_dynamic_indices), start:end].astype(np.float64), axis=0
                )
                err = float(np.max(np.abs(rest_sum - orig_sum)))
                if err > max_sum_error:
                    max_sum_error = err

            data, clipped = pcm24(block)
            if clipped:
                logger.warning("adm restore: %d samples clipped in block %d..%d", clipped, start, end)
            adm.write_pcm(data)
        adm.close(keys_map)
    except BaseException:
        adm.discard()
        raise

    return exact_levels, max_sum_error


def restore_adm_file(
    source: Path,
    target: Path,
    config: RestoreConfig | None = None,
    token: CancellationToken | None = None,
) -> RestoreResult:
    """De-cluster and stabilize dynamic objects of an ADM BWF file into `target`."""
    cfg = config or RestoreConfig()
    prep = PreparedADM(source)
    try:
        sr = prep.sample_rate
        n_samples = prep.frame_count
        pcm = prep.read_frames(0, n_samples).T  # (channels, n_samples) float32

        bed_trs = [tr for tr in prep.tracks if not tr.is_object]
        pinned_trs = [tr for tr in prep.tracks if tr.is_object and tr.name.startswith(("Bed ", "ISF "))]
        dyn_trs = [tr for tr in prep.tracks if tr.is_object and not tr.name.startswith(("Bed ", "ISF "))]

        # Build ADM track layout preserving beds and pinned tracks cleanly
        bed_labels = tuple(tr.speaker_label or "L" for tr in bed_trs)
        bed_name, assigned = _bed(bed_labels)
        by_channel = {ch: idx for idx, ch in assigned.items()}
        bed_ch_names = dict(_BED_SETS).get(bed_name, ())
        adm_bed = tuple(
            _Track(by_channel.get(ch), _BED_CHANNELS[ch][0], ch) for ch in bed_ch_names
        )
        raw_bed_indices = [bed_trs[by_channel[ch]].channel_index for ch in bed_ch_names if ch in by_channel]

        from soundweaver.core.speakers import BED_POSITIONS

        extra_pinned: list[tuple[int, str, tuple[float, float, float]]] = []
        adm_pinned: list[_Track] = []
        next_src = len(bed_labels)
        for idx, tr in enumerate(bed_trs):
            if idx not in assigned:
                lbl = tr.speaker_label or tr.name
                pos = BED_POSITIONS.get(lbl, (0.5, 0.5, 0.0))
                adm_pinned.append(_Track(next_src, tr.name, pinned=pos))
                extra_pinned.append((tr.channel_index, tr.name, pos))
                next_src += 1
        for tr in pinned_trs:
            pos = (tr.blocks[0].x, tr.blocks[0].y, tr.blocks[0].z) if tr.blocks else (0.5, 0.5, 0.0)
            adm_pinned.append(_Track(next_src, tr.name, pinned=pos))
            extra_pinned.append((tr.channel_index, tr.name, pos))
            next_src += 1

        dyn_indices = [tr.channel_index for tr in dyn_trs]
        dyn_audio = pcm[dyn_indices] if dyn_indices else np.zeros((0, n_samples), dtype=np.float32)

        keys_per_slot: list[list[Key]] = []
        for tr in dyn_trs:
            klist: list[Key] = []
            prev_t = 0.0
            for blk in tr.blocks:
                ramp = max(0.0, blk.start - prev_t) if klist else 0.0
                klist.append((blk.start, ramp, blk.x, blk.y, blk.z, blk.active))
                prev_t = blk.start
            if not klist:
                klist.append((0.0, 0.0, 0.5, 0.5, 0.0, True))
            keys_per_slot.append(klist)

        feats = extract_slot_features(dyn_audio, keys_per_slot, sr, cfg, hop_start_sample=0)
        plan = plan_object_restoration(feats, cfg)

        program = Program(
            bed_labels=tuple(bed_labels[i] for i in sorted(assigned)),
            isf_objects=len(adm_pinned),
            dynamic_objects=len(plan.objects),
        )
        # Re-base adm_pinned source ids to start at len(program.bed_labels)
        base_pin = len(program.bed_labels)
        adm_pinned_rebased = tuple(
            _Track(base_pin + i, tr.name, pinned=tr.pinned) for i, tr in enumerate(adm_pinned)
        )
        adm_dyn = tuple(
            _Track(program.anchored + i, f"Object {i + 1}") for i in range(len(plan.objects))
        )
        adm_layout = (bed_name, adm_bed, adm_pinned_rebased + adm_dyn)

        _, max_err = write_restored_adm(
            pcm,
            raw_bed_indices,
            extra_pinned,
            dyn_indices,
            plan,
            program,
            target,
            sr,
            n_samples,
            cfg,
            adm_layout=adm_layout,
            token=token,
        )
        return RestoreResult(
            output_path=target,
            input_objects=len(dyn_trs),
            restored_objects=len(plan.objects),
            bed_channels=len(adm_bed) + len(adm_pinned),
            splits_applied=plan.splits_applied,
            max_sum_error=max_err,
        )
    finally:
        prep.close()
