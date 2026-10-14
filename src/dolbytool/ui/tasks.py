"""The run's steps as a list: state, progress and what each one reported."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHeaderView, QTreeWidgetItem, QVBoxLayout, QWidget
from qfluentwidgets import ProgressBar

from dolbytool.core.i18n import t
from dolbytool.core.task import Step
from dolbytool.ui import fluent, widgets
from dolbytool.ui.worker import Outcome

_ICONS = {
    "queued": "appointment-soon",
    "running": "media-playback-start",
    Outcome.DONE: "dialog-ok-apply",
    Outcome.FAILED: "dialog-error",
    Outcome.CANCELLED: "process-stop",
    Outcome.SKIPPED: "media-skip-forward",
}


def _state_text(outcome: Outcome) -> str:
    return {
        Outcome.DONE: t("status.done"),
        Outcome.FAILED: t("status.failed"),
        Outcome.CANCELLED: t("status.cancelled"),
        Outcome.SKIPPED: t("status.skipped"),
    }[outcome]


def _bar_cell(bar: ProgressBar) -> QWidget:
    """The thin Fluent bar centred in its cell, which an item widget would fill from the top."""
    cell = QWidget()
    layout = QVBoxLayout(cell)
    layout.setContentsMargins(6, 0, 10, 0)
    layout.addWidget(bar)
    return cell


class TaskView(widgets.HintTree):
    """One row per step; a finished row opens its output on double click."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(t("tasks.hint_empty"), parent)
        self.setColumnCount(3)
        self.setHeaderLabels([t("tasks.col_task"), t("tasks.col_progress"), t("tasks.col_status")])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        header = self.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 320)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 160)
        self.itemDoubleClicked.connect(self._open)
        self._bars: list[ProgressBar] = []
        self._values: list[int] = []
        self._targets: list[tuple[Path, ...]] = []
        self._errors: dict[int, str] = {}

    def set_steps(self, steps: list[Step]) -> None:
        self.clear()
        self._bars, self._values, self._errors = [], [0] * len(steps), {}
        self._targets = [step.targets for step in steps]
        for step in steps:
            item = QTreeWidgetItem([step.title, "", t("status.waiting")])
            item.setIcon(0, fluent.qicon(_ICONS["queued"]))
            self.addTopLevelItem(item)
            bar = ProgressBar(useAni=False)
            bar.setRange(0, 100)
            self.setItemWidget(item, 1, _bar_cell(bar))
            self._bars.append(bar)

    def overall(self) -> int:
        return round(sum(self._values) / len(self._values)) if self._values else 0

    def started(self, index: int) -> None:
        item = self.topLevelItem(index)
        item.setIcon(0, fluent.qicon(_ICONS["running"]))
        item.setText(2, t("status.running"))
        self.scrollToItem(item)

    def progress(self, index: int, value: int, message: str) -> None:
        self._values[index] = value
        self._bars[index].setValue(value)
        self.topLevelItem(index).setText(2, message)

    def finished(self, index: int, outcome: str, note: str, seconds: float) -> None:
        item = self.topLevelItem(index)
        item.setIcon(0, fluent.qicon(_ICONS[outcome]))
        state = _state_text(Outcome(outcome))
        if outcome == Outcome.DONE:
            self._values[index] = 100
            self._bars[index].setValue(100)
            state += t("status.elapsed_seconds", seconds=seconds)
            if self._targets[index]:
                item.setToolTip(0, t("status.double_click_to_open"))
        elif outcome != Outcome.SKIPPED:
            self._values[index] = 100
        if outcome == Outcome.FAILED:
            self._errors[index] = note
            self._bars[index].error()
            item.setForeground(2, fluent.negative())
        elif outcome == Outcome.CANCELLED:
            self._bars[index].pause()
        item.setText(2, f"{state}：{note}" if note else state)
        item.setToolTip(2, note)

    def failures(self) -> list[str]:
        """Title and error of every failed step."""
        return [
            f"{self.topLevelItem(i).text(0)}：{error}" for i, error in sorted(self._errors.items())
        ]

    def _open(self, item: QTreeWidgetItem, _column: int) -> None:
        index = self.indexOfTopLevelItem(item)
        existing = [t for t in self._targets[index] if t.exists()]
        if existing:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(existing[0])))

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), 140)
