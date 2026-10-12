"""Stem split: decode the source (Dolby Atmos to the chosen layout), then split it by channel layout."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from dolbytool.core.atmos import decode_atmos
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.pcm import load_audio
from dolbytool.core.speakers import DEFAULT_ATMOS, SpeakerLayout
from dolbytool.core.stems import StemKind, stem_label, stem_path
from dolbytool.core.task import Report, Step
from dolbytool.features.stems.pipeline import clipped, split

# Share of the bar the decode takes: the Atmos rebuild is about as long as the split.
_ATMOS_DECODE = 50
_DECODE = 5


def adm_target(output_dir: Path, source: Path) -> Path:
    return output_dir / f"{source.stem}_adm.wav"


def split_stems(
    source: Path,
    output_dir: Path,
    kinds: Collection[StemKind],
    crossover_hz: float,
    *,
    atmos: bool,
    adm: Path | None,
    speakers: SpeakerLayout | None,
    token: CancellationToken,
    report: Report,
) -> list[str]:
    """Write `kinds` (and, for Dolby Atmos, the ADM BWF to `adm`); returns notes for the log."""
    if atmos:
        decoded = decode_atmos(
            source, token,
            lambda f: report(round(f * _ATMOS_DECODE), f"解码全景声 {f:.0%}"),
            layout=speakers or DEFAULT_ATMOS, speakers=bool(kinds), adm=adm,
        )  # fmt: skip
        audio, start = decoded.speakers, _ATMOS_DECODE
    else:
        if adm is not None:
            raise ValueError("ADM BWF 只能从杜比全景声音轨生成")
        report(0, "解码音频")
        audio, start = load_audio(source, token), _DECODE
    if audio is None:
        return []
    with audio:
        peaks = split(
            audio, source, output_dir, kinds, crossover_hz, token, report, start, speakers
        )
    return list(clipped(peaks))


def split_step(
    source: Path,
    output_dir: Path,
    kinds: list[StemKind],
    crossover_hz: float,
    *,
    atmos: bool,
    adm: Path | None,
    speakers: SpeakerLayout | None,
) -> Step:
    """Write exactly `kinds` (and the ADM BWF if asked) into `output_dir`."""

    def run(token: CancellationToken, report: Report) -> str:
        notes = split_stems(
            source,
            output_dir,
            kinds,
            crossover_hz,
            atmos=atmos,
            adm=adm,
            speakers=speakers,
            token=token,
            report=report,
        )
        names = [stem_label(k) for k in kinds] + (["ADM BWF"] if adm is not None else [])
        return "、".join(names) + "".join(f"；{note}" for note in notes)

    targets = tuple(stem_path(source, output_dir, k) for k in kinds) + ((adm,) if adm else ())
    return Step("分轨", run, targets)
