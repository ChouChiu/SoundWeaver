"""A batch of tracks, or an album of them: finding, ordering and placing their outputs.

No Qt here; the panel probes each track with `source.service.inspect` off the
UI thread and keeps the results.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from dolbytool.core.audio import AudioInfo
from dolbytool.core.motion import MOTION_SUFFIXES, is_motion
from dolbytool.core.project import default_output_dir
from dolbytool.features.source.service import AUDIO_SUFFIXES, IMAGE_SUFFIXES

# Cover art an album folder usually carries, best first
_COVER_NAMES = ("cover", "folder", "front", "album", "albumart", "artwork")
# Album folders hold "Disc 1" style subfolders, not whole libraries
_MAX_DEPTH = 2
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class BatchTrack:
    audio: Path
    info: AudioInfo | None = None
    error: str | None = None
    # the embedded cover, extracted
    cover: Path | None = None
    probing: bool = True

    @property
    def ready(self) -> bool:
        return self.info is not None


def natural_key(path: Path) -> tuple:
    """Sort "2 x" before "10 x": digit runs compare as numbers."""
    parts = re.split(r"(\d+)", str(path).casefold())
    return tuple(int(p) if p.isdigit() else p for p in parts)


def audio_files(paths: Iterable[Path]) -> list[Path]:
    """The audio files among `paths`, folders searched a couple of levels down."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(_scan(path, _MAX_DEPTH))
        elif _is_audio(path):
            found.append(path)
    return found


def _is_audio(path: Path) -> bool:
    # an .mp4 may be a song or soundless motion artwork
    return path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES and not is_motion(path)


def _scan(folder: Path, depth: int) -> list[Path]:
    files: list[Path] = []
    try:
        entries = sorted(folder.iterdir(), key=natural_key)
    except OSError:
        return files
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if depth > 0 and not entry.name.endswith("_stems"):
                files.extend(_scan(entry, depth - 1))
        elif _is_audio(entry):
            files.append(entry)
    return files


def folder_cover(folder: Path) -> Path | None:
    """The album art lying in `folder`: motion artwork first, then an image by its
    conventional name, else the only image there.
    """
    try:
        files = sorted((p for p in folder.iterdir() if p.is_file()), key=natural_key)
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


def album_order(tracks: Sequence[BatchTrack]) -> list[BatchTrack]:
    """Disc, then track number, then file name; untagged tracks keep to file name order."""

    def key(track: BatchTrack) -> tuple:
        info = track.info
        disc = info.disc if info and info.disc else 1
        number = info.track if info and info.track else 10_000
        return disc, number, natural_key(track.audio)

    return sorted(tracks, key=key)


def album_name(tracks: Sequence[BatchTrack]) -> str:
    """The album tag most tracks carry, else the folder they share."""
    tags = Counter(t.info.album for t in tracks if t.info and t.info.album)
    if tags:
        return tags.most_common(1)[0][0]
    folders = {t.audio.parent for t in tracks}
    return folders.pop().name if len(folders) == 1 else ""


def common_folder(tracks: Sequence[BatchTrack]) -> Path | None:
    """The folder holding every track, discs included."""
    if not tracks:
        return None
    folders = [t.audio.parent for t in tracks]
    common = folders[0]
    while not all(f == common or common in f.parents for f in folders):
        if common.parent == common:
            return None
        common = common.parent
    return common


def safe_name(text: str) -> str:
    """`text` usable as one file or folder name."""
    name = _UNSAFE.sub("_", text).strip().strip(".")
    return name or "album"


def track_output_dir(audio: Path, root: Path | None) -> Path:
    """Where one track's outputs go: its own folder under `root`, else next to it."""
    return root / safe_name(audio.stem) if root else default_output_dir(audio)


def album_root(tracks: Sequence[BatchTrack], name: str) -> Path | None:
    """The default output root of an album: beside its tracks, named after it."""
    folder = common_folder(tracks)
    return folder / f"{safe_name(name)}_stems" if folder else None
