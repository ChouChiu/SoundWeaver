"""A Dolby Atmos (E-AC-3 JOC) track, decoded and rendered to a speaker layout.

An AC-4 track (A-JOC) is decoded by `core.ac4` and joins at the renderer.

For E-AC-3, ffmpeg decodes the channel-based core; everything past that is TS 103 420:
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
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from itertools import pairwise
from multiprocessing.pool import AsyncResult
from pathlib import Path

import numpy as np
import soundfile as sf

from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.native import NativePcmStream
from soundweaver.core.ffmpeg import base_command, first_audio_stream, probe, run_ffmpeg
from soundweaver.core.i18n import get_language, t
from soundweaver.core.paths import cache_dir, content_key, work_root
from soundweaver.core.pcm import AudioData, create_pcm_audio
from soundweaver.core.process import NO_WINDOW
from soundweaver.core.speakers import DEFAULT_ATMOS, SpeakerLayout

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
from .trace import TRACE_SUFFIX, ObjectTrace, Tracer, load_cached_trace, store_cached_trace

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
    on_adm_chunk: Callable[[Path, int], None] | None = None,
    remove_bed_placeholders: bool = False,
) -> AtmosDecode:
    """Decode the first audio track of `source` once and produce every output asked for.

    `speakers` returns the render to `layout` (in its channel order), `adm` writes the objects there as ADM
    BWF.  The object trace always comes along: it is cached, so a sound field
    video of the same file needs no second decode.
    """
    from soundweaver.core import ac4  # ac4 builds on this package's modules

    if ac4.is_ac4(source):
        trace, out = ac4.decode_objects(
            source, token, on_fraction, layout, speakers, adm, on_adm_chunk,
            remove_bed_placeholders=remove_bed_placeholders,
        )
        _store_trace(source, layout, trace)
        return AtmosDecode(trace, out, layout)
    playback = on_adm_chunk is not None and adm is not None and not speakers
    with _prepare(source, token, playback) as (frames, core, rate, first, length, fill_core):
        out = create_pcm_audio(len(layout.channels), length, rate) if speakers else None
        try:
            trace = _Decode(
                frames, core, rate, first, length, layout, out, adm,
                remove_bed_placeholders=remove_bed_placeholders,
            ).run(token, on_fraction, on_adm_chunk, fill_core)
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
        *,
        remove_bed_placeholders: bool = False,
    ) -> None:
        self.frames, self.core, self.rate, self.layout = frames, core, rate, layout
        self.first, self.length = first, length
        self.speakers, self.adm_path = speakers, adm
        self.remove_bed_placeholders = remove_bed_placeholders
        total = len(frames)
        workers = min(_MAX_WORKERS, max(1, (os.process_cpu_count() or 2) - 1))
        count = max(1, min(workers * _CHUNKS_PER_WORKER, total // _MIN_CHUNK_FRAMES))
        bounds = [round(total * i / count) for i in range(count + 1)]
        self.chunks = list(pairwise(bounds))
        self.workers = min(workers, count)

    def run(
        self, token: CancellationToken, on_fraction: Callable[[float], None],
        on_adm_chunk: Callable[[Path, int], None] | None,
        fill_core: Callable[[int, int], int] | None,
    ) -> ObjectTrace:
        total = len(self.frames)
        if fill_core is not None:
            # Publish the first half second quickly, then keep a few stretches in flight.
            first_stop = min(total, 1 + (self.first + self.rate // 2) // FRAME)
            self.chunks = list(pairwise([0, *range(first_stop, total, 48), total]))
            self.workers = min(3, self.workers)
        tracer = Tracer(self.rate, self.first, self.length, FRAME, self.layout)
        oamd = OamdDecoder()
        renderer: Renderer | None = None
        adm: AdmFile | None = None
        oamd_payloads: list[bytes] = []
        joc_payloads: list[bytes] = []
        snapshots: list[tuple] = []
        pending: list[AsyncResult] = []
        playback_jobs: list[ChunkJob] = []
        results: list[ChunkResult | None] = [None] * len(self.chunks)
        delivered = 0
        readback: Path | None = None
        done = 0

        def progress(k: int) -> None:
            finished = sum(r.ready() for r in pending)
            on_fraction(_PASS_SHARE * k / total + (1 - _PASS_SHARE) * finished / len(self.chunks))

        def deliver(index: int) -> None:
            """Append chunk `index`'s PCM in order and hand the readable prefix to playback."""
            nonlocal delivered
            result = pending[index]
            while not result.ready():
                token.raise_if_cancelled()
                result.wait(0.1)
                progress(total)
            results[index] = result.get()
            if adm is not None:
                part = Path(scratch) / f"adm-{index}.pcm"
                adm.append(part)
                part.unlink()
            if readback is not None and on_adm_chunk is not None:
                adm.file.flush()
                if adm.frames_written:
                    on_adm_chunk(readback, adm.frames_written)
                adm.refresh()  # the timeline grows past the PCM that just shipped
            delivered += 1

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
                            logger.info(
                                "%d ISF objects on rings %s", meta.program.isf_objects,
                                meta.program.isf_rings,
                            )  # fmt: skip
                        if self.adm_path is not None:
                            adm = AdmFile(
                                self.adm_path,
                                self.rate,
                                self.length,
                                meta.program,
                                meta.object_count,
                                remove_bed_placeholders=self.remove_bed_placeholders,
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
                        job = self._job(
                            done, snapshots[done], oamd_payloads, joc_payloads, Path(scratch), adm
                        )
                        if fill_core is None:
                            pending.append(pool.apply_async(decode_chunk, (job,)))
                        else:
                            playback_jobs.append(job)
                            if done == 0:
                                last = fill_core(job.stop, total)
                                initial = replace(job, last=last, raw_rate=self.rate)
                                pending.append(pool.apply_async(decode_chunk, (initial,)))
                        done += 1
                    if readback is None and adm is not None and on_adm_chunk is not None and done:
                        # Publish the partial timeline now: chunk 0's PCM follows without
                        # waiting for the rest of the metadata scan.
                        readback = adm.prepare_readback(tracer.keys, tracer.size_keys)
                    if readback is not None:
                        # finished chunks ship while the scan keeps running
                        while delivered < len(pending) and pending[delivered].ready():
                            deliver(delivered)
                    if adm is not None:
                        # keep the published timeline ahead of the playhead as the scan runs
                        adm.refresh()
                    if k % 64 == 0:
                        progress(k)
                for index in range(delivered, len(self.chunks)):
                    if fill_core is not None:
                        while len(pending) < min(len(self.chunks), index + self.workers):
                            job = playback_jobs[len(pending)]
                            last = fill_core(job.stop, total)
                            job = replace(job, last=last, raw_rate=self.rate)
                            pending.append(pool.apply_async(decode_chunk, (job,)))
                    deliver(index)
            except BaseException:
                pool.terminate()
                if adm is not None:
                    adm.discard()
                raise
            if adm is not None:
                if readback is not None:
                    adm.refresh(force=True)  # the final timeline, regardless of the span gate
                    tracer.length = adm.frames_written
                    adm.file.close()
                else:
                    adm.close(tracer.keys, tracer.size_keys)
                clipped = sum(r.clipped for r in results if r is not None)
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


# bumped whenever a layout's shares or the decoded objects change, so older traces go unread
_TRACE_VERSION = 8


def _trace_path(source: Path, layout: SpeakerLayout) -> Path:
    # the shares are the layout's, so each layout keeps a trace of its own
    return cache_dir() / f"{content_key(source)}.{layout.name}.v{_TRACE_VERSION}{TRACE_SUFFIX}"


def _cached_trace(source: Path, layout: SpeakerLayout) -> ObjectTrace | None:
    return load_cached_trace(_trace_path(source, layout), layout.channels, component="atmos")


def _store_trace(source: Path, layout: SpeakerLayout, trace: ObjectTrace) -> None:
    store_cached_trace(trace, _trace_path(source, layout), suffix=TRACE_SUFFIX)


def _extract(source: Path, stream: Path, token: CancellationToken) -> list:
    run_ffmpeg(
        ["-i", str(source), "-map", "0:a:0", "-c", "copy", "-f", "eac3", str(stream)], token
    )
    try:
        all_frames = list(syncframes(stream.read_bytes()))
    except BitstreamError as error:
        raise AtmosError(t("atmos.bad_syncframes", error=error)) from error
    if not all_frames:
        raise AtmosError(t("atmos.no_frames"))
    frames = [f for f in all_frames if f.stream_type == INDEPENDENT and f.substream == 0]
    if not frames:
        raise AtmosError(t("atmos.no_substream"))
    if len(frames) != len(all_frames):
        logger.info(
            "atmos: filtered %d frames (dependent/secondary substreams) -> %d independent substream 0 frames",
            len(all_frames), len(frames),
        )
        stream.write_bytes(b"".join(f.data for f in frames))
    return frames


@contextmanager
def _prepare(
    source: Path, token: CancellationToken, playback: bool,
) -> Iterator[tuple[list, Path, int, int, int, Callable[[int, int], int] | None]]:
    """Syncframes, the core, its rate, and the container's span of its timeline."""
    with tempfile.TemporaryDirectory(dir=work_root()) as scratch:
        work = Path(scratch)
        stream, core, reference = work / "stream.ec3", work / "core.wav", work / "reference.wav"
        if playback:
            frames = _extract(source, stream, token)
            with _playback_core(source, stream, work / "core.f32", len(frames), token) as setup:
                yield (frames, *setup)
            return

        # The front left channel as the container presents it, to find its timeline by;
        # decoded alongside the core, each a full pass over the file.
        with ThreadPoolExecutor(1) as side:
            front = side.submit(
                run_ffmpeg,
                ["-i", str(source), "-map", "0:a:0", "-af", "channelmap=map=FL:channel_layout=mono",
                 "-c:a", "pcm_f32le", str(reference)],
                token,
            )  # fmt: skip
            frames = _extract(source, stream, token)

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
        yield frames, core, rate, start, length, None


@contextmanager
def _playback_core(
    source: Path, stream: Path, target: Path, total: int, token: CancellationToken,
) -> Iterator[tuple[Path, int, int, int, Callable[[int, int], int]]]:
    """Decode only the PCM prefixes needed by the upcoming JOC stretches."""
    core_format = first_audio_stream(probe(stream))
    rate = int(core_format.get("sample_rate") or 48000)
    channels = int(core_format.get("channels") or 0)
    if channels != CORE_CHANNELS:
        raise AtmosError(t("atmos.core_mismatch", channels=channels, frames=0))
    cmd = base_command()
    output = ["-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"]
    with ExitStack() as resources:
        pcm = resources.enter_context(closing(NativePcmStream(
            [*cmd, "-f", "eac3", "-i", str(stream), *output],
            pcm_format=(rate, CORE_CHANNELS), token=token,
        )))
        ref = resources.enter_context(closing(NativePcmStream(
            [*cmd, "-i", str(source), "-map", "0:a:0", "-af",
             "channelmap=map=FL:channel_layout=mono", *output],
            pcm_format=(rate, 1), token=token,
        )))
        cache = resources.enter_context(target.open("wb"))
        reach = _MAX_LEAD_FRAMES * FRAME
        head = ref.read_frames(rate)[:, 0]
        ref_frames = len(head)
        ref_eof = ref_frames < rate
        prefix = min(total * FRAME, rate + reach)
        lead = pcm.read_frames(prefix)
        decoded = len(lead)
        if decoded != prefix:
            raise AtmosError(t("atmos.core_mismatch", channels=CORE_CHANNELS, frames=decoded))
        cache.write(lead.tobytes())
        cache.flush()
        at = _loudest_match(head)
        if at is not None and decoded >= at + _MATCH_SAMPLES:
            start = _match_offset(lead[at:at + _MATCH_SAMPLES + reach, 0], head[at:at + _MATCH_SAMPLES])
        else:
            start = _packet_skip(source)
        length = total * FRAME - start

        def fill(stop: int, total_frames: int) -> int:
            nonlocal decoded, ref_frames, ref_eof
            until = min(stop + 1, total_frames) * FRAME
            if decoded < until:
                block = pcm.read_frames(until - decoded)
                decoded += len(block)
                if decoded != until:
                    raise AtmosError(t("atmos.core_mismatch", channels=CORE_CHANNELS, frames=decoded))
                cache.write(block.tobytes())
                cache.flush()
            count = stop * FRAME - start - ref_frames
            if count > 0 and not ref_eof:
                n = len(ref.read_frames(count))
                ref_frames += n
                ref_eof = n < count
            return start + ref_frames

        yield target, rate, start, length, fill


def _packet_skip(source: Path) -> int:
    """Use the container's skip side data when the entire matching window is silent."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-read_intervals", "%+#1",
         "-show_entries", "packet=:packet_side_data=skip_samples",
         "-of", "default=nw=1", str(source)],
        check=False, capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
    )
    for line in result.stdout.splitlines():
        name, _, value = line.partition("=")
        if name == "skip_samples" and value.isdecimal():
            return int(value)
    return 0


def _container_offset(pcm: sf.SoundFile, ref: sf.SoundFile) -> int:
    """Where the container's first sample lies in the elementary stream's decode.

    Both come from the same decoder, so a stretch with signal in it matches
    sample for sample at exactly one offset.
    """
    ref.seek(0)
    head = ref.read(min(ref.frames, 60 * ref.samplerate), dtype="float32", always_2d=True)[:, 0]
    probe_start = _loudest_match(head)
    if probe_start is None:
        return 0
    probe = head[probe_start : probe_start + _MATCH_SAMPLES]
    reach = _MAX_LEAD_FRAMES * FRAME
    pcm.seek(probe_start)
    span = pcm.read(reach + _MATCH_SAMPLES, dtype="float32", always_2d=True)[:, 0]
    return _match_offset(span, probe)


def _match_offset(span: np.ndarray, probe: np.ndarray) -> int:
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


def _loudest_match(head: np.ndarray) -> int | None:
    """Start of the loudest match-sized block in the first minute, None if all silent."""
    usable = head.size - head.size % _MATCH_SAMPLES
    if usable == 0:
        return None
    energy = np.square(head[:usable].reshape(-1, _MATCH_SAMPLES)).sum(axis=1)
    best = int(np.argmax(energy))
    return best * _MATCH_SAMPLES if energy[best] > 0 else None
