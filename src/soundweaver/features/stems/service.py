"""Stem split: decode the source (Dolby Atmos to the chosen layout), then split it by channel layout."""

from __future__ import annotations

from collections.abc import Collection
from pathlib import Path

from soundweaver.core.atmos import PreparedADM, decode_atmos, is_adm_bwf
from soundweaver.core.audio import load_audio
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.i18n import t
from soundweaver.core.pcm import AudioData, write_wav
from soundweaver.core.speakers import DEFAULT_ATMOS, SpeakerLayout
from soundweaver.core.stems import StemKind, stem_label, stem_path
from soundweaver.core.task import Report, Step
from soundweaver.features.stems.pipeline import clipped, split

# Share of the bar the decode takes: the Atmos rebuild is about as long as the split.
_ATMOS_DECODE = 50
_DECODE = 5


def adm_target(output_dir: Path, source: Path, *, remove_bed_placeholders: bool = False) -> Path:
    suffix = "_adm_compact.wav" if remove_bed_placeholders else "_adm.wav"
    return output_dir / f"{source.stem}{suffix}"


def multichannel_target(
    output_dir: Path, source: Path, layout: SpeakerLayout | None, fmt: str
) -> Path:
    tag = layout.name if layout else "multichannel"
    return output_dir / f"{source.stem}_{tag}.{fmt}"


def mono_channel_target(output_dir: Path, source: Path, channel_name: str) -> Path:
    return output_dir / f"{source.stem}_ch_{channel_name}.wav"


def binaural_target(output_dir: Path, source: Path) -> Path:
    return output_dir / f"{source.stem}_binaural.wav"


def av3a_target(output_dir: Path, source: Path) -> Path:
    return output_dir / f"{source.stem}.av3a"


def split_stems(
    source: Path,
    output_dir: Path,
    kinds: Collection[StemKind],
    crossover_hz: float,
    *,
    atmos: bool,
    adm: Path | None,
    speakers: SpeakerLayout | None,
    remove_bed_placeholders: bool = False,
    multichannel: str = "none",
    mono_channels: tuple[str, ...] = (),
    cicp: int = 13,
    binaural: bool = False,
    av3a_encode: bool = False,
    hoa_order: int = 0,
    token: CancellationToken,
    report: Report,
) -> list[str]:
    """Write stems, ADM BWF, multichannel, mono, stereo binaural HRTF, and/or `.av3a` encode."""
    need_pcm = (
        bool(kinds)
        or multichannel != "none"
        or bool(mono_channels)
        or binaural
        or av3a_encode
    )
    if atmos and is_adm_bwf(source):
        if binaural and not bool(kinds) and multichannel == "none" and not mono_channels and not av3a_encode:
            report(10, t("stems.writing", label="Binaural"))
            av3a.render_binaural_file(source, binaural_target(output_dir, source), token=token)
            return []
        if not need_pcm:
            return []
        prepared = PreparedADM(source)
        try:
            audio = prepared.render_to_layout(
                speakers or DEFAULT_ATMOS,
                token,
                lambda f: report(round(f * _ATMOS_DECODE), t("step.decode_atmos_progress", percent=f)),
            )
        finally:
            prepared.close()
        start = _ATMOS_DECODE
    elif atmos and not av3a.is_av3a_file(source):
        decoded = decode_atmos(
            source, token,
            lambda f: report(round(f * _ATMOS_DECODE), t("step.decode_atmos_progress", percent=f)),
            layout=speakers or DEFAULT_ATMOS, speakers=need_pcm, adm=adm,
            remove_bed_placeholders=remove_bed_placeholders,
        )  # fmt: skip
        audio, start = decoded.speakers, _ATMOS_DECODE
    elif mpegh.is_mpegh_file(source):
        if not need_pcm:
            return []
        report(0, t("step.decode_audio"))
        audio, start = mpegh.decode_mpegh(source, token, cicp=cicp), _DECODE
    else:
        if adm is not None and not atmos:
            raise ValueError(t("error.adm_needs_atmos"))
        if not need_pcm:
            return []
        report(0, t("step.decode_audio"))
        audio, start = load_audio(source, token), _DECODE
    if audio is None:
        return []
    with audio:
        peaks = (
            split(audio, source, output_dir, kinds, crossover_hz, token, report, start, speakers)
            if kinds
            else {}
        )
        if multichannel in ("wav", "caf"):
            report(max(start, 95), t("stems.writing", label=multichannel.upper()))
            write_wav(multichannel_target(output_dir, source, speakers, multichannel), audio, token)
        if mono_channels and speakers is not None:
            for ch_name in mono_channels:
                if ch_name not in speakers.index:
                    continue
                ch_idx = speakers.index[ch_name]
                report(max(start, 95), t("stems.writing", label=ch_name))
                mono = AudioData(
                    audio.samples[ch_idx : ch_idx + 1],
                    audio.sample_rate,
                    audio.bit_depth,
                )
                write_wav(mono_channel_target(output_dir, source, ch_name), mono, token)
        if binaural:
            report(max(start, 95), t("stems.writing", label="Binaural"))
            if adm is not None and adm.is_file():
                av3a.render_binaural_file(adm, binaural_target(output_dir, source), token=token)
            elif is_adm_bwf(source):
                av3a.render_binaural_file(source, binaural_target(output_dir, source), token=token)
            else:
                bin_hoa = hoa_order
                spk_names: list[str] | None = None
                if mpegh.is_mpegh_file(source):
                    spk_names = list(mpegh.cicp_layout(cicp).binaural_names[:audio.channels])
                elif av3a.is_av3a_file(source) and bin_hoa == 0:
                    av_info = av3a.probe_av3a(source, quick=True)
                    if av_info.hoa_order > 0:
                        bin_hoa = av_info.hoa_order
                    elif av_info.channel_order and av_info.object_count == 0:
                        spk_names = [c.strip() for c in av_info.channel_order.split(",") if c.strip()]
                elif speakers is not None:
                    spk_names = list(speakers.channels)
                bin_audio = av3a.render_binaural_audio(
                    audio,
                    speakers=None if bin_hoa > 0 else spk_names,
                    hoa_order=bin_hoa,
                    token=token,
                )
                with bin_audio:
                    write_wav(binaural_target(output_dir, source), bin_audio, token)
        if av3a_encode:
            report(max(start, 95), t("stems.writing", label="Audio Vivid (AV3A)"))
            av3a.encode_av3a(
                audio,
                av3a_target(output_dir, source),
                hoa_order=hoa_order,
                token=token,
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
    remove_bed_placeholders: bool = False,
    multichannel: str = "none",
    mono_channels: tuple[str, ...] = (),
    cicp: int = 13,
    binaural: bool = False,
    av3a_encode: bool = False,
    hoa_order: int = 0,
) -> Step:
    """Write stems and optional ADM / multichannel / mono / Binaural / AV3A outputs into `output_dir`."""

    def run(token: CancellationToken, report: Report) -> str:
        notes = split_stems(
            source,
            output_dir,
            kinds,
            crossover_hz,
            atmos=atmos,
            adm=adm,
            speakers=speakers,
            remove_bed_placeholders=remove_bed_placeholders,
            multichannel=multichannel,
            mono_channels=mono_channels,
            cicp=cicp,
            binaural=binaural,
            av3a_encode=av3a_encode,
            hoa_order=hoa_order,
            token=token,
            report=report,
        )
        names = [stem_label(k) for k in kinds]
        if adm is not None:
            names.append("ADM BWF")
        if multichannel in ("wav", "caf"):
            names.append(f"{speakers.name if speakers else 'Multi'} {multichannel.upper()}")
        if mono_channels:
            names.append(t("stems.mono_channels_count", count=len(mono_channels)))
        if binaural:
            names.append("Binaural")
        if av3a_encode:
            names.append("AV3A")
        return t("common.clause_sep").join([t("common.list_sep").join(names), *notes])

    targets: list[Path] = [stem_path(source, output_dir, k) for k in kinds]
    if adm is not None:
        targets.append(adm)
    if multichannel in ("wav", "caf"):
        targets.append(multichannel_target(output_dir, source, speakers, multichannel))
    if mono_channels:
        targets.extend(mono_channel_target(output_dir, source, ch) for ch in mono_channels)
    if binaural:
        targets.append(binaural_target(output_dir, source))
    if av3a_encode:
        targets.append(av3a_target(output_dir, source))
    return Step(t("step.stems_split"), run, tuple(targets))
