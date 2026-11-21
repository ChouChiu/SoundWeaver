"""Sized PCM reads over codec blocks and disk-backed offline decoding."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Protocol

import numpy as np

from soundweaver.core.cancel import CancellationToken
from soundweaver.core.pcm import BLOCK_FRAMES, AudioData
from soundweaver.core.pcm import scratch as pcm_scratch


class PcmStream(Protocol):
    sample_rate: int
    channels: int

    def read_frames(self, frames: int) -> np.ndarray: ...
    def close(self) -> None: ...


class BlockPcmStream:
    """Expose a decoder's variable-sized blocks as frame-counted reads."""

    def __init__(
        self,
        blocks: Generator[tuple[int, int] | np.ndarray, None, None],
    ) -> None:
        pcm_format = next(blocks)
        assert isinstance(pcm_format, tuple)
        self.sample_rate, self.channels = pcm_format
        self._blocks = blocks
        self._pending = np.empty((0, self.channels), np.float32)

    def read_frames(self, frames: int) -> np.ndarray:
        if frames <= 0:
            return self._pending[:0]
        out = np.empty((frames, self.channels), np.float32)
        written = 0
        while written < frames:
            if not len(self._pending):
                block = next(self._blocks, None)
                if block is None:
                    break
                assert isinstance(block, np.ndarray)
                self._pending = block
            moved = min(frames - written, len(self._pending))
            out[written : written + moved] = self._pending[:moved]
            self._pending = self._pending[moved:]
            written += moved
        return out[:written]

    def close(self) -> None:
        self._blocks.close()
        self._pending = self._pending[:0]


def decode_pcm_stream(
    stream: PcmStream,
    token: CancellationToken,
    *,
    error_cls: type[Exception],
) -> AudioData:
    """Spool interleaved PCM and expose a planar view with the usual AudioData lifetime."""
    scratch: Path | None = None
    try:
        scratch = pcm_scratch()
        frames = 0
        with scratch.open("wb") as output:
            while True:
                token.raise_if_cancelled()
                block = stream.read_frames(BLOCK_FRAMES)
                if not len(block):
                    break
                output.write(memoryview(np.ascontiguousarray(block)).cast("B"))
                frames += len(block)
        token.raise_if_cancelled()
        if not frames:
            raise error_cls("The decoder produced no PCM frames.")
        pcm = np.memmap(scratch, mode="r+", dtype=np.float32, shape=(frames, stream.channels))
        return AudioData(pcm.T, stream.sample_rate, backing_path=scratch)
    except BaseException:
        if scratch is not None:
            scratch.unlink(missing_ok=True)
        raise
    finally:
        stream.close()
