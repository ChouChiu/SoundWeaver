"""The outputs as a checklist: tick to produce, select to edit, each with what it will make."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QWidget,
)

from dolbytool.ui.feature import FeaturePanel

# Breeze's negative text colour, readable on light and dark schemes alike
_NEGATIVE = QColor("#da4453")


class _Row(QWidget):
    def __init__(self, panel: FeaturePanel) -> None:
        super().__init__()
        self.check = QCheckBox()
        self.check.setToolTip(f"启用/禁用 {panel.title}")
        self.check.toggled.connect(panel.set_enabled)
        icon = QLabel()
        icon.setPixmap(QIcon.fromTheme(panel.icon).pixmap(QSize(22, 22)))
        title = QLabel(panel.title)
        bold = QFont(title.font())
        bold.setBold(True)
        title.setFont(bold)
        self.status = QLabel()
        self.status.setMinimumWidth(1)
        small = QFont(self.status.font())
        small.setPointSizeF(small.pointSizeF() * 0.9)
        self.status.setFont(small)
        layout = QGridLayout(self)
        layout.setContentsMargins(6, 5, 6, 5)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(0)
        layout.addWidget(self.check, 0, 0, 2, 1)
        layout.addWidget(icon, 0, 1, 2, 1)
        layout.addWidget(title, 0, 2)
        layout.addWidget(self.status, 1, 2)
        layout.setColumnStretch(2, 1)

    def set_status(self, text: str, problem: bool) -> None:
        self.status.setText(text)
        color = _NEGATIVE if problem else self.palette().color(QPalette.ColorRole.PlaceholderText)
        self.status.setStyleSheet(f"color: {color.name()}")


class OutputList(QListWidget):
    """One row per output panel; the current row is the page shown beside it."""

    selected = Signal(int)

    def __init__(self, panels: Sequence[FeaturePanel], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.rows: list[_Row] = []
        for panel in panels:
            row = _Row(panel)
            item = QListWidgetItem(self)
            item.setSizeHint(row.sizeHint())
            self.setItemWidget(item, row)
            self.rows.append(row)
        self.currentRowChanged.connect(self.selected)

    def resizeEvent(self, event) -> None:
        # list mode keeps each item at its hinted width; stretch the rows to the viewport
        super().resizeEvent(event)
        width = self.viewport().width()
        for i, row in enumerate(self.rows):
            self.item(i).setSizeHint(QSize(width, row.sizeHint().height()))

    def sync(self, panels: Sequence[FeaturePanel]) -> None:
        """Show each panel's enabled state without echoing it back."""
        for row, panel in zip(self.rows, panels, strict=True):
            row.check.blockSignals(True)
            row.check.setChecked(panel.enabled)
            row.check.blockSignals(False)

    def set_status(self, index: int, text: str, problem: bool = False) -> None:
        self.rows[index].set_status(text, problem)

    def set_locked(self, locked: bool) -> None:
        for row in self.rows:
            row.check.setEnabled(not locked)

    def sizeHint(self) -> QSize:
        height = sum(self.sizeHintForRow(i) for i in range(self.count())) + 2 * self.frameWidth()
        return QSize(260, height + 4)
