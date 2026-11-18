"""Execute feature steps with cooperative cancellation, without Qt."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from enum import StrEnum
from itertools import groupby

from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.i18n import t
from soundweaver.core.task import Step

logger = logging.getLogger(__name__)

# Parallel steps running at once; each one is an encoder that keeps a core or a GPU session busy.
PARALLEL_STEPS = 3


class Outcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


def run_steps(
    steps: Sequence[Step],
    token: CancellationToken,
    *,
    started: Callable[[int], None],
    progress: Callable[[int, int, str], None],
    finished: Callable[[int, Outcome, str, float], None],
) -> Outcome:
    """Run in order, allowing adjacent parallel steps to share a bounded pool."""

    def run(pair: tuple[int, Step]) -> Outcome:
        return _run_step(*pair, token, started, progress, finished)

    failed = False
    for parallel, group in groupby(enumerate(steps), key=lambda pair: pair[1].parallel):
        batch = list(group)
        if parallel and len(batch) > 1 and not token.cancelled:
            with ThreadPoolExecutor(min(PARALLEL_STEPS, len(batch))) as pool:
                outcomes = list(pool.map(run, batch))
        else:
            outcomes = [run(pair) for pair in batch]
        failed |= Outcome.FAILED in outcomes
    if token.cancelled:
        return Outcome.CANCELLED
    return Outcome.FAILED if failed else Outcome.DONE


def _run_step(
    index: int,
    step: Step,
    token: CancellationToken,
    started_event: Callable[[int], None],
    progress_event: Callable[[int, int, str], None],
    finished_event: Callable[[int, Outcome, str, float], None],
) -> Outcome:
    if token.cancelled:
        finished_event(index, Outcome.SKIPPED, "", 0.0)
        return Outcome.SKIPPED
    started_event(index)
    logger.info(t("log.step_started", title=step.title))
    started = time.monotonic()

    def report(value: int, message: str) -> None:
        progress_event(index, max(0, min(100, int(value))), message)

    report(0, t("status.preparing"))
    try:
        note = step.run(token, report) or ""
    except ProcessingCancelled:
        logger.info(t("log.step_cancelled", title=step.title))
        finished_event(index, Outcome.CANCELLED, "", time.monotonic() - started)
        return Outcome.CANCELLED
    except Exception as error:  # surface every failure in the log, keep the app alive
        message = str(error) or type(error).__name__
        logger.exception(t("log.step_failed", title=step.title, message=message))
        finished_event(index, Outcome.FAILED, message, time.monotonic() - started)
        return Outcome.FAILED
    seconds = time.monotonic() - started
    done = t("log.step_done", title=step.title, seconds=seconds)
    logger.info(t("common.labeled", label=done, text=note) if note else done)
    finished_event(index, Outcome.DONE, note, seconds)
    return Outcome.DONE
