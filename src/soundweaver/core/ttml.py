"""Shared TTML parsing, title normalization, track-lyric matching, and font CSS helpers."""

from __future__ import annotations

import html
import json
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from functools import lru_cache
from pathlib import Path

from soundweaver.core.paths import file_stamp, natural_key

LYRIC_DIRS = {"lyrics", "lyric", "ttml", "歌词"}
_DISC_DIR = re.compile(r"(disc|disk|cd)\s*\d+", re.IGNORECASE)
_BRACKETS = re.compile(r"[\[(（【{][^\])）】}]*[\])）】}]")
_TRACK_NUMBER = re.compile(r"^\s*(?:\d{1,2}\s*[-.]\s*)?\d{1,3}\s*(?:[-._)]\s*|\s+)(?=\S)")
_SEPARATOR = re.compile(r"\s+[-–—]\s+")
_NOISE = re.compile(r"[\W_]+")
_HEAD_BYTES = 64 * 1024
_META = re.compile(r"<(?:\w+:)?meta\b([^>]*)>", re.IGNORECASE)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')
_TITLE = re.compile(r"<(?:\w+:)?title\b[^>]*>([^<]+)<", re.IGNORECASE)
_TITLE_KEYS = {"musicname", "title"}
_MIN_PARTIAL = 3
_EXACT, _WHOLE, _PART, _CONTAINS = 4, 3, 2, 1


def css_font_list(families: tuple[str, ...]) -> str:
    """Quoted CSS font-family list; defaults to `sans-serif` when empty."""
    names_list = [json.dumps(f.strip()) for f in families if f.strip()]
    return ", ".join(names_list) or "sans-serif"


def _squash(text: str) -> str:
    return _NOISE.sub("", unicodedata.normalize("NFKC", text).casefold())


def without_number(name: str) -> str:
    """`name` without a leading track number like '01. ' or '1-02 '."""
    return _TRACK_NUMBER.sub("", name, count=1) or name


def names(text: str) -> dict[str, int]:
    """Candidate song-title forms for `text`, weighted by reliability."""
    text = unicodedata.normalize("NFKC", text)
    bare = _BRACKETS.sub(" ", text)
    found: dict[str, int] = {}

    def keep(candidate: str, weight: int) -> None:
        if key := _squash(candidate):
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
    return tuple(html.unescape(t_str).strip() for t_str in titles if t_str.strip())


def declared_titles(ttml: Path) -> tuple[str, ...]:
    """Song titles declared in a TTML header (`musicName`, `ttm:title`)."""
    return _declared(ttml, file_stamp(ttml)) if ttml.is_file() else ()


def lyric_files(paths: Iterable[Path]) -> list[Path]:
    """Collect `.ttml` files directly under `paths` or in common lyric subfolders."""
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
    """Nearby `.ttml` files around `audio` (and album root for Disc subfolders)."""
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
    """Pair unambiguous `(audio_path, title_tag)` tracks to `.ttml` lyric files."""
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
        (t_idx, l_idx): s
        for t_idx, tn in enumerate(track_names)
        for l_idx, ln in enumerate(lyric_names)
        if (s := _score(tn, ln))
    }
    paired: dict[Path, Path] = {}
    taken_tracks: set[int] = set()
    taken_lyrics: set[int] = set()
    for level in (_EXACT, _WHOLE, _PART, _CONTAINS):
        open_pairs = [
            (t_idx, l_idx)
            for (t_idx, l_idx), s in scores.items()
            if s == level and t_idx not in taken_tracks and l_idx not in taken_lyrics
        ]
        per_track: dict[int, int] = {}
        per_lyric: dict[int, int] = {}
        for t_idx, l_idx in open_pairs:
            per_track[t_idx] = per_track.get(t_idx, 0) + 1
            per_lyric[l_idx] = per_lyric.get(l_idx, 0) + 1
        for t_idx, l_idx in open_pairs:
            if per_track[t_idx] == 1 and per_lyric[l_idx] == 1:
                paired[tracks[t_idx][0]] = lyrics[l_idx]
                taken_tracks.add(t_idx)
                taken_lyrics.add(l_idx)
        for t_idx, l_idx in open_pairs:
            if per_track[t_idx] > 1 or per_lyric[l_idx] > 1:
                taken_tracks.add(t_idx)
                taken_lyrics.add(l_idx)
    return paired


def _parse_timecode(tc: str | None) -> float:
    if not tc:
        return 0.0
    s = tc.strip()
    if s.endswith("s"):
        try:
            return float(s[:-1])
        except ValueError:
            return 0.0
    parts = s.split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(s)
    except ValueError:
        return 0.0


def parse_ttml_lines(path: Path) -> list[tuple[float, float, str]]:
    """Parse a `.ttml` file into a list of `(begin_seconds, end_seconds, text)` lines."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return []
    lines: list[tuple[float, float, str]] = []
    for elem in root.iter():
        if elem.tag.rsplit("}", 1)[-1] == "p":
            begin = _parse_timecode(elem.attrib.get("begin"))
            end = _parse_timecode(elem.attrib.get("end"))
            text = "".join(elem.itertext()).strip()
            if text:
                lines.append((begin, end, text))
    return lines
