"""Read playlist tags and artwork without preparing playback PCM."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from soundweaver.core import ffmpeg
from soundweaver.core.sources import embedded_cover, sidecar_cover


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    title: str
    artist: str = ""
    duration: float = 0.0
    artwork: Path | None = None


def read_track_metadata(path: Path) -> TrackMetadata:
    data = ffmpeg.probe(path)
    stream = ffmpeg.first_audio_stream(data)
    tags = ffmpeg.media_tags(data, stream)
    duration = stream.get("duration") or data.get("format", {}).get("duration") or 0.0
    try:
        seconds = float(duration)
    except ValueError:
        seconds = 0.0
    artwork = embedded_cover(path, ffmpeg.cover_stream(data)) or sidecar_cover(path)
    return TrackMetadata(
        tags.get("title") or path.stem,
        tags.get("artist") or tags.get("album_artist", ""),
        seconds,
        artwork,
    )
