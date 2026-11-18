"""Run a slow call off the UI thread and hand its result back on it."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Relay(QObject):
    # generation, result
    done = Signal(int, object)


class Latest(QObject):
    """Runs calls in the background; only the newest call's result is delivered.

    A call that raises delivers its exception as the result.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._generation = 0
        self._relay = _Relay(self)
        self._relay.done.connect(self._deliver)
        self._callbacks: dict[int, Callable[[Any], None]] = {}

    def run(self, call: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        self._generation += 1
        generation, relay = self._generation, self._relay
        self._callbacks = {generation: on_done}
        QThreadPool.globalInstance().start(_Task(call, lambda r: relay.done.emit(generation, r)))

    def cancel(self) -> None:
        """Drop the result of whatever is still running."""
        self._generation += 1
        self._callbacks = {}

    @property
    def busy(self) -> bool:
        return bool(self._callbacks)

    def _deliver(self, generation: int, result: object) -> None:
        callback = self._callbacks.pop(generation, None)
        if callback is not None:
            callback(result)


class Each(QObject):
    """Runs calls in the background and delivers every result, unless dropped.

    `drop()` forgets every call still running, e.g. when the list they fill is cleared.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._generation = 0
        self._pending = 0
        self._relay = _Relay(self)
        self._relay.done.connect(self._deliver)

    def run(self, call: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        generation, relay = self._generation, self._relay
        self._pending += 1
        task = _Task(call, lambda r, cb=on_done: relay.done.emit(generation, (cb, r)))
        QThreadPool.globalInstance().start(task)

    def drop(self) -> None:
        self._generation += 1
        self._pending = 0

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def _deliver(self, generation: int, payload: object) -> None:
        if generation != self._generation:
            return
        callback, result = payload  # type: ignore[misc]
        self._pending -= 1
        callback(result)


class _Task(QRunnable):
    def __init__(self, call: Callable[[], Any], deliver: Callable[[Any], None]) -> None:
        super().__init__()
        self._call, self._deliver = call, deliver

    def run(self) -> None:
        try:
            result = self._call()
        except Exception as error:  # noqa: BLE001 - handed to the caller, who reports it
            result = error
        self._deliver(result)
