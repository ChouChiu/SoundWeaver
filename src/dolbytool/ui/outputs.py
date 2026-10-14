"""The outputs as a checklist: tick to produce, select to edit, each with what it will make."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QGridLayout, QListWidgetItem, QWidget
from qfluentwidgets import CaptionLabel, IconWidget, ListWidget, StrongBodyLabel, SwitchButton

from dolbytool.core.i18n import t
from dolbytool.ui import fluent
from dolbytool.ui.feature import FeaturePanel


class _Row(QWidget):
    def __init__(self, panel: FeaturePanel) -> None:
        super().__init__()
        self.check = SwitchButton()
        self.check.setOnText("")
        self.check.setOffText("")
        self.check.label.hide()
        self.check.setToolTip(t("switch.enable_disable", name=panel.title))
        self.check.checkedChanged.connect(panel.set_enabled)
        icon = IconWidget(fluent.icon(panel.icon))
        icon.setFixedSize(20, 20)
        self.title = title = StrongBodyLabel(panel.title)
        self.status = CaptionLabel()
        self.status.setMinimumWidth(1)
        # a batch's status can say why some tracks cannot make the output
        self.status.setWordWrap(True)
        layout = QGridLayout(self)
        layout.setContentsMargins(10, 7, 6, 7)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(0)
        layout.addWidget(icon, 0, 0, 2, 1)
        layout.addWidget(title, 0, 1)
        layout.addWidget(self.status, 1, 1)
        layout.addWidget(self.check, 0, 2, 2, 1)
        layout.setColumnStretch(1, 1)

    def height_for(self, width: int) -> int:
        """The row's height at `width`, its status wrapped to the room beside icon and switch."""
        margins = self.layout().contentsMargins()
        spacing = 2 * self.layout().horizontalSpacing()
        room = width - margins.left() - margins.right() - spacing - 20
        room -= self.check.sizeHint().width()
        # a wrapping label's own size hint assumes its minimum width, one pixel: far too tall
        line = self.status.fontMetrics().height()
        status = self.status.heightForWidth(max(room, 1)) if self.status.text() else line
        text = self.title.sizeHint().height() + max(status, line)
        return max(text, self.check.sizeHint().height()) + margins.top() + margins.bottom()

    def set_status(self, text: str, problem: bool) -> None:
        self.status.setText(text)
        # only show a tooltip if text is long enough to wrap/truncate
        self.status.setToolTip(text if len(text) > 20 else "")
        fluent.secondary(self.status, problem)


class OutputList(ListWidget):
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
        self._fit_rows()

    def _fit_rows(self) -> None:
        width = self.viewport().width()
        for i, row in enumerate(self.rows):
            self.item(i).setSizeHint(QSize(width, row.height_for(width)))
        # a new size hint alone leaves the rows, and the widgets on them, where they were
        self.doItemsLayout()
        for i, row in enumerate(self.rows):
            row.setGeometry(self.visualItemRect(self.item(i)))

    def sync(self, panels: Sequence[FeaturePanel]) -> None:
        """Show each panel's enabled state without echoing it back."""
        for row, panel in zip(self.rows, panels, strict=True):
            row.check.blockSignals(True)
            row.check.setChecked(panel.enabled)
            row.check.blockSignals(False)

    def set_status(self, index: int, text: str, problem: bool = False) -> None:
        row = self.rows[index]
        resized = text != row.status.text()
        row.set_status(text, problem)
        if resized:
            # a wrapped status may take another line, or give one back
            self._fit_rows()

    def set_locked(self, locked: bool) -> None:
        for row in self.rows:
            row.check.setEnabled(not locked)

    def sizeHint(self) -> QSize:
        height = sum(self.sizeHintForRow(i) for i in range(self.count())) + 2 * self.frameWidth()
        return QSize(260, height + 4)
