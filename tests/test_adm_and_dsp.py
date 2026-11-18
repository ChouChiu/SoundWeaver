"""Tests for ADM BWF reader/caching, trace builder, and stem pipeline + binaural/AV3A exports."""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from soundweaver.core.atmos.adm_reader import PreparedADM, is_adm_bwf
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.speakers import BY_NAME
from soundweaver.core.stems import StemKind
from soundweaver.features.stems.service import av3a_target, binaural_target, split_stems

AXML_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<ituADM>
  <audioFormatExtended versionID="ITU-R_BS.2076-2">
    <audioProgramme audioProgrammeID="APR_1001" audioProgrammeName="Test"/>
    <audioTrackUID UID="ATU_00000001">
      <audioTrackFormatRef>AT_00010001_01</audioTrackFormatRef>
    </audioTrackUID>
    <audioTrackUID UID="ATU_00000002">
      <audioTrackFormatRef>AT_00030001_01</audioTrackFormatRef>
    </audioTrackUID>
    <audioChannelFormat audioChannelFormatID="AC_00010001" audioChannelFormatName="RoomCentricLeft" typeLabel="0001" typeDefinition="DirectSpeakers">
      <audioBlockFormat audioBlockFormatID="AB_00010001_00000001" rtime="00:00:00.00000" duration="00:00:00.50000">
        <speakerLabel>M+030</speakerLabel>
        <position coordinate="azimuth">30.0</position>
        <position coordinate="elevation">0.0</position>
        <position coordinate="distance">1.0</position>
      </audioBlockFormat>
    </audioChannelFormat>
    <audioChannelFormat audioChannelFormatID="AC_00030001" audioChannelFormatName="Spark" typeLabel="0003" typeDefinition="Objects">
      <audioBlockFormat audioBlockFormatID="AB_00030001_00000001" rtime="00:00:00.00000" duration="00:00:00.50000">
        <cartesian>1</cartesian>
        <position coordinate="X">-0.5</position>
        <position coordinate="Y">1.0</position>
        <position coordinate="Z">0.5</position>
        <gain gainUnit="linear">1.0</gain>
      </audioBlockFormat>
    </audioChannelFormat>
    <audioStreamFormat audioStreamFormatID="AS_00010001" audioStreamFormatName="L">
      <audioChannelFormatIDRef>AC_00010001</audioChannelFormatIDRef>
      <audioTrackFormatIDRef>AT_00010001_01</audioTrackFormatIDRef>
    </audioStreamFormat>
    <audioStreamFormat audioStreamFormatID="AS_00030001" audioStreamFormatName="Obj1">
      <audioChannelFormatIDRef>AC_00030001</audioChannelFormatIDRef>
      <audioTrackFormatIDRef>AT_00030001_01</audioTrackFormatIDRef>
    </audioStreamFormat>
    <audioTrackFormat audioTrackFormatID="AT_00010001_01" audioTrackFormatName="L_tr">
      <audioStreamFormatIDRef>AS_00010001</audioStreamFormatIDRef>
    </audioTrackFormat>
    <audioTrackFormat audioTrackFormatID="AT_00030001_01" audioTrackFormatName="Obj1_tr">
      <audioStreamFormatIDRef>AS_00030001</audioStreamFormatIDRef>
    </audioTrackFormat>
  </audioFormatExtended>
</ituADM>
"""


def _write_adm_wav(path: Path, sample_rate: int = 48000, frames: int = 12000) -> None:
    channels = 2
    t = np.arange(frames, dtype=np.float32) / sample_rate
    pcm = np.zeros((frames, channels), dtype="<i2")
    pcm[:, 0] = (np.sin(2.0 * np.pi * 440.0 * t) * 12000).astype("<i2")
    pcm[:, 1] = (np.sin(2.0 * np.pi * 660.0 * t) * 12000).astype("<i2")

    fmt_body = struct.pack("<HHIIHH", 1, channels, sample_rate, sample_rate * channels * 2, channels * 2, 16)
    axml_bytes = AXML_SAMPLE.encode("utf-8")
    # chna: numTracks=2, numUIDs=2, two 40-byte entries
    chna_entries = b"".join(
        [
            struct.pack("<H12s14s11sB", 1, b"ATU_00000001", b"AT_00010001_01", b"AP_00010001", 0),
            struct.pack("<H12s14s11sB", 2, b"ATU_00000002", b"AT_00030001_01", b"AP_00030001", 0),
        ]
    )
    chna_body = struct.pack("<HH", 2, 2) + chna_entries
    data_bytes = pcm.tobytes()

    def chunk(cid: bytes, body: bytes) -> bytes:
        pad = b"\x00" if len(body) % 2 else b""
        return struct.pack("<4sI", cid, len(body)) + body + pad

    riff_body = b"WAVE" + chunk(b"fmt ", fmt_body) + chunk(b"axml", axml_bytes) + chunk(b"chna", chna_body) + chunk(b"data", data_bytes)
    path.write_bytes(struct.pack("<4sI", b"RIFF", len(riff_body)) + riff_body)


class AdmAndDspTests(unittest.TestCase):
    def test_adm_reader_reuse_and_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "adm_test.wav"
            _write_adm_wav(wav)
            self.assertTrue(is_adm_bwf(wav))
            adm = PreparedADM(wav)
            try:
                self.assertEqual(adm.info.object_count, 1)
                self.assertEqual(adm.info.bed_channels, ("L",))
                blk1 = adm.read_frames(0, 1024)
                blk2 = adm.read_frames(1024, 1024)
                self.assertEqual(blk1.shape, (1024, 2))
                self.assertEqual(blk2.shape, (1024, 2))
                rendered = adm.render_to_layout(BY_NAME["5.1"], CancellationToken())
                with rendered:
                    self.assertEqual(rendered.channels, 6)
                    self.assertGreater(float(np.max(np.abs(rendered.samples))), 0.05)
                trace = adm.build_trace(BY_NAME["5.1"], step_seconds=0.05, token=CancellationToken())
                self.assertEqual(len(trace.objects), 1)
            finally:
                adm.close()

    def test_stems_split_binaural_and_av3a_encode(self) -> None:
        import soundfile as sf

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "bed51.wav"
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            sr = 48000
            frames = sr // 2
            t = np.arange(frames, dtype=np.float32) / sr
            buf = np.zeros((frames, 6), dtype=np.float32)
            buf[:, 0] = 0.25 * np.sin(2.0 * np.pi * 440.0 * t)
            buf[:, 1] = 0.25 * np.sin(2.0 * np.pi * 554.0 * t)
            buf[:, 2] = 0.20 * np.sin(2.0 * np.pi * 660.0 * t)
            sf.write(src, buf, sr, subtype="PCM_16", format="WAV")

            split_stems(
                src,
                out_dir,
                [StemKind.SURROUND],
                120.0,
                atmos=False,
                adm=None,
                speakers=BY_NAME["5.1"],
                binaural=True,
                av3a_encode=True,
                token=CancellationToken(),
                report=lambda _pct, _msg: None,
            )
            self.assertTrue((out_dir / "bed51_surround.wav").is_file())
            self.assertTrue(binaural_target(out_dir, src).is_file())
            self.assertTrue(av3a_target(out_dir, src).is_file())
