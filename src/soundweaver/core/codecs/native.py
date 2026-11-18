"""Shared binary lookup, subprocess execution, and float32 streaming PCM client for native engines."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import threading
import time
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
    deadline = time.monotonic() + timeout if timeout > 0 else None
    if token is not None:
        token.raise_if_cancelled()
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            creationflags=NO_WINDOW,
        )
    except OSError as error:
        raise error_cls(str(error)) from error

    name = Path(cmd[0]).name
    with proc:
        try:
            while True:
                if token is not None:
                    token.raise_if_cancelled()
                remaining = deadline - time.monotonic() if deadline is not None else 0.1
                if remaining <= 0:
                    raise error_cls(f"{name} timed out after {timeout:.0f}s")
                try:
                    stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    continue
                break
        except ProcessingCancelled:
            proc.terminate()
            try:
                proc.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise
        except BaseException:
            proc.kill()
            proc.communicate()
            raise

    if token is not None:
        token.raise_if_cancelled()
    if proc.returncode:
        detail = stderr.strip() or stdout.strip()
        raise error_cls(detail or f"{name} exited with status {proc.returncode}")
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
        *,
        pcm_format: tuple[int, int] | None = None,
        token: CancellationToken | None = None,
    ) -> None:
        self._remap = list(remap) if remap is not None else None
        self._name = Path(cmd[0]).name
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

        self._closed = threading.Event()
        if token is not None:
            threading.Thread(target=self._cancel_on_token, args=(token,), daemon=True).start()
        if pcm_format is None:
            hdr = self._read_exact(8)
            if len(hdr) < 8:
                detail = self.stderr_text()
                self.close()
                if token is not None:
                    token.raise_if_cancelled()
                raise RuntimeError(detail or f"{self._name} closed stream before PCM header")
            pcm_format = struct.unpack("<ii", hdr)
        self.sample_rate, self.channels = pcm_format
        if self.sample_rate <= 0 or self.channels <= 0:
            self.close()
            raise RuntimeError(f"Invalid stream header: rate={self.sample_rate}, ch={self.channels}")
        self._eof = False

    def _cancel_on_token(self, token: CancellationToken) -> None:
        # Interrupt even a blocked pipe read. The decoding thread closes the pipes.
        while not self._closed.is_set() and self._proc.poll() is None:
            if token.wait(0.05):
                if self._proc.poll() is None:
                    self._proc.terminate()
                    if not self._closed.wait(1.5) and self._proc.poll() is None:
                        self._proc.kill()
                return

    def _drain_stderr(self, callback: Callable[[str], None] | None) -> None:
        assert self._proc.stderr is not None
        try:
            for raw in self._proc.stderr:
                line = raw.decode("utf-8", errors="replace").rstrip()
                if line:
                    self._stderr_lines.append(line)
                    if len(self._stderr_lines) > 200:
                        del self._stderr_lines[:50]
                    if callback is not None:
                        callback(line)
        except (ValueError, OSError):
            if not self._closed.is_set():
                raise

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
        if len(raw) < frames * stride:
            self._eof = True
            code = self._proc.wait()
            self._stderr_thread.join()
            if code:
                raise RuntimeError(self.stderr_text() or f"{self._name} exited with status {code}")
            if usable != len(raw):
                raise RuntimeError(f"{self._name} wrote an incomplete PCM frame")
        arr = np.frombuffer(raw[:usable], dtype="<f4").reshape(-1, self.channels).copy()
        if self._remap is not None and len(self._remap) == self.channels:
            arr = arr[:, self._remap]
        return arr

    def stderr_text(self) -> str:
        return "\n".join(self._stderr_lines)

    def close(self) -> None:
        self._closed.set()
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        if self._proc.stdout is not None:
            self._proc.stdout.close()
        if self._stderr_thread.is_alive() and self._stderr_thread is not threading.current_thread():
            self._stderr_thread.join(timeout=1.0)
        if self._proc.stderr is not None:
            self._proc.stderr.close()
