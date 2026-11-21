"""Tests for the 360RA / MPEG-H ADM writer, its object records, and the export path."""

from __future__ import annotations

import struct
import tempfile
import unittest
from itertools import pairwise
from pathlib import Path

import numpy as np

from soundweaver.core.atmos.adm_reader import PreparedADM, is_adm_bwf
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import mpegh
from soundweaver.core.codecs.mpegh_adm import MpeghAdmFile, key_of, position, spread
from soundweaver.core.codecs.mpegh_eri import (
    OBJECT_FIELDS,
    OBJECTS_MAX,
    RECORD_BYTES,
    MpeghObject,
    read_record,
)

RATE = 48000


def _object(
    index: int = 0,
    azimuth: float = 0.0,
    elevation: float = 0.0,
    gain: float = 1.0,
    width: float = 0.0,
    height: float = 0.0,
    depth: float = 0.0,
) -> MpeghObject:
    return MpeghObject(index, azimuth, elevation, 1.0, gain, width, height, depth, 7.0)


def _record(objects: tuple[MpeghObject, ...]) -> bytes:
    """The worker's record, built the way `sw_mpegh_object_state` writes it."""
    header = struct.pack("<I", len(objects))
    rows = np.zeros(OBJECTS_MAX * OBJECT_FIELDS, np.float32)
    for index, meta in enumerate(objects):
        rows[index * OBJECT_FIELDS : (index + 1) * OBJECT_FIELDS] = (
            meta.azimuth,
            meta.elevation,
            meta.radius,
            meta.gain,
            meta.width,
            meta.height,
            meta.depth,
            meta.priority,
        )
    return header + rows.tobytes()


class ObjectRecordTests(unittest.TestCase):
    def test_record_round_trip(self) -> None:
        written = (_object(0, 30.0, 10.0, 0.5), _object(1, -90.0, -20.0, 0.25, 30.0, 60.0, 0.5))
        objects = read_record(_record(written))
        self.assertEqual(len(objects), 2)
        self.assertAlmostEqual(objects[0].azimuth, 30.0)
        self.assertAlmostEqual(objects[1].gain, 0.25)
        self.assertAlmostEqual(objects[1].depth, 0.5)
        self.assertEqual([meta.index for meta in objects], [0, 1])

    def test_record_length_is_fixed(self) -> None:
        self.assertEqual(len(_record(())), RECORD_BYTES)
        with self.assertRaises(ValueError):
            read_record(b"\0" * (RECORD_BYTES - 4))

    def test_too_many_objects_is_refused(self) -> None:
        header = struct.pack("<I", OBJECTS_MAX + 1)
        with self.assertRaises(ValueError):
            read_record(header + bytes(RECORD_BYTES - 4))

    def test_silent_object_is_not_audible(self) -> None:
        self.assertFalse(_object(gain=0.0).audible)
        self.assertTrue(_object(gain=0.5).audible)


class GeometryTests(unittest.TestCase):
    def test_azimuth_puts_left_on_the_left(self) -> None:
        x, y, z = position(90.0, 0.0)
        self.assertAlmostEqual(x, -1.0, places=6)
        self.assertAlmostEqual(y, 0.0, places=6)
        self.assertAlmostEqual(z, 0.0, places=6)

    def test_elevation_reaches_the_ceiling(self) -> None:
        self.assertAlmostEqual(position(0.0, 90.0)[2], 1.0, places=6)
        self.assertAlmostEqual(position(0.0, 0.0)[1], 1.0, places=6)

    def test_spread_becomes_normalised_size(self) -> None:
        width, height, depth = spread(_object(width=90.0, height=180.0, depth=0.5))
        self.assertAlmostEqual(width, 0.5)
        self.assertAlmostEqual(height, 1.0)
        self.assertAlmostEqual(depth, 0.5)
        self.assertEqual(spread(_object(width=720.0)), (1.0, 0.0, 0.0))


class ObjectAdmTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "master_adm.wav"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, frames: int = 3, samples: int = 2000, **object_kwargs) -> Path:
        writer = MpeghAdmFile(self.target, RATE, frames * samples, 1)
        signal = np.full((1, samples), 0.25, np.float32)
        for _ in range(frames):
            writer.write_frame(signal, (_object(**object_kwargs),), signal)
        writer.close()
        return self.target

    def test_objects_become_objects_tracks(self) -> None:
        target = self._write()
        self.assertTrue(is_adm_bwf(target))
        prepared = PreparedADM(target)
        try:
            self.assertEqual(prepared.channels, 1)
            self.assertEqual(prepared.frame_count, 6000)
            track = prepared.tracks[0]
            self.assertTrue(track.is_object)
            self.assertEqual(track.name, "Object 1")
            self.assertFalse(track.is_lfe)
            block = track.blocks[0]
            self.assertAlmostEqual(block.start, 0.0)
            self.assertAlmostEqual(block.x, 0.5)
        finally:
            prepared.close()

    def test_gain_is_applied_to_the_audio(self) -> None:
        target = self._write(samples=512, gain=0.5)
        prepared = PreparedADM(target)
        try:
            pcm = prepared.read_frames(0, 64)
            self.assertAlmostEqual(float(np.abs(pcm).max()), 0.125, places=5)
        finally:
            prepared.close()

    def test_moving_object_writes_contiguous_blocks(self) -> None:
        writer = MpeghAdmFile(self.target, RATE, 4 * 512, 1)
        signal = np.zeros((1, 512), np.float32)
        for azimuth in (0.0, 0.0, 90.0, 90.0):
            writer.write_frame(signal, (_object(azimuth=azimuth),), signal)
        writer.close()
        prepared = PreparedADM(self.target)
        try:
            blocks = prepared.tracks[0].blocks
            self.assertEqual(len(blocks), 2)
            for earlier, later in pairwise(blocks):
                self.assertAlmostEqual(earlier.start + earlier.duration, later.start, places=4)
            self.assertAlmostEqual(blocks[1].x, 0.0, places=3)
        finally:
            prepared.close()

    def test_container_length_cuts_the_last_frame(self) -> None:
        writer = MpeghAdmFile(self.target, RATE, 2000, 1)
        signal = np.zeros((1, 1024), np.float32)
        for _ in range(3):
            writer.write_frame(signal, (_object(),), signal)
        writer.close()
        prepared = PreparedADM(self.target)
        try:
            self.assertEqual(prepared.frame_count, 2000)
        finally:
            prepared.close()

    def test_frame_padding_keeps_the_timeline(self) -> None:
        """A short first render must not shift the object signals that follow it."""
        writer = MpeghAdmFile(self.target, RATE, 1024 + 64, 1)
        signal = np.arange(1024, dtype=np.float32)[None, :] / 4096.0
        rendered = np.zeros((1, 64), np.float32)
        writer.write_frame(signal, (_object(),), rendered)
        writer.close()
        prepared = PreparedADM(self.target)
        try:
            head = prepared.read_frames(0, 3)
            self.assertAlmostEqual(float(head[0, 0]), 0.0, places=6)
        finally:
            prepared.close()

    def test_channel_stream_becomes_a_bed(self) -> None:
        layout = mpegh.cicp_layout(16)
        writer = MpeghAdmFile(self.target, RATE, 512, 0, bed=layout.speakers)
        rendered = np.zeros((layout.channels, 512), np.float32)
        rendered[0] = 0.5
        writer.write_frame(np.zeros((0, 512), np.float32), (), rendered)
        writer.close()
        prepared = PreparedADM(self.target)
        try:
            self.assertEqual(prepared.channels, layout.channels)
            self.assertEqual([t.name for t in prepared.tracks], list(layout.channel_names))
            self.assertTrue(all(not t.is_object for t in prepared.tracks))
            self.assertEqual(sum(t.is_lfe for t in prepared.tracks), 1)
        finally:
            prepared.close()

    def test_published_master_stays_readable_while_it_grows(self) -> None:
        """A playback decode publishes the metadata, then keeps writing the audio."""
        key, size = key_of(_object(azimuth=90.0), 0.0)
        self.assertEqual(size[1], (0.0, 0.0, 0.0))
        self.assertFalse(any(size[1]))
        writer = MpeghAdmFile(
            self.target, RATE, 2 * 512, 1, positions={0: [key]}, sizes={}
        )
        published = writer.publish()
        first = PreparedADM(published)
        try:
            self.assertEqual(first.frame_count, 2 * 512)
            self.assertAlmostEqual(first.tracks[0].blocks[0].x, 0.0, places=3)
        finally:
            first.close()
        writer.write_frame(np.zeros((1, 512), np.float32), (_object(),), np.zeros((1, 512), np.float32))
        writer.close()
        second = PreparedADM(self.target)
        try:
            self.assertEqual(second.frame_count, 512)
        finally:
            second.close()

    def test_object_count_change_is_refused(self) -> None:
        writer = MpeghAdmFile(self.target, RATE, 512, 2)
        with self.assertRaises(ValueError):
            writer.write_frame(
                np.zeros((1, 512), np.float32), (_object(),), np.zeros((1, 512), np.float32)
            )
        writer.discard()


class TraceTests(unittest.TestCase):
    """The object trace behind the sound field video and the monitor's object meters."""

    fixtures = Path(__file__).resolve().parents[1] / "native/mpegh/smoke_test_suite/inp"

    def test_key_of_reports_adm_position_and_spread(self) -> None:
        key, size = key_of(_object(azimuth=0.0, width=90.0), 1.5)
        self.assertAlmostEqual(key[0], 1.5)
        self.assertAlmostEqual(key[2][1], 1.0, places=3)  # ADM Y: +1 is the front wall
        self.assertAlmostEqual(size[1][0], 0.5)

    @unittest.skipUnless(mpegh.find_decoder(), "MPEG-H decoder is not built")
    def test_smoke_stream_traces_objects(self) -> None:
        source = self.fixtures / "sine_1khz_cicp16.mhas"
        if not source.is_file():
            self.skipTest("smoke fixture missing")
        from soundweaver.core.codecs.mpegh_trace import trace_mpegh
        from soundweaver.core.speakers import BY_NAME

        layout = BY_NAME["5.1.4"]
        trace = trace_mpegh(source, CancellationToken(), lambda _f: None, layout)
        self.assertGreaterEqual(len(trace.objects), 1)
        self.assertEqual(trace.speakers, layout.channels)
        self.assertGreater(trace.duration, 0.0)
        self.assertEqual(len(trace.to_json()["objects"]), len(trace.objects))


class ExportTests(unittest.TestCase):
    """The decode path writes a master straight from a stream, when the engine is built."""

    fixtures = Path(__file__).resolve().parents[1] / "native/mpegh/smoke_test_suite/inp"

    @unittest.skipUnless(mpegh.find_decoder(), "MPEG-H decoder is not built")
    def test_smoke_stream_exports_an_adm(self) -> None:
        source = self.fixtures / "sine_1khz_cicp16.mhas"
        if not source.is_file():
            self.skipTest("smoke fixture missing")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sine_adm.wav"
            audio = mpegh.decode_mpegh(source, cicp=16, adm=target, pcm=True)
            self.assertIsNotNone(audio)
            assert audio is not None
            try:
                self.assertEqual(audio.channels, mpegh.cicp_layout(16).channels)
                prepared = PreparedADM(target)
                try:
                    self.assertGreater(prepared.frame_count, 0)
                    self.assertEqual(prepared.sample_rate, audio.sample_rate)
                    self.assertEqual(prepared.frame_count, audio.samples.shape[1])
                finally:
                    prepared.close()
            finally:
                audio.cleanup()


if __name__ == "__main__":
    unittest.main()
