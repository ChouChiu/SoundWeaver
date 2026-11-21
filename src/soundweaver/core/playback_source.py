"""Prepare PCM and spatial metadata for playback without Qt or an audio device.

An Atmos or 360RA track plays through an ADM master, so the monitor can meter
and place single objects; that master is written ahead of playback by the same
decoders that export it, and a ready-made ADM BWF is played as it is.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from soundweaver.core import ffmpeg
from soundweaver.core.ac4.decoder import playback_blocks, playback_from_cache
from soundweaver.core.atmos import PreparedADM, decode_atmos, is_adm_bwf
from soundweaver.core.audio import AudioInfo, probe_audio
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.codecs.native import NativePcmStream
from soundweaver.core.paths import work_root
from soundweaver.core.pcm import SOUNDFILE_SUFFIXES, AudioData
from soundweaver.core.playback_buffer import AdmReadAhead, PlaybackBuffer
from soundweaver.core.playback_mix import cicp_fold_matrix
from soundweaver.core.speakers import SpeakerLayout


@dataclass(frozen=True, slots=True)
class PlaybackOptions:
    layout: SpeakerLayout
    cicp: int


@dataclass(slots=True)
class PlaybackSource:
    info: AudioInfo
    channels: tuple[str, ...]
    resources: ExitStack
    audio: AudioData | None = None
    mpegh_matrix: np.ndarray | None = None
    adm: PreparedADM | None = None
    vvm1: av3a.Vvm1Reader | None = None
    buffer: PlaybackBuffer | None = None
    adm_progress: AdmReadAhead | None = None
    vvm1_path: Path | None = None

    @property
    def progress(self) -> PlaybackBuffer | AdmReadAhead | None:
        return self.buffer or self.adm_progress

    def request(self, frame: int) -> None:
        if self.buffer is not None:
            self.buffer.request(frame)
        if self.adm_progress is not None:
            self.adm_progress.request(frame)

    def close(self) -> None:
        if self.buffer is not None:
            self.buffer.close()
        self.audio = None
        self.resources.close()
        if self.vvm1 is not None:
            self.vvm1.close()
            self.vvm1 = None
        if self.adm_progress is not None:
            self.adm_progress.close()
            self.adm_progress = None


def _open_pcm(path: Path, info: AudioInfo, token: CancellationToken, decoder: ExitStack):
    if path.suffix.lower() in SOUNDFILE_SUFFIXES:
        try:
            wav = decoder.enter_context(sf.SoundFile(path))
        except sf.LibsndfileError:
            pass
        else:
            return (
                wav.samplerate,
                wav.channels,
                lambda count: wav.read(count, dtype="float32", always_2d=True),
            )

    rate, channels = info.sample_rate, info.channels
    if rate <= 0 or channels <= 0:
        raise ffmpeg.FFmpegError("The source audio has no usable PCM format.")
    cmd = [
        *ffmpeg.base_command(),
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-ac",
        str(channels),
        "-ar",
        str(rate),
        "-c:a",
        "pcm_f32le",
        "-f",
        "f32le",
        "pipe:1",
    ]
    stream = decoder.enter_context(
        closing(NativePcmStream(cmd, pcm_format=(rate, channels), token=token))
    )
    return rate, channels, stream.read_frames


def _prepare_buffer(
    path: Path,
    options: PlaybackOptions,
    token: CancellationToken,
    source: PlaybackSource,
    start_at: float,
) -> None:
    info = source.info
    stereo = False
    with ExitStack() as decoder:
        if info.is_mpegh:
            layout = mpegh.cicp_layout(options.cicp)
            pcm = decoder.enter_context(
                closing(mpegh.stream_mpegh(path, cicp=layout.index, token=token))
            )
            rate, channels = pcm.sample_rate, pcm.channels
            source.channels = layout.channel_names[:channels]
            source.mpegh_matrix = cicp_fold_matrix(layout, channels)
            read = pcm.read_frames
            if layout.index != 2 and channels > 1:
                listen = decoder.enter_context(
                    closing(mpegh.stream_mpegh(path, cicp=2, token=token))
                )
                if listen.sample_rate != rate or listen.channels != 2:
                    raise mpegh.MpeghError(
                        "The stereo renderer returned an incompatible PCM format."
                    )

                def read(count: int) -> np.ndarray:
                    block = pcm.read_frames(count)
                    alternate = listen.read_frames(count)
                    if len(block) != len(alternate):
                        raise mpegh.MpeghError(
                            "The speaker and stereo renders have different lengths."
                        )
                    return np.column_stack((block, alternate))

                stereo = True
        elif info.is_av3a:
            tmp = Path(source.resources.enter_context(tempfile.TemporaryDirectory(dir=work_root())))
            source.vvm1_path = tmp / "timeline.vvm1"
            pcm = decoder.enter_context(
                closing(
                    av3a.stream_av3a(
                        path,
                        "source",
                        source.vvm1_path,
                        Path(os.devnull),
                        token=token,
                    )
                )
            )
            rate, channels, read = pcm.sample_rate, pcm.channels, pcm.read_frames
            source.channels = info.bed_channels or tuple(f"Ch {i + 1}" for i in range(channels))
        else:
            if info.codec.startswith("AC-4"):
                cached = playback_from_cache(path, token)
                if cached is not None:
                    ac4_info, wav = cached
                    wav = decoder.enter_context(closing(wav))
                    rate, channels = ac4_info.sample_rate, len(ac4_info.channels)

                    def read(count: int, _wav=wav) -> np.ndarray:
                        return _wav.read(count, dtype="float32", always_2d=True)
                else:
                    ac4_info, blocks = playback_blocks(path, token)
                    decoder.callback(blocks.close)
                    rate, channels = ac4_info.sample_rate, len(ac4_info.channels)
                    empty = np.empty((0, channels), dtype=np.float32)

                    def read(_count: int) -> np.ndarray:
                        return next(blocks, empty)
            else:
                rate, channels, read = _open_pcm(path, info, token, decoder)
            source.channels = (
                info.speakers.channels
                if info.speakers
                else tuple(f"Ch {i + 1}" for i in range(channels))
            )

        buffer = PlaybackBuffer(rate, channels, stereo, read, token, decoder.pop_all())
        buffer.start(start_at)
        source.buffer = buffer
        if source.vvm1_path is not None:
            header = av3a.read_vvm1_header(source.vvm1_path)
            if header is not None:
                source.vvm1 = av3a.Vvm1Reader(source.vvm1_path, header)


def prepare_playback(
    path: Path,
    options: PlaybackOptions,
    token: CancellationToken,
    start_at: float = 0.0,
) -> PlaybackSource:
    """Open a source after its initial PCM is available and transfer ownership to the player."""
    info = probe_audio(path, measure_raw_duration=False)
    token.raise_if_cancelled()
    with ExitStack() as resources:
        source = PlaybackSource(info, options.layout.channels, resources)
        try:
            if info.is_av3a or (not info.atmos and not info.is_mpegh):
                _prepare_buffer(path, options, token, source, start_at)
            else:
                adm_path = path
                if not is_adm_bwf(path):
                    progress = AdmReadAhead(token, info.sample_rate or 48000, start_at)
                    source.adm_progress = progress
                    # 360RA decodes to an ADM master so the objects play and meter like
                    # Atmos ones; its own streams carry the objects the master needs.
                    length = round(info.duration * info.sample_rate) or None if info.duration else None
                    if info.is_mpegh:

                        def decode(target: Path, publish: Callable[[Path, int], None]) -> None:
                            mpegh.decode_mpegh(
                                path, token, cicp=options.cicp, adm=target, pcm=False,
                                length=length, on_adm_chunk=publish,
                            )  # fmt: skip

                    else:

                        def decode(target: Path, publish: Callable[[Path, int], None]) -> None:
                            decode_atmos(
                                path, token, layout=options.layout, adm=target, on_adm_chunk=publish
                            )

                    adm_path = progress.start(decode)
                source.adm = PreparedADM(adm_path)
                resources.callback(source.adm.close)
            token.raise_if_cancelled()
        except BaseException:
            source.close()
            raise
        source.resources = resources.pop_all()
        return source
