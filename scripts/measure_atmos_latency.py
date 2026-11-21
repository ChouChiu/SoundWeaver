"""Measure Atmos first-sound latency: the wall clock from decode start to each published ADM chunk.

Run: `uv run python scripts/measure_atmos_latency.py <file.m4a> [outfile]`
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from soundweaver.core.atmos import decode_atmos
from soundweaver.core.cancel import CancellationToken


def main() -> int:
    source = Path(sys.argv[1])
    adm = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/measure.wav")
    token = CancellationToken()
    events: list[tuple[float, int]] = []
    start = time.perf_counter()

    def on_chunk(path: Path, frames: int) -> None:
        events.append((time.perf_counter() - start, frames))

    out = decode_atmos(source, token, lambda f: None, adm=adm, on_adm_chunk=on_chunk)
    total = time.perf_counter() - start
    first = events[0][0] if events else float("nan")
    print(
        f"duration={out.trace.duration:.1f}s total_decode={total:.2f}s "
        f"first_publish={first:.2f}s share={first / total * 100:.0f}% chunks={len(events)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
