"""Read-ahead PCM cached on disk for seeking without decoding the whole track first."""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from typing import BinaryIO

import numpy as np

from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.paths import work_root

logger = logging.getLogger(__name__)

BLOCK = 8192
START_SECONDS = 0.5
AHEAD_SECONDS = 6


class PlaybackBuffer:
    """One producer publishes complete PCM blocks. The player never waits on a decoder read.

    Producer and consumer use separate file handles, so a slow disk write on the
    producer side never holds up the audio callback, which reads its own handle.
    """

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
        # guards only the counters; file I/O happens on one dedicated handle per side
        self._lock = Lock()
        self._scratch = tempfile.TemporaryDirectory(dir=work_root())
        self._path = Path(self._scratch.name) / "buffer.pcm"
        # unbuffered: the consumer counts on `self._frames` matching what is on disk
        self._cache: BinaryIO = self._path.open("wb", buffering=0)
        self._view: BinaryIO = self._path.open("rb")
        self._frames = 0
        self._wanted = 0
        self._eof = False
        self._error: str | None = None
        self.decoder_closed = Event()

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
            try:
                self._decoder.close()
            finally:
                self.decoder_closed.set()
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
            with self._lock:
                self._eof = True
            return False
        # the producer thread is the only writer, so the append needs no lock
        self._cache.write(np.asarray(block, dtype="<f4").tobytes())
        with self._lock:
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
            try:
                self._decoder.close()
            finally:
                self.decoder_closed.set()

    def state(self) -> tuple[int, bool, str | None]:
        with self._lock:
            return self._frames, self._eof, self._error

    def request(self, frame: int) -> None:
        with self._lock:
            self._wanted = frame

    def read(self, start: int, count: int) -> np.ndarray:
        with self._lock:
            if self._view.closed:
                return np.zeros((0, self._width), dtype=np.float32)
            count = max(0, min(count, self._frames - start))
            self._wanted = start + count
        data = b""
        if count:
            self._view.seek(start * self._width * 4)
            data = self._view.read(count * self._width * 4)
        return np.frombuffer(data, dtype="<f4").reshape(len(data) // 4 // self._width, self._width)

    def close(self) -> None:
        self._token.cancel()
        with self._lock:
            self._view.close()
        try:
            self._cache.close()
        finally:
            self._scratch.cleanup()


class AdmReadAhead:
    """Marks the readable prefix of a growing, private ADM playback file."""

    def __init__(self, token: CancellationToken, sample_rate: int, start_at: float = 0.0) -> None:
        self._token = token
        self._wanted = int(max(0, start_at) * sample_rate)
        self._min_frames = self._wanted + round(sample_rate * START_SECONDS)
        self._ahead = round(sample_rate * AHEAD_SECONDS)
        self._changed = Condition()
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
                    with self._changed:
                        self._error = str(error) or type(error).__name__
            finally:
                with self._changed:
                    self._eof = True
                    if self._closed:
                        self._scratch.cleanup()
                    self._changed.notify_all()
                self._ready.set()

        Thread(target=worker, daemon=True).start()
        try:
            while not self._ready.wait(0.05):
                self._token.raise_if_cancelled()
            self._token.raise_if_cancelled()
            with self._changed:
                if self._error is not None:
                    raise RuntimeError(self._error)
                if self._path is None:
                    raise ValueError("The audio decoder produced no ADM PCM.")
                return self._path
        except BaseException:
            self.close()
            raise

    def _publish(self, path: Path, frames: int) -> None:
        with self._changed:
            self._path, self._frames = path, frames
            self._changed.notify_all()
            if frames >= self._min_frames:
                self._ready.set()
            while frames >= self._wanted + self._ahead:
                self._token.raise_if_cancelled()
                self._changed.wait(0.05)
            self._token.raise_if_cancelled()

    def request(self, frame: int) -> None:
        with self._changed:
            self._wanted = frame
            self._changed.notify_all()

    def wait_for(self, end: int) -> int:
        """Wait on the decoder thread only, never from the audio sink."""
        with self._changed:
            while self._frames < end and not self._eof:
                self._token.raise_if_cancelled()
                self._changed.wait(0.05)
            self._token.raise_if_cancelled()
            if self._error is not None:
                raise RuntimeError(self._error)
            return self._frames

    def state(self) -> tuple[int, bool, str | None]:
        with self._changed:
            return self._frames, self._eof, self._error

    def close(self) -> None:
        self._token.cancel()
        with self._changed:
            self._closed = True
            self._changed.notify_all()
            if self._eof:
                self._scratch.cleanup()
