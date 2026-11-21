"""Tests for ADM BWF reader/caching, trace builder, and stem pipeline and AV3A export."""

from __future__ import annotations

import struct
import tempfile
import time
import unittest
from pathlib import Path
from threading import Event, Thread

import numpy as np

from soundweaver.core.atmos.adm import AdmFile
from soundweaver.core.atmos.adm_reader import PreparedADM, _parse_axml, is_adm_bwf
from soundweaver.core.atmos.oamd import Frame, Program, Render, Update
from soundweaver.core.atmos.trace import Tracer
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.mpegh import cicp_layout
from soundweaver.core.playback_buffer import AdmReadAhead
from soundweaver.core.playback_mix import (
    SILENCE_DB,
    cicp_fold_matrix,
    mix_adm_block,
    monitor_gain,
)
from soundweaver.core.speakers import BY_NAME
from soundweaver.core.stems import StemKind
from soundweaver.features.stems.service import av3a_target, split_stems

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
    def test_mpegh_fold_reserves_headroom_when_a_channel_is_muted(self) -> None:
        layout = cicp_layout(13)
        matrix = cicp_fold_matrix(layout, layout.channels)
        rear_left = layout.channel_names.index("Lrs")
        rear_right = layout.channel_names.index("Rrs")
        self.assertGreater(matrix[rear_left, 0], 0)
        self.assertEqual(matrix[rear_left, 1], 0)
        self.assertGreater(matrix[rear_right, 1], 0)
        self.assertEqual(matrix[rear_right, 0], 0)

        pcm = np.zeros((layout.channels, 1), dtype=np.float32)
        pcm[0, 0] = 0.9
        pcm[rear_left, 0] = 0.9
        center = layout.channel_names.index("C")
        pcm[center, 0] = -0.9
        full_mix = pcm.T @ matrix
        self.assertLess(full_mix[0, 0], 1)
        # The same analytic mute/solo bound `stereo_mix` applies before capping.
        mask = np.ones(layout.channels)
        peak = float((np.abs(matrix) * mask[:, None]).sum(axis=0).max())
        pcm[center, 0] = 0
        solo_mix = pcm.T @ matrix
        self.assertGreater(solo_mix[0, 0], 1)
        self.assertLess(float(np.abs(solo_mix * monitor_gain(2.0, peak)).max()), 1)

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

    def test_adm_monitor_keeps_object_solo_and_bed_mix_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "monitor.wav"
            _write_adm_wav(wav)
            adm = PreparedADM(wav)
            try:
                layout = BY_NAME["5.1"]
                full, objects = mix_adm_block(adm, layout, 0, 1024, set(), set())
                bed, muted = mix_adm_block(adm, layout, 0, 1024, {0}, set())
                solo, selected = mix_adm_block(adm, layout, 0, 1024, set(), {0})
                np.testing.assert_allclose(full, bed + solo, atol=1e-7)
                self.assertGreater(float(np.abs(solo).max()), 0)
                self.assertEqual(len(objects), 1)
                self.assertEqual(selected, objects)
                self.assertFalse(muted[0].active)
                self.assertEqual(muted[0].level_db, SILENCE_DB)
            finally:
                adm.close()

    def test_adm_playback_file_survives_decoder_completion(self) -> None:
        progress = AdmReadAhead(CancellationToken(), sample_rate=4)
        release = Event()
        finished = Event()

        def decode(target, publish):
            writer = AdmFile(target, 48000, 4, Program((), 0, 1), 1)
            try:
                path = writer.prepare_readback({})
                writer.write_pcm(b"\x00\x00\x20" * 2)
                writer.file.flush()
                publish(path, writer.frames_written)
                if not release.wait(2):
                    raise TimeoutError("playback did not release the decoder")
                writer.write_pcm(b"\x00\x00\xe0" * 2)
                writer.file.flush()
                publish(path, writer.frames_written)
            finally:
                writer.discard()
                finished.set()

        reader = None
        try:
            path = progress.start(decode)
            self.assertEqual(path.name, "playback.wav")
            self.assertEqual(progress.state(), (2, False, None))
            reader = PreparedADM(path)
            self.assertEqual(reader.frame_count, 4)

            # Open the PCM handle for the first time after the decoder has released its writer.
            release.set()
            self.assertTrue(finished.wait(2))
            np.testing.assert_allclose(reader.read_frames(0, 2), [[0.25], [0.25]])
            np.testing.assert_allclose(reader.read_frames(2, 2), [[-0.25], [-0.25]])
        finally:
            release.set()
            if reader is not None:
                reader.close()
            progress.close()

    def test_adm_size_respects_authored_ramp_and_jump(self) -> None:
        data = b"""
        <audioFormatExtended>
          <audioChannelFormat audioChannelFormatID="AC_00031001" typeLabel="0003">
            <audioBlockFormat rtime="0" duration="1">
              <position coordinate="azimuth">0</position>
            </audioBlockFormat>
            <audioBlockFormat rtime="1" duration="1">
              <position coordinate="azimuth">0</position>
              <height>180</height>
              <jumpPosition>0</jumpPosition>
            </audioBlockFormat>
            <audioBlockFormat rtime="2" duration="1">
              <position coordinate="azimuth">0</position>
              <jumpPosition>1</jumpPosition>
            </audioBlockFormat>
          </audioChannelFormat>
        </audioFormatExtended>
        """
        channels, _, _ = _parse_axml(data)
        _, _, blocks, _ = channels["AC_00031001"]
        self.assertEqual(blocks[0].size_at(0.5), 0.0)
        self.assertEqual(blocks[1].size_at(1.5), 0.25)
        self.assertEqual(blocks[1].size_at(2.0), 0.5)
        self.assertEqual(blocks[2].size_at(2.0), 0.0)

    def test_atmos_size_only_updates_survive_adm_export_and_playback(self) -> None:
        rate = 48000
        program = Program((), 0, 1)
        tracer = Tracer(rate, 0, rate, rate // 8, BY_NAME["5.1"])
        for frame, size, ramp in ((0, 0.0, 0), (2, 0.75, rate // 8), (6, 0.0, 0)):
            update = Update(0, ramp, True, 0.0, Render(size=(size,) * 3))
            tracer.metadata(frame, Frame(1, program, [[update]]))
        self.assertEqual(len(tracer.keys[0]), 1)

        with tempfile.TemporaryDirectory() as tmp:
            for playback in (False, True):
                with self.subTest(playback=playback):
                    path = Path(tmp) / f"size-{playback}.wav"
                    writer = AdmFile(path, rate, rate, program, 1)
                    try:
                        if playback:
                            writer.prepare_readback(tracer.keys, tracer.size_keys)
                        writer.write_pcm(bytes(3 * rate))
                        if playback:
                            writer.file.flush()
                        else:
                            writer.close(tracer.keys, tracer.size_keys)
                        reader = PreparedADM(path)
                        try:
                            track = reader.tracks[0]
                            self.assertEqual(reader.block_at(track, 0.125).size, 0.0)
                            growing = reader.block_at(track, 0.32)
                            self.assertGreater(growing.size, 0.0)
                            self.assertLess(growing.size, 0.75)
                            self.assertEqual(reader.block_at(track, 0.5).size, 0.75)
                            self.assertEqual(reader.block_at(track, 0.875).size, 0.0)
                            self.assertEqual({(b.x, b.y, b.z) for b in track.blocks}, {(0.5, 0.5, 0.0)})
                        finally:
                            reader.close()
                    finally:
                        writer.discard()

    def test_adm_playback_file_grows_its_metadata_while_playing(self) -> None:
        """The published ADM keeps its PCM readable while the axml timeline is rewritten."""
        rate = 48000
        program = Program((), 0, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "grow.wav"
            writer = AdmFile(path, rate, rate, program, 1)
            try:
                keys = {0: [(0.0, 0.0, 0.5, 0.5, 0.0, True)]}
                readback = writer.prepare_readback(keys)
                writer.write_pcm(bytes(3 * rate // 2))  # the first half second
                writer.file.flush()
                reader = PreparedADM(readback)
                try:
                    self.assertEqual(reader.frame_count, rate)
                    self.assertEqual(
                        (reader.block_at(reader.tracks[0], 0.75).x,
                         reader.block_at(reader.tracks[0], 0.75).z),
                        (0.5, 0.0),
                    )
                    self.assertFalse(reader.refresh())  # nothing grew yet

                    # the scan moves the object and the PCM keeps appending underneath
                    keys[0].append((0.5, 0.0, -1.0, 1.0, 1.0, True))
                    writer.refresh(force=True)
                    time.sleep(0.11)  # past the reader's probe interval
                    self.assertTrue(reader.refresh())
                    self.assertEqual(
                        (reader.block_at(reader.tracks[0], 0.75).x,
                         reader.block_at(reader.tracks[0], 0.75).z),
                        (0.0, 1.0),
                    )

                    writer.write_pcm(b"\x01\x00\x00" * (rate // 2))
                    writer.file.flush()
                    pcm = reader.read_frames(0, rate)
                    self.assertEqual(pcm.shape, (rate, 1))
                    self.assertTrue((pcm[: rate // 2] == 0.0).all())
                    self.assertTrue((pcm[rate // 2 :] == 1 / 8388608).all())
                finally:
                    reader.close()
            finally:
                writer.discard()

    def test_prepared_adm_refresh_keeps_tracks_when_the_rewrite_tears(self) -> None:
        rate = 48000
        program = Program((), 0, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "torn.wav"
            writer = AdmFile(path, rate, rate, program, 1)
            try:
                keys = {0: [(0.0, 0.0, 0.5, 0.5, 0.0, True)]}
                readback = writer.prepare_readback(keys)
                reader = PreparedADM(readback)
                try:
                    self.assertFalse(reader.refresh())
                    time.sleep(0.11)

                    # a rewrite that died between the length field and the body
                    with path.open("r+b") as fh:
                        fh.seek(reader._axml_offset - 4)
                        fh.write(struct.pack("<I", 4096))
                    self.assertFalse(reader.refresh())  # truncated body: keep the old tracks
                    self.assertEqual(reader.tracks[0].blocks[0].x, 0.5)
                    time.sleep(0.11)

                    writer.keys[0].append((0.5, 0.0, -1.0, 1.0, 1.0, True))
                    writer.refresh(force=True)
                    self.assertTrue(reader.refresh())
                    self.assertEqual(reader.tracks[0].blocks[-1].x, 0.0)
                finally:
                    reader.close()
            finally:
                writer.discard()

    def test_adm_reader_tracks_a_growing_axml_while_pcm_appends(self) -> None:
        """A writer thread appends PCM and rewrites the axml; the mixer-side reader never errors."""
        rate = 48000
        program = Program((), 0, 1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "race.wav"
            writer = AdmFile(path, rate, rate * 2, program, 1)
            keys = {0: [(0.0, 0.0, 0.5, 0.5, 0.0, True)]}
            readback = writer.prepare_readback(keys)
            reader = PreparedADM(readback)
            errors: list[Exception] = []
            reached: list[float] = []
            stop = Event()
            try:

                def play() -> None:
                    t = 0.0
                    try:
                        while not stop.is_set() and t < 1.9:
                            reader.refresh()
                            point = reader.block_at(reader.tracks[0], t)
                            if point is not None:
                                reached.append(point.x)
                            reader.read_frames(min(rate - 1, int(t * rate)), 64)
                            t += 0.05
                            # pace the playhead so its last polls land after the
                            # writer's final rewrite and the probes picked it up
                            time.sleep(0.02)
                    except Exception as error:  # the audio path must never see these
                        errors.append(error)

                player = Thread(target=play, daemon=True)
                player.start()
                for step in range(1, 20):
                    keys[0].append((step * 0.1, 0.0, min(1.0, step / 10), 0.5, 0.0, True))
                    writer.refresh(force=True)
                    writer.write_pcm(bytes(3 * rate // 20))
                    writer.file.flush()
                    time.sleep(0.005)
                # the reader walks the whole timeline past the writer's last rewrite
                player.join(2)
                stop.set()
                player.join(1)
            finally:
                stop.set()
                reader.close()
                writer.discard()
        self.assertEqual(errors, [])
        # the reader picked up the growing timeline: x walks to the right edge
        self.assertGreater(max(reached, default=0.0), 0.95)

    def test_adm_playback_limits_decode_ahead_and_restarts_after_seek(self) -> None:
        rate = 4
        progress = AdmReadAhead(CancellationToken(), sample_rate=rate)
        release = Event()
        first_window = Event()
        advanced = Event()

        def decode(target, publish):
            publish(target, rate // 2)
            if not release.wait(2):
                raise TimeoutError("playback did not open the initial buffer")
            first_window.set()
            publish(target, rate * 6)
            advanced.set()
            publish(target, rate * 7)

        try:
            progress.start(decode)
            self.assertEqual(progress.state(), (2, False, None))
            release.set()
            self.assertTrue(first_window.wait(2))
            self.assertFalse(advanced.wait(0.05))
            progress.request(rate * 4)
            self.assertTrue(advanced.wait(2))
            self.assertEqual(progress.wait_for(rate * 8), rate * 7)
        finally:
            release.set()
            progress.close()

    def test_stems_split_and_av3a_encode(self) -> None:
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
                av3a_encode=True,
                token=CancellationToken(),
                report=lambda _pct, _msg: None,
            )
            self.assertTrue((out_dir / "bed51_surround.wav").is_file())
            self.assertTrue(av3a_target(out_dir, src).is_file())

