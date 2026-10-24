"""AC-4 Dolby Atmos (A-JOC) as `atmos.decode_atmos` hands it out: rendered, traced, or as ADM.

The objects come out of `frame.FrameDecoder` frame by frame with their
metadata; from there on they take the same road as the objects JOC rebuilds
from E-AC-3: the room renderer, the sound field trace and the ADM writer.
It all runs in one pass in this process: AC-4 frames are decoded here, not
by ffmpeg, so there is no core to share out among workers.
"""

from __future__ import annotations

import logging
import tempfile
from collections import deque
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

import numpy as np

from dolbytool.core.atmos.adm import AdmFile, adm_block, pcm24
from dolbytool.core.atmos.bitstream import BitstreamError
from dolbytool.core.atmos.oamd import Frame
from dolbytool.core.atmos.render import Renderer, joc_objects
from dolbytool.core.atmos.sinks import RenderedFrame, speaker_feeds
from dolbytool.core.atmos.trace import Measure, ObjectTrace, Tracer, measure
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.i18n import t
from dolbytool.core.paths import work_root
from dolbytool.core.pcm import AudioData, create_pcm_audio
from dolbytool.core.speakers import SpeakerLayout

from .decoder import Ac4Error, Resampler, _rates, stream_bytes
from .frame import FrameDecoder
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
    """Feeds each frame's objects, delayed by the QMF tools, to the renderer with its metadata."""

    def __init__(
        self, frame_len: int, layout: SpeakerLayout, sink: Callable[[RenderedFrame], None]
    ):
        self.frame_len, self.layout, self.sink = frame_len, layout, sink
        self.renderer: Renderer | None = None
        self.objects = np.zeros((0, 0))
        self.lfe = np.zeros(0)
        self.queue: deque[tuple[int, Frame]] = deque()
        self.trimmed = 0

    def push(self, k: int, meta: Frame, pcm: dict[str, np.ndarray], delay: int) -> None:
        names = sorted((n for n in pcm if n.startswith("o")), key=lambda n: int(n[1:]))
        block = np.stack([pcm[n] for n in names]) / 32768.0
        lfe = pcm["LFE"] / 32768.0 if "LFE" in pcm else np.zeros(block.shape[1])
        if self.objects.shape[0] != block.shape[0]:
            if self.objects.size:
                raise Ac4Error(t("atmos.object_count_changed", frame=k))
            self.objects = np.zeros((block.shape[0], 0))
        if self.trimmed < delay:
            cut = min(delay - self.trimmed, block.shape[1])
            block, lfe, self.trimmed = block[:, cut:], lfe[cut:], self.trimmed + cut
        self.objects = np.concatenate([self.objects, block], axis=1)
        self.lfe = np.concatenate([self.lfe, lfe])
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


def decode_objects(
    source: Path,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    layout: SpeakerLayout,
    speakers: bool,
    adm: Path | None,
) -> tuple[ObjectTrace, AudioData | None]:
    """The trace, and the render to `layout` when `speakers`, of `source`'s A-JOC track; ADM to `adm`."""
    frames = read_frames(source, token)
    if not is_ajoc(frames[0]):
        raise Ac4Error(t("ac4.not_objects"))
    return render_objects(frames, token, on_fraction, layout, speakers, adm)


def render_objects(
    frames: list[bytes],
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    layout: SpeakerLayout,
    speakers: bool,
    adm: Path | None,
) -> tuple[ObjectTrace, AudioData | None]:
    toc = read_toc(frames[0])
    coded, rate = _rates(toc)
    ratio = Fraction(rate) / coded
    n = toc.frame_length
    length = n * len(frames)
    out_len = -(-length * ratio.numerator // ratio.denominator)
    tracer = Tracer(int(coded) if coded.denominator == 1 else float(coded), 0, length, n, layout)
    measured: list[Measure] = []
    out = create_pcm_audio(len(layout.channels), out_len, rate) if speakers else None
    feeds = Resampler(ratio, len(layout.channels))
    written = 0
    lfe_index = layout.index.get("LFE")
    adm_file: AdmFile | None = None
    adm_tracks: Resampler | None = None
    scratch = tempfile.TemporaryDirectory(dir=work_root())
    adm_part = Path(scratch.name) / "adm.pcm"

    def put(block: np.ndarray) -> None:
        nonlocal written
        block = block[:, : out_len - written]
        out.samples[:, written : written + block.shape[1]] = block
        written += block.shape[1]

    def put_adm(block: np.ndarray) -> None:
        data, clipped = pcm24(block)
        with adm_part.open("ab") as part:
            part.write(data)
        if clipped:
            logger.warning("adm: %d samples clipped at full scale", clipped)

    def sink(f: RenderedFrame) -> None:
        nonlocal adm_file, adm_tracks
        measured.append(measure(f))
        if out is not None:
            put(feeds.push(speaker_feeds(f, lfe_index)))
        if adm is not None:
            if adm_file is None:
                program, count = f.metadata.program, f.metadata.object_count
                adm_file = AdmFile(adm, rate, out_len, program, count)
                adm_tracks = Resampler(ratio, adm_file.channels)
            put_adm(adm_tracks.push(adm_block(f, adm_file.sources)))

    feeder = _Objects(n, layout, sink)
    decoder = FrameDecoder()
    try:
        for k, frame in enumerate(frames):
            if k % 32 == 0:
                token.raise_if_cancelled()
                on_fraction(k / len(frames))
            try:
                pcm = decoder.decode(frame)
            except BitstreamError as error:
                raise Ac4Error(t("ac4.bad_frame", frame=k, error=error)) from error
            if decoder.payload is None:
                raise Ac4Error(t("ac4.layout_changed", frame=k))
            meta = decoder.payload.metadata
            tracer.metadata(k, meta)
            feeder.push(k, meta, pcm, decoder.delay)
        feeder.flush()
        if out is not None:
            put(feeds.finish())
        if adm_file is not None:
            put_adm(adm_tracks.finish())
            adm_file.append(adm_part)
            adm_file.close(tracer.keys)
    except BaseException:
        if out is not None:
            out.cleanup()
        if adm_file is not None:
            adm_file.discard()
        raise
    finally:
        scratch.cleanup()
    on_fraction(1.0)
    logger.info("ac4: rendered %d A-JOC frames to %s", len(frames), layout.name)
    return tracer.result(measured), out
