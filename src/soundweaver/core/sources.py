"""Shared input discovery and classification (audio, artwork, TTML) used by source, batch, and UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from soundweaver.core import ffmpeg, ttml
from soundweaver.core.audio import AudioInfo, probe_audio
from soundweaver.core.motion import MOTION_SUFFIXES, is_motion
from soundweaver.core.paths import atomic_output, cache_dir, content_key, natural_key

AUDIO_SUFFIXES = {
    ".m4a",
    ".mp4",
    ".mov",
    ".mka",
    ".mkv",
    ".ts",
    ".m2ts",
    ".ec3",
    ".eac3",
    ".ac3",
    ".ac4",
    ".thd",
    ".av3a",
    ".avs3a",
    ".mhas",
    ".wav",
    ".flac",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
TTML_SUFFIXES = {".ttml"}
_COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork")
_OUTPUT_MARKERS = ("_lyrics", "_soundfield", "_player", "_gradient")


@dataclass(frozen=True, slots=True)
class Inspection:
    """Result of inspecting one picked audio track (audio stream, artwork, lyrics)."""

    audio: Path
    info: AudioInfo | None = None
    error: str | None = None
    cover: Path | None = None
    ttml: Path | None = None


def inspect_source(audio: Path, want_cover: bool = True) -> Inspection:
    """Probe `audio` and resolve its cover art and matching TTML lyrics."""
    try:
        info = probe_audio(audio)
    except (ffmpeg.FFmpegError, OSError, RuntimeError) as error:
        return Inspection(audio, error=str(error))
    cover = (_embedded_cover(audio, info) or folder_cover(audio.parent)) if want_cover else None
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
    """Resolve album cover in `folder` (motion artwork first, then standard cover filenames)."""
    try:
        files = sorted(
            (p for p in folder.iterdir() if p.is_file() and not p.stem.endswith(_OUTPUT_MARKERS)),
            key=natural_key,
        )
    except OSError:
        return None
    moving = [p for p in files if p.suffix.lower() in MOTION_SUFFIXES and is_motion(p)]
    if moving:
        return min(moving, key=lambda p: "square" not in p.name.casefold())
    images = [p for p in files if p.suffix.lower() in IMAGE_SUFFIXES]
    by_name = {p.stem.casefold(): p for p in images}
    for name in _COVER_NAMES:
        if name in by_name:
            return by_name[name]
    return images[0] if len(images) == 1 else None


def find_lyrics(audio: Path, title: str = "") -> Path | None:
    """Find matching `.ttml` for `audio` (same-named sibling, fuzzy title match, or lone file)."""
    sibling = audio.with_suffix(".ttml")
    if sibling.is_file():
        return sibling
    nearby = ttml.lyrics_near(audio)
    if found := ttml.match([(audio, title)], nearby).get(audio):
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
    """Return True if `path` is an audio file (excluding silent motion artwork MP4s)."""
    return path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES and not is_motion(path)


def kind_of(path: Path) -> str | None:
    """Classify a dropped file as `'audio'`, `'cover'`, or `'ttml'`."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES or is_motion(path):
        return "cover"
    if suffix in TTML_SUFFIXES:
        return "ttml"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return None
