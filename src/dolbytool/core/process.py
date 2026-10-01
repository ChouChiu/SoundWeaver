"""Run a child process line by line, honouring cancellation."""

from __future__ import annotations

import subprocess
import sys
import threading
from collections.abc import Callable, Sequence

from dolbytool.core.cancel import CancellationToken, ProcessingCancelled

# How often a running child is checked against its cancellation token.
_POLL_SECONDS = 0.2

# Keeps a console child such as ffmpeg from opening a window under the windowed Windows build.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run_streaming(
    cmd: Sequence[str],
    on_line: Callable[[str], None],
    token: CancellationToken | None = None,
    *,
    error: type[Exception] = RuntimeError,
    exit_message: str = "{name} exited with {code}",
    grace: float | None = None,
) -> None:
    """Feed each stdout line to `on_line`; raise `error` with stderr on a non-zero exit.

    A cancel stops the child even while it prints nothing.  With `grace` the
    child gets SIGTERM and that many seconds to clean up before it is killed,
    otherwise it is killed at once.  stderr is drained on its own thread so a
    chatty child cannot fill the pipe and stall.
    """
    with subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=NO_WINDOW,
    ) as proc:
        assert proc.stdout is not None and proc.stderr is not None
        stderr: list[str] = []
        drain = threading.Thread(target=lambda: stderr.append(proc.stderr.read()), daemon=True)
        drain.start()
        if token is not None:
            threading.Thread(target=_watch, args=(proc, token, grace), daemon=True).start()
        try:
            for line in proc.stdout:
                on_line(line)
        except BaseException:
            _stop(proc, grace)
            raise
        code = proc.wait()
        drain.join()
    if token is not None and token.cancelled:
        raise ProcessingCancelled
    if code != 0:
        fallback = exit_message.format(name=cmd[0], code=code)
        raise error("".join(stderr).strip() or fallback)


def _watch(proc: subprocess.Popen, token: CancellationToken, grace: float | None) -> None:
    while proc.poll() is None:
        if token.wait(_POLL_SECONDS):
            _stop(proc, grace)
            return


def _stop(proc: subprocess.Popen, grace: float | None) -> None:
    if grace is None:
        proc.kill()
        proc.wait()
        return
    proc.terminate()
    try:
        proc.wait(grace)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
