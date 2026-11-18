"""What the app needs to know about a source audio file."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from soundweaver.core import ac4, ffmpeg
from soundweaver.core.atmos.adm_reader import PreparedADM, is_adm_bwf
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.i18n import t
from soundweaver.core.pcm import SOUNDFILE_SUFFIXES, AudioData, decode_audio, read_audio
from soundweaver.core.speakers import BY_NAME, LAYOUTS, SpeakerLayout, file_layout
from soundweaver.core.stems import StemLayout, layout_for

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AudioInfo:
    # the stream's own channels; a Dolby Atmos track's are its core's
    channels: int
    duration: float
    atmos: bool = False
    codec: str = ""
    sample_rate: int = 0
    # stream index of an embedded cover picture
    cover_stream: int | None = None
    # container tags, empty when missing
    title: str = ""
    artist: str = ""
    album: str = ""
    # position on the album, 0 when untagged
    disc: int = 0
    track: int = 0
    # the speakers of a multichannel file; a Dolby Atmos track's are picked for the render
    speakers: SpeakerLayout | None = None
    adm_bwf: bool = False
    object_count: int = 0
    bed_channels: tuple[str, ...] = ()
    is_av3a: bool = False
    is_mpegh: bool = False
    hoa_order: int = 0
    av3a_profile: int = 0

    @property
    def layout(self) -> StemLayout:
        return layout_for(self.channels, self.atmos)

    def describe(self, render: SpeakerLayout | None = None) -> str:
        """One line about the file; `render` is what a Dolby Atmos track is rendered to."""
        layout_name = t(f"audio.layout.{self.layout.value}")

        minutes, seconds = int(self.duration // 60), int(self.duration % 60)
        if self.atmos:
            parts = [
                t("audio.describe.atmos_rendered", render=render)
                if render
                else t("audio.describe.atmos")
            ]
        elif self.speakers is not None:
            parts = [
                t(
                    "audio.describe.channels_layout_spk",
                    channels=self.channels,
                    layout=layout_name,
                    speakers=self.speakers,
                )
            ]
        else:
            parts = [
                t("audio.describe.channels_layout", channels=self.channels, layout=layout_name)
            ]
        parts.append(f"{minutes}:{seconds:02d}")
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz")
        if self.codec:
            parts.append(self.codec)
        return " · ".join(parts)


def _is_atmos(stream: dict) -> bool:
    """Dolby Atmos carried as E-AC-3 JOC, the form the split can render."""
    return stream.get("codec_name") == "eac3" and "Atmos" in stream.get("profile", "")


def _number(tag: str) -> int:
    """The leading number of a "3" or "3/12" track or disc tag, 0 when there is none."""
    head = tag.split("/", 1)[0].strip()
    return int(head) if head.isdigit() else 0


def _av3a_speakers(channel_order: str) -> SpeakerLayout | None:
    if not channel_order:
        return None
    alias = {
        "Ltf": "Tfl", "Rtf": "Tfr",
        "Ltm": "Tsl", "Rtm": "Tsr",
        "Ltr": "Tbl", "Rtr": "Tbr",
        "Lrs": "Lb", "Rrs": "Rb",
    }
    labels = tuple(alias.get(c.strip(), c.strip()) for c in channel_order.split(",") if c.strip())
    for layout in LAYOUTS:
        if layout.channels == labels:
            return layout
    return None


def _probe_av3a_info(path: Path) -> AudioInfo:
    try:
        av = av3a.probe_av3a(path)
    except av3a.Av3aError as error:
        raise ffmpeg.FFmpegError(str(error)) from error
    if av.hoa_order > 0:
        codec = f"Audio Vivid (HOA Order {av.hoa_order})"
        spk = None
    else:
        codec = "Audio Vivid (AVS3)"
        spk = None if av.object_count > 0 else _av3a_speakers(av.channel_order)
    bed = tuple(s.strip() for s in av.channel_order.split(",") if s.strip())
    return AudioInfo(
        av.channels,
        max(0.01, av.duration),
        atmos=av.object_count > 0 or av.hoa_order > 0,
        codec=codec,
        sample_rate=av.sample_rate,
        speakers=spk,
        object_count=av.object_count,
        bed_channels=bed,
        is_av3a=True,
        hoa_order=av.hoa_order,
        av3a_profile=av.profile,
    )


def probe_audio(path: Path, *, measure_raw_duration: bool = True) -> AudioInfo:
    """Layout, length and embedded cover of the first audio stream; raises FFmpegError if unusable."""
    if av3a.is_av3a_file(path):
        return _probe_av3a_info(path)
    is_mpegh = mpegh.is_mpegh_file(path)
    if is_mpegh and path.suffix.lower() in mpegh.MPEGH_SUFFIXES:
        info = mpegh.probe_mpegh(path, measure_duration=measure_raw_duration)
        return AudioInfo(
            info.channels,
            max(0.01, info.duration),
            atmos=False,
            codec="MPEG-H 3D Audio (360RA)",
            sample_rate=info.sample_rate,
            speakers=BY_NAME.get("22.2"),
            is_mpegh=True,
        )
    data = ffmpeg.probe(path)
    stream = ffmpeg.first_audio_stream(data)
    ac4_info = _ac4_info(path) if ac4.is_ac4_stream(stream) else None
    if is_mpegh:
        # An mha1/mhm1 sample entry can omit the channel count. MPEG-H is
        # rendered to the configured CICP target, independent of its source bed.
        channels = mpegh.cicp_layout(13).channels
    else:
        # A bare AC-4 stream gets its channel count from the TOC below.
        channels = int(stream.get("channels", 0)) if ac4_info is None else 1
    if channels < 1:
        raise ffmpeg.FFmpegError(t("error.no_channel_count"))
    try:
        stream_duration = stream.get("duration") if is_mpegh else None
        seconds = float(stream_duration or data["format"]["duration"])
    except (KeyError, ValueError) as error:
        if ac4_info is None or ac4_info.duration is None:
            raise ffmpeg.FFmpegError(t("error.no_duration", name=path.name)) from error
        seconds = ac4_info.duration
    if seconds <= 0:
        raise ffmpeg.FFmpegError(t("error.zero_duration"))
    atmos = _is_atmos(stream)
    tags = ffmpeg.media_tags(data, stream)
    codec = str(stream.get("codec_long_name") or stream.get("codec_name") or "")
    speakers = None if atmos or is_mpegh else file_layout(str(stream.get("channel_layout", "")), channels)
    adm_bwf = False
    object_count = 0
    bed_channels: tuple[str, ...] = ()
    if is_mpegh:
        codec = "MPEG-H 3D Audio (360RA)"
        speakers = BY_NAME["22.2"]
    elif ac4_info is not None:
        channels, codec, speakers, atmos = _ac4_layout(ac4_info)
    elif is_adm_bwf(path):
        try:
            adm = PreparedADM(path)
            try:
                adm_bwf = True
                atmos = True
                speakers = None
                object_count = adm.info.object_count
                bed_channels = adm.info.bed_channels
                codec = f"ADM BWF ({adm.info.bits_per_sample}-bit PCM)"
            finally:
                adm.close()
        except (OSError, ValueError) as error:
            logger.warning("Failed to parse ADM BWF chunks in %s: %s", path, error)
    return AudioInfo(
        channels,
        seconds,
        atmos=atmos,
        codec=codec,
        sample_rate=ac4_info.sample_rate if ac4_info else int(stream.get("sample_rate") or 0),
        cover_stream=ffmpeg.cover_stream(data),
        title=tags.get("title", ""),
        artist=tags.get("artist", "") or tags.get("album_artist", ""),
        album=tags.get("album", ""),
        disc=_number(tags.get("disc", "")),
        track=_number(tags.get("track", "")),
        speakers=speakers,
        adm_bwf=adm_bwf,
        object_count=object_count,
        bed_channels=bed_channels,
        is_mpegh=is_mpegh,
    )


def load_audio(path: Path, token: CancellationToken) -> AudioData:
    """Decode or read any supported audio input into a float32 `AudioData` buffer."""
    if av3a.is_av3a_file(path):
        return av3a.decode_av3a(path, token)
    if mpegh.is_mpegh_file(path):
        return mpegh.decode_mpegh(path, token)
    if path.suffix.lower() in SOUNDFILE_SUFFIXES:
        try:
            return read_audio(path, token)
        except sf.LibsndfileError:
            pass  # container/codec variant libsndfile lacks; fall through to ffmpeg
    stream = ffmpeg.first_audio_stream(ffmpeg.probe(path))
    if ac4.is_ac4_stream(stream):
        return read_audio(ac4.decoded_path(path, token), token)
    return decode_audio(path, token, stream=stream)


def _ac4_info(path: Path) -> ac4.Ac4Info:
    try:
        return ac4.describe(path)
    except ac4.Ac4Error as error:
        raise ffmpeg.FFmpegError(str(error)) from error


def _ac4_layout(info: ac4.Ac4Info) -> tuple[int, str, SpeakerLayout | None, bool]:
    """Channels, codec name, speakers and Dolby Atmos-ness of what the AC-4 decoder makes of a track.

    ffprobe has no AC-4 decoder, so its channel count is the container's word;
    the first frame's own table of contents says what decoding gives.  A-JOC
    objects count as Dolby Atmos, with the 5.1 render as their core.
    """
    if info.objects:
        return len(info.channels), "AC-4 A-JOC", None, True
    # as `file_layout` does, a layout only for a discrete surround master
    speakers = next(
        (
            layout
            for layout in LAYOUTS
            if layout.channels == info.channels and len(layout.channels) >= 6
        ),
        None,
    )
    return len(info.channels), "AC-4 IMS" if info.ims else "AC-4", speakers, False
