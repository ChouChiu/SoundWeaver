"""AC-4 Dolby Atmos (A-JOC) as `atmos.decode_atmos` hands it out: rendered, traced, or as ADM.

The objects come out of `frame.FrameDecoder` frame by frame with their
metadata; from there on they take the same road as the objects JOC rebuilds
from E-AC-3: the room renderer, the sound field trace and the ADM writer.
The stretches of `parallel` are each decoded and rendered by a worker of their
own.  This process goes through the parse pass's metadata in order, for the
trace and for the renderer's glides, whose state at each stretch's first frame
it hands over (as `atmos.decoder` does), then resamples what the workers wrote.
"""

from __future__ import annotations

import logging
import tempfile
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import numpy as np

from soundweaver.core.atmos.adm import AdmFile, _tracks, adm_block, pcm24
from soundweaver.core.atmos.bitstream import BitstreamError
from soundweaver.core.atmos.oamd import Frame, Program
from soundweaver.core.atmos.obj_restore import (
    HopFeatureExtractor,
    RestoreConfig,
    SlotFeatures,
    build_fingerprints,
    build_restored_trace,
    eval_keys_on_grid,
    plan_object_restoration,
    write_restored_adm,
)
from soundweaver.core.atmos.qmf import BANDS, DELAY
from soundweaver.core.atmos.render import Renderer, RendererState, joc_objects
from soundweaver.core.atmos.sinks import RenderedFrame, speaker_feeds
from soundweaver.core.atmos.trace import Measure, ObjectTrace, Tracer, measure
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.i18n import t
from soundweaver.core.paths import work_root
from soundweaver.core.pcm import AudioData, create_pcm_audio
from soundweaver.core.speakers import SpeakerLayout

from . import aspx, parallel
from .decoder import Ac4Error, Resampler, _rates, stream_bytes
from .frame import ALIGNMENT, FrameDecoder
from .toc import read_toc, sync_frames

logger = logging.getLogger(__name__)


def is_ajoc(frame: bytes) -> bool:
    """Whether the first presentation of `frame` is A-JOC coded objects."""
    toc = read_toc(frame)
    group = toc.groups[toc.presentations[0].groups[0]]
    return not group.channel_coded and group.substreams[0].kind == "ajoc"


def read_frames(source: Path, token: CancellationToken) -> list[bytes]:
    """The raw AC-4 frames of the first audio track of `source`."""
    frames = list(sync_frames(stream_bytes(source, token)))
    if not frames:
        raise Ac4Error(t("ac4.no_frames"))
    return frames


class _Objects:
    """Feeds each frame's objects, delayed by the QMF tools, to the renderer with its metadata.

    `skip` more samples go before the first frame rendered: a stretch's decoder
    starts that far before it.
    """

    def __init__(
        self,
        frame_len: int,
        layout: SpeakerLayout,
        sink: Callable[[RenderedFrame], None],
        renderer: Renderer | None,
        skip: int,
    ):
        self.frame_len, self.layout, self.sink = frame_len, layout, sink
        self.renderer = renderer
        self.objects = np.zeros((0, 0))
        self.lfe = np.zeros(0)
        self.queue: deque[tuple[int, Frame]] = deque()
        self.trimmed, self.skip = 0, skip

    def push(self, k: int, meta: Frame | None, pcm: dict[str, np.ndarray], delay: int) -> None:
        """Frame k's samples, and its metadata unless it is not to be rendered."""
        names = sorted((n for n in pcm if n.startswith("o")), key=lambda n: int(n[1:]))
        block = np.stack([pcm[n] for n in names]) / 32768.0
        lfe = pcm["LFE"] / 32768.0 if "LFE" in pcm else np.zeros(block.shape[1])
        if self.objects.shape[0] != block.shape[0]:
            if self.objects.size:
                raise Ac4Error(t("atmos.object_count_changed", frame=k))
            self.objects = np.zeros((block.shape[0], 0))
        if self.trimmed < delay + self.skip:
            cut = min(delay + self.skip - self.trimmed, block.shape[1])
            block, lfe, self.trimmed = block[:, cut:], lfe[cut:], self.trimmed + cut
        self.objects = np.concatenate([self.objects, block], axis=1)
        self.lfe = np.concatenate([self.lfe, lfe])
        if meta is not None:
            self.queue.append((k, meta))
        self._render()

    def flush(self) -> None:
        """The last frames' samples still held back by the delay, followed by silence."""
        missing = self.frame_len * len(self.queue) - self.objects.shape[1]
        if missing > 0:
            self.objects = np.pad(self.objects, ((0, 0), (0, missing)))
            self.lfe = np.pad(self.lfe, (0, missing))
        self._render()

    def _render(self) -> None:
        n = self.frame_len
        while self.queue and self.objects.shape[1] >= n:
            k, meta = self.queue.popleft()
            if self.renderer is None:
                self.renderer = Renderer(len(joc_objects(meta)), self.layout)
            if len(joc_objects(meta)) != self.objects.shape[0]:
                raise Ac4Error(t("atmos.object_mismatch"))
            gains = self.renderer.gains(meta, n)
            frame = RenderedFrame(
                k, meta, gains[..., :-1], gains[..., -1], self.objects[:, :n], self.lfe[:n],
                slice(0, n),
            )  # fmt: skip
            self.objects, self.lfe = self.objects[:, n:], self.lfe[n:]
            self.sink(frame)


@dataclass(frozen=True, slots=True)
class _Job:
    frames: list[bytes]  # from the stretch's first decoded frame through `tail` past its end
    warm: int  # the frame frames[0] is
    start: int  # first frame rendered
    stop: int  # one past the last
    total: int  # frames in the track
    resume: dict[str, aspx.Cursor] | None
    gains: RendererState | None  # None at the start of the track
    layout: SpeakerLayout
    # planar float32 at the coded rate, every frame's span at k * frame_len, or None
    speakers: Path | None
    adm_tracks: Path | None
    # this stretch's ADM as `pcm24`, when the coded rate is the delivered one
    adm_part: Path | None
    adm_sources: tuple[int | None, ...]
    extract_features: bool = False
    feature_hop: int = 512
    coded_rate: float = 48000.0


def _choose_hop(frame_len: int, target_hop: int) -> int:
    for sub in range(max(1, round(frame_len / target_hop)), 0, -1):
        if frame_len % sub == 0 and frame_len // sub <= 1024:
            return frame_len // sub
    return frame_len


def _tail(frame_len: int) -> int:
    """Frames past a stretch that hold the rest of its last frame's objects: the
    QMF tools delay them by at most d_pcm, the banks' and A-SPX's delay."""
    delay = ALIGNMENT.get(frame_len, (0, 0))[0] + DELAY + aspx.HFGEN_ATS * 2 * BANDS
    return -(-delay // frame_len)


def _render_stretch(job: _Job) -> tuple[list[Measure], int, dict[str, np.ndarray] | None]:
    """Decode and render frames [start, stop); their measures, ADM clipped, and optional features."""
    n = read_toc(job.frames[0]).frame_length
    hop = _choose_hop(n, job.feature_hop)
    channels = len(job.layout.channels)
    lfe_index = job.layout.index.get("LFE")
    speakers = (
        np.memmap(job.speakers, dtype=np.float32, mode="r+").reshape(channels, -1)
        if job.speakers is not None
        else None
    )
    tracks = (
        np.memmap(job.adm_tracks, dtype=np.float32, mode="r+").reshape(len(job.adm_sources), -1)
        if job.adm_tracks is not None
        else None
    )
    part = job.adm_part.open("wb") if job.adm_part is not None else None
    measured: list[Measure] = []
    clipped = 0
    extractor: HopFeatureExtractor | None = None
    feature_chunks: list[dict[str, np.ndarray]] = []

    def sink(f: RenderedFrame) -> None:
        nonlocal clipped, extractor
        measured.append(measure(f))
        if job.extract_features:
            anchored = f.metadata.program.anchored
            dyn_rows = [o for o, idx in enumerate(joc_objects(f.metadata)) if idx >= anchored]
            if dyn_rows:
                if extractor is None:
                    extractor = HopFeatureExtractor(
                        len(dyn_rows),
                        job.coded_rate,
                        hop_samples=hop,
                        n_fft=max(1024, hop),
                    )
                pick = (
                    slice(dyn_rows[0], dyn_rows[-1] + 1)
                    if len(dyn_rows) == dyn_rows[-1] - dyn_rows[0] + 1
                    else dyn_rows
                )
                dyn_audio = f.essence[pick] * f.envelope[pick]
                feature_chunks.append(extractor.process(dyn_audio))
        at = f.index * n
        if speakers is not None:
            speakers[:, at : at + n] = speaker_feeds(f, lfe_index)
        if tracks is not None:
            tracks[:, at : at + n] = adm_block(f, job.adm_sources)
        if part is not None:
            data, count = pcm24(adm_block(f, job.adm_sources))
            part.write(data)
            clipped += count

    renderer = Renderer.restore(job.gains, job.layout) if job.gains is not None else None
    feeder = _Objects(n, job.layout, sink, renderer, (job.start - job.warm) * n)
    decoder = FrameDecoder()
    decoder.resume = job.resume
    try:
        for k, frame in enumerate(job.frames, job.warm):
            if k >= job.stop and not feeder.queue:
                break
            try:
                pcm = decoder.decode(frame)
            except BitstreamError as error:
                raise parallel.FrameError(k, error) from error
            meta = None
            if job.start <= k < job.stop:
                payload = decoder.payload
                if payload is None:
                    raise Ac4Error(t("ac4.layout_changed", frame=k))
                meta = payload.metadata
            feeder.push(k, meta, pcm, decoder.delay)
        if job.stop == job.total:
            feeder.flush()
        if feeder.queue:
            raise Ac4Error(t("atmos.misaligned"))
    finally:
        if part is not None:
            part.close()
    for mapped in (speakers, tracks):
        if mapped is not None:
            mapped.flush()
    features = (
        {
            key: np.concatenate([c[key] for c in feature_chunks], axis=1)
            for key in feature_chunks[0]
        }
        if feature_chunks
        else None
    )
    return measured, clipped, features


def decode_objects(
    source: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    layout: SpeakerLayout,
    speakers: bool,
    adm: Path | None,
    on_adm_chunk: Callable[[Path, int], None] | None = None,
    *,
    restore_objects: bool = True,
    remove_bed_placeholders: bool = False,
) -> tuple[ObjectTrace, AudioData | None]:
    """The trace, and the render to `layout` when `speakers`, of `source`'s A-JOC track; ADM to `adm`."""
    frames = read_frames(source, token)
    if not is_ajoc(frames[0]):
        raise Ac4Error(t("ac4.not_objects"))
    return render_objects(
        frames, token, on_fraction, layout, speakers, adm, on_adm_chunk,
        restore_objects=restore_objects, remove_bed_placeholders=remove_bed_placeholders,
    )


def render_objects(
    frames: list[bytes],
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    layout: SpeakerLayout,
    speakers: bool,
    adm: Path | None,
    on_adm_chunk: Callable[[Path, int], None] | None = None,
    *,
    restore_objects: bool = True,
    remove_bed_placeholders: bool = False,
) -> tuple[ObjectTrace, AudioData | None]:
    out = None
    try:
        with tempfile.TemporaryDirectory(dir=work_root()) as scratch:
            render = _Render(
                frames, layout, Path(scratch), adm, restore_objects=restore_objects,
                remove_bed_placeholders=remove_bed_placeholders,
            )
            if speakers:
                out = render.out = create_pcm_audio(
                    len(layout.channels), render.out_len, render.rate
                )
            trace = render.run(token, on_fraction, on_adm_chunk)
    except parallel.FrameError as error:
        if out is not None:
            out.cleanup()
        raise Ac4Error(t("ac4.bad_frame", frame=error.index, error=error.error)) from error
    except BaseException:
        if out is not None:
            out.cleanup()
        raise
    on_fraction(1.0)
    logger.info("ac4: rendered %d A-JOC frames to %s", len(frames), layout.name)
    return trace, out


class _Render:
    """The run of `render_objects`: metadata in order here, stretches in the workers."""

    def __init__(
        self,
        frames: list[bytes],
        layout: SpeakerLayout,
        scratch: Path,
        adm: Path | None,
        *,
        restore_objects: bool = True,
        remove_bed_placeholders: bool = False,
    ) -> None:
        self.frames, self.layout, self.scratch, self.adm_path = frames, layout, scratch, adm
        self.restore_objects = restore_objects
        self.remove_bed_placeholders = remove_bed_placeholders
        toc = read_toc(frames[0])
        coded, self.rate = _rates(toc)
        self.coded_rate = float(coded)
        self.ratio = Fraction(self.rate) / coded
        self.n = toc.frame_length
        self.hop = _choose_hop(self.n, RestoreConfig().hop_samples)
        self.length = self.n * len(frames)
        self.out_len = -(-self.length * self.ratio.numerator // self.ratio.denominator)
        self.tracer = Tracer(
            int(coded) if coded.denominator == 1 else float(coded), 0, self.length, self.n, layout
        )
        self.out: AudioData | None = None
        self.renderer: Renderer | None = None
        self.adm: AdmFile | None = None
        self.first_program: Program | None = None
        self.first_objects = 0
        self.raw_adm_sources: tuple[int | None, ...] = ()
        self.do_restore = False
        self.taken = 0  # frames whose metadata the tracer and the renderer went through
        # what the workers write at the coded rate, when it is resampled here
        self.speakers: np.memmap | None = None
        self.tracks: np.memmap | None = None
        self.written = 0

    @property
    def resampled(self) -> bool:
        return self.ratio != 1

    def run(
        self, token: CancellationToken, on_fraction: Callable[[float], None],
        on_adm_chunk: Callable[[Path, int], None] | None,
    ) -> ObjectTrace:
        self.do_restore = self.restore_objects and on_adm_chunk is None
        stretches = parallel.plan(self.frames)
        if self.out is not None and self.resampled:
            self.speakers = self._coded("speakers.f32", len(self.layout.channels))
        feeds = Resampler(self.ratio, len(self.layout.channels))
        adm_tracks: Resampler | None = None
        raw_out_mm: np.memmap | None = None
        raw_out_written = 0
        measured: list[Measure] = []
        feature_parts: list[dict[str, np.ndarray] | None] = []
        clipped = 0
        readback: Path | None = None

        def publish() -> None:
            if readback is not None and self.adm is not None and on_adm_chunk is not None:
                self.adm.file.flush()
                if self.adm.frames_written:
                    on_adm_chunk(readback, self.adm.frames_written)

        try:
            with parallel.worker_pool(len(stretches)) as pool:
                parse = parallel.Parse(pool, self.frames, stretches, token, here=1)
                pending: deque = deque()
                queued = 1

                def submit(block: bool) -> None:
                    nonlocal queued
                    while queued < len(stretches):
                        s = stretches[queued]
                        if not parse.take(max(s.start, 1), block):
                            return
                        self._take(parse, max(s.start, 1))
                        pending.append(
                            (s, pool.apply_async(_render_stretch, (self._job(s, parse),)))
                        )
                        queued += 1

                # the first stretch is rendered here while the workers start, the second
                # handed out before it
                parse.take(1, block=True)
                self._take(parse, 1)
                first = self._job(stretches[0], parse)
                submit(block=False)
                pending.appendleft((stretches[0], parallel.done(_render_stretch(first))))
                if self.adm is not None and on_adm_chunk is not None:
                    # Finish the metadata map while the PCM workers render upcoming stretches.
                    submit(block=True)
                    if queued < len(stretches) or not parse.take(len(self.frames), True):
                        raise parse.error()
                    self._take(parse, len(self.frames))
                    readback = self.adm.prepare_readback(self.tracer.keys)
                while pending or (queued < len(stretches) and not parse.broken):
                    submit(block=not pending)
                    if not pending:
                        break
                    s, result = pending.popleft()
                    while not result.ready():
                        token.raise_if_cancelled()
                        submit(block=False)
                        result.wait(0.05)
                    part_measured, part_clipped, part_features = result.get()
                    measured.extend(part_measured)
                    clipped += part_clipped
                    feature_parts.append(part_features)
                    if self.speakers is not None:
                        self._put(feeds.push(self.speakers[:, s.start * self.n : s.stop * self.n]))
                    if self.adm is not None:
                        if self.tracks is not None:
                            if adm_tracks is None:
                                adm_tracks = Resampler(self.ratio, self.adm.channels)
                            block = self.tracks[:, s.start * self.n : s.stop * self.n]
                            clipped += self._put_adm(adm_tracks.push(block), readback is not None)
                        else:
                            self.adm.append(self._part(s))
                            self._part(s).unlink()
                    elif self.tracks is not None and self.resampled:
                        if adm_tracks is None:
                            adm_tracks = Resampler(self.ratio, len(self.raw_adm_sources))
                            raw_out_mm = np.memmap(
                                self.scratch / "adm_out.f32",
                                mode="w+",
                                dtype=np.float32,
                                shape=(len(self.raw_adm_sources), self.out_len),
                            )
                        block = self.tracks[:, s.start * self.n : s.stop * self.n]
                        res_blk = adm_tracks.push(block)
                        if res_blk.shape[1] > 0 and raw_out_mm is not None:
                            take_n = min(res_blk.shape[1], self.out_len - raw_out_written)
                            if take_n > 0:
                                raw_out_mm[:, raw_out_written : raw_out_written + take_n] = res_blk[:, :take_n]
                                raw_out_written += take_n
                    publish()
                    on_fraction(s.stop / len(self.frames))
                if queued < len(stretches) or not parse.take(len(self.frames), True):
                    raise parse.error()
                self._take(parse, len(self.frames))
            if self.speakers is not None:
                self._put(feeds.finish())
            if adm_tracks is not None:
                tail_blk = adm_tracks.finish()
                if self.adm is not None:
                    clipped += self._put_adm(tail_blk, readback is not None)
                    publish()
                elif raw_out_mm is not None and tail_blk.shape[1] > 0:
                    take_n = min(tail_blk.shape[1], self.out_len - raw_out_written)
                    if take_n > 0:
                        raw_out_mm[:, raw_out_written : raw_out_written + take_n] = tail_blk[:, :take_n]
                        raw_out_written += take_n
            if self.adm is not None:
                if clipped:
                    logger.warning("adm: %d samples clipped at full scale", clipped)
                if readback is not None:
                    self.adm.file.close()
                else:
                    if self.tracks is not None:
                        self.adm.append(self.scratch / "adm.pcm")
                    self.adm.close(self.tracer.keys)
            if (
                self.do_restore
                and self.first_program is not None
                and self.first_program.dynamic_objects > 0
                and feature_parts
                and all(p is not None for p in feature_parts)
            ):
                restore_cfg = replace(RestoreConfig(), hop_samples=self.hop)
                bark = np.concatenate([p["bark_power"] for p in feature_parts], axis=1)  # type: ignore[index]
                rms = np.concatenate([p["rms"] for p in feature_parts], axis=1)  # type: ignore[index]
                peak = np.concatenate([p["peak"] for p in feature_parts], axis=1)  # type: ignore[index]
                flat = np.concatenate([p["flatness_3"] for p in feature_parts], axis=1)  # type: ignore[index]
                harm = np.concatenate([p["harmonicity"] for p in feature_parts], axis=1)  # type: ignore[index]
                fp = build_fingerprints(bark, flat, harm)
                n_dyn_raw, t_len = rms.shape
                times = (np.arange(t_len, dtype=np.float64) * self.hop + 0.5 * self.hop) / self.coded_rate
                dyn_indices = sorted(self.tracer.keys)
                pos = np.empty((n_dyn_raw, t_len, 3), dtype=np.float32)
                active = np.empty((n_dyn_raw, t_len), dtype=bool)
                for slot_i, meta_idx in enumerate(dyn_indices):
                    pos[slot_i], active[slot_i] = eval_keys_on_grid(self.tracer.keys[meta_idx], times)
                feats = SlotFeatures(
                    sample_rate=self.coded_rate,
                    hop_samples=self.hop,
                    hop_start_sample=0,
                    bark_power=bark,
                    fingerprint=fp,
                    rms=rms,
                    peak=peak,
                    pos=pos,
                    active=active,
                )
                plan = plan_object_restoration(feats, restore_cfg)
                logger.info(
                    "ac4: restored %d -> %d dynamic objects (%d splits)",
                    plan.input_objects,
                    len(plan.objects),
                    plan.splits_applied,
                )
                exact_levels = None
                if self.adm_path is not None and self.tracks is not None:
                    if self.resampled and raw_out_mm is not None:
                        raw_out_mm.flush()
                        raw_mm = raw_out_mm
                    else:
                        self.tracks.flush()
                        raw_mm = self.tracks
                    n_static = len(self.raw_adm_sources) - n_dyn_raw
                    raw_bed_indices = [i for i in range(n_static) if self.raw_adm_sources[i] is not None]
                    raw_dyn_indices = list(range(n_static, len(self.raw_adm_sources)))
                    exact_levels, _ = write_restored_adm(
                        raw_mm,
                        raw_bed_indices,
                        (),
                        raw_dyn_indices,
                        plan,
                        self.first_program,
                        self.adm_path,
                        self.rate,
                        self.out_len,
                        restore_cfg,
                        hop_samples_out=self.hop * float(self.ratio),
                        hop_start_out=0.0,
                        syncframe_samples=self.n if not self.resampled else 0,
                        first_sample=0,
                        n_syncframes=len(self.frames) if not self.resampled else 0,
                        token=token,
                        remove_bed_placeholders=self.remove_bed_placeholders,
                    )
                    raw_mm = None
                raw_out_mm = None
                self.tracks = None
                return build_restored_trace(
                    plan,
                    self.first_program,
                    self.layout,
                    len(self.frames),
                    self.n,
                    0,
                    self.length,
                    exact_levels=exact_levels,
                )
        except BaseException:
            if self.adm is not None:
                self.adm.discard()
            raise
        return self.tracer.result(measured)

    def _take(self, parse: parallel.Parse, until: int) -> None:
        """The metadata of the frames before `until` into the tracer and the renderer."""
        for k in range(self.taken, until):
            meta = parse.delayed(k)
            if meta is None:
                raise Ac4Error(t("ac4.layout_changed", frame=k))
            if self.renderer is None:
                self.renderer = Renderer(len(joc_objects(meta)), self.layout)
                self.first_program = meta.program
                self.first_objects = meta.object_count
                if self.do_restore and meta.program.dynamic_objects > 0:
                    _, bed_tr, other_tr = _tracks(meta.program, meta.object_count)
                    self.raw_adm_sources = tuple(tr.source for tr in (bed_tr + other_tr))
                    if self.adm_path is not None:
                        self.tracks = self._coded("adm.f32", len(self.raw_adm_sources))
                        self.tracks.flush()
                elif self.adm_path is not None:
                    self.adm = AdmFile(
                        self.adm_path, self.rate, self.out_len, meta.program, meta.object_count,
                        remove_bed_placeholders=self.remove_bed_placeholders,
                    )
                    self.raw_adm_sources = self.adm.sources
                    if self.resampled:
                        self.tracks = self._coded("adm.f32", self.adm.channels)
                        self.tracks.flush()
            elif len(joc_objects(meta)) != self.renderer.current.shape[0]:
                raise Ac4Error(t("atmos.object_count_changed", frame=k))
            if self.adm_path is not None and self.first_program is not None and (
                meta.object_count != self.first_objects
                or meta.program.bed_labels != self.first_program.bed_labels
            ):
                raise Ac4Error(t("atmos.adm_layout_changed"))
            self.tracer.metadata(k, meta)
            self.renderer.advance(meta, self.n)
        self.taken = max(self.taken, until)

    def _job(self, s: parallel.Stretch, parse: parallel.Parse) -> _Job:
        # the renderer went through frame 0 already when the first stretch is handed out
        gains = self.renderer.state() if s.start else None
        direct = self.out is not None and not self.resampled
        extract = bool(
            self.do_restore and self.first_program is not None and self.first_program.dynamic_objects > 0
        )
        return _Job(
            frames=self.frames[s.warm : min(len(self.frames), s.stop + _tail(self.n))],
            warm=s.warm,
            start=s.start,
            stop=s.stop,
            total=len(self.frames),
            resume=parse.resume(s),
            gains=gains,
            layout=self.layout,
            speakers=self.out.backing_path if direct else self._path(self.speakers),
            adm_tracks=self._path(self.tracks),
            adm_part=self._part(s) if self.adm is not None and self.tracks is None else None,
            adm_sources=self.adm.sources if self.adm is not None else self.raw_adm_sources,
            extract_features=extract,
            feature_hop=self.hop,
            coded_rate=self.coded_rate,
        )

    def _coded(self, name: str, channels: int) -> np.memmap:
        return np.memmap(
            self.scratch / name, mode="w+", dtype=np.float32, shape=(channels, self.length)
        )

    def _path(self, mapped: np.memmap | None) -> Path | None:
        return Path(mapped.filename) if mapped is not None else None

    def _part(self, s: parallel.Stretch) -> Path:
        return self.scratch / f"adm-{s.start}.pcm"

    def _put(self, block: np.ndarray) -> None:
        block = block[:, : self.out_len - self.written]
        self.out.samples[:, self.written : self.written + block.shape[1]] = block
        self.written += block.shape[1]

    def _put_adm(self, block: np.ndarray, readback: bool) -> int:
        data, clipped = pcm24(block)
        if readback:
            self.adm.write_pcm(data)
        else:
            with (self.scratch / "adm.pcm").open("ab") as part:
                part.write(data)
        return clipped
