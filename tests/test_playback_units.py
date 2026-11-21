"""Unit tests for the playback layer: read-ahead buffer, telemetry reader, AC-4 cache, probe cache."""

from __future__ import annotations

import struct
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

from soundweaver.core import audio, ffmpeg
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import av3a
from soundweaver.core.playback_buffer import PlaybackBuffer


class PlaybackBufferTests(unittest.TestCase):
    def test_read_survives_slow_producer_write(self) -> None:
        """The audio callback never waits on the producer's disk write."""
        pcm = np.linspace(-1, 1, 4096, dtype=np.float32).reshape(-1, 2)
        cursor = 0

        def slow_reader(_count: int) -> np.ndarray:
            nonlocal cursor
            time.sleep(0.25)  # a slow disk write on the producer side
            block = pcm[cursor : cursor + 8192]
            cursor += len(block)
            return block

        with tempfile.TemporaryDirectory(), ExitStack() as stack:
            token = CancellationToken()
            buffer = PlaybackBuffer(16000, 2, False, slow_reader, token, stack)
            buffer.start(0)
            # The first 8192 frames are on disk before the producer thread starts,
            # so a read issued while the producer sleeps must not block on it.
            began = time.monotonic()
            rows = buffer.read(0, 4096)
            self.assertLess(time.monotonic() - began, 0.2)
            np.testing.assert_array_equal(rows, pcm[:4096])
            buffer.close()

    def test_read_after_close_is_silent(self) -> None:
        pcm = np.zeros((8192, 2), dtype=np.float32)
        with tempfile.TemporaryDirectory(), ExitStack() as stack:
            buffer = PlaybackBuffer(16000, 2, False, lambda _c: pcm, CancellationToken(), stack)
            buffer.start(0)
            buffer.close()
            self.assertEqual(buffer.read(0, 100).shape[0], 0)

    def test_read_pasts_written_frames_clamps(self) -> None:
        pcm = np.full((4096, 1), 0.5, dtype=np.float32)
        blocks = iter([pcm, np.zeros((0, 1), dtype=np.float32)])
        with tempfile.TemporaryDirectory(), ExitStack() as stack:
            buffer = PlaybackBuffer(
                16000, 1, False, lambda _c: next(blocks, pcm[:0]), CancellationToken(), stack
            )
            buffer.start(0)
            self.assertEqual(buffer.read(8000, 4096).shape[0], 0)
            self.assertEqual(buffer.read(0, 8192).shape[0], 4096)
            buffer.close()


class Vvm1ReaderTests(unittest.TestCase):
    def test_rows_match_the_one_shot_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timeline.vvm1"
            header_data = struct.pack("<6I", av3a.VVM1_MAGIC, 48000, 1, 2, 0, 1024)
            points = np.arange(30, dtype="<f4").reshape(2, 3, 5)
            path.write_bytes(header_data + points.tobytes())
            header = av3a.read_vvm1_header(path)
            self.assertIsNotNone(header)
            reader = av3a.Vvm1Reader(path, header)
            try:
                for index in range(2):
                    np.testing.assert_array_equal(
                        reader.row(index), av3a.read_vvm1_frame(path, header, index)
                    )
                self.assertIsNone(reader.row(2))
            finally:
                reader.close()


class Ac4CacheTests(unittest.TestCase):
    def _write_cache(self, directory: Path, key: str, pcm: np.ndarray, rate: int) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{key}.v999.ac4.flac"
        sf.write(target, pcm, rate, format="FLAC", subtype="PCM_24")
        return target

    def test_plain_stream_uses_the_cache(self) -> None:
        from soundweaver.core.ac4 import decoder as ac4

        pcm = np.zeros((480, 2), dtype=np.float32)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
            source = Path(tmp) / "song.m4a"
            source.write_bytes(b"")
            target = self._write_cache(Path(cache), f"{source}.stub", pcm, 48000)
            info = ac4.Ac4Info(("L", "R"), 48000, objects=False)
            with (
                patch.object(ac4, "cache_dir", lambda _name=None: Path(cache)),
                patch.object(ac4, "content_key", lambda _p: f"{source}.stub"),
                patch.object(ac4, "_CACHE_VERSION", 999, create=True),
                patch.object(ac4, "describe", return_value=info),
            ):
                self.assertEqual(target.name, ac4.decoded_path(source, CancellationToken()).name)
                plan = ac4.playback_from_cache(source, CancellationToken())
            self.assertIsNotNone(plan)
            cached_info, wav = plan
            self.assertEqual((cached_info.sample_rate, len(cached_info.channels)), (48000, 2))
            rows = wav.read(10, dtype="float32", always_2d=True)
            self.assertEqual(rows.shape, (10, 2))
            wav.close()

    def test_object_stream_ignores_the_cache(self) -> None:
        from soundweaver.core.ac4 import decoder as ac4

        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as cache:
            source = Path(tmp) / "song.m4a"
            source.write_bytes(b"")
            self._write_cache(Path(cache), "key", np.zeros((10, 2), dtype=np.float32), 48000)
            info = ac4.Ac4Info(("L", "R"), 48000, objects=True)
            with (
                patch.object(ac4, "cache_dir", lambda _name=None: Path(cache)),
                patch.object(ac4, "content_key", lambda _p: "key"),
                patch.object(ac4, "_CACHE_VERSION", 999, create=True),
                patch.object(ac4, "describe", return_value=info),
            ):
                self.assertIsNone(ac4.playback_from_cache(source, CancellationToken()))


class ProbeCacheTests(unittest.TestCase):
    def test_probe_reuses_the_result_for_an_unchanged_file(self) -> None:
        ffmpeg._probe_json.cache_clear()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tone.wav"
            sf.write(path, np.zeros((1600, 2), dtype=np.float32), 16000, format="WAV")
            rate = 16000
            first = audio.probe_audio(path, measure_raw_duration=False)
            with patch.object(ffmpeg.subprocess, "run", side_effect=AssertionError("no reprobe")):
                second = audio.probe_audio(path, measure_raw_duration=False)
            self.assertEqual((first.sample_rate, first.channels), (second.sample_rate, second.channels))
            self.assertEqual(rate, first.sample_rate)
        ffmpeg._probe_json.cache_clear()


if __name__ == "__main__":
    unittest.main()
