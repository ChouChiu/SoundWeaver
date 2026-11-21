"""Feed Audio Vivid elementary bytes from raw audio, MP4 or MPEG-TS."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from typing import BinaryIO

from soundweaver.core import mp4
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.native import NativeProcess

_SYNC = b"\xff\xf2"


def _ts_payloads(source: BinaryIO, size: int, prefix: int) -> Generator[bytes, None, None]:
    pid = continuity = None
    pes_remaining = None
    while raw := source.read(size):
        if len(raw) != size or raw[prefix] != 0x47:
            raise ValueError("Truncated packet or lost MPEG-TS sync.")
        packet = raw[prefix : prefix + 188]
        current_pid = ((packet[1] & 31) << 8) | packet[2]
        control = (packet[3] >> 4) & 3
        if (pid is not None and current_pid != pid) or not (control & 1):
            continue
        if packet[1] & 0x80 or packet[3] & 0xc0:
            if current_pid == pid:
                raise ValueError("Damaged or encrypted Audio Vivid TS packet.")
            continue
        adaptation = packet[4] if control & 2 else 0
        start = 5 + adaptation if control & 2 else 4
        if start >= len(packet):
            continue
        counter = packet[3] & 15
        if current_pid == pid and continuity is not None:
            if counter == continuity:
                continue
            if counter != (continuity + 1) % 16 and not (adaptation and packet[5] & 0x80):
                raise ValueError("Missing Audio Vivid TS packets.")
        if packet[1] & 0x40:
            if packet[start : start + 3] != b"\0\0\x01" or len(packet) - start < 9:
                continue
            length = int.from_bytes(packet[start + 4 : start + 6], "big")
            extra = packet[start + 8]
            start += 9 + extra
            if start > len(packet):
                if pid is None:
                    continue
                raise ValueError("Fragmented Audio Vivid PES header.")
            if pid is None and not packet.startswith(_SYNC, start):
                continue
            if length and length < 3 + extra:
                raise ValueError("Invalid Audio Vivid PES length.")
            pid = current_pid
            pes_remaining = length - 3 - extra if length else None
        if pid is None:
            continue
        continuity = counter
        payload = packet[start:]
        if pes_remaining is not None:
            payload = payload[:pes_remaining]
            pes_remaining -= len(payload)
        if payload:
            yield payload


def elementary_chunks(path: Path, token: CancellationToken | None) -> Generator[bytes, None, None]:
    with path.open("rb") as source:
        head = source.read(612)
        source.seek(0)
        if head.startswith(_SYNC):
            while block := source.read(65536):
                yield block
            return
        for size in (188, 192, 204):
            prefix = 4 if size == 192 else 0
            if len(head) > prefix + 2 * size and head[prefix : prefix + 2 * size + 1 : size] == b"GGG":
                yield from _ts_payloads(source, size, prefix)
                return

    moov = mp4.find_mp4_moov(path)
    entry = mp4.find_sample_entry(moov, (b"av3a",)) if moov is not None else None
    if entry is None:
        raise ValueError("No Audio Vivid elementary stream, MPEG-TS, or MP4 audio track found.")
    table = mp4.read_sample_table(entry)
    if table is not None:
        yield from table.read_chunks(path, entry.index)
        return

    # Fragmented MP4 requires its movie fragments to be demuxed in sequence.
    track = f"0:i:{entry.track_id}" if entry.track_id else "0:a:0"
    cmd = [
        os.environ.get("AV3A_FFMPEG_BIN") or "ffmpeg",
        "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
        "-map", track, "-c:a", "copy", "-f", "data", "pipe:1",
    ]
    with closing(NativeProcess(cmd, token=token)) as demux:
        while block := demux.read_exact(65536):
            yield block
        demux.check_exit()
