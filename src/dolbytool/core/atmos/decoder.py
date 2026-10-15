"""A Dolby Atmos (E-AC-3 JOC) track, decoded and rendered to a speaker layout.

ffmpeg decodes the channel-based core; everything past that is TS 103 420:
the OAMD and JOC payloads are read from each syncframe, the objects are
rebuilt in the QMF domain from the core's five main channels (JOC downmix
configurations 0 and 3; the 7.X ones are refused), and the room
renderer places them.  The core is decoded from the bare elementary stream so
PCM frame k is syncframe k; the result is then cut to the container's own
timeline, which an edit list may start a few frames in.

The metadata is read in one pass here; the audio is rebuilt in stretches by
worker processes (`chunk.py`), each started as soon as the pass has the
state its first frame needs.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import pairwise
from multiprocessing.pool import AsyncResult
from pathlib import Path

import numpy as np
import soundfile as sf

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.ffmpeg import run_ffmpeg
from dolbytool.core.i18n import get_language, t
from dolbytool.core.paths import cache_dir, content_key, work_root
from dolbytool.core.pcm import AudioData, create_pcm_audio
from dolbytool.core.speakers import DEFAULT_ATMOS, SpeakerLayout

from .adm import AdmFile
from .bitstream import BitstreamError
from .chunk import (
    CORE_CHANNELS,
    FRAME,
    AtmosError,
    ChunkJob,
    ChunkResult,
    decode_chunk,
    init_worker,
)
from .eac3 import INDEPENDENT, syncframes
from .emdf import JOC_PAYLOAD, OAMD_PAYLOAD, object_payloads
from .oamd import OamdDecoder
from .render import Renderer, joc_objects
from .trace import ObjectTrace, Tracer

logger = logging.getLogger(__name__)

__all__ = ["AtmosDecode", "AtmosError", "decode_atmos", "trace_atmos"]

# How far into the elementary stream the container's first sample may lie.
_MAX_LEAD_FRAMES = 16
_MATCH_SAMPLES = 4096
# Stretches per worker, so a slow stretch does not leave the others idle, and
# the shortest stretch worth the two frames of warm-up each one costs.
_CHUNKS_PER_WORKER = 3
_MIN_CHUNK_FRAMES = 96
_MAX_WORKERS = 12
# Share of the progress bar the metadata pass takes; the workers fill the rest.
_PASS_SHARE = 0.1


@dataclass(frozen=True, slots=True)
class AtmosDecode:
    trace: ObjectTrace
    # the speaker render on the container's timeline, owned by the caller (`with` it)
    speakers: AudioData | None = None
    layout: SpeakerLayout = DEFAULT_ATMOS


def decode_atmos(
    source: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None] = lambda _f: None,
    *,
    layout: SpeakerLayout = DEFAULT_ATMOS,
    speakers: bool = False,
    adm: Path | None = None,
) -> AtmosDecode:
    """Decode the first audio track of `source` once and produce every output asked for.

    `speakers` returns the render to `layout` (in its channel order), `adm` writes the objects there as ADM
    BWF.  The object trace always comes along: it is cached, so a sound field
    video of the same file needs no second decode.
    """
    with _prepare(source, token) as (frames, core, rate, first, length):
        out = create_pcm_audio(len(layout.channels), length, rate) if speakers else None
        try:
            trace = _Decode(frames, core, rate, first, length, layout, out, adm).run(
                token, on_fraction
            )
        except BaseException:
            if out is not None:
                out.cleanup()
            raise
    _store_trace(source, layout, trace)
    return AtmosDecode(trace, out, layout)


def trace_atmos(
    source: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None] = lambda _f: None,
    layout: SpeakerLayout = DEFAULT_ATMOS,
) -> ObjectTrace:
    """Where each dynamic object is, how loud it is and how `layout` shares it out."""
    cached = _cached_trace(source, layout)
    if cached is not None:
        on_fraction(1.0)
        return cached
    return decode_atmos(source, token, on_fraction, layout=layout).trace


class _Decode:
    def __init__(
        self,
        frames: list,
        core: Path,
        rate: int,
        first: int,
        length: int,
        layout: SpeakerLayout,
        speakers: AudioData | None,
        adm: Path | None,
    ) -> None:
        self.frames, self.core, self.rate, self.layout = frames, core, rate, layout
        self.first, self.length = first, length
        self.speakers, self.adm_path = speakers, adm
        total = len(frames)
        workers = min(_MAX_WORKERS, max(1, (os.process_cpu_count() or 2) - 1))
        count = max(1, min(workers * _CHUNKS_PER_WORKER, total // _MIN_CHUNK_FRAMES))
        bounds = [round(total * i / count) for i in range(count + 1)]
        self.chunks = list(pairwise(bounds))
        self.workers = min(workers, count)

    def run(self, token: CancellationToken, on_fraction: Callable[[float], None]) -> ObjectTrace:
        total = len(self.frames)
        tracer = Tracer(self.rate, self.first, self.length, FRAME, self.layout)
        oamd = OamdDecoder()
        renderer: Renderer | None = None
        adm: AdmFile | None = None
        oamd_payloads: list[bytes] = []
        joc_payloads: list[bytes] = []
        snapshots: list[tuple] = []
        pending: list[AsyncResult] = []
        done = 0

        def progress(k: int) -> None:
            finished = sum(r.ready() for r in pending)
            on_fraction(_PASS_SHARE * k / total + (1 - _PASS_SHARE) * finished / len(self.chunks))

        with tempfile.TemporaryDirectory(dir=work_root()) as scratch, self._pool() as pool:
            try:
                for k, frame in enumerate(self.frames):
                    token.raise_if_cancelled()
                    payloads = object_payloads(frame.data)
                    if OAMD_PAYLOAD not in payloads or JOC_PAYLOAD not in payloads:
                        raise AtmosError(t("atmos.no_metadata", frame=k))
                    oamd_payloads.append(payloads[OAMD_PAYLOAD])
                    joc_payloads.append(payloads[JOC_PAYLOAD])
                    starts_chunk = (
                        len(snapshots) < len(self.chunks) and k == self.chunks[len(snapshots)][0]
                    )
                    oamd_state = oamd.snapshot() if starts_chunk else None
                    try:
                        meta = oamd.decode(payloads[OAMD_PAYLOAD])
                    except BitstreamError as error:
                        raise AtmosError(t("atmos.bad_metadata", frame=k, error=error)) from error
                    if renderer is None:
                        renderer = Renderer(len(joc_objects(meta)), self.layout)
                        if meta.program.isf_objects:
                            logger.warning(
                                "%d ISF objects carry no position here and play from the room centre",
                                meta.program.isf_objects,
                            )
                        if self.adm_path is not None:
                            adm = AdmFile(
                                self.adm_path,
                                self.rate,
                                self.length,
                                meta.program,
                                meta.object_count,
                            )
                    elif len(joc_objects(meta)) != renderer.current.shape[0]:
                        raise AtmosError(t("atmos.object_count_changed", frame=k))
                    if adm is not None and (
                        meta.object_count != adm.objects
                        or meta.program.bed_labels != adm.program.bed_labels
                    ):
                        raise AtmosError(t("atmos.adm_layout_changed"))
                    if starts_chunk:
                        snapshots.append((oamd_state, renderer.state() if k else None))
                    tracer.metadata(k, meta)
                    renderer.advance(meta, FRAME)
                    # a stretch can start once its last frame and the one after are read
                    while done < len(snapshots) and min(self.chunks[done][1], total - 1) <= k:
                        pending.append(
                            pool.apply_async(
                                decode_chunk,
                                (self._job(done, snapshots[done], oamd_payloads, joc_payloads,
                                           Path(scratch), adm),),
                            )
                        )  # fmt: skip
                        done += 1
                    if k % 64 == 0:
                        progress(k)
                results: list[ChunkResult] = []
                for result in pending:
                    while not result.ready():
                        token.raise_if_cancelled()
                        result.wait(0.1)
                        progress(total)
                    results.append(result.get())
            except BaseException:
                pool.terminate()
                if adm is not None:
                    adm.discard()
                raise
            if adm is not None:
                for index in range(len(self.chunks)):
                    adm.append(Path(scratch) / f"adm-{index}.pcm")
                adm.close(tracer.keys)
                clipped = sum(r.clipped for r in results)
                if clipped:
                    logger.warning("adm: %d samples clipped at full scale", clipped)
        on_fraction(1.0)
        return tracer.result([m for r in results for m in r.measured])

    def _job(
        self,
        index: int,
        snapshot: tuple,
        oamd_payloads: list[bytes],
        joc_payloads: list[bytes],
        scratch: Path,
        adm: AdmFile | None,
    ) -> ChunkJob:
        start, stop = self.chunks[index]
        joc_from = max(0, start - 2)
        oamd_state, gains = snapshot
        return ChunkJob(
            core=self.core,
            start=start,
            stop=stop,
            total=len(self.frames),
            joc_from=joc_from,
            joc=tuple(joc_payloads[joc_from : min(stop, len(self.frames) - 1) + 1]),
            oamd=tuple(oamd_payloads[start:stop]),
            oamd_state=oamd_state,
            gains=gains,
            first=self.first,
            last=self.first + self.length,
            layout=self.layout,
            speakers=self.speakers.backing_path if self.speakers is not None else None,
            adm=scratch / f"adm-{index}.pcm" if adm is not None else None,
            adm_sources=adm.sources if adm is not None else (),
        )

    @contextmanager
    def _pool(self) -> Iterator[multiprocessing.pool.Pool]:
        # spawn: the caller may be a thread of a Qt application, which must not fork
        pool = multiprocessing.get_context("spawn").Pool(
            self.workers, init_worker, (get_language(),)
        )
        try:
            yield pool
            pool.close()
        finally:
            pool.terminate()
            pool.join()


_TRACE_SUFFIX = ".trace.pickle"
_KEEP_TRACES = 16
# bumped whenever the shares a layout gets change, so older traces go unread
_TRACE_VERSION = 3


def _trace_path(source: Path, layout: SpeakerLayout) -> Path:
    # the shares are the layout's, so each layout keeps a trace of its own
    return cache_dir() / f"{content_key(source)}.{layout.name}.v{_TRACE_VERSION}{_TRACE_SUFFIX}"


def _cached_trace(source: Path, layout: SpeakerLayout) -> ObjectTrace | None:
    path = _trace_path(source, layout)
    try:
        with path.open("rb") as file:
            trace = pickle.load(file)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("atmos: discarding unreadable trace cache %s", path, exc_info=True)
        path.unlink(missing_ok=True)
        return None
    fits = isinstance(trace, ObjectTrace) and trace.speakers == layout.channels
    return trace if fits else None


def _store_trace(source: Path, layout: SpeakerLayout, trace: ObjectTrace) -> None:
    directory = cache_dir()
    path = _trace_path(source, layout)
    partial = path.with_suffix(".partial")
    with partial.open("wb") as file:
        pickle.dump(trace, file, protocol=pickle.HIGHEST_PROTOCOL)
    partial.replace(path)
    old = sorted(directory.glob("*" + _TRACE_SUFFIX), key=lambda p: p.stat().st_mtime)
    for stale in old[:-_KEEP_TRACES]:
        stale.unlink(missing_ok=True)


@contextmanager
def _prepare(source: Path, token: CancellationToken) -> Iterator[tuple[list, Path, int, int, int]]:
    """Syncframes, the decoded core, its rate, and the container's span of its timeline."""
    with tempfile.TemporaryDirectory(dir=work_root()) as scratch:
        work = Path(scratch)
        stream, core, reference = work / "stream.ec3", work / "core.wav", work / "reference.wav"

        # The front left channel as the container presents it, to find its timeline by;
        # decoded alongside the core, each a full pass over the file.
        with ThreadPoolExecutor(1) as side:
            front = side.submit(
                run_ffmpeg,
                ["-i", str(source), "-map", "0:a:0", "-af", "channelmap=map=FL:channel_layout=mono",
                 "-c:a", "pcm_f32le", str(reference)],
                token,
            )  # fmt: skip
            run_ffmpeg(
                ["-i", str(source), "-map", "0:a:0", "-c", "copy", "-f", "eac3", str(stream)], token
            )
            try:
                all_frames = list(syncframes(stream.read_bytes()))
            except BitstreamError as error:
                raise AtmosError(t("atmos.bad_syncframes", error=error)) from error
            if not all_frames:
                raise AtmosError(t("atmos.no_frames"))

            # Atmos JOC and OAMD metadata are carried in independent substream 0.
            # If dependent substreams (e.g. 7.1) or secondary substreams are present,
            # filter to independent substream 0 so FFmpeg decodes the 5.1 core for JOC reconstruction.
            frames = [f for f in all_frames if f.stream_type == INDEPENDENT and f.substream == 0]
            if not frames:
                raise AtmosError(t("atmos.no_substream"))
            if len(frames) != len(all_frames):
                logger.info(
                    "atmos: filtered %d frames (dependent/secondary substreams) -> %d independent substream 0 frames",
                    len(all_frames),
                    len(frames),
                )
                stream.write_bytes(b"".join(f.data for f in frames))

            run_ffmpeg(["-f", "eac3", "-i", str(stream), "-c:a", "pcm_f32le", str(core)], token)
            front.result()
        stream.unlink()
        with sf.SoundFile(core) as pcm, sf.SoundFile(reference) as ref:
            if pcm.channels != CORE_CHANNELS or pcm.frames != len(frames) * FRAME:
                raise AtmosError(t("atmos.core_mismatch", channels=pcm.channels, frames=pcm.frames))
            start = _container_offset(pcm, ref)
            rate, length = pcm.samplerate, ref.frames
        reference.unlink()
        logger.info(
            "atmos: %d syncframes, container starts %d samples in, %d samples long",
            len(frames), start, length,
        )  # fmt: skip
        yield frames, core, rate, start, length


def _container_offset(pcm: sf.SoundFile, ref: sf.SoundFile) -> int:
    """Where the container's first sample lies in the elementary stream's decode.

    Both come from the same decoder, so a stretch with signal in it matches
    sample for sample at exactly one offset.
    """
    probe_start = _loudest_block(ref)
    if probe_start is None:
        return 0
    ref.seek(probe_start)
    probe = ref.read(_MATCH_SAMPLES, dtype="float32", always_2d=True)[:, 0]
    reach = _MAX_LEAD_FRAMES * FRAME
    pcm.seek(probe_start)
    span = pcm.read(reach + _MATCH_SAMPLES, dtype="float32", always_2d=True)[:, 0]
    if span.size < probe.size:
        return 0
    windows = np.lib.stride_tricks.sliding_window_view(span, probe.size)
    # the offsets whose first sample matches, checked in full
    for offset in np.flatnonzero(windows[:, 0] == probe[0]):
        if np.array_equal(windows[offset], probe):
            return int(offset)
    # Not bit-identical after all: fall back on the best normalised correlation,
    # so a loud stretch of the span does not win over the matching one.
    energy = np.sqrt(np.einsum("ij,ij->i", windows, windows)) + 1e-12
    offset = int(np.argmax((windows @ probe) / energy))
    logger.warning("atmos: no exact timeline match, correlating (offset %d)", offset)
    return offset


def _loudest_block(ref: sf.SoundFile) -> int | None:
    """Start of the loudest match-sized block in the first minute, None if all silent."""
    ref.seek(0)
    head = ref.read(min(ref.frames, 60 * ref.samplerate), dtype="float32", always_2d=True)[:, 0]
    usable = head.size - head.size % _MATCH_SAMPLES
    if usable == 0:
        return None
    energy = np.square(head[:usable].reshape(-1, _MATCH_SAMPLES)).sum(axis=1)
    best = int(np.argmax(energy))
    return best * _MATCH_SAMPLES if energy[best] > 0 else None
