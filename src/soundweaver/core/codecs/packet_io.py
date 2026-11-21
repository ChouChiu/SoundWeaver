"""Read compressed byte streams across container chunk boundaries."""

from __future__ import annotations

from collections.abc import Generator


def read_bits(data: bytes, start: int, count: int) -> int:
    """Read an MSB-first field from the bytes that cover it."""
    end = start + count
    if end > len(data) * 8:
        raise ValueError("Compressed audio bit field is truncated.")
    stop = (end + 7) // 8
    value = int.from_bytes(data[start // 8 : stop], "big")
    return (value >> (stop * 8 - end)) & ((1 << count) - 1)


class ChunkReader:
    def __init__(self, chunks: Generator[bytes, None, None]) -> None:
        self._chunks = chunks
        self._pending = memoryview(b"")

    def read(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            if not self._pending:
                self._pending = memoryview(next(self._chunks, b""))
                if not self._pending:
                    break
            take = size - len(data)
            data += self._pending[:take]
            self._pending = self._pending[take:]
        return bytes(data)

    def close(self) -> None:
        self._chunks.close()
