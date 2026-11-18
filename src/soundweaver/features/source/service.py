"""Read the source metadata shown in the stream inspector."""

from __future__ import annotations

import logging
from contextlib import closing
from pathlib import Path

from soundweaver.core.atmos.adm_reader import PreparedADM
from soundweaver.core.audio import AudioInfo
from soundweaver.core.codecs import mpegh
from soundweaver.core.i18n import t

logger = logging.getLogger(__name__)


def stream_properties(info: AudioInfo) -> list[tuple[str, str]]:
    """Build the localized summary shared by the inspector and clipboard."""
    mins, secs = int(info.duration // 60), int(info.duration % 60)
    rows: list[tuple[str, str]] = [
        (t("inspector.codec"), info.codec or "-"),
        (t("inspector.channels"), str(info.channels)),
        (t("inspector.sample_rate"), f"{info.sample_rate} Hz" if info.sample_rate else "-"),
        (t("inspector.duration"), f"{mins}:{secs:02d} ({info.duration:.2f} s)"),
        (
            t("inspector.spatial"),
            t("inspector.yes") if (info.atmos or info.is_mpegh) else t("inspector.no"),
        ),
    ]
    if info.speakers is not None:
        rows.append((t("inspector.layout"), str(info.speakers)))
    if info.hoa_order > 0:
        rows.append((t("inspector.hoa_order"), f"Order {info.hoa_order} ({info.channels} ACN)"))
    if info.bed_channels:
        rows.append((t("inspector.bed_channels"), ", ".join(info.bed_channels)))
    if info.object_count > 0 or info.adm_bwf:
        rows.append((t("inspector.objects"), str(info.object_count)))
    tags = ", ".join(filter(None, (info.title, info.artist, info.album)))
    if tags:
        rows.append((t("inspector.tags"), tags))
    return rows


def stream_channels(path: Path, info: AudioInfo) -> list[tuple[str, str, str, str]]:
    """Build the best available channel table for the identified stream."""
    if info.adm_bwf:
        try:
            with closing(PreparedADM(path)) as adm:
                return [
                    (
                        str(tr.channel_index + 1),
                        t("inspector.type_object")
                        if tr.is_object
                        else (t("inspector.type_lfe") if tr.is_lfe else t("inspector.type_bed")),
                        tr.name or tr.speaker_label or "-",
                        str(len(tr.blocks)),
                    )
                    for tr in adm.tracks
                ]
        except (OSError, ValueError) as exc:
            logger.warning("Failed to inspect ADM track table in %s: %s", path, exc)
            return []
    if info.is_av3a:
        rows: list[tuple[str, str, str, str]] = []
        for idx, label in enumerate(info.bed_channels):
            kind = t("inspector.type_lfe") if label == "LFE" else t("inspector.type_bed")
            rows.append((str(idx + 1), kind, label, "-"))
        base = len(info.bed_channels)
        for oi in range(info.object_count):
            rows.append((str(base + oi + 1), t("inspector.type_object"), f"Object {oi + 1}", "-"))
        return rows
    if info.is_mpegh:
        layout = mpegh.cicp_layout(13)
        return [
            (
                str(idx + 1),
                t("inspector.type_lfe") if sp.is_lfe else t("inspector.type_bed"),
                f"{sp.name} (az {sp.azimuth_deg:g}°, el {sp.elevation_deg:g}°)",
                "-",
            )
            for idx, sp in enumerate(layout.speakers)
        ]
    return []
