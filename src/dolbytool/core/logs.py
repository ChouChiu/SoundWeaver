"""The one log line format: `2026/10/13 17:20:05.123 [INFO] core.atmos.decoder : message`.

The terminal and the in-app log share it; the module is the logger's name
without the package prefix.
"""

from __future__ import annotations

import logging
import sys

_PREFIX = "dolbytool."


class LogFormatter(logging.Formatter):
    def __init__(self, tracebacks: bool = True) -> None:
        super().__init__(
            "%(asctime)s.%(msecs)03d [%(levelname)s] %(source)s : %(message)s",
            datefmt="%Y/%m/%d %H:%M:%S",
        )
        self.tracebacks = tracebacks

    def format(self, record: logging.LogRecord) -> str:
        record.source = record.name.removeprefix(_PREFIX)
        if self.tracebacks:
            return super().format(record)
        # the message alone: a traceback is for the terminal
        exc_info, exc_text = record.exc_info, record.exc_text
        record.exc_info = record.exc_text = None
        try:
            return super().format(record)
        finally:
            record.exc_info, record.exc_text = exc_info, exc_text


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(LogFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
