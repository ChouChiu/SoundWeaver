"""Stream and spatial metadata inspector dialog."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QGridLayout, QHeaderView, QTableWidgetItem, QWidget
from qfluentwidgets import BodyLabel, MessageBoxBase, StrongBodyLabel, SubtitleLabel, TableWidget

from soundweaver.core.atmos.adm_reader import PreparedADM, is_adm_bwf
from soundweaver.core.audio import AudioInfo
from soundweaver.core.codecs import mpegh
from soundweaver.core.i18n import t

logger = logging.getLogger(__name__)


class StreamInspectorDialog(MessageBoxBase):
    """Fluent modal inspecting stream properties and ADM / AV3A / 360RA channel tables."""

    def __init__(self, path: Path, info: AudioInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent.window() if parent else None)
        self.widget.setMinimumWidth(580)

        self.viewLayout.addWidget(SubtitleLabel(t("inspector.title"), self.widget))
        file_lbl = BodyLabel(path.name, self.widget)
        file_lbl.setWordWrap(True)
        self.viewLayout.addWidget(file_lbl)

        mins, secs = int(info.duration // 60), int(info.duration % 60)
        rows: list[tuple[str, str]] = [
            (t("inspector.codec"), info.codec or "-"),
            (t("inspector.channels"), str(info.channels)),
            (t("inspector.sample_rate"), f"{info.sample_rate} Hz" if info.sample_rate else "-"),
            (t("inspector.duration"), f"{mins}:{secs:02d} ({info.duration:.2f} s)"),
            (t("inspector.spatial"), t("inspector.yes") if (info.atmos or info.is_mpegh) else t("inspector.no")),
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

        grid = QGridLayout()
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(6)
        for r, (k, v) in enumerate(rows):
            kl = StrongBodyLabel(k, self.widget)
            vl = BodyLabel(v, self.widget)
            vl.setWordWrap(True)
            grid.addWidget(kl, r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(vl, r, 1)
        grid.setColumnStretch(1, 1)
        self.viewLayout.addLayout(grid)

        self._summary_text = "\n".join(f"{k}: {v}" for k, v in rows)

        table_rows = self._collect_track_rows(path, info)
        if table_rows:
            tbl = TableWidget(self.widget)
            tbl.setRowCount(len(table_rows))
            tbl.setColumnCount(4)
            tbl.setHorizontalHeaderLabels(
                [t("inspector.col_ch"), t("inspector.col_type"), t("inspector.col_name"), t("inspector.col_blocks")]
            )
            tbl.verticalHeader().hide()
            tbl.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
            tbl.setMinimumHeight(180)
            tbl.setMaximumHeight(240)
            tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            for idx, (ch_str, kind, name, blocks) in enumerate(table_rows):
                tbl.setItem(idx, 0, QTableWidgetItem(ch_str))
                tbl.setItem(idx, 1, QTableWidgetItem(kind))
                tbl.setItem(idx, 2, QTableWidgetItem(name))
                tbl.setItem(idx, 3, QTableWidgetItem(blocks))
            tbl.resizeColumnsToContents()
            self.viewLayout.addWidget(tbl)

        self.yesButton.setText(t("inspector.copy"))
        self.cancelButton.setText(t("inspector.close"))
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._copy)

    def _collect_track_rows(self, path: Path, info: AudioInfo) -> list[tuple[str, str, str, str]]:
        if info.adm_bwf and is_adm_bwf(path):
            try:
                adm = PreparedADM(path)
                try:
                    return [
                        (
                            str(tr.channel_index + 1),
                            t("inspector.type_object") if tr.is_object else (t("inspector.type_lfe") if tr.is_lfe else t("inspector.type_bed")),
                            tr.name or tr.speaker_label or "-",
                            str(len(tr.blocks)),
                        )
                        for tr in adm.tracks
                    ]
                finally:
                    adm.close()
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

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._summary_text)
        self.yesButton.setText(t("inspector.copied"))
