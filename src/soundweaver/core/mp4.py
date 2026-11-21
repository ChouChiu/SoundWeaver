"""Shared MP4/ISOBMFF box parsing, elementary stream extraction, and TS/MHAS sync detection."""

from __future__ import annotations

import struct
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from itertools import islice, pairwise
from pathlib import Path

_MP4_SUFFIXES = (".mp4", ".m4a", ".m4v", ".mov")
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


@dataclass(frozen=True, slots=True)
class SampleEntry:
    track_id: int
    index: int
    kind: bytes
    data: bytes
    table: bytes
    fragmented: bool


def find_sample_entry(moov: bytes, entry_codes: tuple[bytes, ...]) -> SampleEntry | None:
    """Locate a sample description and the table of its own track."""
    fragmented = any(kind == b"mvex" for kind, _, _ in iter_boxes(moov))
    for kind, start, end in iter_boxes(moov):
        if kind != b"trak":
            continue
        track_id = 0
        for kind, pos, stop in iter_boxes(moov, start, end):
            if kind == b"tkhd" and stop - pos >= 16:
                offset = 20 if moov[pos] == 1 else 12
                if pos + offset + 4 <= stop:
                    track_id = struct.unpack_from(">I", moov, pos + offset)[0]
        for ttype, ts, te in iter_boxes(moov, start, end):
            if ttype != b"mdia":
                continue
            for mtype, ms, me in iter_boxes(moov, ts, te):
                if mtype != b"minf":
                    continue
                for ntype, ns, ne in iter_boxes(moov, ms, me):
                    if ntype != b"stbl":
                        continue
                    for stype, ss, se in iter_boxes(moov, ns, ne):
                        if stype != b"stsd" or se - ss < 8:
                            continue
                        for index, (tag, es, ee) in enumerate(iter_boxes(moov, ss + 8, se), 1):
                            if tag in entry_codes:
                                return SampleEntry(
                                    track_id, index, tag, moov[es:ee], moov[ns:ne], fragmented,
                                )
    return None


def has_mp4_sample_entry(path: Path, entry_codes: tuple[bytes, ...]) -> bool:
    """True when the container carries a track with one of the given sample entries."""
    moov = find_mp4_moov(path)
    return moov is not None and find_sample_entry(moov, entry_codes) is not None


@dataclass(frozen=True, slots=True)
class SampleTable:
    count: int
    fixed_size: int
    sizes: bytes
    offsets: tuple[int, ...]
    runs: tuple[tuple[int, int, int], ...]

    def read_chunks(self, path: Path, index: int) -> Iterator[bytes]:
        """Read contiguous samples in file order within each selected track chunk."""
        sizes = (size for (size,) in struct.iter_unpack(">I", self.sizes))
        run = 0
        read = 0
        with path.open("rb") as source:
            file_size = source.seek(0, 2)
            for chunk, offset in enumerate(self.offsets, 1):
                while run + 1 < len(self.runs) and self.runs[run + 1][0] <= chunk:
                    run += 1
                _, samples, description = self.runs[run]
                count = min(samples, self.count - read)
                length = count * self.fixed_size if self.fixed_size else sum(islice(sizes, count))
                read += count
                if description != index:
                    raise ValueError("MP4 audio sample description changes within the track.")
                if offset + length > file_size:
                    raise ValueError("Truncated MP4 audio chunk.")
                source.seek(offset)
                # A chunk can contain an entire track, so keep reads bounded.
                while length:
                    part = source.read(min(length, 65536))
                    if not part:
                        raise ValueError("Truncated MP4 audio chunk.")
                    length -= len(part)
                    yield part
                if read == self.count:
                    break
        if read != self.count:
            raise ValueError("MP4 audio sample table ended before the last sample.")


def _table_entries(box: bytes, start: int, count: int, fmt: str) -> Iterator[tuple]:
    size = struct.calcsize(fmt)
    end = start + count * size
    if end > len(box):
        raise ValueError("Truncated MP4 audio sample table.")
    return struct.iter_unpack(fmt, box[start:end])


def read_sample_table(entry: SampleEntry) -> SampleTable | None:
    if entry.fragmented:
        return None
    count = fixed = 0
    sizes = b""
    offsets: tuple[int, ...] = ()
    runs: tuple[tuple[int, int, int], ...] = ()
    for kind, start, end in iter_boxes(entry.table):
        box = entry.table[start:end]
        if kind == b"stsz" and len(box) >= 12:
            fixed, count = struct.unpack_from(">II", box, 4)
            if not fixed:
                if 12 + count * 4 > len(box):
                    raise ValueError("Truncated MP4 audio sample sizes.")
                sizes = box[12 : 12 + count * 4]
        elif kind in (b"stco", b"co64", b"stsc") and len(box) >= 8:
            length = struct.unpack_from(">I", box, 4)[0]
            if kind == b"stsc":
                runs = tuple(_table_entries(box, 8, length, ">III"))
            else:
                fmt = ">Q" if kind == b"co64" else ">I"
                offsets = tuple(value for (value,) in _table_entries(box, 8, length, fmt))
    if not count or not offsets or not runs:
        return None
    if runs[0][0] != 1 or any(x[0] >= y[0] for x, y in pairwise(runs)):
        raise ValueError("Invalid MP4 chunk-to-sample table.")
    return SampleTable(count, fixed, sizes, offsets, runs)


def extract_elementary_stream(
    mp4_path: Path, entry_codes: tuple[bytes, ...], out_path: Path
) -> bool:
    """Extract raw elementary stream frames from an MP4 track matching `entry_codes`."""
    moov = find_mp4_moov(mp4_path)
    entry = find_sample_entry(moov, entry_codes) if moov is not None else None
    table = read_sample_table(entry) if entry else None
    if entry is None or table is None:
        return False
    with closing(table.read_chunks(mp4_path, entry.index)) as chunks, out_path.open("wb") as output:
        for chunk in chunks:
            output.write(chunk)
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
