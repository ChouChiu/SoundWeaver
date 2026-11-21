"""Tests for the AC-4 A-JOC render orchestration: playback publishing and the export master.

The decode runs the real `Parse`/pool scaffolding over stub frames — a real
bitstream would need a whole AC-4 writer, and the container and reader sides
the playback path leans on are covered by the Atmos ADM tests.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from soundweaver.core.ac4 import parallel
from soundweaver.core.ac4.objects import render_objects
from soundweaver.core.atmos.adm import pcm24
from soundweaver.core.atmos.adm_reader import PreparedADM, _scan_chunks
from soundweaver.core.atmos.oamd import Program
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.speakers import BY_NAME

_FRAME = 480  # samples per stub frame: 48 kHz, so one frame is 10 ms
_RATE = 48000
_TOTAL = 6  # stub frames, cut into three stretches of two


class _StubToc:
    frame_length = _FRAME
    fs_index = False
    frame_rate_index = 0


class _StubRenderer:
    def __init__(self, count: int, _layout) -> None:
        self.current = np.zeros((count, 0))

    def advance(self, _meta, _samples: int) -> None:
        pass

    def state(self) -> None:
        return None


class _StubTracer:
    """Feeds `AdmFile` a timeline that grows one distinct key per frame taken."""

    def __init__(self) -> None:
        self.keys: dict[int, list[tuple]] = {}
        self.size_keys: dict[int, list[tuple]] = {}
        self.taken: list[int] = []

    def metadata(self, k: int, _meta) -> None:
        self.taken.append(k)
        self.keys.setdefault(0, []).append(
            (k * _FRAME / _RATE, 0.0, min(0.9, 0.3 + k * 0.1), 0.5, 0.0, True)
        )  # fmt: skip

    def result(self, _measured):
        return None


@contextmanager
def _inline_pool(_stretches: int):
    yield parallel._Inline()


class AjoicRenderTest(unittest.TestCase):
    def _render(self, tmp: Path, playback: bool):
        """`render_objects` over four stub frames.

        Returns the publish events as (metadata taken, axml size, frames), the
        reader opened at the first publish, the tracer, and the master path.
        """
        program = Program((), 0, 2)
        metas = [SimpleNamespace(program=program, object_count=2) for _ in range(_TOTAL)]
        adm = tmp / "master.wav"
        events: list[tuple[int, int, int]] = []
        reader: list[PreparedADM] = []
        tracer = _StubTracer()

        def publish(path: Path, frames: int) -> None:
            with path.open("rb") as fh:
                chunks, _ = _scan_chunks(fh)
            events.append((len(tracer.taken), chunks["axml"].size, frames))
            if not reader:
                reader.append(PreparedADM(path))

        def fake_render(job, emit_adm=None) -> tuple[list, int]:
            if emit_adm is not None:
                # the first stretch, rendered here: two frames onto the growing master
                for _ in range(2):
                    emit_adm(np.zeros((len(job.adm_sources), _FRAME)))
            elif job.adm_part is not None:
                # a worker stretch: its own two frames as the real one writes them
                job.adm_part.write_bytes(pcm24(np.zeros((len(job.adm_sources), 2 * _FRAME)))[0])
            return [], 0

        def fake_parse(frames: list[bytes], skip: int):
            return [({}, metas[skip + i]) for i in range(len(frames))]

        with (
            patch("soundweaver.core.ac4.objects.read_toc", lambda _f: _StubToc()),
            patch("soundweaver.core.ac4.parallel.read_toc", lambda _f: _StubToc()),
            patch(
                "soundweaver.core.ac4.parallel.plan",
                lambda _f: [parallel.Stretch(0, 0, 2), parallel.Stretch(2, 2, 4),
                            parallel.Stretch(4, 4, _TOTAL)],
            ),
            patch("soundweaver.core.ac4.parallel.worker_pool", _inline_pool),
            patch("soundweaver.core.ac4.parallel._parse", fake_parse),
            patch("soundweaver.core.ac4.objects._render_stretch", fake_render),
            patch("soundweaver.core.ac4.objects.Renderer", _StubRenderer),
            patch("soundweaver.core.ac4.objects.Tracer", lambda *_a: tracer),
            patch("soundweaver.core.ac4.objects.joc_objects", lambda _m: (0, 1)),
        ):
            render_objects(
                [b"frame"] * _TOTAL, CancellationToken(), lambda _f: None, BY_NAME["5.1"],
                False, adm, publish if playback else None,
            )  # fmt: skip
        return events, reader, tracer, adm

    def test_playback_publishes_before_the_parse_finishes(self) -> None:
        """Chunk 0 ships with the first two stretches' metadata only, and the reader grows."""
        with tempfile.TemporaryDirectory() as tmp:
            events, reader, _tracer, _adm = self._render(Path(tmp), playback=True)
            # every publish went out before the third stretch's parse was taken
            self.assertEqual([taken for taken, _, _ in events], [4, 4, 4, 4, 4])
            self.assertEqual(
                [frames for _, _, frames in events],
                [_FRAME, 2 * _FRAME, 3 * _FRAME, 4 * _FRAME, 6 * _FRAME],
            )
            adm = reader[0]
            try:
                # the published file already spans the track, with the timeline so far
                self.assertEqual(adm.frame_count, 6 * _FRAME)
                self.assertEqual(len(adm.tracks[0].blocks), 4)
                time.sleep(0.11)  # past the reader's probe interval
                self.assertTrue(adm.refresh())  # the final axml landed during the run
                self.assertEqual(len(adm.tracks[0].blocks), 6)
            finally:
                adm.close()

    def test_export_writes_a_complete_master(self) -> None:
        """Without a playback callback nothing publishes and close lands the whole master."""
        with tempfile.TemporaryDirectory() as tmp:
            events, _reader, _tracer, adm = self._render(Path(tmp), playback=False)
            self.assertEqual(events, [])
            master = PreparedADM(adm)
            try:
                self.assertEqual(master.frame_count, 6 * _FRAME)
                self.assertEqual(len(master.tracks[0].blocks), 6)
            finally:
                master.close()


if __name__ == "__main__":
    unittest.main()
