"""The run's steps as a list: state, progress and what each one reported."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, QUrl
from PySide6.QtGui import QDesktopServices, QIcon
from PySide6.QtWidgets import QHeaderView, QProgressBar, QTreeWidget, QTreeWidgetItem, QWidget

from dolbytool.core.task import Step
from dolbytool.ui.worker import Outcome

_ICONS = {
    "queued": "appointment-soon",
    "running": "media-playback-start",
    Outcome.DONE: "dialog-ok-apply",
    Outcome.FAILED: "dialog-error",
    Outcome.CANCELLED: "process-stop",
    Outcome.SKIPPED: "media-skip-forward",
}
_STATES = {
    Outcome.DONE: "已完成",
    Outcome.FAILED: "失败",
    Outcome.CANCELLED: "已取消",
    Outcome.SKIPPED: "已跳过",
}


class TaskView(QTreeWidget):
    """One row per step; a finished row opens its output on double click."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setColumnCount(3)
        self.setHeaderLabels(["任务", "进度", "状态"])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        header = self.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 160)
        self.itemDoubleClicked.connect(self._open)
        self._bars: list[QProgressBar] = []
        self._values: list[int] = []
        self._targets: list[tuple[Path, ...]] = []
        self._errors: dict[int, str] = {}

    def set_steps(self, steps: list[Step]) -> None:
        self.clear()
        self._bars, self._values, self._errors = [], [0] * len(steps), {}
        self._targets = [step.targets for step in steps]
        for step in steps:
            item = QTreeWidgetItem([step.title, "", "等待中"])
            item.setIcon(0, QIcon.fromTheme(_ICONS["queued"]))
            self.addTopLevelItem(item)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setTextVisible(True)
            bar.setMaximumHeight(18)
            self.setItemWidget(item, 1, bar)
            self._bars.append(bar)

    def overall(self) -> int:
        return round(sum(self._values) / len(self._values)) if self._values else 0

    def started(self, index: int) -> None:
        item = self.topLevelItem(index)
        item.setIcon(0, QIcon.fromTheme(_ICONS["running"]))
        item.setText(2, "运行中")
        self.scrollToItem(item)

    def progress(self, index: int, value: int, message: str) -> None:
        self._values[index] = value
        self._bars[index].setValue(value)
        self.topLevelItem(index).setText(2, message)

    def finished(self, index: int, outcome: str, note: str, seconds: float) -> None:
        item = self.topLevelItem(index)
        item.setIcon(0, QIcon.fromTheme(_ICONS[outcome]))
        state = _STATES[Outcome(outcome)]
        if outcome == Outcome.DONE:
            self._values[index] = 100
            self._bars[index].setValue(100)
            state += f"，耗时 {seconds:.1f} 秒"
            if self._targets[index]:
                item.setToolTip(0, "双击在默认应用中打开输出文件")
        elif outcome != Outcome.SKIPPED:
            self._values[index] = 100
        if outcome == Outcome.FAILED:
            self._errors[index] = note
        item.setText(2, f"{state}：{note}" if note else state)
        item.setToolTip(2, note)

    def failures(self) -> list[str]:
        """Title and error of every failed step."""
        return [f"{self.topLevelItem(i).text(0)}：{error}" for i, error in sorted(self._errors.items())]

    def _open(self, item: QTreeWidgetItem, _column: int) -> None:
        index = self.indexOfTopLevelItem(item)
        existing = [t for t in self._targets[index] if t.exists()]
        if existing:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(existing[0])))

    def sizeHint(self) -> QSize:
        return QSize(super().sizeHint().width(), 140)
