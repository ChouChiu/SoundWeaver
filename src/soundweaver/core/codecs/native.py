"""Shared binary lookup, subprocess execution, and float32 streaming PCM client for native engines."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.process import NO_WINDOW

_PACKAGE_BIN = Path(__file__).resolve().parents[2] / "bin"
_NATIVE_BUILD_BIN = Path(__file__).resolve().parents[4] / "native" / ".build"


def bin_dir() -> Path:
    """Bundled binary directory (PyInstaller `_MEIPASS/bin` or `src/soundweaver/bin`)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            cand = Path(meipass) / "bin"
            if cand.is_dir():
                return cand
        return Path(sys.executable).resolve().parent / "bin"
    return _PACKAGE_BIN


def find_binary(name: str, env_var: str | None = None) -> str | None:
    """Locate native executable `name` via env override, bundled `bin/`, or PATH."""
    if env_var:
        override = os.environ.get(env_var, "").strip()
        if override and Path(override).is_file():
            return override
    custom_dir = os.environ.get("SOUNDWEAVER_BIN_DIR", "").strip()
    exe_name = f"{name}.exe" if sys.platform == "win32" else name
    for directory in (Path(custom_dir) if custom_dir else None, bin_dir(), _PACKAGE_BIN):
        if directory is not None:
            candidate = directory / exe_name
            if candidate.is_file():
                return str(candidate)
    if _NATIVE_BUILD_BIN.is_dir():
        found = next(_NATIVE_BUILD_BIN.rglob(exe_name), None)
        if found and found.is_file():
            return str(found)
    return shutil.which(name)


def run_native(
    cmd: Sequence[str],
    token: CancellationToken | None,
    error_cls: type[Exception],
    timeout: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    """Run a native CLI tool cleanly, polling `token` for cancellation."""
    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=NO_WINDOW,
    )
    deadline = None
    if timeout > 0:
        import time

        deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if token is not None and token.cancelled:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise ProcessingCancelled
        if deadline is not None:
            import time

            if time.monotonic() > deadline:
                proc.kill()
                proc.wait()
                raise error_cls(f"{Path(cmd[0]).name} timed out after {timeout:.0f}s")
        if token is not None:
            token.wait(0.1)
        else:
            import time

            time.sleep(0.05)

    stdout, stderr = proc.communicate()
    if token is not None and token.cancelled:
        raise ProcessingCancelled
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


class NativePcmStream:
    """Streaming decoder reader for native tools emitting `(LE int32 rate, LE int32 ch)` + F32 PCM.

    Shared by `vivid stream` (AV3A) and `ia_mpeghd_testbench -stream:1` (MPEG-H / 360RA).
    """

    def __init__(
        self,
        cmd: Sequence[str],
        remap: Sequence[int] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> None:
        self._remap = list(remap) if remap is not None else None
        self._proc = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=NO_WINDOW,
        )
        assert self._proc.stdout is not None and self._proc.stderr is not None
        self._stderr_lines: list[str] = []
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(on_stderr,), daemon=True
        )
        self._stderr_thread.start()

        hdr = self._read_exact(8)
        if len(hdr) < 8:
            detail = self.stderr_text()
            self.close()
            raise RuntimeError(detail or f"{Path(cmd[0]).name} closed stream before PCM header")
        self.sample_rate, self.channels = struct.unpack("<ii", hdr)
        if self.sample_rate <= 0 or self.channels <= 0:
            self.close()
            raise RuntimeError(f"Invalid stream header: rate={self.sample_rate}, ch={self.channels}")
        self._eof = False

    def _drain_stderr(self, callback: Callable[[str], None] | None) -> None:
        assert self._proc.stderr is not None
        for raw in self._proc.stderr:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                self._stderr_lines.append(line)
                if len(self._stderr_lines) > 200:
                    del self._stderr_lines[:50]
                if callback is not None:
                    callback(line)

    def _read_exact(self, nbytes: int) -> bytes:
        assert self._proc.stdout is not None
        chunks: list[bytes] = []
        remaining = nbytes
        while remaining > 0:
            chunk = self._proc.stdout.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_frames(self, frames: int) -> np.ndarray:
        """Read up to `frames` interleaved float32 frames of shape `(n, channels)`."""
        if self._eof or frames <= 0:
            return np.zeros((0, self.channels), dtype=np.float32)
        stride = self.channels * 4
        raw = self._read_exact(frames * stride)
        usable = (len(raw) // stride) * stride
        if usable == 0:
            self._eof = True
            return np.zeros((0, self.channels), dtype=np.float32)
        if usable < frames * stride:
            self._eof = True
        arr = np.frombuffer(raw[:usable], dtype="<f4").reshape(-1, self.channels).copy()
        if self._remap is not None and len(self._remap) == self.channels:
            arr = arr[:, self._remap]
        return arr

    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self._proc.stdout is not None:
            self._proc.stdout.close()
        if self._proc.stderr is not None:
            self._proc.stderr.close()
