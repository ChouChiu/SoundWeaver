"""Decode one stretch of syncframes, in a worker process of its own.

Every piece of decoder state at a frame boundary can be rebuilt exactly: the
analysis bank's history is the core audio just before it, the JOC matrix
history is the previous frame's parameters, the synthesis bank's history is
whatever one frame of output leaves behind, and the OAMD and gain states are
snapshots the metadata pass hands over.  So a stretch decoded on its own is
sample for sample what one long run produces there.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from dolbytool.core.speakers import SpeakerLayout

from . import joc
from .adm import adm_block, pcm24
from .bitstream import BitstreamError
from .oamd import Frame, OamdDecoder, ObjectState
from .qmf import BANDS, DELAY, LENGTH, Analysis, Synthesis
from .render import Renderer, RendererState, joc_objects
from .sinks import RenderedFrame, container_span, speaker_feeds
from .trace import Measure, measure

FRAME = 1536
# ffmpeg's 5.1(side) order is FL FR FC LFE SL SR; JOC takes L R C Ls Rs.
CORE_CHANNELS = 6
_JOC_INPUTS = [0, 1, 2, 4, 5]
_SURROUNDS = slice(3, 5)
_LFE = 3
# joc_dmx_config_idx values this decoder handles (table 47): a 5.X downmix,
# plain or with the surrounds shifted 90 degrees before encoding.
FIVE_CHANNEL_CONFIGS = (0, 3)
_PHASE_SHIFTED = (3, 4)
# The encoder delays Ls and Rs by 90 degrees; a panned source shows up in L
# and Ls a quarter cycle apart, and multiplying the surround subbands by -j
# brings it back in phase before the real-valued JOC matrix combines them.
_UNSHIFT = -1j


class AtmosError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ChunkJob:
    core: Path  # the decoded core, float WAV in ffmpeg's 5.1 order
    start: int  # first syncframe to render
    stop: int  # one past the last
    total: int  # syncframes in the stream
    joc_from: int  # syncframe of joc[0]
    joc: tuple[bytes, ...]  # JOC payloads from joc_from through stop (or the last frame)
    oamd: tuple[bytes, ...]  # OAMD payloads of [start, stop)
    oamd_state: list[ObjectState]
    gains: RendererState | None  # None at the start of the stream
    first: int  # the container's span of the stream's timeline
    last: int
    layout: SpeakerLayout  # what the speaker feeds and the trace's shares are in
    speakers: Path | None  # planar float32 (speakers, container samples) buffer to fill
    adm: Path | None  # where this stretch's ADM PCM goes
    adm_channels: int


@dataclass(frozen=True, slots=True)
class ChunkResult:
    measured: list[Measure]  # one entry per rendered frame
    clipped: int  # ADM samples clipped at full scale


class _Chunk:
    def __init__(self, job: ChunkJob, pcm: sf.SoundFile) -> None:
        self.job, self.pcm = job, pcm
        self.analysis = Analysis(len(_JOC_INPUTS))
        self.joc = joc.JocDecoder()
        self.synthesis: Synthesis | None = None
        self.matrices = np.empty(0)
        self.objects = np.zeros((0, 0))  # rebuilt samples not yet rendered
        self.skip = 0
        self.queue: list[tuple[int, Frame, np.ndarray]] = []  # (frame, metadata, LFE)
        self.renderer = Renderer.restore(job.gains, job.layout) if job.gains is not None else None
        self.oamd = OamdDecoder(job.oamd_state)
        self.measured: list[Measure] = []
        self.clipped = 0
        self.speakers = (
            np.memmap(job.speakers, dtype=np.float32, mode="r+").reshape(
                len(job.layout.channels), -1
            )
            if job.speakers is not None
            else None
        )
        self.adm = job.adm.open("wb") if job.adm is not None else None

    def params(self, k: int) -> joc.JocFrame:
        try:
            params = joc.parse(self.job.joc[k - self.job.joc_from])
        except BitstreamError as error:
            raise AtmosError(f"第 {k} 帧的全景声元数据无法解析：{error}") from error
        if params.dmx_config not in FIVE_CHANNEL_CONFIGS:
            raise AtmosError(
                f"暂不支持这种全景声下混配置（joc_dmx_config_idx={params.dmx_config}）"
            )
        return params

    def read(self, k: int) -> np.ndarray:
        self.pcm.seek(k * FRAME)
        return self.pcm.read(FRAME, dtype="float32", always_2d=True).T.astype(np.float64)

    def run(self) -> ChunkResult:
        job = self.job
        if job.start > 0:
            self.warm_up(job.start - 1)
        self.skip = DELAY
        for k in range(job.start, job.stop):
            try:
                meta = self.oamd.decode(job.oamd[k - job.start])
            except BitstreamError as error:
                raise AtmosError(f"第 {k} 帧的全景声元数据无法解析：{error}") from error
            params = self.params(k)
            core = self.read(k)
            self.queue.append((k, meta, core[_LFE]))
            self.rebuild(params, core[_JOC_INPUTS])
        if job.stop < job.total:
            self.rebuild(self.params(job.stop), self.read(job.stop)[_JOC_INPUTS])
        else:
            # Run silence through to flush the rest of the last frame out of the filter bank.
            self.rebuild(self.params(job.total - 1), np.zeros((len(_JOC_INPUTS), FRAME)))
        if self.queue:
            raise AtmosError("全景声解码没有对齐到帧边界")
        if self.adm is not None:
            self.adm.close()
        if self.speakers is not None:
            self.speakers.flush()
        return ChunkResult(self.measured, self.clipped)

    def warm_up(self, k: int) -> None:
        """Put every filter bank where a run from the start would have it after frame `k`."""
        history = LENGTH - BANDS
        start = k * FRAME - history
        self.pcm.seek(max(0, start))
        lead = self.pcm.read(k * FRAME - max(0, start), dtype="float32", always_2d=True)
        self.analysis.history[:, history - lead.shape[0] :] = lead.T[_JOC_INPUTS]
        if k > 0:
            previous = self.params(k - 1)
            scratch = np.empty((previous.objects, joc.SLOTS_PER_FRAME, previous.channels, BANDS))
            self.joc.matrices(previous, scratch)
        self.rebuild(self.params(k), self.read(k)[_JOC_INPUTS])
        self.objects = np.zeros((self.objects.shape[0], 0))

    def rebuild(self, params: joc.JocFrame, downmix: np.ndarray) -> None:
        if self.synthesis is None:
            self.synthesis = Synthesis(params.objects)
            self.objects = np.zeros((params.objects, 0))
            self.matrices = np.empty((params.objects, joc.SLOTS_PER_FRAME, params.channels, BANDS))
        elif params.objects != self.objects.shape[0]:
            raise AtmosError("全景声对象数中途改变，暂不支持")
        q = self.analysis(downmix)
        if params.dmx_config in _PHASE_SHIFTED:
            q[_SURROUNDS] *= _UNSHIFT
        self.joc.matrices(params, self.matrices)
        rebuilt = self.synthesis(joc.apply(self.matrices, q) * params.clipgain)
        rebuilt, self.skip = rebuilt[:, self.skip :], 0
        self.objects = np.concatenate([self.objects, rebuilt], axis=1)
        while self.queue and self.objects.shape[1] >= FRAME:
            self.render(*self.queue.pop(0))

    def render(self, k: int, meta: Frame, lfe: np.ndarray) -> None:
        if self.renderer is None:
            self.renderer = Renderer(self.objects.shape[0], self.job.layout)
        if len(joc_objects(meta)) != self.objects.shape[0]:
            raise AtmosError("全景声对象元数据与 JOC 对象数对不上")
        gains = self.renderer.gains(meta, FRAME)
        job = self.job
        f = RenderedFrame(
            k, meta, gains[..., :-1], gains[..., -1], self.objects[:, :FRAME], lfe,
            container_span(k, FRAME, job.first, job.last),
        )  # fmt: skip
        self.objects = self.objects[:, FRAME:]
        self.measured.append(measure(f))
        if f.span.start == f.span.stop:
            return
        if self.speakers is not None:
            at = k * FRAME + f.span.start - job.first
            self.speakers[:, at : at + f.span.stop - f.span.start] = speaker_feeds(
                f, job.layout.index["LFE"]
            )
        if self.adm is not None:
            data, clipped = pcm24(adm_block(f, job.adm_channels))
            self.adm.write(data)
            self.clipped += clipped


def decode_chunk(job: ChunkJob) -> ChunkResult:
    with sf.SoundFile(job.core) as pcm:
        return _Chunk(job, pcm).run()
