"""Cooperative cancellation shared by every long-running step."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event


class ProcessingCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class CancellationToken:
    _event: Event = field(default_factory=Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Block up to `timeout` seconds; True once cancelled."""
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ProcessingCancelled
