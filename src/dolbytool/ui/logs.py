"""Logging in the Qt application: Qt's own messages, the in-app log and Ctrl+C."""

from __future__ import annotations

import logging
import signal
import socket
from collections.abc import Callable

from PySide6.QtCore import QMessageLogContext, QObject, QSocketNotifier, QtMsgType, Signal
from PySide6.QtCore import qInstallMessageHandler as _install
from PySide6.QtWidgets import QApplication

from dolbytool.core.i18n import t
from dolbytool.core.logs import LogFormatter

_qt = logging.getLogger("qt")
_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def route_qt_messages() -> None:
    """Send qDebug, qWarning and the like through `logging`, in its format."""

    def handle(kind: QtMsgType, context: QMessageLogContext, message: str) -> None:
        _qt.log(_LEVELS.get(kind, logging.INFO), message)

    _install(handle)


class _Lines(QObject):
    line = Signal(str)


class LogPanelHandler(logging.Handler):
    """Hands each record, formatted, to the UI thread as `lines.line`; records may come
    from any thread.
    """

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.setFormatter(LogFormatter(tracebacks=False))
        self.lines = _Lines()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.line.emit(self.format(record))
        except Exception:  # noqa: BLE001 - a Handler reports its own failures
            self.handleError(record)


class InterruptHandler:
    """Ctrl+C in the terminal: the first closes `window` without asking, stopping any
    run; another quits outright.  Keep it for as long as the application runs.

    Python runs its signal handlers between bytecodes, which never happen while
    Qt's event loop waits; writing the signal to a socket the loop watches wakes it.
    """

    def __init__(self, app: QApplication, close: Callable[[], None]) -> None:
        self.app, self.close, self.pressed = app, close, 0
        self.reader, self.writer = socket.socketpair()
        for end in (self.reader, self.writer):
            end.setblocking(False)
        signal.set_wakeup_fd(self.writer.fileno())
        self.notifier = QSocketNotifier(self.reader.fileno(), QSocketNotifier.Type.Read)
        self.notifier.activated.connect(self._drain)
        signal.signal(signal.SIGINT, self._interrupt)

    def _drain(self) -> None:
        try:
            self.reader.recv(64)
        except BlockingIOError:
            pass

    def _interrupt(self, _signum: int, _frame: object) -> None:
        self.pressed += 1
        if self.pressed == 1:
            logging.getLogger(__name__).info(t("status.sigint_exit"))
            self.close()
        else:
            self.app.exit(130)
