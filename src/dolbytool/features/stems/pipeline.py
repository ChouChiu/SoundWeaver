"""The stem-split pipeline: one decoded master in, the stems asked for out.

The only thing to go on is the mix itself and how its channels relate, so the
channel count decides which stems exist: a discrete surround master carries
its centre channel, a stereo master only implies one.  Only the stems asked
for are rendered, together with any stem that shares one gain with them.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Collection, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.pcm import AudioData, create_pcm_audio, write_wav
from dolbytool.core.speakers import SURROUND_5_1, SpeakerLayout
from dolbytool.core.stems import (
    StemKind,
    StemLayout,
    layout_for,
    layout_stems,
    stem_label,
    stem_path,
)
from dolbytool.core.task import Report
from dolbytool.features.stems.dsp import (
    cancel_centre_from_front,
    copy_channels_into,
    difference,
    downmix_into,
    fit_group,
    height_into,
    lfe_lag,
    lowpass,
    mid_into,
    separate_centre,
    share_centre_with_surrounds,
    side_into,
    surround_into,
)
from dolbytool.features.stems.dsp.matrix import (
    FRONT_CENTER,
    FRONT_LEFT,
    FRONT_RIGHT,
    LOW_FREQUENCY,
)

logger = logging.getLogger(__name__)

# The band the bass and the kick drum live under (a setting, within
# CROSSOVER_RANGE), and where the mono path splits off its presence band.
DEFAULT_CROSSOVER_HZ = 150.0
CROSSOVER_RANGE = (20.0, 20_000.0)
PRESENCE_HZ = 4000.0


@dataclass(slots=True)
class _Writer:
    """Writes each finished stem and moves the bar across the stems asked for."""

    source: Path
    output_dir: Path
    wanted: tuple[StemKind, ...]  # in layout order
    token: CancellationToken
    report: Report
    start: int
    peaks: dict[StemKind, float] = field(default_factory=dict)

    def __contains__(self, kind: StemKind) -> bool:
        return kind in self.wanted

    def any(self, *kinds: StemKind) -> bool:
        return any(k in self.wanted for k in kinds)

    def _value(self, done: float) -> int:
        return min(99, round(self.start + (99 - self.start) * done / len(self.wanted)))

    def rendering(self, kind: StemKind) -> Callable[[float], None]:
        """Announce `kind`; the returned callback follows a long pass that makes it."""
        done, label = len(self.peaks), stem_label(kind)

        def at(fraction: float) -> None:
            self.report(self._value(done + 0.7 * fraction), f"渲染{label}")

        at(0.0)
        return at

    def write(self, kind: StemKind, audio: AudioData) -> None:
        if kind not in self.wanted:
            return
        self.report(self._value(len(self.peaks) + 0.7), f"写入{stem_label(kind)}")
        path = stem_path(self.source, self.output_dir, kind)
        self.peaks[kind] = write_wav(path, audio, self.token)
        logger.info("wrote the %s stem: %s", kind.value, path)


def _surround(
    source: AudioData,
    out: _Writer,
    crossover: float,
    token: CancellationToken,
    speakers: SpeakerLayout,
) -> None:
    """A discrete surround master, decoded channel by channel.

    Nothing here is estimated except the leak of the centre channel into the
    front pair: the ambience, the lead and the sub are separate channels on the
    master, which is why this path is worth taking when a file offers it.  The
    backs, wides and heights a layout has come out as their own stems, and
    every one of them goes into the fold-down.
    """
    frames, rate, depth = source.frames, source.sample_rate, source.bit_depth
    ch = speakers.index

    def pair(left: str) -> tuple[int, int]:
        return ch[left], ch["R" + left[1:] if left[0] == "L" else left[:-1] + "r"]

    def by_side(labels: tuple[str, ...]) -> tuple[tuple[int, ...], tuple[int, ...]]:
        present = [pair(label) for label in labels if label in ch]
        return tuple(a for a, _ in present), tuple(b for _, b in present)

    def buffer(channels: int) -> AudioData:
        return create_pcm_audio(channels, frames, rate, depth)

    bands = (StemKind.DOWNMIX, StemKind.LOW, StemKind.HIGH)
    lag = lfe_lag(source, token) if out.any(StemKind.SURROUND, *bands) else 0

    if StemKind.SURROUND in out:
        # Both start silent, which the spectral pass needs: it adds into what it is given.
        with buffer(1) as shared, buffer(2) as surround:
            share_centre_with_surrounds(
                source, shared, FRONT_CENTER, pair("Ls"), token,
                out.rendering(StemKind.SURROUND),
            )  # fmt: skip
            surround_into(surround, source, shared, by_side(("Ls", "Lb")), token, lag)
            fit_group((surround,), token)
            out.write(StemKind.SURROUND, surround)

    def copy(kind: StemKind, channels: tuple[int, ...]) -> None:
        if kind in out:
            out.rendering(kind)
            with buffer(len(channels)) as audio:
                copy_channels_into(audio, source, channels, token)
                out.write(kind, audio)

    copy(StemKind.CENTER, (FRONT_CENTER,))
    copy(StemKind.LFE, (LOW_FREQUENCY,))
    copy(StemKind.FRONT, (FRONT_LEFT, FRONT_RIGHT))
    if speakers.has("Lb"):
        copy(StemKind.BACK, pair("Lb"))
    if speakers.has("Lw"):
        copy(StemKind.WIDE, pair("Lw"))

    if StemKind.FRONT_NO_CENTER in out:
        with buffer(2) as cleaned:
            cancel_centre_from_front(
                source, cleaned, (FRONT_LEFT, FRONT_RIGHT), FRONT_CENTER, token,
                out.rendering(StemKind.FRONT_NO_CENTER),
            )  # fmt: skip
            out.write(StemKind.FRONT_NO_CENTER, cleaned)

    if StemKind.HEIGHT in out:
        out.rendering(StemKind.HEIGHT)
        with buffer(2) as height:
            height_into(height, source, by_side(("Tfl", "Tsl", "Tbl")), token)
            fit_group((height,), token)
            out.write(StemKind.HEIGHT, height)
    for kind, left in (
        (StemKind.HEIGHT_FRONT, "Tfl"),
        (StemKind.HEIGHT_REAR, "Tbl"),
        (StemKind.HEIGHT_SIDE, "Tsl"),
    ):
        if left in ch:
            copy(kind, pair(left))

    if out.any(*bands):
        # The fold-down and its two bands are one decomposition - the bands
        # add back up to it - so all three are rendered and held under full
        # scale by one shared factor, whichever of them is written.
        out.rendering(next(k for k in bands if k in out))
        with buffer(2) as downmix, buffer(2) as low, buffer(2) as high:
            downmix_into(downmix, source, speakers.sides(), token, lag)
            lowpass(downmix, low, crossover, token)
            difference(high, downmix, low, token)
            fit_group((downmix, low, high), token)
            for kind, audio in zip(bands, (downmix, low, high), strict=True):
                out.write(kind, audio)


def _stereo(source: AudioData, out: _Writer, crossover: float, token: CancellationToken) -> None:
    """A stereo master, where the centre is implied by the two channels rather than carried."""
    stereo = source.stereo()
    frames, rate, depth = stereo.frames, stereo.sample_rate, stereo.bit_depth

    def buffer(channels: int) -> AudioData:
        return create_pcm_audio(channels, frames, rate, depth)

    if out.any(StemKind.CENTER, StemKind.INSTRUMENTAL):
        first = StemKind.CENTER if StemKind.CENTER in out else StemKind.INSTRUMENTAL
        with buffer(1) as centre, buffer(2) as instrumental:
            separate_centre(stereo, centre, instrumental, token, out.rendering(first))
            # The centre and what is left of the mix without it are the two
            # halves of one split, so they carry one gain between them.
            fit_group((centre, instrumental), token)
            out.write(StemKind.CENTER, centre)
            out.write(StemKind.INSTRUMENTAL, instrumental)
    # Both are bounded by the loudest channel of the mix, so neither can pass
    # full scale unless the mix already did.
    for kind, fold in ((StemKind.SIDE, side_into), (StemKind.MID, mid_into)):
        if kind in out:
            out.rendering(kind)
            with buffer(1) as mono:
                fold(mono, stereo, token)
                out.write(kind, mono)
    if out.any(StemKind.LOW, StemKind.HIGH):
        out.rendering(StemKind.LOW if StemKind.LOW in out else StemKind.HIGH)
        with buffer(2) as low, buffer(2) as high:
            lowpass(stereo, low, crossover, token)
            difference(high, stereo, low, token)
            fit_group((low, high), token)
            out.write(StemKind.LOW, low)
            out.write(StemKind.HIGH, high)


def _mono(source: AudioData, out: _Writer, crossover: float, token: CancellationToken) -> None:
    """One channel has no stereo image to read, so it is split by frequency alone.

    Three complementary bands rather than three filters: the presence band is
    the difference of the two low-passes, so the three add back up to the
    recording instead of overlapping around each corner.
    """
    out.rendering(out.wanted[0])
    with ExitStack() as owned:
        presence, low, middle, high = (
            owned.enter_context(
                create_pcm_audio(1, source.frames, source.sample_rate, source.bit_depth)
            )
            for _ in range(4)
        )
        lowpass(source, presence, PRESENCE_HZ, token)
        lowpass(source, low, crossover, token)
        difference(middle, presence, low, token)
        difference(high, source, presence, token)
        fit_group((low, middle, high), token)
        for kind, audio in (
            (StemKind.LOW, low),
            (StemKind.MID_BAND, middle),
            (StemKind.HIGH, high),
        ):
            out.write(kind, audio)


_SPLITTERS = {StemLayout.STEREO: _stereo, StemLayout.MONO: _mono}


def split(
    audio: AudioData,
    source: Path,
    output_dir: Path,
    kinds: Collection[StemKind],
    crossover_hz: float,
    token: CancellationToken,
    report: Report,
    start: int = 0,
    speakers: SpeakerLayout | None = None,
) -> dict[StemKind, float]:
    """Write the stems in `kinds` that `audio`'s layout has, named after `source`.

    `speakers` names the channels of a surround master (a render's or the
    file's); one it does not fit is read as 5.1 from its first six.  Progress
    runs from `start` to 99; returns each written stem's peak.
    """
    layout = layout_for(audio.channels)
    if layout is StemLayout.SURROUND and (
        speakers is None or len(speakers.channels) != audio.channels
    ):
        speakers = SURROUND_5_1
    if layout is not StemLayout.SURROUND:
        speakers = None
    wanted = tuple(k for k in layout_stems(layout, speakers) if k in kinds)
    if not wanted:
        raise ValueError("所选分轨不属于这种声道布局")
    logger.info("%d channels: splitting as %s into %s", audio.channels, speakers or layout, wanted)
    output_dir.mkdir(parents=True, exist_ok=True)
    out = _Writer(source, output_dir, wanted, token, report, start)
    if speakers is not None:
        _surround(audio, out, crossover_hz, token, speakers)
    else:
        _SPLITTERS[layout](audio, out, crossover_hz, token)
    return out.peaks


def clipped(peaks: dict[StemKind, float]) -> Iterator[str]:
    """A note for every stem that reached full scale and was clipped on write."""
    for kind, peak in peaks.items():
        if peak > 1.0:
            yield f"{stem_label(kind)}峰值 {20 * math.log10(peak):+.2f} dBFS，写入时削波"
