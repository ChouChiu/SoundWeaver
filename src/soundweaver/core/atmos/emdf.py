"""The EMDF container (TS 102 366 Annex H) that carries the object audio side information.

The container sits in a syncframe's skip fields or auxdata, not necessarily
on a byte boundary, so it is found by its syncword and confirmed by parsing.
"""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from .bitstream import BitReader, BitstreamError

SYNCWORD = 0x5838
_SYNC_BYTES = SYNCWORD.to_bytes(2, "big")
# TS 103 420 table 55
OAMD_PAYLOAD = 11
JOC_PAYLOAD = 14
_WANTED = (OAMD_PAYLOAD, JOC_PAYLOAD)
# protection_length_primary/secondary codes; primary 0 is reserved
_PROTECTION_BITS = (0, 8, 32, 128)


def _parse_container(frame: bytes, bit_pos: int) -> dict[int, bytes]:
    """The object payloads of the container whose emdf_sync starts at `bit_pos`."""
    head = BitReader(frame, bit_pos, bit_pos + 32)
    if head.read(16) != SYNCWORD:
        raise BitstreamError("no EMDF syncword")
    length = head.read(16)
    end = head.pos + length * 8
    if end > len(frame) * 8:
        raise BitstreamError("EMDF container runs past the syncframe")
    bits = BitReader(frame, head.pos, end)
    version = bits.read(2)
    if version == 3:
        version += bits.variable_bits(2)
    if version != 0:
        raise BitstreamError(f"EMDF version {version}")
    if bits.read(3) == 7:  # key_id
        bits.variable_bits(3)
    payloads: dict[int, bytes] = {}
    while (payload_id := bits.read(5)) != 0:
        if payload_id == 0x1F:
            payload_id += bits.variable_bits(5)
        # emdf_payload_config (H.2.1.3): nothing here changes how the payload is read
        sample_offset = bits.flag()
        if sample_offset:
            bits.skip(12)
        if bits.flag():  # duratione
            bits.variable_bits(11)
        if bits.flag():  # groupide
            bits.variable_bits(2)
        if bits.flag():  # codecdatae
            bits.skip(8)
        if not bits.flag():  # discard_unknown_payload
            aligned = False
            if not sample_offset:
                aligned = bits.flag()
                if aligned:
                    bits.skip(2)
            if sample_offset or aligned:
                bits.skip(7)
        size = bits.variable_bits(8)
        data = bits.read_bytes(size)
        if payload_id in _WANTED:
            payloads[payload_id] = data
    # emdf_protection (H.2.2.11) fills the rest of the container, give or take
    # byte padding; a stray 0x5838 in the audio data that parses this far won't
    primary = _PROTECTION_BITS[bits.read(2)]
    secondary = _PROTECTION_BITS[bits.read(2)]
    if primary == 0 or not 0 <= end - bits.pos - primary - secondary < 8:
        raise BitstreamError("EMDF protection does not close the container")
    return payloads


def _syncword_positions(frame: bytes) -> Iterator[int]:
    """Bit offsets of 0x5838: byte-aligned ones first, as encoders put them there."""
    start = frame.find(_SYNC_BYTES)
    while start != -1:
        yield start * 8
        start = frame.find(_SYNC_BYTES, start + 1)
    if len(frame) < 3:
        return
    # every 24-bit window holds the 16 bits starting at each of its first 8 offsets
    data = np.frombuffer(frame, dtype=np.uint8).astype(np.uint32)
    windows = (data[:-2] << 16) | (data[1:-1] << 8) | data[2:]
    found = [
        np.flatnonzero(((windows >> (8 - shift)) & 0xFFFF) == SYNCWORD) * 8 + shift
        for shift in range(1, 8)
    ]
    yield from (int(p) for p in np.sort(np.concatenate(found)))


def object_payloads(frame: bytes) -> dict[int, bytes]:
    """The OAMD and JOC payloads of a syncframe, or an empty dict if it carries none."""
    for pos in _syncword_positions(frame):
        try:
            found = _parse_container(frame, pos)
        except BitstreamError:
            continue
        if found:
            return found
    return {}
