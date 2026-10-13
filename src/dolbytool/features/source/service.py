"""Reading a picked audio file: its layout, its cover and its lyrics.

No Qt here; the panels run `inspect` off the UI thread.  The single source
and the batch share it, so a track finds the same cover and lyrics either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dolbytool.core import ffmpeg
from dolbytool.core.audio import AudioInfo, probe_audio
from dolbytool.core.motion import MOTION_SUFFIXES, is_motion
from dolbytool.core.paths import atomic_output, cache_dir, content_key, natural_key
from dolbytool.features.source import lyrics

AUDIO_SUFFIXES = {".m4a", ".mp4", ".mka", ".mkv", ".ec3", ".eac3", ".ac3", ".thd", ".wav", ".flac"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TTML_SUFFIXES = {".ttml"}
# Cover art an album folder usually carries, best first
_COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork")
# what this app writes: a silent lyric video looks just like motion artwork
_OUTPUT_MARKERS = ("_lyrics", "_soundfield", "_player", "_gradient")


@dataclass(frozen=True, slots=True)
class Inspection:
    audio: Path
    info: AudioInfo | None = None
    error: str | None = None
    # the embedded cover, extracted, else the art in the audio's folder
    cover: Path | None = None
    # the lyrics found for it, see `find_lyrics`
    ttml: Path | None = None


def inspect(audio: Path, want_cover: bool = True) -> Inspection:
    """Probe `audio` and find its cover and lyrics; slow, so run it off the UI thread."""
    try:
        info = probe_audio(audio)
    except (ffmpeg.FFmpegError, OSError) as error:
        return Inspection(audio, error=str(error))
    cover = None
    if want_cover:
        cover = _embedded_cover(audio, info) or folder_cover(audio.parent)
    return Inspection(audio, info, cover=cover, ttml=find_lyrics(audio, info.title))


def _embedded_cover(audio: Path, info: AudioInfo) -> Path | None:
    if info.cover_stream is None:
        return None
    target = cache_dir("covers") / f"{audio.stem}_{content_key(audio)[:8]}.png"
    if not target.is_file():
        try:
            with atomic_output(target) as partial:
                ffmpeg.extract_frame(audio, info.cover_stream, partial)
        except ffmpeg.FFmpegError:
            return None
    return target


def folder_cover(folder: Path) -> Path | None:
    """The album art lying in `folder`: motion artwork first, then an image by its
    conventional name, else the only image there; this app's own outputs never count.
    """
    try:
        files = sorted(
            (p for p in folder.iterdir() if p.is_file() and not p.stem.endswith(_OUTPUT_MARKERS)),
            key=natural_key,
        )
    except OSError:
        return None
    moving = [p for p in files if p.suffix.lower() in MOTION_SUFFIXES and is_motion(p)]
    if moving:
        # the player's cover frame is square: Apple's square artwork before the tall one
        return min(moving, key=lambda p: "square" not in p.name.casefold())
    images = [p for p in files if p.suffix.lower() in IMAGE_SUFFIXES]
    by_name = {p.stem.casefold(): p for p in images}
    for name in _COVER_NAMES:
        if name in by_name:
            return by_name[name]
    return images[0] if len(images) == 1 else None


def find_lyrics(audio: Path, title: str = "") -> Path | None:
    """The lyrics of one track: a same-named .ttml, one nearby whose name or declared
    title fits, else the only .ttml beside the only song in its folder.
    """
    sibling = audio.with_suffix(".ttml")
    if sibling.is_file():
        return sibling
    nearby = lyrics.lyrics_near(audio)
    if found := lyrics.match([(audio, title)], nearby).get(audio):
        return found
    if len(nearby) == 1 and _songs_in(audio.parent) == 1:
        return nearby[0]
    return None


def _songs_in(folder: Path) -> int:
    try:
        return sum(1 for p in folder.iterdir() if is_audio(p))
    except OSError:
        return 0


def is_audio(path: Path) -> bool:
    # an .mp4 may be a song or soundless motion artwork
    return path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES and not is_motion(path)


def kind_of(path: Path) -> str | None:
    """Which input a dropped file is: audio, cover (motion artwork included) or ttml."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES or is_motion(path):
        return "cover"
    if suffix in TTML_SUFFIXES:
        return "ttml"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return None
