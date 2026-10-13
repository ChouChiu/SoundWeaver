"""The unit of work a feature hands to the runner: a titled, cancellable step."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dolbytool.core.cancel import CancellationToken

# Progress of the current step: 0..100 and a status line.
Report = Callable[[int, str], None]
# A step receives the token and a progress callback and returns a log note.
StepFn = Callable[[CancellationToken, Report], str | None]


@dataclass(frozen=True, slots=True)
class Step:
    title: str
    run: StepFn
    # files the step writes, checked for overwrites before the run starts
    targets: tuple[Path, ...] = ()
    # neighbouring parallel steps may run side by side; the rest run alone, in order
    parallel: bool = False


def percent(report: Report, message: str) -> Callable[[float], None]:
    """Adapt a 0..1 fraction callback onto a step's 0..100 report."""
    return lambda fraction: report(round(fraction * 100), message)


class Progress:
    """A report fed by 0..1 fractions under `message`, and by status lines that keep the bar where it is."""

    def __init__(self, report: Report, message: str) -> None:
        self._report = report
        self.message = message
        self.value = 0

    def fraction(self, fraction: float) -> None:
        self.value = round(fraction * 100)
        self._report(self.value, self.message)

    def status(self, text: str) -> None:
        self._report(self.value, text)
