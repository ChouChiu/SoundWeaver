"""Splitting an E-AC-3 elementary stream into syncframes (TS 102 366 clause E.1.2)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

from .bitstream import BitReader

logger = logging.getLogger(__name__)

SYNCWORD = 0x0B77
INDEPENDENT = 0
# bsid of an E-AC-3 syncframe; 0..8 is plain AC-3, whose header reads differently
_EAC3_BSIDS = range(11, 17)


@dataclass(frozen=True, slots=True)
class Syncframe:
    stream_type: int  # strmtyp: 0 independent, 1 dependent, 2 converted AC-3
    substream: int
    data: bytes


def _header(stream: bytes, pos: int) -> tuple[int, int, int] | None:
    """(strmtyp, substreamid, frame bytes) of the syncframe at `pos`, None if there is none."""
    if (stream[pos] << 8 | stream[pos + 1]) != SYNCWORD:
        return None
    bits = BitReader(stream, (pos + 2) * 8, (pos + 6) * 8)
    stream_type = bits.read(2)
    substream = bits.read(3)
    size = (bits.read(11) + 1) * 2  # frmsiz counts 16-bit words
    bits.read(2)  # fscod
    bits.read(2)  # numblkscod, or fscod2 when fscod is 3
    bits.read(4)  # acmod, lfeon
    if bits.read(5) not in _EAC3_BSIDS:
        return None
    return stream_type, substream, size


def syncframes(stream: bytes) -> Iterator[Syncframe]:
    """Every syncframe of `stream` in order, stepping over junk between them.

    A container may pad the end with zeros, and a cut file may end inside a
    frame; neither is an error, and both end the stream.
    """
    pos = 0
    while pos + 6 <= len(stream):
        header = _header(stream, pos)
        if header is None:
            if not any(stream[pos:]):
                break
            found = pos + 1
            while (found := stream.find(SYNCWORD.to_bytes(2, "big"), found)) != -1:
                if found + 6 <= len(stream) and _header(stream, found) is not None:
                    break
                found += 1
            if found == -1:
                logger.warning(
                    "E-AC-3: %d bytes after byte %d hold no syncframe", len(stream) - pos, pos
                )
                break
            logger.warning("E-AC-3: sync lost at byte %d, found again at %d", pos, found)
            pos = found
            continue
        stream_type, substream, size = header
        if pos + size > len(stream):
            break
        yield Syncframe(stream_type, substream, stream[pos : pos + size])
        pos += size
