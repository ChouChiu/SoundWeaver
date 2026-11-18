"""Shared MP4/ISOBMFF box parsing, elementary stream extraction, and TS/MHAS sync detection."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from pathlib import Path

_MP4_SUFFIXES = (".mp4", ".m4a", ".mov")
_TS_PACKET_SIZES = (188, 192, 204)


def iter_boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[bytes, int, int]]:
    """Yield `(box_type, payload_start, box_end)` for ISOBMFF boxes in `data[start:end]`."""
    limit = len(data) if end is None else end
    pos = start
    while pos + 8 <= limit:
        size = struct.unpack(">I", data[pos : pos + 4])[0]
        box_type = data[pos + 4 : pos + 8]
        hdr = 8
        if size == 1:
            if pos + 16 > limit:
                break
            size = struct.unpack(">Q", data[pos + 8 : pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = limit - pos
        if size < hdr or pos + size > limit:
            break
        yield box_type, pos + hdr, pos + size
        pos += size


def find_mp4_moov(path: Path) -> bytes | None:
    """Locate and read the `moov` box of an MP4/M4A file regardless of `mdat` order."""
    if path.suffix.lower() not in _MP4_SUFFIXES:
        return None
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            pos = 0
            while pos + 8 <= file_size:
                fh.seek(pos)
                hdr = fh.read(8)
                if len(hdr) < 8:
                    break
                size = struct.unpack(">I", hdr[:4])[0]
                btype = hdr[4:8]
                hdr_len = 8
                if size == 1:
                    ext = fh.read(8)
                    if len(ext) < 8:
                        break
                    size = struct.unpack(">Q", ext)[0]
                    hdr_len = 16
                elif size == 0:
                    size = file_size - pos
                if size < hdr_len or pos + size > file_size:
                    break
                if btype == b"moov":
                    fh.seek(pos + hdr_len)
                    return fh.read(size - hdr_len)
                pos += size
    except OSError:
        return None
    return None


def _stsd_has_entry(stbl: bytes, s_start: int, s_end: int, entry_codes: tuple[bytes, ...]) -> bool:
    for btype, start, end in iter_boxes(stbl, s_start, s_end):
        if btype == b"stsd" and end - start >= 8:
            for entry_type, _, _ in iter_boxes(stbl, start + 8, end):
                if entry_type in entry_codes:
                    return True
    return False


def find_stbl_for_entries(moov: bytes, entry_codes: tuple[bytes, ...]) -> bytes | None:
    """Return the `stbl` payload bytes whose `stsd` contains any of `entry_codes`."""
    for btype, s, e in iter_boxes(moov):
        if btype != b"trak":
            continue
        for ttype, ts, te in iter_boxes(moov, s, e):
            if ttype != b"mdia":
                continue
            for mtype, ms, me in iter_boxes(moov, ts, te):
                if mtype != b"minf":
                    continue
                for ntype, ns, ne in iter_boxes(moov, ms, me):
                    if ntype == b"stbl" and _stsd_has_entry(moov, ns, ne, entry_codes):
                        return moov[ns:ne]
    return None


def has_mp4_sample_entry(path: Path, entry_codes: tuple[bytes, ...]) -> bool:
    """True when `path` is an MP4/M4A/MOV carrying a track with `stsd` sample entry in `entry_codes`."""
    moov = find_mp4_moov(path)
    return moov is not None and find_stbl_for_entries(moov, entry_codes) is not None


def extract_elementary_stream(
    mp4_path: Path, entry_codes: tuple[bytes, ...], out_path: Path
) -> bool:
    """Extract raw elementary stream frames from an MP4 track matching `entry_codes`."""
    moov = find_mp4_moov(mp4_path)
    if moov is None:
        return False
    stbl = find_stbl_for_entries(moov, entry_codes)
    if stbl is None:
        return False

    sample_sizes: list[int] = []
    chunk_offsets: list[int] = []
    stsc_entries: list[tuple[int, int]] = []

    for btype, s, e in iter_boxes(stbl):
        box = stbl[s:e]
        if btype == b"stsz" and len(box) >= 12:
            fixed_sz, count = struct.unpack(">II", box[4:12])
            if fixed_sz > 0:
                sample_sizes = [fixed_sz] * count
            else:
                n = min(count, (len(box) - 12) // 4)
                sample_sizes = [struct.unpack(">I", box[12 + i * 4 : 16 + i * 4])[0] for i in range(n)]
        elif btype == b"stco" and len(box) >= 8:
            count = min(struct.unpack(">I", box[4:8])[0], (len(box) - 8) // 4)
            chunk_offsets = [struct.unpack(">I", box[8 + i * 4 : 12 + i * 4])[0] for i in range(count)]
        elif btype == b"co64" and len(box) >= 8:
            count = min(struct.unpack(">I", box[4:8])[0], (len(box) - 8) // 8)
            chunk_offsets = [struct.unpack(">Q", box[8 + i * 8 : 16 + i * 8])[0] for i in range(count)]
        elif btype == b"stsc" and len(box) >= 8:
            count = min(struct.unpack(">I", box[4:8])[0], (len(box) - 8) // 12)
            for i in range(count):
                first_chunk, spc, _ = struct.unpack(">III", box[8 + i * 12 : 20 + i * 12])
                stsc_entries.append((first_chunk, spc))

    if not sample_sizes or not chunk_offsets or not stsc_entries:
        return False

    with mp4_path.open("rb") as in_fh, out_path.open("wb") as out_fh:
        sample_idx = 0
        stsc_idx = 0
        for c_idx, offset in enumerate(chunk_offsets):
            chunk_num = c_idx + 1
            while stsc_idx + 1 < len(stsc_entries) and stsc_entries[stsc_idx + 1][0] <= chunk_num:
                stsc_idx += 1
            spc = stsc_entries[stsc_idx][1]
            in_fh.seek(offset)
            for _ in range(spc):
                if sample_idx >= len(sample_sizes):
                    break
                out_fh.write(in_fh.read(sample_sizes[sample_idx]))
                sample_idx += 1
    return out_path.stat().st_size > 0


def has_ts_pes_sync(path: Path, sync: bytes = b"\xff\xf2", scan_bytes: int = 1_048_576) -> bool:
    """Detect 188/192/204-byte MPEG-TS packets carrying a PES elementary payload starting with `sync`."""
    try:
        with path.open("rb") as fh:
            data = fh.read(scan_bytes)
    except OSError:
        return False
    n = len(data)
    for pkt_size in _TS_PACKET_SIZES:
        prefix = 4 if pkt_size == 192 else 0
        if n <= prefix + pkt_size * 2:
            continue
        if not (data[prefix] == 0x47 and data[prefix + pkt_size] == 0x47 and data[prefix + pkt_size * 2] == 0x47):
            continue
        for base in range(prefix, n - 188 + 1, pkt_size):
            if data[base] != 0x47:
                continue
            if not (data[base + 1] & 0x40) or not (data[base + 3] & 0x10):
                continue
            adapt = 1 + data[base + 4] if (data[base + 3] & 0x20) else 0
            start = base + 4 + adapt
            if start + 9 > base + 188 or data[start : start + 3] != b"\x00\x00\x01":
                continue
            payload = start + 9 + data[start + 8]
            if payload + len(sync) <= base + 188 and data[payload : payload + len(sync)] == sync:
                return True
    return False


def has_mhas_sync(path: Path, scan_bytes: int = 4096) -> bool:
    """Detect MPEG-H Audio Stream (`C0 01 A5`) sync marker."""
    try:
        with path.open("rb") as fh:
            return b"\xc0\x01\xa5" in fh.read(scan_bytes)
    except OSError:
        return False
