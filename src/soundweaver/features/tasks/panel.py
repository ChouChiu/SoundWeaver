"""Task history, run lifecycle, and the in-app log."""

from __future__ import annotations

import logging

from PySide6.QtCore import QByteArray, Qt, Signal, Slot
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget
from qfluentwidgets import PlainTextEdit, TitleLabel

from soundweaver.core.i18n import t
from soundweaver.core.task import Step
from soundweaver.features.tasks.view import TaskView
from soundweaver.features.tasks.worker import Worker
from soundweaver.ui import widgets
from soundweaver.ui.logs import LogPanelHandler
from soundweaver.ui.scrolling import use_direct_wheel


class TasksPage(QWidget):
    progress = Signal(int, str, str)
    finished = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._worker: Worker | None = None
        self._tasks = TaskView()
        self._tasks.setAccessibleName(t("tasks.section_tasks"))
        log = PlainTextEdit()
        use_direct_wheel(log, pixel_scroll=False)
        log.setReadOnly(True)
        log.setAccessibleName(t("tasks.section_logs"))
        log.setPlaceholderText(t("tasks.log_placeholder"))
        log.setMaximumBlockCount(5000)
        self._log_handler = LogPanelHandler()
        self._log_handler.lines.line.connect(log.appendPlainText)
        logging.getLogger().addHandler(self._log_handler)
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.addWidget(widgets.section(t("tasks.section_tasks"), self._tasks))
        self._splitter.addWidget(widgets.section(t("tasks.section_logs"), log))
        self._splitter.setChildrenCollapsible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.SECTION_SPACING)
        layout.addWidget(TitleLabel(t("tasks.title")))
        layout.addWidget(self._splitter, 1)

    @property
    def running(self) -> bool:
        return self._worker is not None

    def start(self, steps: list[Step]) -> None:
        assert not self.running
        self._tasks.set_steps(steps)
        worker = self._worker = Worker(steps, self)
        worker.step_started.connect(self._tasks.started)
        worker.step_progress.connect(self._on_progress)
        worker.step_finished.connect(self._on_step_finished)
        worker.finished.connect(self._on_finished)
        worker.start()

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    @Slot(int, int, str)
    def _on_progress(self, index: int, value: int, message: str) -> None:
        if self.running:
            self._tasks.progress(index, value, message)
            self.progress.emit(self._tasks.overall(), self._tasks.title(index), message)

    @Slot(int, str, str, float)
    def _on_step_finished(self, index: int, outcome: str, note: str, seconds: float) -> None:
        if self.running:
            self._tasks.finished(index, outcome, note, seconds)
            self.progress.emit(self._tasks.overall(), self._tasks.title(index), "")

    @Slot()
    def _on_finished(self) -> None:
        if (worker := self._worker) is not None:
            self._worker = None
            worker.deleteLater()
            self.finished.emit(worker.outcome)

    def failures(self) -> list[str]:
        return self._tasks.failures()

    def save_layout(self) -> QByteArray:
        return self._splitter.saveState()

    def restore_layout(self, state: QByteArray) -> None:
        if not self._splitter.restoreState(state):
            self._splitter.setSizes([500, 200])

    def shutdown(self) -> None:
        if (worker := self._worker) is not None:
            self._worker = None
            worker.cancel()
            worker.wait()
            worker.deleteLater()
        logging.getLogger().removeHandler(self._log_handler)
        self._log_handler.close()
