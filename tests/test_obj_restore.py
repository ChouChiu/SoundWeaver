"""Tests for Atmos object stabilization, >15-object restoration, and dual-circle splitting."""

from __future__ import annotations

import math
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from soundweaver.core.atmos.adm import AdmFile, _Track
from soundweaver.core.atmos.adm_reader import PreparedADM, is_adm_bwf
from soundweaver.core.atmos.oamd import Program
from soundweaver.core.atmos.obj_restore import (
    RestoreConfig,
    SlotFeatures,
    build_fingerprints,
    extract_slot_features,
    plan_object_restoration,
    restore_adm_file,
    separate_stretch_audio,
    write_restored_adm,
)
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.speakers import BY_NAME
from soundweaver.core.atmos.trace import Key


def _harmonic_voice(
    t: np.ndarray, freqs_amps: list[tuple[float, float]], mod_hz: float = 0.0
) -> np.ndarray:
    sig = np.zeros_like(t, dtype=np.float64)
    for f, a in freqs_amps:
        sig += a * np.sin(2.0 * np.pi * f * t)
    if mod_hz > 0.0:
        sig *= 0.65 + 0.35 * np.sin(2.0 * np.pi * mod_hz * t)
    peak = float(np.max(np.abs(sig)))
    if peak > 0:
        sig *= 0.35 / peak
    return sig.astype(np.float32)


def _quantize_oamd(pos: np.ndarray) -> np.ndarray:
    q = pos.copy()
    q[..., 0] = np.round(np.clip(q[..., 0], 0.0, 1.0) * 62.0) / 62.0
    q[..., 1] = np.round(np.clip(q[..., 1], 0.0, 1.0) * 62.0) / 62.0
    q[..., 2] = np.round(np.clip(q[..., 2], -1.0, 1.0) * 15.0) / 15.0
    return q


def _pos_to_keys(pos_hop: np.ndarray, active_hop: np.ndarray, sr: int, hop: int) -> list[Key]:
    keys: list[Key] = []
    for h in range(len(pos_hop)):
        t_s = h * hop / sr
        x, y, z = float(pos_hop[h, 0]), float(pos_hop[h, 1]), float(pos_hop[h, 2])
        act = bool(active_hop[h])
        if not keys or keys[-1][2:] != (x, y, z, act):
            keys.append((t_s, 0.0, x, y, z, act))
    return keys


class ObjRestoreTests(unittest.TestCase):
    def test_nearby_slot_switch_requires_position_and_timbre_change(self) -> None:
        sr, hop = 48000, 512
        n_hops = 200
        t = np.arange(n_hops * hop) / sr
        switch = n_hops // 2
        low = _harmonic_voice(t, [(176, 1.0), (352, 0.6)])
        high = _harmonic_voice(t, [(3000, 1.0), (6000, 0.6)])
        audio = np.concatenate((low[: switch * hop], high[switch * hop :]))[None, :]
        pos = np.tile([0.5, 0.25, 0.2], (n_hops, 1))
        pos[switch:, 0] += 2.0 / 62.0
        keys = _pos_to_keys(pos, np.ones(n_hops, dtype=bool), sr, hop)
        cfg = RestoreConfig()
        plan = plan_object_restoration(extract_slot_features(audio, [keys], sr, cfg), cfg)

        self.assertEqual(len(plan.objects), 2)
        self.assertEqual(plan.splits_applied, 0)
        first, second = sorted(plan.objects, key=lambda obj: np.flatnonzero(obj.active)[0])
        self.assertLessEqual(abs(first.spans[-1].end_hop - switch), 1)
        self.assertLessEqual(abs(second.spans[0].start_hop - switch), 1)

        same_timbre = extract_slot_features(low[None, :], [keys], sr, cfg)
        self.assertEqual(len(plan_object_restoration(same_timbre, cfg).objects), 1)

    def test_spectral_separation_without_solo_frames(self) -> None:
        sr, hop = 48000, 512
        n_hops = 384
        t = np.arange(n_hops * hop) / sr
        shares = np.tile(np.repeat([0.25, 0.75, 0.4, 0.6], 24), 4)
        weight = np.repeat(shares, hop)
        a = _harmonic_voice(t, [(410, 1.0), (830, 0.6), (1253, 0.5)])
        b = _harmonic_voice(t, [(479, 1.0), (915, 0.6), (1374, 0.5)])
        a *= np.sqrt(weight)
        b *= np.sqrt(1.0 - weight)
        mix = a + b

        s1, s2 = separate_stretch_audio(mix, sr, hop, shares)
        np.testing.assert_allclose(s1 + s2, mix, atol=6e-8, rtol=0.0)
        for pred, ref in ((s1, a), (s2, b)):
            sdr = 10.0 * np.log10(np.sum(ref**2) / np.sum((pred - ref) ** 2))
            self.assertGreater(sdr, 20.0)

    def test_compact_restored_adm_keeps_lfe_and_source_bed_channels(self) -> None:
        sr = 48_000
        audio = np.zeros((3, 8192), dtype=np.float32)
        audio[1] = 0.125
        audio[2] = 0.25
        cfg = RestoreConfig(enable_split=False)
        keys = [[(0.0, 0.0, 0.5, 0.25, 0.0, True)]]
        plan = plan_object_restoration(extract_slot_features(audio[2:], keys, sr, cfg), cfg)
        # R is a source channel with silent samples. Only synthetic padding may be removed.
        program = Program(("R", "LFE"), 0, 1)
        with tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "default.wav"
            compact = Path(tmp) / "compact.wav"
            for target, remove in ((default, False), (compact, True)):
                write_restored_adm(
                    audio, (0, 1), (), (2,), plan, program, target, sr, audio.shape[1], cfg,
                    remove_bed_placeholders=remove,
                )
            dolby_adm = PreparedADM(default)
            compact_adm = PreparedADM(compact)
            try:
                self.assertEqual(dolby_adm.channels, 7)
                self.assertEqual(compact_adm.channels, 3)
                self.assertEqual(compact_adm.info.bed_channels, ("R", "LFE"))
                self.assertTrue(compact_adm.tracks[1].is_lfe)
                self.assertEqual(compact_adm.info.object_count, 1)
                np.testing.assert_array_equal(
                    compact_adm.read_frames(0, audio.shape[1]),
                    dolby_adm.read_frames(0, audio.shape[1])[:, [1, 3, 6]],
                )
            finally:
                dolby_adm.close()
                compact_adm.close()

    def test_pack_quiet_fragments_preserves_pcm_and_positions(self) -> None:
        sr, hop = 48_000, 512
        n_hops = 80
        audio = np.zeros((4, n_hops * hop), dtype=np.float32)
        keys = []
        fragments = (
            (8, 20, 0.10, 0.02),
            (12, 24, 0.40, 0.01),
            (48, 60, 0.90, 0.01),
            (8, 20, 0.70, 0.10),
        )
        for slot, (start, end, x, level) in enumerate(fragments):
            audio[slot, start * hop : end * hop] = level
            keys.append([(0.0, 0.0, x, 0.25, 0.0, True)])

        cfg = RestoreConfig(enable_split=False)
        feats = extract_slot_features(audio, keys, sr, cfg)
        plan = plan_object_restoration(feats, cfg)
        self.assertEqual(len(plan.objects), 3)
        self.assertEqual([len(obj.spans) for obj in plan.objects], [1, 2, 1])

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "restored.wav"
            _, max_err = write_restored_adm(
                audio, (), (), list(range(4)), plan, Program((), 0, 4),
                target, sr, audio.shape[1], cfg,
            )
            self.assertEqual(max_err, 0.0)
            prep = PreparedADM(target)
            try:
                pcm = prep.read_frames(0, audio.shape[1])
                np.testing.assert_allclose(pcm.sum(axis=1), audio.sum(axis=0), atol=3e-7)
                for start, _, x, level in fragments:
                    probe = start * hop + 1
                    positions = [
                        prep.block_at(track, probe / sr).x
                        for i, track in enumerate(prep.tracks)
                        if np.isclose(pcm[probe, i], level)
                    ]
                    self.assertTrue(any(math.isclose(p, x, abs_tol=1e-4) for p in positions))
            finally:
                prep.close()

    def test_single_object_stability_across_slot_hopping(self) -> None:
        sr = 48000
        dur_s = 6.0
        n_samples = int(sr * dur_s)
        t = np.arange(n_samples, dtype=np.float64) / sr
        hop = 512
        n_hops = math.ceil(n_samples / hop)

        specs = [
            ([(160, 1.0), (320, 0.7), (480, 0.4)], (0.12, 0.15, 0.0)),
            ([(280, 1.0), (560, 0.8), (840, 0.5)], (0.88, 0.15, 0.0)),
            ([(450, 1.0), (900, 0.6), (1350, 0.4)], (0.15, 0.85, 0.3)),
            ([(700, 1.0), (1400, 0.8), (2800, 0.5)], (0.85, 0.85, 0.5)),
            ([(1200, 1.0), (2400, 0.7), (3600, 0.5)], (0.50, 0.20, 0.9)),
            ([(2200, 1.0), (4400, 0.8), (6600, 0.4)], (0.50, 0.82, 0.8)),
        ]
        gt_audio = np.stack([_harmonic_voice(t, fa) for fa, _ in specs])
        gt_pos = np.array([p for _, p in specs], dtype=np.float32)

        n_slots = 6
        raw_audio = np.zeros((n_slots, n_samples), dtype=np.float32)
        slot_pos_hop = np.tile(np.array([0.5, 0.5, 0.0], dtype=np.float32), (n_slots, n_hops, 1))
        slot_act_hop = np.zeros((n_slots, n_hops), dtype=bool)

        rng = np.random.default_rng(42)
        block_samples = sr // 2  # permute every 0.5s
        for b_start in range(0, n_samples, block_samples):
            b_end = min(n_samples, b_start + block_samples)
            perm = rng.permutation(n_slots)
            h0 = b_start // hop
            h1 = min(n_hops, math.ceil(b_end / hop))
            for inst_idx, slot in enumerate(perm):
                raw_audio[slot, b_start:b_end] = gt_audio[inst_idx, b_start:b_end]
                slot_pos_hop[slot, h0:h1] = gt_pos[inst_idx]
                slot_act_hop[slot, h0:h1] = True

        keys = [_pos_to_keys(slot_pos_hop[s], slot_act_hop[s], sr, hop) for s in range(n_slots)]
        cfg = RestoreConfig()
        feats = extract_slot_features(raw_audio, keys, sr, cfg)
        plan = plan_object_restoration(feats, cfg)

        self.assertEqual(len(plan.objects), 6)
        self.assertEqual(plan.splits_applied, 0)
        for obj in plan.objects:
            act_p = obj.pos[obj.active]
            std = float(np.sqrt(np.mean(np.sum((act_p - act_p.mean(axis=0)) ** 2, axis=1))))
            self.assertLess(std, 0.01)

        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / "restored.wav"
            prog = Program((), 0, len(plan.objects))
            _, max_err = write_restored_adm(
                raw_audio, (), (), list(range(n_slots)), plan, prog, out_wav, sr, n_samples, cfg
            )
            self.assertLess(max_err, 1e-5)

            prep = PreparedADM(out_wav)
            try:
                pcm = prep.read_frames(0, n_samples).T
                self.assertEqual(pcm.shape, (6, n_samples))
                # Each restored object matches one distinct ground-truth instrument with corr > 0.99
                matched: set[int] = set()
                for row in pcm:
                    corrs = [
                        float(np.corrcoef(row, gt_audio[j])[0, 1]) for j in range(6)
                    ]
                    best_j = int(np.argmax(corrs))
                    self.assertGreater(corrs[best_j], 0.99)
                    matched.add(best_j)
                self.assertEqual(len(matched), 6)
            finally:
                prep.close()

    def test_recover_beyond_15_time_multiplexed_objects(self) -> None:
        sr = 48000
        dur_s = 8.0
        n_samples = int(sr * dur_s)
        t = np.arange(n_samples, dtype=np.float64) / sr
        hop = 512
        n_hops = math.ceil(n_samples / hop)

        n_sources = 24
        n_slots = 15
        raw_audio = np.zeros((n_slots, n_samples), dtype=np.float32)
        slot_pos_hop = np.tile(np.array([0.5, 0.5, 0.0], dtype=np.float32), (n_slots, n_hops, 1))
        slot_act_hop = np.zeros((n_slots, n_hops), dtype=bool)

        # 24 distinct spatial+timbral sources scheduled across 4 epochs (6 active per epoch)
        for src in range(n_sources):
            epoch = src // 6  # 4 epochs of 2.0s each, 6 sources per epoch, 24 total distinct objects
            slot = (src % 6 + epoch * 4) % n_slots
            s0 = int((epoch * 2.0 + 0.08) * sr)
            s1 = int(((epoch + 1) * 2.0 - 0.08) * sr)
            f0 = 150.0 + src * 170.0
            voice = _harmonic_voice(t[s0:s1], [(f0, 1.0), (2 * f0, 0.6), (3 * f0, 0.35)])
            # Fade edges slightly
            fade = 400
            voice[:fade] *= np.linspace(0.0, 1.0, fade, dtype=np.float32)
            voice[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
            raw_audio[slot, s0:s1] += voice

            angle = 2.0 * math.pi * src / n_sources
            px = 0.5 + 0.42 * math.cos(angle)
            py = 0.5 + 0.42 * math.sin(angle)
            pz = -0.5 + 1.4 * ((src % 5) / 4.0)
            h0 = s0 // hop
            h1 = min(n_hops, math.ceil(s1 / hop))
            slot_pos_hop[slot, h0:h1] = (px, py, pz)
            slot_act_hop[slot, h0:h1] = True

        keys = [_pos_to_keys(slot_pos_hop[s], slot_act_hop[s], sr, hop) for s in range(n_slots)]
        cfg = RestoreConfig()
        feats = extract_slot_features(raw_audio, keys, sr, cfg)
        plan = plan_object_restoration(feats, cfg)

        self.assertGreaterEqual(len(plan.objects), 20)
        self.assertEqual(len(plan.objects), 24)
        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / "24obj.wav"
            prog = Program((), 0, len(plan.objects))
            _, max_err = write_restored_adm(
                raw_audio, (), (), list(range(n_slots)), plan, prog, out_wav, sr, n_samples, cfg
            )
            self.assertLess(max_err, 1e-5)

    def test_dual_circle_coclustered_decomposition_screenshot_scenario(self) -> None:
        sr = 48000
        dur_s = 6.0
        n_samples = int(sr * dur_s)
        t = np.arange(n_samples, dtype=np.float64) / sr
        hop = 512
        n_hops = math.ceil(n_samples / hop)

        # Two circular trajectories with slightly different radius & z:
        # obj1: R=0.38, z=0.72, low-mid harmonic
        # obj2: R=0.27, z=0.50, bright high-harmonic
        omega = 2.0 * math.pi * 0.35
        theta_s = omega * t
        gt_p1_s = np.column_stack(
            [0.5 + 0.38 * np.cos(theta_s), 0.5 + 0.38 * np.sin(theta_s), np.full_like(t, 0.72)]
        )
        gt_p2_s = np.column_stack(
            [0.5 + 0.27 * np.cos(theta_s), 0.5 + 0.27 * np.sin(theta_s), np.full_like(t, 0.50)]
        )

        # Independently modulated envelopes yielding solo-obj1, solo-obj2, and overlap intervals
        env1 = np.zeros(n_samples, dtype=np.float64)
        env2 = np.zeros(n_samples, dtype=np.float64)
        for seg_idx in range(24):
            a = int(seg_idx * n_samples / 24)
            b = int((seg_idx + 1) * n_samples / 24)
            mode = seg_idx % 4
            if mode == 0:
                env1[a:b], env2[a:b] = 1.0, 0.05
            elif mode == 1:
                env1[a:b], env2[a:b] = 0.05, 1.0
            elif mode == 2:
                env1[a:b], env2[a:b] = 0.85, 0.65
            else:
                env1[a:b], env2[a:b] = 0.65, 0.85

        s1_gt = _harmonic_voice(t, [(190, 1.0), (380, 0.7), (570, 0.4)]) * env1.astype(np.float32)
        s2_gt = _harmonic_voice(t, [(1450, 0.9), (2900, 0.8), (4350, 0.6), (5800, 0.4)]) * env2.astype(
            np.float32
        )
        s3_mix = (s1_gt + s2_gt)[None, :]

        # Energy-weighted barycenter quantized to OAMD 6-bit X/Y and 4-bit Z
        t_h = (np.arange(n_hops, dtype=np.float64) * hop + 0.5 * hop) / sr
        th_h = omega * t_h
        gt_p1_h = np.column_stack([0.5 + 0.38 * np.cos(th_h), 0.5 + 0.38 * np.sin(th_h), np.full_like(t_h, 0.72)])
        gt_p2_h = np.column_stack([0.5 + 0.27 * np.cos(th_h), 0.5 + 0.27 * np.sin(th_h), np.full_like(t_h, 0.50)])
        e1_h = np.array([np.mean(s1_gt[h * hop : min(n_samples, (h + 1) * hop)] ** 2) for h in range(n_hops)])
        e2_h = np.array([np.mean(s2_gt[h * hop : min(n_samples, (h + 1) * hop)] ** 2) for h in range(n_hops)])
        w_true = e1_h / (e1_h + e2_h + 1e-12)
        p3_h = _quantize_oamd(w_true[:, None] * gt_p1_h + (1.0 - w_true[:, None]) * gt_p2_h).astype(
            np.float32
        )
        act_h = np.ones(n_hops, dtype=bool)
        keys = [_pos_to_keys(p3_h, act_h, sr, hop)]

        cfg = RestoreConfig()
        feats = extract_slot_features(s3_mix, keys, sr, cfg)
        t0 = time.perf_counter()
        plan = plan_object_restoration(feats, cfg)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 1.0)

        self.assertEqual(len(plan.objects), 2)
        self.assertGreaterEqual(plan.splits_applied, 1)

        # Match reconstructed trajectories to ground truth circles
        p_a = plan.objects[0].pos
        p_b = plan.objects[1].pos
        err_01 = float(np.mean(np.linalg.norm(p_a - gt_p1_h, axis=1)))
        err_02 = float(np.mean(np.linalg.norm(p_a - gt_p2_h, axis=1)))
        if err_01 <= err_02:
            err_1, err_2 = err_01, float(np.mean(np.linalg.norm(p_b - gt_p2_h, axis=1)))
            sp1_idx, sp2_idx = 0, 1
        else:
            err_1 = float(np.mean(np.linalg.norm(p_b - gt_p1_h, axis=1)))
            err_2 = err_02
            sp1_idx, sp2_idx = 1, 0
        self.assertLess(err_1, 0.035)
        self.assertLess(err_2, 0.035)

        with tempfile.TemporaryDirectory() as tmp:
            out_wav = Path(tmp) / "split_circles.wav"
            prog = Program((), 0, 2)
            _, max_err = write_restored_adm(
                s3_mix, (), (), [0], plan, prog, out_wav, sr, n_samples, cfg
            )
            self.assertLess(max_err, 1e-5)

            prep = PreparedADM(out_wav)
            try:
                pcm = prep.read_frames(0, n_samples).T
                self.assertEqual(pcm.shape, (2, n_samples))
                c1 = float(np.corrcoef(pcm[sp1_idx], s1_gt)[0, 1])
                c2 = float(np.corrcoef(pcm[sp2_idx], s2_gt)[0, 1])
                self.assertGreater(c1, 0.85)
                self.assertGreater(c2, 0.85)
            finally:
                prep.close()

    def test_banded_solver_speed_on_3000_hops(self) -> None:
        sr = 48000
        hop = 512
        n_hops = 3000
        n_samples = n_hops * hop
        t = np.arange(n_samples, dtype=np.float64) / sr
        t_h = (np.arange(n_hops, dtype=np.float64) * hop + 0.5 * hop) / sr

        omega = 2.0 * math.pi * 0.35
        th = omega * t_h
        p1 = np.column_stack([0.5 + 0.38 * np.cos(th), 0.5 + 0.38 * np.sin(th), np.full_like(th, 0.72)])
        p2 = np.column_stack([0.5 + 0.27 * np.cos(th), 0.5 + 0.27 * np.sin(th), np.full_like(th, 0.50)])
        gate = np.sin(2.0 * math.pi * 1.5 * t) > 0.0
        s1 = _harmonic_voice(t, [(200, 1.0), (400, 0.6)]) * np.where(gate, 1.0, 0.1).astype(np.float32)
        s2 = _harmonic_voice(t, [(1800, 1.0), (3600, 0.6)]) * np.where(gate, 0.1, 1.0).astype(np.float32)
        gate_h = np.sin(2.0 * math.pi * 1.5 * t_h) > 0.0
        w_h = np.where(gate_h, 0.99, 0.01)
        p3 = _quantize_oamd(w_h[:, None] * p1 + (1.0 - w_h[:, None]) * p2).astype(np.float32)
        keys = [_pos_to_keys(p3, np.ones(n_hops, dtype=bool), sr, hop)]

        cfg = RestoreConfig()
        feats = extract_slot_features((s1 + s2)[None, :], keys, sr, cfg)
        t0 = time.perf_counter()
        plan = plan_object_restoration(feats, cfg)
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(plan.objects), 2)
        self.assertLess(elapsed, 0.8)

    def test_negative_no_false_split_on_single_fast_circle(self) -> None:
        sr = 48000
        dur_s = 5.0
        n_samples = int(sr * dur_s)
        t = np.arange(n_samples, dtype=np.float64) / sr
        hop = 512
        n_hops = math.ceil(n_samples / hop)

        t_h = (np.arange(n_hops, dtype=np.float64) * hop + 0.5 * hop) / sr
        th = 2.0 * math.pi * 0.75 * t_h
        p_h = _quantize_oamd(
            np.column_stack([0.5 + 0.35 * np.cos(th), 0.5 + 0.35 * np.sin(th), np.full_like(t_h, 0.6)])
        ).astype(np.float32)
        audio = _harmonic_voice(t, [(300, 1.0), (600, 0.7), (1200, 0.5)], mod_hz=1.5)[None, :]
        keys = [_pos_to_keys(p_h, np.ones(n_hops, dtype=bool), sr, hop)]

        cfg = RestoreConfig()
        feats = extract_slot_features(audio, keys, sr, cfg)
        plan = plan_object_restoration(feats, cfg)
        self.assertEqual(len(plan.objects), 1)
        self.assertEqual(plan.splits_applied, 0)

    def test_negative_no_false_split_on_stationary_vocal(self) -> None:
        sr = 48000
        dur_s = 5.0
        n_samples = int(sr * dur_s)
        t = np.arange(n_samples, dtype=np.float64) / sr
        hop = 512
        n_hops = math.ceil(n_samples / hop)

        # Alternating warm vowel (250/500 Hz) and bright sibilant/consonant (4500/7000 Hz) at same spot
        audio = np.zeros(n_samples, dtype=np.float32)
        vowel = _harmonic_voice(t, [(250, 1.0), (500, 0.8), (750, 0.5)])
        sibilant = _harmonic_voice(t, [(4200, 1.0), (6300, 0.8), (8400, 0.6)])
        for k in range(20):
            a = int(k * n_samples / 20)
            b = int((k + 1) * n_samples / 20)
            audio[a:b] = vowel[a:b] if k % 2 == 0 else sibilant[a:b]

        p_h = np.tile(np.array([0.5, 0.15, 0.1], dtype=np.float32), (n_hops, 1))
        keys = [_pos_to_keys(p_h, np.ones(n_hops, dtype=bool), sr, hop)]

        cfg = RestoreConfig()
        feats = extract_slot_features(audio[None, :], keys, sr, cfg)
        plan = plan_object_restoration(feats, cfg)
        self.assertEqual(len(plan.objects), 1)
        self.assertEqual(plan.splits_applied, 0)

    def test_end_to_end_adm_bwf_roundtrip_and_fast_lookup(self) -> None:
        sr = 48000
        dur_s = 4.0
        n_samples = int(sr * dur_s)
        t = np.arange(n_samples, dtype=np.float64) / sr
        hop = 512
        n_hops = math.ceil(n_samples / hop)

        bed_labels = ("L", "R", "C", "LFE", "Ls", "Rs")
        # 15 dynamic objects: slots 0..13 hold 18 time-multiplexed sources + slot hopping,
        # slot 14 holds a co-clustered pair!
        n_slots = 15
        program = Program(bed_labels, 0, n_slots)

        with tempfile.TemporaryDirectory() as tmp:
            src_adm = Path(tmp) / "coded_15.wav"
            dst_adm = Path(tmp) / "restored.wav"

            adm = AdmFile(src_adm, sr, n_samples, program, len(bed_labels) + n_slots)
            dyn_audio = np.zeros((n_slots, n_samples), dtype=np.float32)
            slot_pos_hop = np.tile(np.array([0.5, 0.5, 0.0], dtype=np.float32), (n_slots, n_hops, 1))
            slot_act_hop = np.zeros((n_slots, n_hops), dtype=bool)

            # Slots 0..13 carry 18 distinct sources across 2 halves
            for src in range(18):
                half = src // 9
                slot = (src * 3) % 14
                if half == 0:
                    s0, s1 = int(0.05 * sr), int(1.90 * sr)
                else:
                    s0, s1 = int(2.10 * sr), int(3.95 * sr)
                f0 = 180.0 + src * 210.0
                v = _harmonic_voice(t[s0:s1], [(f0, 1.0), (2 * f0, 0.5)]) * 0.4
                dyn_audio[slot, s0:s1] = v
                ang = 2.0 * math.pi * src / 18
                h0, h1 = s0 // hop, min(n_hops, math.ceil(s1 / hop))
                slot_pos_hop[slot, h0:h1] = (0.5 + 0.4 * math.cos(ang), 0.5 + 0.4 * math.sin(ang), (src % 3) * 0.4)
                slot_act_hop[slot, h0:h1] = True

            # Slot 14: dual-circle co-clustered pair
            th_s = 2.0 * math.pi * 0.5 * t
            p1_s = np.column_stack([0.5 + 0.36 * np.cos(th_s), 0.5 + 0.36 * np.sin(th_s), np.full_like(t, 0.8)])
            p2_s = np.column_stack([0.5 + 0.24 * np.cos(th_s), 0.5 + 0.24 * np.sin(th_s), np.full_like(t, 0.4)])
            gate = (np.sin(2.0 * math.pi * 2.0 * t) > 0.0)
            c1 = _harmonic_voice(t, [(220, 1.0), (440, 0.6)]) * np.where(gate, 1.0, 0.08).astype(np.float32) * 0.4
            c2 = _harmonic_voice(t, [(2400, 1.0), (4800, 0.6)]) * np.where(gate, 0.08, 1.0).astype(np.float32) * 0.4
            dyn_audio[14] = c1 + c2
            t_h = (np.arange(n_hops, dtype=np.float64) * hop + 0.5 * hop) / sr
            th_h = 2.0 * math.pi * 0.5 * t_h
            p1_h = np.column_stack([0.5 + 0.36 * np.cos(th_h), 0.5 + 0.36 * np.sin(th_h), np.full_like(t_h, 0.8)])
            p2_h = np.column_stack([0.5 + 0.24 * np.cos(th_h), 0.5 + 0.24 * np.sin(th_h), np.full_like(t_h, 0.4)])
            e1_h = np.array([np.mean(c1[h * hop : min(n_samples, (h + 1) * hop)] ** 2) for h in range(n_hops)])
            e2_h = np.array([np.mean(c2[h * hop : min(n_samples, (h + 1) * hop)] ** 2) for h in range(n_hops)])
            w_h = e1_h / (e1_h + e2_h + 1e-12)
            slot_pos_hop[14] = _quantize_oamd(w_h[:, None] * p1_h + (1.0 - w_h[:, None]) * p2_h)
            slot_act_hop[14] = True

            # Write 6 bed channels (L has 330Hz tone) + 15 dynamic objects
            bed_audio = np.zeros((len(bed_labels), n_samples), dtype=np.float32)
            bed_audio[0] = (0.15 * np.sin(2.0 * math.pi * 330.0 * t)).astype(np.float32)
            full_pcm = np.vstack([bed_audio, dyn_audio])

            from soundweaver.core.atmos.adm import pcm24

            data, _ = pcm24(full_pcm)
            adm.write_pcm(data)
            keys_map = {
                program.anchored + i: _pos_to_keys(slot_pos_hop[i], slot_act_hop[i], sr, hop)
                for i in range(n_slots)
            }
            adm.close(keys_map)

            res = restore_adm_file(src_adm, dst_adm)
            self.assertTrue(is_adm_bwf(dst_adm))
            self.assertEqual(res.input_objects, 15)
            self.assertGreater(res.restored_objects, 15)
            self.assertGreaterEqual(res.splits_applied, 1)
            self.assertLess(res.max_sum_error, 1e-5)

            prep = PreparedADM(dst_adm)
            try:
                self.assertEqual(prep.info.object_count, res.restored_objects)
                # Verify O(log B) block_at matches linear scan on every track
                for tr in prep.tracks:
                    if not tr.blocks:
                        continue
                    for probe_t in (0.0, 0.25, 1.23, 2.5, 3.99):
                        expected = tr.blocks[0]
                        for b in tr.blocks:
                            if b.start <= probe_t:
                                expected = b
                            else:
                                break
                        self.assertEqual(prep.block_at(tr, probe_t), expected)

                rendered = prep.render_to_layout(BY_NAME["5.1.4"], CancellationToken())
                with rendered:
                    self.assertEqual(rendered.channels, 10)
                    self.assertGreater(float(np.max(np.abs(rendered.samples))), 0.05)

                trace = prep.build_trace(BY_NAME["5.1.4"], token=CancellationToken())
                self.assertEqual(len(trace.objects), res.restored_objects)
            finally:
                prep.close()

    def test_tight_dual_circle_and_stationary_cocluster_split(self) -> None:
        sr = 48000
        hop = 512
        dur_s = 6.0
        n_samples = int(sr * dur_s)
        t = np.arange(n_samples, dtype=np.float64) / sr
        n_hops = math.ceil(n_samples / hop)
        t_h = (np.arange(n_hops, dtype=np.float64) * hop + 0.5 * hop) / sr

        env1 = np.zeros(n_samples, dtype=np.float64)
        env2 = np.zeros(n_samples, dtype=np.float64)
        for seg_idx in range(24):
            a = int(seg_idx * n_samples / 24)
            b = int((seg_idx + 1) * n_samples / 24)
            mode = seg_idx % 4
            if mode == 0:
                env1[a:b], env2[a:b] = 1.0, 0.05
            elif mode == 1:
                env1[a:b], env2[a:b] = 0.05, 1.0
            elif mode == 2:
                env1[a:b], env2[a:b] = 0.85, 0.65
            else:
                env1[a:b], env2[a:b] = 0.65, 0.85

        s1_gt = _harmonic_voice(t, [(200, 1.0), (400, 0.7)]) * env1.astype(np.float32)
        s2_gt = _harmonic_voice(t, [(1800, 1.0), (3600, 0.7)]) * env2.astype(np.float32)
        mix = (s1_gt + s2_gt)[None, :]
        e1_h = np.array([np.mean(s1_gt[h * hop : min(n_samples, (h + 1) * hop)] ** 2) for h in range(n_hops)])
        e2_h = np.array([np.mean(s2_gt[h * hop : min(n_samples, (h + 1) * hop)] ** 2) for h in range(n_hops)])
        w_true = e1_h / (e1_h + e2_h + 1e-12)

        th = 2.0 * math.pi * 0.35 * t_h
        cases = [
            (
                np.column_stack([0.5 + 0.34 * np.cos(th), 0.5 + 0.34 * np.sin(th), np.full_like(t_h, 0.60)]),
                np.column_stack([0.5 + 0.28 * np.cos(th), 0.5 + 0.28 * np.sin(th), np.full_like(t_h, 0.52)]),
            ),
            (
                np.tile(np.array([0.25, 0.30, 0.20], dtype=np.float64), (n_hops, 1)),
                np.tile(np.array([0.35, 0.32, 0.27], dtype=np.float64), (n_hops, 1)),
            ),
        ]
        cfg = RestoreConfig()
        for gt_p1, gt_p2 in cases:
            p3 = _quantize_oamd(w_true[:, None] * gt_p1 + (1.0 - w_true[:, None]) * gt_p2).astype(
                np.float32
            )
            keys = [_pos_to_keys(p3, np.ones(n_hops, dtype=bool), sr, hop)]
            feats = extract_slot_features(mix, keys, sr, cfg)
            plan = plan_object_restoration(feats, cfg)
            self.assertEqual(len(plan.objects), 2)
            self.assertGreaterEqual(plan.splits_applied, 1)

            pa, pb = plan.objects[0].pos, plan.objects[1].pos
            e01 = float(np.mean(np.linalg.norm(pa - gt_p1, axis=1)))
            e02 = float(np.mean(np.linalg.norm(pa - gt_p2, axis=1)))
            if e01 <= e02:
                err1, err2 = e01, float(np.mean(np.linalg.norm(pb - gt_p2, axis=1)))
                idx1, idx2 = 0, 1
            else:
                err1, err2 = float(np.mean(np.linalg.norm(pb - gt_p1, axis=1))), e02
                idx1, idx2 = 1, 0
            self.assertLess(err1, 0.045)
            self.assertLess(err2, 0.045)

            with tempfile.TemporaryDirectory() as tmp:
                out_wav = Path(tmp) / "tight.wav"
                _, max_err = write_restored_adm(
                    mix, (), (), [0], plan, Program((), 0, 2), out_wav, sr, n_samples, cfg
                )
                self.assertLess(max_err, 1e-5)
                prep = PreparedADM(out_wav)
                try:
                    pcm = prep.read_frames(0, n_samples).T
                    self.assertGreater(float(np.corrcoef(pcm[idx1], s1_gt)[0, 1]), 0.85)
                    self.assertGreater(float(np.corrcoef(pcm[idx2], s2_gt)[0, 1]), 0.85)
                finally:
                    prep.close()

    def test_four_minute_15_slot_restoration_performance(self) -> None:
        t_len, n_slots = 22500, 15
        rng = np.random.default_rng(7)
        bark = rng.uniform(0.01, 1.0, (n_slots, t_len, 24)).astype(np.float32)
        flat = rng.uniform(0.1, 0.5, (n_slots, t_len, 3)).astype(np.float32)
        harm = rng.uniform(0.2, 0.8, (n_slots, t_len)).astype(np.float32)
        fp = build_fingerprints(bark, flat, harm)
        rms = np.full((n_slots, t_len), 0.1, dtype=np.float32)
        peak = np.full((n_slots, t_len), 0.2, dtype=np.float32)
        pos = np.zeros((n_slots, t_len, 3), dtype=np.float32)
        for m in range(n_slots):
            for b in range(0, t_len, 375):
                pos[m, b : b + 375] = [
                    (m % 5) * 0.2 + 0.1,
                    ((m // 5 + b // 375) % 3) * 0.4 + 0.1,
                    0.2,
                ]
        active = np.ones((n_slots, t_len), dtype=bool)
        feats = SlotFeatures(48000.0, 512, 0, bark, fp, rms, peak, pos, active)
        cfg = RestoreConfig()

        t0 = time.perf_counter()
        plan = plan_object_restoration(feats, cfg)
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 3.0)
        self.assertGreater(len(plan.objects), 0)

    def test_ac4_objects_restore_imports_and_hop_selection(self) -> None:
        import inspect
        from soundweaver.core.ac4 import objects as ac4_objects

        self.assertTrue(hasattr(ac4_objects, "replace"))
        for frame_len in (384, 512, 768, 960, 1024, 1536, 1920, 2048):
            hop = ac4_objects._choose_hop(frame_len, 512)
            self.assertEqual(frame_len % hop, 0)
            self.assertLessEqual(hop, 1024)
        for fn in (ac4_objects.decode_objects, ac4_objects.render_objects):
            sig = inspect.signature(fn)
            self.assertIn("restore_objects", sig.parameters)
            self.assertIs(sig.parameters["restore_objects"].default, True)
