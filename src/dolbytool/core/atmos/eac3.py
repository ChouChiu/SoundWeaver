"""Splitting an E-AC-3 elementary stream into syncframes (TS 102 366 clause E.1.2)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .bitstream import BitReader, BitstreamError

SYNCWORD = 0x0B77
INDEPENDENT = 0


@dataclass(frozen=True, slots=True)
class Syncframe:
    stream_type: int  # strmtyp: 0 independent, 1 dependent, 2 converted AC-3
    substream: int
    blocks: int  # audio blocks of 256 samples
    data: bytes


def syncframes(stream: bytes) -> Iterator[Syncframe]:
    pos = 0
    sync_bytes = SYNCWORD.to_bytes(2, "big")
    while pos + 6 <= len(stream):
        if (stream[pos] << 8 | stream[pos + 1]) != SYNCWORD:
            # Trailing zero-padding from containers (MP4/M4A) at EOF
            if not stream[pos:].strip(b"\x00"):
                break
            # Resynchronize by locating the next valid syncword
            next_pos = stream.find(sync_bytes, pos + 1)
            if next_pos == -1 or next_pos + 6 > len(stream):
                break
            pos = next_pos
        bits = BitReader(stream, (pos + 2) * 8, (pos + 6) * 8)
        stream_type = bits.read(2)
        substream = bits.read(3)
        size = (bits.read(11) + 1) * 2  # frmsiz counts 16-bit words
        if pos + size > len(stream):
            # Guard against truncated syncframe at the end of stream
            break
        fscod = bits.read(2)
        blocks = 6 if fscod == 3 else (1, 2, 3, 6)[bits.read(2)]
        yield Syncframe(stream_type, substream, blocks, stream[pos : pos + size])
        pos += size
