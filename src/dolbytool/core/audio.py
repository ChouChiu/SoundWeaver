"""What the app needs to know about a source audio file."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dolbytool.core import ffmpeg
from dolbytool.core.speakers import SpeakerLayout, file_layout
from dolbytool.core.stems import StemLayout, layout_for

LAYOUT_NAMES = {
    StemLayout.ATMOS: "杜比全景声",
    StemLayout.SURROUND: "多声道母带",
    StemLayout.STEREO: "立体声",
    StemLayout.MONO: "单声道",
}


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

    @property
    def layout(self) -> StemLayout:
        return layout_for(self.channels, self.atmos)

    def describe(self, render: SpeakerLayout | None = None) -> str:
        """One line about the file; `render` is what a Dolby Atmos track is rendered to."""
        from dolbytool.core.i18n import t

        layout_key = {
            StemLayout.ATMOS: "audio.layout.atmos",
            StemLayout.SURROUND: "audio.layout.surround",
            StemLayout.STEREO: "audio.layout.stereo",
            StemLayout.MONO: "audio.layout.mono",
        }.get(self.layout, "")
        layout_name = t(layout_key) if layout_key else LAYOUT_NAMES.get(self.layout, "")

        minutes, seconds = int(self.duration // 60), int(self.duration % 60)
        if self.atmos:
            parts = [t("audio.describe.atmos_rendered", render=render) if render else t("audio.describe.atmos")]
        elif self.speakers is not None:
            parts = [t("audio.describe.channels_layout_spk", channels=self.channels, layout=layout_name, speakers=self.speakers)]
        else:
            parts = [t("audio.describe.channels_layout", channels=self.channels, layout=layout_name)]
        parts.append(f"{minutes}:{seconds:02d}")
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz")
        if self.codec:
            parts.append(self.codec)
        return " · ".join(parts)


def _is_atmos(stream: dict) -> bool:
    """Dolby Atmos carried as E-AC-3 JOC, the form the split can render."""
    return stream.get("codec_name") == "eac3" and "Atmos" in stream.get("profile", "")


def _tags(data: dict, stream: dict) -> dict[str, str]:
    """Container tags, then the audio stream's, with lower-cased keys (Matroska shouts them)."""
    tags: dict[str, str] = {}
    for source in (stream.get("tags", {}), data.get("format", {}).get("tags", {})):
        for key, value in source.items():
            if str(value).strip():
                tags[key.lower()] = str(value).strip()
    return tags


def _number(tag: str) -> int:
    """The leading number of a "3" or "3/12" track or disc tag, 0 when there is none."""
    head = tag.split("/", 1)[0].strip()
    return int(head) if head.isdigit() else 0


def probe_audio(path: Path) -> AudioInfo:
    """Layout, length and embedded cover of the first audio stream; raises FFmpegError if unusable."""
    data = ffmpeg.probe(path)
    stream = ffmpeg.first_audio_stream(data)
    channels = int(stream.get("channels", 0))
    if channels < 1:
        raise ffmpeg.FFmpegError("无法识别声道数")
    try:
        seconds = float(data["format"]["duration"])
    except (KeyError, ValueError) as error:
        raise ffmpeg.FFmpegError("无法读取时长") from error
    if seconds <= 0:
        raise ffmpeg.FFmpegError("音频时长为 0")
    cover = next(
        (s["index"] for s in data["streams"] if s.get("disposition", {}).get("attached_pic")),
        None,
    )
    atmos = _is_atmos(stream)
    tags = _tags(data, stream)
    return AudioInfo(
        channels,
        seconds,
        atmos=atmos,
        codec=str(stream.get("codec_long_name") or stream.get("codec_name") or ""),
        sample_rate=int(stream.get("sample_rate") or 0),
        cover_stream=cover,
        title=tags.get("title", ""),
        artist=tags.get("artist", "") or tags.get("album_artist", ""),
        album=tags.get("album", ""),
        disc=_number(tags.get("disc", "")),
        track=_number(tags.get("track", "")),
        speakers=None if atmos else file_layout(str(stream.get("channel_layout", "")), channels),
    )
