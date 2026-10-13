"""Runs the planned steps off the UI thread.

Steps run in order; a run of neighbouring parallel steps runs side by side.
A failed step is logged and the rest still run (a step that needed its output
fails on its own with a clear message); a cancel stops everything.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from itertools import groupby

from PySide6.QtCore import QThread, Signal

from dolbytool.core.cancel import CancellationToken, ProcessingCancelled
from dolbytool.core.task import Step

logger = logging.getLogger(__name__)

# Parallel steps running at once; each one is an encoder that keeps a core or a GPU session busy.
PARALLEL_STEPS = 3


class Outcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class Worker(QThread):
    step_started = Signal(int)
    # step index, 0..100, status line
    step_progress = Signal(int, int, str)
    # step index, outcome, note or error, seconds taken
    step_finished = Signal(int, str, str, float)
    # the outcome of the whole run: done, failed (some step) or cancelled
    finished_with = Signal(str)

    def __init__(self, steps: list[Step], parent=None) -> None:
        super().__init__(parent)
        self.steps = steps
        self.token = CancellationToken()
        self._failed = False

    def cancel(self) -> None:
        self.token.cancel()

    def run(self) -> None:
        indexed = list(enumerate(self.steps))
        groups = [list(g) for _, g in groupby(indexed, key=lambda pair: pair[1].parallel)]
        for group in groups:
            batches = [group] if group[0][1].parallel else [[pair] for pair in group]
            for batch in batches:
                # after a cancel every remaining step just reports itself skipped
                if len(batch) == 1 or self.token.cancelled:
                    for pair in batch:
                        self._run_step(*pair)
                else:
                    with ThreadPoolExecutor(min(PARALLEL_STEPS, len(batch))) as pool:
                        list(pool.map(lambda pair: self._run_step(*pair), batch))
        if self.token.cancelled:
            self.finished_with.emit(Outcome.CANCELLED)
        else:
            self.finished_with.emit(Outcome.FAILED if self._failed else Outcome.DONE)

    def _run_step(self, index: int, step: Step) -> None:
        if self.token.cancelled:
            self.step_finished.emit(index, Outcome.SKIPPED, "", 0.0)
            return
        self.step_started.emit(index)
        logger.info("开始 %s", step.title)
        started = time.monotonic()

        def report(value: int, message: str) -> None:
            self.step_progress.emit(index, max(0, min(100, int(value))), message)

        report(0, "准备中")
        try:
            note = step.run(self.token, report) or ""
        except ProcessingCancelled:
            logger.info("已取消 %s", step.title)
            self.step_finished.emit(index, Outcome.CANCELLED, "", time.monotonic() - started)
            return
        except Exception as error:  # surface every failure in the log, keep the app alive
            self._failed = True
            message = str(error) or type(error).__name__
            logger.exception("失败 %s：%s", step.title, message)
            self.step_finished.emit(index, Outcome.FAILED, message, time.monotonic() - started)
            return
        seconds = time.monotonic() - started
        logger.info("完成 %s（%.1f 秒）%s", step.title, seconds, f"：{note}" if note else "")
        self.step_finished.emit(index, Outcome.DONE, note, seconds)
