"""Stream and spatial metadata inspector dialog."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QGridLayout, QHeaderView, QTableWidgetItem, QWidget
from qfluentwidgets import BodyLabel, MessageBoxBase, StrongBodyLabel, SubtitleLabel, TableWidget

from soundweaver.core.audio import AudioInfo
from soundweaver.core.i18n import t
from soundweaver.features.source.service import stream_channels, stream_properties


class StreamInspectorDialog(MessageBoxBase):
    """Fluent modal inspecting stream properties and ADM / AV3A / 360RA channel tables."""

    def __init__(self, path: Path, info: AudioInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent.window() if parent else None)
        self.widget.setMinimumWidth(580)

        self.viewLayout.addWidget(SubtitleLabel(t("inspector.title"), self.widget))
        file_lbl = BodyLabel(path.name, self.widget)
        file_lbl.setWordWrap(True)
        self.viewLayout.addWidget(file_lbl)

        rows = stream_properties(info)
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

        table_rows = stream_channels(path, info)
        if table_rows:
            tbl = TableWidget(self.widget)
            tbl.setAccessibleName(t("inspector.channels"))
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

    def _copy(self) -> None:
        QApplication.clipboard().setText(self._summary_text)
        self.yesButton.setText(t("inspector.copied"))
