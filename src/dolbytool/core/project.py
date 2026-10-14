"""The current inputs and the audio tracks derived from them, shared by every feature."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from dolbytool.core.audio import AudioInfo
from dolbytool.core.speakers import DEFAULT_ATMOS, SpeakerLayout
from dolbytool.core.stems import layout_stems, stem_hint, stem_label, stem_path

ORIGINAL = "original"
# the setting holding the Dolby Atmos render layout, shared by every input panel
ATMOS_LAYOUT_KEY = "atmos_layout"


def default_output_dir(audio: Path) -> Path:
    return audio.with_name(f"{audio.stem}_stems")


@dataclass(frozen=True, slots=True)
class Track:
    key: str  # stem kind, or ORIGINAL
    path: Path

    @property
    def is_original(self) -> bool:
        return self.key == ORIGINAL

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def label(self) -> str:
        from dolbytool.core.i18n import t

        return t("audio.original") if self.is_original else stem_label(self.key)

    @property
    def hint(self) -> str:
        from dolbytool.core.i18n import t

        return t("audio.original_hint") if self.is_original else stem_hint(self.key)


def split_speakers(info: AudioInfo, atmos: SpeakerLayout) -> SpeakerLayout | None:
    """The speakers a split works on: the render's for Dolby Atmos, else the file's own."""
    return atmos if info.atmos else info.speakers


def expected_tracks(
    source: Path, output_dir: Path, info: AudioInfo, atmos: SpeakerLayout
) -> tuple[Track, ...]:
    """The source itself, then the stems a split would write, in pipeline order."""
    stems = (
        Track(kind.value, stem_path(source, output_dir, kind))
        for kind in layout_stems(info.layout, split_speakers(info, atmos))
    )
    return (Track(ORIGINAL, source), *stems)


@dataclass(frozen=True, slots=True)
class Project:
    audio: Path | None = None
    cover: Path | None = None
    ttml: Path | None = None
    output_dir: Path | None = None
    # None when there is no audio, it is still being read, or it could not be read
    info: AudioInfo | None = None
    # the source audio first, then every stem its layout splits into
    tracks: tuple[Track, ...] = ()
    probing: bool = False
    # why the audio could not be read
    audio_error: str | None = None
    # what a Dolby Atmos source is rendered to
    atmos_layout: SpeakerLayout = DEFAULT_ATMOS

    @property
    def stems(self) -> tuple[Track, ...]:
        return tuple(t for t in self.tracks if not t.is_original)

    @property
    def speakers(self) -> SpeakerLayout | None:
        """The speakers the stems are split from, None without a surround source."""
        return split_speakers(self.info, self.atmos_layout) if self.info else None

    @property
    def has_audio(self) -> bool:
        return self.info is not None

    def inputs(self) -> tuple[Path, ...]:
        return tuple(p for p in (self.audio, self.cover, self.ttml) if p is not None)


@dataclass(frozen=True, slots=True)
class PlanContext:
    """A project plus what the enabled features ask of each other."""

    project: Project
    # stems some output needs but that are not on disk yet
    required_stems: frozenset[str] = frozenset()
    # titles of the features asking for them
    requested_by: tuple[str, ...] = field(default=())
    # one track of a batch: nothing may be asked of the user, and per-track
    # details (title, cover colours) come from the track, not the panel
    batch: bool = False
    # one track of an album, whose outputs may be made for the album as a whole instead
    album: bool = False
