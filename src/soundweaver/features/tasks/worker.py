"""Forward task execution events from the worker thread to Qt views."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from soundweaver.core.cancel import CancellationToken
from soundweaver.core.runner import Outcome, run_steps
from soundweaver.core.task import Step


class Worker(QThread):
    step_started = Signal(int)
    # step index, 0..100, status line
    step_progress = Signal(int, int, str)
    # step index, outcome, note or error, seconds taken
    step_finished = Signal(int, str, str, float)

    def __init__(self, steps: list[Step], parent=None) -> None:
        super().__init__(parent)
        self.outcome: Outcome
        self.steps = steps
        self.token = CancellationToken()

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        self.outcome = run_steps(
            self.steps,
            self.token,
            started=self.step_started.emit,
            progress=self.step_progress.emit,
            finished=self.step_finished.emit,
        )
