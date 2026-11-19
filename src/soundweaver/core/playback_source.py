"""Prepare PCM and spatial metadata for playback without Qt or an audio device."""

from __future__ import annotations

import os
import tempfile
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from soundweaver.core import ffmpeg
from soundweaver.core.ac4.decoder import playback_blocks
from soundweaver.core.atmos import ObjectTrace, PreparedADM, decode_atmos, is_adm_bwf
from soundweaver.core.audio import AudioInfo, load_audio, probe_audio
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.codecs.native import NativePcmStream
from soundweaver.core.paths import work_root
from soundweaver.core.pcm import SOUNDFILE_SUFFIXES, AudioData, read_audio
from soundweaver.core.playback_buffer import AdmReadAhead, PlaybackBuffer
from soundweaver.core.playback_mix import cicp_fold_matrix, fold_peak_bound, stereo_peak
from soundweaver.core.speakers import SpeakerLayout


@dataclass(frozen=True, slots=True)
class PlaybackOptions:
    layout: SpeakerLayout
    cicp: int
    binaural: bool


@dataclass(frozen=True, slots=True)
class MpegHMonitor:
    matrix: np.ndarray
    fold_peak: float
    stereo_peak: float


@dataclass(slots=True)
class PlaybackSource:
    info: AudioInfo
    channels: tuple[str, ...]
    resources: ExitStack
    audio: AudioData | None = None
    stereo_audio: AudioData | None = None
    mpegh_monitor: MpegHMonitor | None = None
    adm: PreparedADM | None = None
    trace: ObjectTrace | None = None
    vvm1_header: av3a.VvmTimelineHeader | None = None
    vvm1_frames: np.ndarray | None = None
    buffer: PlaybackBuffer | None = None
    adm_progress: AdmReadAhead | None = None
    vvm1_path: Path | None = None

    @property
    def progress(self) -> PlaybackBuffer | AdmReadAhead | None:
        return self.buffer or self.adm_progress

    def close(self) -> None:
        self.audio = self.stereo_audio = None
        self.resources.close()
        if self.progress is not None:
            self.progress.close()


def _channel_numbers(audio: AudioData) -> tuple[str, ...]:
    return tuple(f"Ch {i + 1}" for i in range(audio.channels))


def _prepare_av3a(path: Path, token: CancellationToken, source: PlaybackSource) -> None:
    tmp = Path(source.resources.enter_context(tempfile.TemporaryDirectory(dir=work_root())))
    timeline, pcm, interleaved = (
        tmp / name for name in ("timeline.vvm1", "source.f32", "stream.f32")
    )
    with closing(av3a.stream_av3a(path, "source", timeline, pcm)) as stream:
        with interleaved.open("wb") as output:
            while True:
                token.raise_if_cancelled()
                block = stream.read_frames(8192)
                if not block.size:
                    break
                block.tofile(output)
        rate, channels = stream.sample_rate, stream.channels

    frames = interleaved.stat().st_size // (4 * channels)
    if frames:
        samples = np.memmap(interleaved, dtype="<f4", mode="r", shape=(channels, frames), order="F")
    else:
        samples = np.empty((channels, 0), dtype=np.float32)
    source.audio = AudioData(samples, rate)
    source.channels = source.info.bed_channels or _channel_numbers(source.audio)
    source.vvm1_header, source.vvm1_frames = av3a.read_vvm1_timeline(timeline)


def _prepare_mpegh(
    path: Path,
    options: PlaybackOptions,
    token: CancellationToken,
    source: PlaybackSource,
    binaural: bool,
) -> None:
    layout = mpegh.cicp_layout(options.cicp)
    resources = source.resources
    audio = resources.enter_context(mpegh.decode_mpegh(path, token, cicp=layout.index))
    source.audio = audio
    source.channels = layout.channel_names[: audio.channels]
    if binaural and audio.channels >= 4:
        source.stereo_audio = resources.enter_context(
            av3a.render_binaural_audio(
                audio, speakers=layout.binaural_names[: audio.channels], token=token
            )
        )
    elif layout.index != 2 and audio.channels > 1:
        # The native 2.0 renderer uses the encoded scene and downmix gains.
        source.stereo_audio = resources.enter_context(mpegh.decode_mpegh(path, token, cicp=2))
    matrix = cicp_fold_matrix(layout, audio.channels)
    source.mpegh_monitor = MpegHMonitor(
        matrix,
        fold_peak_bound(audio, matrix, token),
        stereo_peak(source.stereo_audio, token) if source.stereo_audio is not None else 0.0,
    )


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
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
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
            source.mpegh_monitor = MpegHMonitor(cicp_fold_matrix(layout, channels), 0.0, 0.0)
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
            else:
                read = pcm.read_frames
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
            source.vvm1_header = av3a.read_vvm1_header(source.vvm1_path)


def prepare_playback(
    path: Path,
    options: PlaybackOptions,
    token: CancellationToken,
    start_at: float = 0.0,
) -> PlaybackSource:
    """Transfer ownership of scratch PCM and ADM handles only after a successful load."""
    info = probe_audio(path, measure_raw_duration=False)
    token.raise_if_cancelled()
    needs_renderer = (
        info.atmos
        and not (info.is_av3a or info.is_mpegh)
        or info.is_av3a
        and (info.hoa_order > 0 or info.channels >= 4)
        or info.is_mpegh
        and mpegh.cicp_layout(options.cicp).channels >= 4
    )
    binaural = options.binaural and needs_renderer and av3a.find_binaural_renderer() is not None
    with ExitStack() as resources:
        source = PlaybackSource(info, options.layout.channels, resources)
        adm_path = None
        if not binaural and (info.is_av3a or info.is_mpegh or not info.atmos):
            try:
                _prepare_buffer(path, options, token, source, start_at)
                token.raise_if_cancelled()
            except BaseException:
                source.close()
                raise
            source.resources = resources.pop_all()
            return source
        if is_adm_bwf(path):
            adm_path = path
        elif info.is_av3a:
            _prepare_av3a(path, token, source)
        elif info.is_mpegh:
            _prepare_mpegh(path, options, token, source, binaural)
        elif info.atmos:
            tmp = Path(resources.enter_context(tempfile.TemporaryDirectory(dir=work_root())))
            target = tmp / "decoded_adm.wav"
            decoded = decode_atmos(
                path,
                token,
                lambda _f: None,
                layout=options.layout,
                speakers=False,
                adm=target,
                restore_objects=True,
            )
            source.trace = decoded.trace
            if target.is_file():
                adm_path = target
            else:
                decoded = decode_atmos(
                    path,
                    token,
                    lambda _f: None,
                    layout=options.layout,
                    speakers=True,
                    restore_objects=True,
                )
                if decoded.speakers is not None:
                    source.audio = resources.enter_context(decoded.speakers)
        else:
            source.audio = resources.enter_context(load_audio(path, token))
            source.channels = (
                info.speakers.channels if info.speakers else _channel_numbers(source.audio)
            )

        if adm_path is not None:
            source.adm = PreparedADM(adm_path)
            resources.callback(source.adm.close)
            if binaural:
                tmp = Path(resources.enter_context(tempfile.TemporaryDirectory(dir=work_root())))
                output = av3a.render_binaural_file(adm_path, tmp / "binaural.wav", token=token)
                source.stereo_audio = resources.enter_context(read_audio(output, token))
        elif (
            binaural
            and info.is_av3a
            and source.audio is not None
            and (info.hoa_order > 0 or source.audio.channels >= 4)
        ):
            source.stereo_audio = resources.enter_context(
                av3a.render_binaural_audio(
                    source.audio,
                    speakers=None if info.hoa_order else source.channels,
                    hoa_order=info.hoa_order,
                    token=token,
                )
            )

        token.raise_if_cancelled()
        source.resources = resources.pop_all()
        return source
