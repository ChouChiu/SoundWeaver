"""Read-ahead PCM cached on disk for seeking without decoding the whole track first."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from threading import Event, Lock, Thread
from typing import BinaryIO

import numpy as np

from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.paths import work_root

logger = logging.getLogger(__name__)

BLOCK = 8192
START_SECONDS = 0.5
AHEAD_SECONDS = 6


class PlaybackBuffer:
    """One producer publishes complete PCM blocks. The player never waits on a decoder read."""

    def __init__(
        self,
        sample_rate: int,
        channels: int,
        stereo: bool,
        reader: Callable[[int], np.ndarray],
        token: CancellationToken,
        decoder: ExitStack,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.stereo = stereo
        self._width = channels + 2 if stereo else channels
        self._reader = reader
        self._token = token
        self._decoder = decoder
        self._lock = Lock()
        self._cache: BinaryIO = tempfile.TemporaryFile(dir=work_root())  # noqa: SIM115
        self._frames = 0
        self._wanted = 0
        self._eof = False
        self._error: str | None = None

    def start(self, start_at: float) -> None:
        self._wanted = int(max(0, start_at) * self.sample_rate)
        until = self._wanted + round(self.sample_rate * START_SECONDS)
        try:
            while self._frames < until and self._fill():
                pass
            if not self._frames:
                raise ValueError("The audio decoder produced no PCM.")
            Thread(target=self._produce, daemon=True).start()
        except BaseException:
            self._decoder.close()
            self.close()
            raise

    def _fill(self) -> bool:
        self._token.raise_if_cancelled()
        block = self._reader(BLOCK)
        self._token.raise_if_cancelled()
        with self._lock:
            if self._cache.closed:
                return False
            if len(block) == 0:
                self._eof = True
                return False
            self._cache.seek(0, 2)
            self._cache.write(np.asarray(block, dtype="<f4").tobytes())
            self._frames += len(block)
            return True

    def _produce(self) -> None:
        try:
            while not self._token.cancelled:
                with self._lock:
                    if self._eof:
                        break
                    full = self._frames >= self._wanted + self.sample_rate * AHEAD_SECONDS
                if full:
                    self._token.wait(0.05)
                elif not self._fill():
                    break
        except ProcessingCancelled:
            pass
        except Exception as error:
            if self._token.cancelled:
                return
            logger.exception("Playback decoder stopped before the end of the track")
            with self._lock:
                self._error = str(error) or type(error).__name__
        finally:
            self._decoder.close()

    def state(self) -> tuple[int, bool, str | None]:
        with self._lock:
            return self._frames, self._eof, self._error

    def request(self, frame: int) -> None:
        with self._lock:
            self._wanted = frame

    def read(self, start: int, count: int) -> np.ndarray:
        with self._lock:
            count = max(0, min(count, self._frames - start))
            self._cache.seek(start * self._width * 4)
            data = self._cache.read(count * self._width * 4)
            self._wanted = start + count
        return np.frombuffer(data, dtype="<f4").reshape(count, self._width)

    def close(self) -> None:
        self._token.cancel()
        with self._lock:
            self._cache.close()


class AdmReadAhead:
    """Marks the readable prefix of a growing, private ADM playback file."""

    def __init__(self, token: CancellationToken, min_frames: int = 0) -> None:
        self._token = token
        self._min_frames = max(0, min_frames)
        self._lock = Lock()
        self._scratch = tempfile.TemporaryDirectory(dir=work_root())
        self._frames = 0
        self._eof = False
        self._error: str | None = None
        self._closed = False
        self._path: Path | None = None
        self._ready = Event()

    def start(
        self,
        decode: Callable[[Path, Callable[[Path, int], None]], object],
    ) -> Path:
        def worker() -> None:
            try:
                decode(Path(self._scratch.name) / "playback.wav", self._publish)
            except ProcessingCancelled:
                pass
            except Exception as error:
                if not self._token.cancelled:
                    logger.exception("ADM playback decoder failed")
                    with self._lock:
                        self._error = str(error) or type(error).__name__
            finally:
                with self._lock:
                    self._eof = True
                    if self._closed:
                        self._scratch.cleanup()
                self._ready.set()

        Thread(target=worker, daemon=True).start()
        try:
            while not self._ready.wait(0.05):
                self._token.raise_if_cancelled()
            self._token.raise_if_cancelled()
            with self._lock:
                if self._error is not None:
                    raise RuntimeError(self._error)
                if self._path is None:
                    raise ValueError("The audio decoder produced no ADM PCM.")
                return self._path
        except BaseException:
            self.close()
            raise

    def _publish(self, path: Path, frames: int) -> None:
        self._token.raise_if_cancelled()
        with self._lock:
            self._path, self._frames = path, frames
            ready = frames >= self._min_frames
        if ready:
            self._ready.set()

    def state(self) -> tuple[int, bool, str | None]:
        with self._lock:
            return self._frames, self._eof, self._error

    def close(self) -> None:
        self._token.cancel()
        with self._lock:
            self._closed = True
            if self._eof:
                self._scratch.cleanup()
