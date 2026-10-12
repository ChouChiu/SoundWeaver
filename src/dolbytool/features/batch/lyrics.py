"""Pairing a batch's tracks with lyric files whose names only roughly agree.

Downloads name the same song "XLOV - HIPS (Hyun&Haru) [Dolby Atmos 768 kbps]"
on one side and "03. HIPS" on the other, so both names are boiled down to
the bare title, with the track's title tag and the title a TTML file
declares as further names.  Each lyric file goes to at most one track, and
a file that fits several tracks equally well goes to none: picking it by
hand is cheaper than a wrong guess.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path

from dolbytool.core.paths import file_stamp
from dolbytool.features.batch.service import natural_key

# subfolders lyrics are commonly kept in, beside the audio or the album's discs
LYRIC_DIRS = {"lyrics", "lyric", "ttml", "歌词"}
_DISC_DIR = re.compile(r"(disc|disk|cd)\s*\d+", re.IGNORECASE)
_BRACKETS = re.compile(r"[\[(（【{][^\])）】}]*[\])）】}]")
_TRACK_NUMBER = re.compile(r"^\s*(?:\d{1,2}\s*[-.]\s*)?\d{1,3}\s*(?:[-._)]\s*|\s+)(?=\S)")
_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_NOISE = re.compile(r"[\W_]+")
# a TTML head is small; AMLL's lyric database names the song in it
_HEAD_BYTES = 64 * 1024
_META = re.compile(r"<(?:\w+:)?meta\b([^>]*)>", re.IGNORECASE)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_TITLE = re.compile(r"<(?:\w+:)?title\b[^>]*>([^<]+)<", re.IGNORECASE)
_TITLE_KEYS = {"musicname", "title"}
# a shared fragment this short says nothing
_MIN_PARTIAL = 3

# how strongly two names agree
_EXACT, _WHOLE, _PART, _CONTAINS = 4, 3, 2, 1


def _squash(text: str) -> str:
    return _NOISE.sub("", unicodedata.normalize("NFKC", text).casefold())


def without_number(name: str) -> str:
    """`name` without a leading track number like "01. " or "1-02 "."""
    return _TRACK_NUMBER.sub("", name, count=1) or name


def names(text: str) -> dict[str, int]:
    """The ways `text` may name a song, each with how much it can be trusted.

    The name as given says most; without brackets and track number it is
    still the song's name; the sides of an "Artist - Title" only possibly so.
    """
    text = unicodedata.normalize("NFKC", text)
    bare = _BRACKETS.sub(" ", text)
    found: dict[str, int] = {}

    def keep(name: str, weight: int) -> None:
        if key := _squash(name):
            found[key] = max(found.get(key, 0), weight)

    keep(text, _EXACT)
    for whole in (bare, _TRACK_NUMBER.sub("", bare, count=1)):
        keep(whole, _WHOLE)
        parts = _SEPARATOR.split(whole)
        if len(parts) > 1:
            for part in parts:
                keep(part, _PART)
    return found


@lru_cache(maxsize=1024)
def _declared(path: Path, _stamp: object) -> tuple[str, ...]:
    try:
        with path.open("rb") as file:
            head = file.read(_HEAD_BYTES).decode("utf-8", "replace")
    except OSError:
        return ()
    titles = [m.group(1) for m in _TITLE.finditer(head)]
    for meta in _META.finditer(head):
        attrs = {k.casefold(): v for k, v in _ATTR.findall(meta.group(1))}
        if attrs.get("key", "").casefold() in _TITLE_KEYS and attrs.get("value"):
            titles.append(attrs["value"])
    return tuple(html.unescape(t).strip() for t in titles if t.strip())


def declared_titles(ttml: Path) -> tuple[str, ...]:
    """The song titles a TTML file states in its head (AMLL's musicName, ttm:title)."""
    return _declared(ttml, file_stamp(ttml)) if ttml.is_file() else ()


def lyric_files(paths: Iterable[Path]) -> list[Path]:
    """The .ttml files among `paths`, a folder's own and its lyric subfolders' included."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(_in_folder(path))
        elif path.suffix.lower() == ".ttml" and path.is_file():
            found.append(path)
    return found


def _in_folder(folder: Path) -> list[Path]:
    try:
        entries = sorted(folder.iterdir(), key=natural_key)
    except OSError:
        return []
    found: list[Path] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            if entry.name.casefold() in LYRIC_DIRS:
                found.extend(_in_folder(entry))
        elif entry.suffix.lower() == ".ttml":
            found.append(entry)
    return found


def lyrics_near(audio: Path) -> list[Path]:
    """The lyric files a track's lyrics are likely among: beside it, in a lyrics
    subfolder there, or, for a disc folder, the same around the album folder.
    """
    folders = [audio.parent]
    if _DISC_DIR.fullmatch(audio.parent.name.strip()):
        folders.append(audio.parent.parent)
    return lyric_files(folders)


def _score(track: Mapping[str, int], lyric: Mapping[str, int]) -> int:
    best = 0
    for key, weight in track.items():
        if key in lyric:
            best = max(best, min(weight, lyric[key]))
    if best:
        return best
    # "HIPS" against "HIPS Hyun Haru Ver": one whole name inside the other
    for key, weight in track.items():
        if weight < _WHOLE or len(key) < _MIN_PARTIAL:
            continue
        for other, other_weight in lyric.items():
            if (
                other_weight >= _WHOLE
                and len(other) >= _MIN_PARTIAL
                and (key in other or other in key)
            ):
                return _CONTAINS
    return 0


def match(tracks: Sequence[tuple[Path, str]], lyrics: Sequence[Path]) -> dict[Path, Path]:
    """Pair each (audio, title tag) with a lyric file, where one plainly fits.

    The closest pairs go first; a track or file with two equally close
    partners is left out, along with those partners at that closeness.
    """
    lyrics = list(dict.fromkeys(lyrics))
    track_names = []
    for audio, title in tracks:
        found = names(audio.stem)
        if title:
            for key, weight in names(title).items():
                found[key] = max(found.get(key, 0), weight)
        track_names.append(found)
    lyric_names = []
    for lyric in lyrics:
        found = names(lyric.stem)
        for title in declared_titles(lyric):
            for key, weight in names(title).items():
                found[key] = max(found.get(key, 0), weight)
        lyric_names.append(found)

    scores = {
        (t, l): s
        for t, tn in enumerate(track_names)
        for l, ln in enumerate(lyric_names)
        if (s := _score(tn, ln))
    }
    paired: dict[Path, Path] = {}
    taken_tracks: set[int] = set()
    taken_lyrics: set[int] = set()
    for level in (_EXACT, _WHOLE, _PART, _CONTAINS):
        at_level = [pair for pair, s in scores.items() if s == level]
        open_pairs = [
            (t, l) for t, l in at_level if t not in taken_tracks and l not in taken_lyrics
        ]
        per_track: dict[int, int] = {}
        per_lyric: dict[int, int] = {}
        for t, l in open_pairs:
            per_track[t] = per_track.get(t, 0) + 1
            per_lyric[l] = per_lyric.get(l, 0) + 1
        for t, l in open_pairs:
            if per_track[t] == 1 and per_lyric[l] == 1:
                paired[tracks[t][0]] = lyrics[l]
                taken_tracks.add(t)
                taken_lyrics.add(l)
        # an ambiguous pair must not fall through to a weaker, wrong match
        for t, l in open_pairs:
            if per_track[t] > 1 or per_lyric[l] > 1:
                taken_tracks.add(t)
                taken_lyrics.add(l)
    return paired
