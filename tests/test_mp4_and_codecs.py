"""Tests for MP4 box walker, AV3A AATF parser/encoder, and MPEG-H CICP layouts."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

from soundweaver.core import audio, mp4
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.codecs.native import NativePcmStream, run_native
from soundweaver.core.pcm import AudioData

SAMPLE_AV3A_ZIP = Path(
    os.environ.get("SOUNDWEAVER_AV3A_SAMPLE", "/home/chouchiu/Documents/AV3A/8033F932_audio.av3a.zip")
)


def _box(tag: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), tag) + payload


def _build_minimal_mp4(sample_entry: bytes, frames: list[bytes], entry_extra: bytes = b"") -> bytes:
    """Assemble a minimal self-contained ISOBMFF MP4 with one stco/stsz/stsd audio track."""
    ftyp = _box(b"ftyp", b"isom\x00\x00\x02\x00isommp41")
    # 28 bytes match the ISO base AudioSampleEntry that _mp4_source skips before child boxes.
    entry_payload = b"\x00" * 28 + entry_extra
    stsd = _box(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + _box(sample_entry, entry_payload))
    stsz_sizes = b"".join(struct.pack(">I", len(fr)) for fr in frames)
    stsz = _box(b"stsz", b"\x00\x00\x00\x00\x00\x00\x00\x00" + struct.pack(">I", len(frames)) + stsz_sizes)
    mdat_body = b"".join(frames)
    # Build moov skeleton once with zeroed stco offset, then patch offset after ftyp + mdat header.
    mdat = _box(b"mdat", mdat_body)
    chunk_offset = len(ftyp) + 8
    stco = _box(b"stco", b"\x00\x00\x00\x00" + struct.pack(">II", 1, chunk_offset))
    stsc = _box(b"stsc", b"\x00\x00\x00\x00" + struct.pack(">IIII", 1, 1, len(frames), 1))
    stbl = _box(b"stbl", stsd + stsz + stco + stsc)
    minf = _box(b"minf", stbl)
    mdia = _box(b"mdia", minf)
    trak = _box(b"trak", mdia)
    moov = _box(b"moov", trak)
    return ftyp + mdat + moov


def _read_vvm1_timeline(path: Path) -> tuple[av3a.VvmTimelineHeader, np.ndarray]:
    """Test helper: read the header plus every telemetry frame from a decoder sidecar."""
    header = av3a.read_vvm1_header(path)
    assert header is not None
    frames = []
    index = 0
    while (frame := av3a.read_vvm1_frame(path, header, index)) is not None:
        frames.append(frame)
        index += 1
    return header, np.stack(frames)


def _build_m2ts(elementary: bytes) -> bytes:
    pes = (
        b"\x00\x00\x01\xbd" + struct.pack(">H", len(elementary) + 3)
        + b"\x80\x00\x00" + elementary
    )
    packets = []
    for index, start in enumerate(range(0, len(pes), 184)):
        payload = pes[start : start + 184]
        counter = index % 16
        packet = b"\x00" * 4 + bytes((0x47, 0x40 if index == 0 else 0, 0x45))
        if len(payload) < 184:
            padding = 183 - len(payload)
            packet += bytes((0x30 | counter, padding)) + b"\0" * padding
        else:
            packet += bytes((0x10 | counter,))
        packets.append(packet + payload)
    return b"".join(packets)


class Mp4AndCodecTests(unittest.TestCase):
    def test_vivid_timeline_reads_size_and_legacy_frames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timeline.vvm"
            for magic, fields in ((av3a.VVM1_MAGIC, 5), (av3a.VVM2_MAGIC, 6)):
                with self.subTest(fields=fields):
                    header_data = struct.pack("<6I", magic, 48000, 1, 1, 0, 1024)
                    points = np.zeros((2, 2, fields), dtype="<f4")
                    points[:, 1, 3] = 0.5
                    if fields == 6:
                        points[:, 1, 5] = (0.25, 0.75)
                    path.write_bytes(header_data + points.tobytes() + b"\x00")
                    header = av3a.read_vvm1_header(path)
                    self.assertIsNotNone(header)
                    self.assertEqual(header.fields, fields)
                    np.testing.assert_array_equal(av3a.read_vvm1_frame(path, header, 1), points[1])
                    self.assertIsNone(av3a.read_vvm1_frame(path, header, 2))
                    full_header, timeline = _read_vvm1_timeline(path)
                    self.assertEqual(full_header, header)
                    np.testing.assert_array_equal(timeline, points)

    def test_mp4_sample_entry_and_extract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "test.mp4"
            raw_frames = [b"\xff\xf2\x00\x11" + b"\xaa" * 16, b"\xff\xf2\x00\x22" + b"\xbb" * 12]
            p.write_bytes(_build_minimal_mp4(b"av3a", raw_frames))
            self.assertIsNotNone(mp4.find_mp4_moov(p))
            self.assertTrue(mp4.has_mp4_sample_entry(p, (b"av3a",)))
            self.assertFalse(mp4.has_mp4_sample_entry(p, (b"mhm1",)))
            out = Path(tmp) / "out.av3a"
            self.assertTrue(mp4.extract_elementary_stream(p, (b"av3a",), out))
            self.assertEqual(out.read_bytes(), b"".join(raw_frames))

    def test_mhas_and_ts_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mhas = Path(tmp) / "track.mhas"
            mhas.write_bytes(b"\x00\x00\xc0\x01\xa5\x00")
            self.assertTrue(mp4.has_mhas_sync(mhas))
            self.assertTrue(mpegh.is_mpegh_file(mhas))

    def test_mha1_in_m4a_is_identified_with_unknown_source_channel_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "track.m4a"
            path.write_bytes(_build_minimal_mp4(b"mha1", [b"frame"]))
            metadata = {
                "streams": [
                    {"codec_type": "audio", "codec_tag_string": "mha1",
                     "channels": 0, "sample_rate": "48000", "duration": "2.5"},
                    {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 1}},
                ],
                "format": {"duration": "5", "tags": {"TITLE": "Track", "artist": "Artist"}},
            }
            with patch("soundweaver.core.ffmpeg.probe", return_value=metadata):
                info = audio.probe_audio(path)
            self.assertTrue(info.is_mpegh)
            self.assertEqual((info.channels, info.sample_rate, info.duration), (24, 48000, 2.5))
            self.assertEqual((info.title, info.artist, info.cover_stream), ("Track", "Artist", 1))

    def test_mpegh_mp4_source_routes_config_to_the_worker(self) -> None:
        """mha1 without mhaC carries its config in-band; only maei scene metadata needs the legacy testbench."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.m4a"
            cases = [
                (b"", b""),
                (_box(b"mhaC", b"\x01\x02\x03" + struct.pack(">H", 3) + b"ABC"), b"ABC"),
            ]
            for entry_extra, config in cases:
                with self.subTest(config=len(config)):
                    path.write_bytes(_build_minimal_mp4(b"mha1", [b"frame"], entry_extra))
                    exe, mp4_source = mpegh.decoder_input(path, None)
                    self.assertEqual(Path(exe).stem, "soundweaver_mpegh")
                    self.assertEqual(mp4_source[2], config)
            # maei holds scene metadata outside the samples: the worker cannot read it.
            path.write_bytes(_build_minimal_mp4(b"mha1", [b"frame"], _box(b"maei", b"\x00" * 4)))
            self.assertIsNone(mpegh._mp4_source(path))

    def test_cicp_layouts_and_remap(self) -> None:
        self.assertEqual(len(mpegh.CICP_22_2_REMAP), 24)
        self.assertEqual(sorted(mpegh.CICP_22_2_REMAP), list(range(24)))
        self.assertEqual(mpegh.cicp_layout(13).channels, 24)
        for cid in mpegh.SUPPORTED_CICP:
            lay = mpegh.CICP_LAYOUTS[cid]
            self.assertGreaterEqual(lay.channels, 1)
            for sp in lay.speakers:
                self.assertIn(sp.layer, ("upper", "middle", "lower"))
                x, y, _z = sp.cartesian()
                self.assertTrue(-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0)

    def test_mpegh_mono_source_uses_actual_stereo_render_format(self) -> None:
        fixtures = Path(__file__).resolve().parents[1] / "native/mpegh/smoke_test_suite/inp"
        base = fixtures / "sine_1khz_000_cicp1"
        with patch("soundweaver.core.codecs.mpegh.subprocess.run") as probe:
            info = mpegh.probe_mpegh(base.with_suffix(".mhas"), measure_duration=False)
            self.assertEqual(info.sample_rate, 48000)
            probe.assert_not_called()
        with closing(mpegh.stream_mpegh(base.with_suffix(".mhas"), cicp=2)) as raw:
            self.assertEqual((raw.sample_rate, raw.channels), (48000, 2))
            expected = raw.read_frames(5093)
        self.assertGreater(float(np.max(np.abs(expected))), 0)
        with closing(mpegh.stream_mpegh(base.with_suffix(".mp4"), cicp=2)) as wrapped:
            self.assertEqual((wrapped.sample_rate, wrapped.channels), (48000, 2))
            block = np.concatenate((wrapped.read_frames(17), wrapped.read_frames(5076)))
        np.testing.assert_array_equal(block, expected)

    def test_av3a_aatf_hoa_header_parser(self) -> None:
        # Construct 9-byte AATF header: sync=0xFFF2, res=0, nn=0, profile=2(HOA), fs_idx=2(48k),
        # skipped 8 bits (pos 27..34), hoa_order-1 = 0 (FOA -> 4ch, pos 35..38),
        # resolution=0 (pos 39..40), br_idx=1 (96000, pos 41..44), crc=0 (pos 45..52).
        bits_val = 0
        def put(val: int, width: int) -> None:
            nonlocal bits_val
            bits_val = (bits_val << width) | (val & ((1 << width) - 1))

        put(0xFFF2, 16)
        put(0, 1)
        put(0, 3)      # nn=0
        put(2, 3)      # profile=2 (HOA)
        put(2, 4)      # fs_idx=2 (48000 Hz)
        put(0, 8)      # reserved/crc placeholder up to bit 35
        put(0, 4)      # hoaOrder - 1 = 0 -> FOA (4ch)
        put(0, 2)      # resolution = 0
        put(1, 4)      # br_idx = 1 -> 96000 bps
        put(0, 8)      # 8-bit CRC
        put(0, 19)     # pad to 72 bits = 9 bytes
        hdr_bytes = bits_val.to_bytes(9, "big")
        parsed = av3a.parse_aatf_header(hdr_bytes)
        self.assertEqual(parsed.profile, 2)
        self.assertEqual(parsed.sample_rate, 48000)
        self.assertEqual(parsed.hoa_order, 1)
        self.assertEqual(parsed.channels, 4)
        self.assertEqual(parsed.bitrate, 96000)
        self.assertEqual(parsed.header_bytes, 7)

    def test_native_process_drains_large_error_output_and_reports_failed_exit(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            "import sys; sys.stderr.write('x' * 200_000 + ' decoder error'); sys.exit(7)",
        ]
        with self.assertRaisesRegex(av3a.Av3aError, "decoder error$"):
            run_native(cmd, CancellationToken(), av3a.Av3aError, timeout=5.0)

    def test_native_stream_reports_failed_exit_after_pcm_header(self) -> None:
        cmd = [
            sys.executable,
            "-c",
            (
                "import struct, sys; sys.stdout.buffer.write(struct.pack('<iif', 48000, 1, 0.5)); "
                "sys.stderr.write('frame decode failed'); sys.exit(7)"
            ),
        ]
        stream = NativePcmStream(cmd)
        try:
            self.assertEqual(stream.read_frames(1)[0, 0], 0.5)
            with self.assertRaisesRegex(RuntimeError, "frame decode failed"):
                stream.read_frames(1)
        finally:
            stream.close()

    def test_av3a_encode_decode_roundtrip(self) -> None:
        sr = 48000
        dur_frames = sr // 2
        t = np.arange(dur_frames, dtype=np.float32) / sr
        tone = (0.25 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)
        # 4-channel FOA (HOA order 1) encoding -> probe -> decode
        foa = np.zeros((4, dur_frames), dtype=np.float32)
        foa[0] = tone
        foa[1] = tone * 0.5
        audio_foa = AudioData(foa, sr, 24)
        with tempfile.TemporaryDirectory() as tmp:
            out_av3a = Path(tmp) / "foa.av3a"
            av3a.encode_av3a(audio_foa, out_av3a, hoa_order=1, token=CancellationToken())
            self.assertTrue(av3a.is_av3a_file(out_av3a))
            reference = Path(tmp) / "reference.av3a"
            encoder = av3a.find_encoder()
            self.assertIsNotNone(encoder)
            av3a.encode_av3a(audio_foa, reference, hoa_order=1, binary=encoder)
            self.assertEqual(out_av3a.read_bytes(), reference.read_bytes())
            info = av3a.probe_av3a(out_av3a)
            self.assertEqual(info.sample_rate, 48000)
            self.assertEqual(info.channels, 4)
            self.assertEqual(info.hoa_order, 1)
            self.assertEqual(info.profile, 2)
            hdr = av3a.parse_aatf_header(out_av3a.read_bytes()[:16])
            self.assertEqual(hdr.sample_rate, info.sample_rate)
            self.assertEqual(hdr.hoa_order, info.hoa_order)
            self.assertEqual(hdr.channels, info.channels)
            self.assertEqual(hdr.bitrate, info.bitrate)

            aatf = out_av3a.read_bytes()
            mp4_path = Path(tmp) / "hoa.m4a"
            ts_path = Path(tmp) / "hoa.m2ts"
            mp4_path.write_bytes(_build_minimal_mp4(b"av3a", [aatf[:153], aatf[153:]]))
            ts_path.write_bytes(_build_m2ts(aatf))
            self.assertEqual(av3a.probe_av3a(mp4_path), info)
            self.assertEqual(av3a.probe_av3a(ts_path), info)
            damaged = Path(tmp) / "damaged.av3a"
            damaged.write_bytes(aatf[:-1] + bytes((aatf[-1] ^ 1,)))
            self.assertEqual(av3a.probe_av3a(damaged, quick=True).frames, 1)
            with self.assertRaisesRegex(av3a.Av3aError, "CRC mismatch"):
                av3a.probe_av3a(damaged)

            decoded = av3a.decode_av3a(out_av3a, CancellationToken())
            with decoded:
                self.assertEqual(decoded.channels, 4)
                self.assertEqual(decoded.sample_rate, 48000)
                timeline = Path(tmp) / "hoa.vvm"
                source_pcm = Path(tmp) / "hoa.pcm"
                with closing(av3a.stream_av3a(
                    ts_path, timeline_path=timeline, source_pcm_path=source_pcm,
                )) as stream:
                    first = stream.read_frames(1003)
                    rest = stream.read_frames(decoded.frames)
                np.testing.assert_array_equal(np.concatenate((first, rest)).T, decoded.samples)
                sidecar_header, sidecar = _read_vvm1_timeline(timeline)
                self.assertEqual((sidecar_header.fields, len(sidecar)), (6, info.frames))
                np.testing.assert_array_equal(
                    np.fromfile(source_pcm, "<f4").reshape(-1, decoded.channels).T,
                    decoded.samples,
                )
    def test_av3a_object_gain_ramp_and_vvm2_stay_frame_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav, metadata, encoded = (root / name for name in ("input.wav", "meta.bin", "out.av3a"))
            frames = 8192
            tone = (np.sin(np.arange(frames) * (2 * np.pi * 440 / 48000)) * 8000).astype("<i2")
            source_audio = AudioData(np.tile(tone.astype(np.float32) / 32767, (3, 1)), 48000)
            av3a._write_pcm16_wav(wav, source_audio, None)
            # A stereo bed and one object with alternating Cartesian position and gain.
            records = []
            for frame in range(16):
                records.append(struct.pack(
                    "<10h7fh",
                    0, 1, 0, 0, 2,   # static/dynamic flags, level, mute, channel reference
                    1, 1, 1, 0, 0,   # Cartesian, extent, gain, diffuse, importance
                    -0.2 if frame % 2 else 0.3, 0.8, 0.1,
                    0.4, 0.2, 0.3, 0.4 if frame % 2 else 0.8,
                    frame % 3 == 0,   # jump position
                ))
            metadata.write_bytes(b"".join(records))
            encoder = av3a.find_encoder()
            self.assertIsNotNone(encoder)
            run_native(
                [
                    encoder, "-MAX_BAND", "FB", "-BITDEPTH", "16",
                    "-MIX", "1", "STEREO", "96000", "1", "64000",
                    "-META_FILE", str(metadata), "160000", "48", str(wav), str(encoded),
                ],
                CancellationToken(),
                av3a.Av3aError,
            )

            timeline, source = root / "objects.vvm", root / "source.f32"
            with closing(av3a.stream_av3a(
                encoded, timeline_path=timeline, source_pcm_path=source,
            )) as stream:
                rendered = stream.read_frames(16 * av3a.FRAME_SAMPLES).reshape(-1, 1024, 3)
            header, points = _read_vvm1_timeline(timeline)
            self.assertEqual((header.bed_channels, header.object_count, header.fields), (2, 1, 6))
            original = np.fromfile(source, "<f4").reshape(rendered.shape)
            np.testing.assert_array_equal(points[:, :, 3], np.max(np.abs(original), axis=1))
            np.testing.assert_array_equal(rendered[:, :, :2], original[:, :, :2])
            np.testing.assert_array_equal(points[:4, 2, 4], (1, 0, 0, 1))
            self.assertTrue(np.all(points[:, 2, 5] > 0.39))
            self.assertGreater(points[0, 2, 0], 0)
            self.assertLess(points[1, 2, 0], 0)
            self.assertGreater(np.max(np.abs(rendered[1, :, 2] - original[1, :, 2])), 0.01)
            np.testing.assert_array_equal(rendered[3, :, 2], original[3, :, 2])

    @unittest.skipUnless(SAMPLE_AV3A_ZIP.is_file(), "External 8033F932_audio.av3a.zip fixture absent")
    def test_real_av3a_fixture_probe_inspect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with zipfile.ZipFile(SAMPLE_AV3A_ZIP) as zf:
                member = zf.namelist()[0]
                zf.extract(member, tmp)
                av3a_path = Path(tmp) / member
            self.assertTrue(av3a.is_av3a_file(av3a_path))
            inspected = av3a.probe_av3a(av3a_path, quick=True)
            self.assertEqual(inspected.channels, 10)
            self.assertEqual(inspected.sample_rate, 48000)
            self.assertEqual(inspected.bitrate, 704000)
