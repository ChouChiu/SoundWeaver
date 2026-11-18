"""The outputs as a checklist: tick to produce, select to edit, each with what it will make."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, QObject, QSignalBlocker, QSize, Qt
from PySide6.QtWidgets import QAbstractItemView, QGridLayout, QListWidgetItem, QWidget
from qfluentwidgets import CaptionLabel, IconWidget, StrongBodyLabel, SwitchButton

from soundweaver.core.i18n import t
from soundweaver.ui import fluent, widgets
from soundweaver.ui.feature import FeaturePanel
from soundweaver.ui.scrolling import DirectListWidget


class _Row(QWidget):
    def __init__(self, panel: FeaturePanel) -> None:
        super().__init__()
        self.check = SwitchButton()
        self.check.setOnText("")
        self.check.setOffText("")
        self.check.label.hide()
        self.check.setToolTip(t("switch.enable_disable", name=panel.title))
        # the switch shows no text of its own: say what it turns on
        widgets.name_switch(self.check, panel.title)
        self.check.checkedChanged.connect(panel.set_enabled)
        icon = IconWidget(fluent.icon(panel.icon))
        icon.setFixedSize(20, 20)
        self.title = title = StrongBodyLabel(panel.title)
        self.status = CaptionLabel()
        self.status.setMinimumWidth(1)
        # a batch's status can say why some tracks cannot make the output
        self.status.setWordWrap(True)
        layout = QGridLayout(self)
        layout.setContentsMargins(12, 8, 8, 8)
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
        self.status.setToolTip(text)
        fluent.secondary(self.status, problem)


class OutputList(DirectListWidget):
    """One row per output with a switch, status, and activation to edit its settings."""

    def __init__(self, panels: Sequence[FeaturePanel], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName(t("inputs.outputs_section"))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.rows: list[_Row] = []
        for panel in panels:
            row = _Row(panel)
            item = QListWidgetItem(self)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, panel.title)
            item.setSizeHint(row.sizeHint())
            self.setItemWidget(item, row)
            row.installEventFilter(self)
            self.rows.append(row)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        # The rows cover the viewport, so it never sees the mouse move and the
        # list's own hover (from `entered`) stays on whichever row the cursor
        # last reached through a gap; each row reports its own entry instead.
        if event.type() == QEvent.Type.Enter and watched in self.rows:
            self._setHoverRow(self.rows.index(watched))
            self.viewport().update()
        return super().eventFilter(watched, event)

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
        self.updateGeometry()

    def sync(self, panels: Sequence[FeaturePanel]) -> None:
        """Show each panel's enabled state without echoing it back."""
        for row, panel in zip(self.rows, panels, strict=True):
            with QSignalBlocker(row.check):
                row.check.setChecked(panel.enabled)

    def set_statuses(self, statuses: Sequence[tuple[str, bool]]) -> None:
        resized = False
        for index, (row, (text, problem)) in enumerate(zip(self.rows, statuses, strict=True)):
            resized |= text != row.status.text()
            row.set_status(text, problem)
            self.item(index).setData(Qt.ItemDataRole.AccessibleDescriptionRole, text)
        if resized:
            self._fit_rows()

    def set_locked(self, locked: bool) -> None:
        for row in self.rows:
            row.check.setEnabled(not locked)

    def sizeHint(self) -> QSize:
        height = sum(self.sizeHintForRow(i) for i in range(self.count())) + 2 * self.frameWidth()
        return QSize(260, height + 4)

    def minimumSizeHint(self) -> QSize:
        return QSize(248, self.sizeHint().height())
