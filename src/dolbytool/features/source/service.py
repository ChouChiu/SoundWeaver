"""Reading a picked audio file: its layout, its embedded cover and its lyrics beside it.

No Qt here; the panel runs `inspect` off the UI thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dolbytool.core import ffmpeg
from dolbytool.core.audio import AudioInfo, probe_audio
from dolbytool.core.paths import cache_dir, content_key

AUDIO_SUFFIXES = {".m4a", ".mp4", ".mka", ".mkv", ".ec3", ".eac3", ".ac3", ".thd", ".wav", ".flac"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TTML_SUFFIXES = {".ttml"}


@dataclass(frozen=True, slots=True)
class Inspection:
    audio: Path
    info: AudioInfo | None = None
    error: str | None = None
    # the embedded cover, extracted, when one was asked for and found
    cover: Path | None = None


def inspect(audio: Path, want_cover: bool) -> Inspection:
    """Probe `audio` and, with `want_cover`, pull its embedded cover out to the work root."""
    try:
        info = probe_audio(audio)
    except (ffmpeg.FFmpegError, OSError) as error:
        return Inspection(audio, error=str(error))
    cover = None
    if want_cover and info.cover_stream is not None:
        target = cache_dir("covers") / f"{audio.stem}_{content_key(audio)[:8]}.png"
        try:
            if not target.is_file():
                ffmpeg.extract_frame(audio, info.cover_stream, target)
            cover = target
        except ffmpeg.FFmpegError:
            target.unlink(missing_ok=True)
    return Inspection(audio, info, cover=cover)


def sibling_ttml(audio: Path) -> Path | None:
    """A same-named .ttml next to the audio is almost certainly its lyrics."""
    sibling = audio.with_suffix(".ttml")
    return sibling if sibling.is_file() else None


def kind_of(path: Path) -> str | None:
    """Which input a dropped file is: audio, cover or ttml."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "cover"
    if suffix in TTML_SUFFIXES:
        return "ttml"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return None
