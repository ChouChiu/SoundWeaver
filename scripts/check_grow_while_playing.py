"""Exercise the real grow-while-playing path: AdmReadAhead decode + PreparedADM mixing.

Run: `uv run python scripts/check_grow_while_playing.py <file.m4a> [seconds]`
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

from soundweaver.core.atmos import PreparedADM, decode_atmos
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.playback_buffer import AdmReadAhead
from soundweaver.core.playback_mix import mix_adm_block
from soundweaver.core.speakers import DEFAULT_ATMOS


def main() -> int:
    source = Path(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 5.0
    rate = 48000
    token = CancellationToken()
    progress = AdmReadAhead(token, rate, 0.0)
    start = time.perf_counter()

    def decode(target: Path, publish) -> None:
        decode_atmos(source, token, lambda f: None, adm=target, on_adm_chunk=publish)

    path = progress.start(decode)
    ready = time.perf_counter() - start
    adm = PreparedADM(path)
    previous: list = []
    print(f"playback ready (first publish) at {ready:.2f}s, file {path.name}")
    initial_blocks = {i: len(t.blocks) for i, t in enumerate(adm.tracks) if t.is_object}

    # consume like the audio callback: 100 ms blocks held to their wall-clock slot
    # while the decode keeps running, asking for the playhead the way the player
    # does and timing how far past the published edge the mix runs (that stretch
    # would be silence on the sink)
    moved = 0
    errors: list[str] = []
    gaps = 0
    blocks = int(seconds * 10)
    began = time.perf_counter()
    for step in range(blocks):
        played = (step + 1) * rate // 10
        due = began + (step + 1) * 0.1
        if due > time.perf_counter():
            time.sleep(due - time.perf_counter())
        progress.request(played + rate // 10)
        published = progress.state()[0]
        if played > published:
            gaps = max(gaps, played - published)
        bed, objects = mix_adm_block(
            adm, DEFAULT_ATMOS, step * rate // 10, rate // 10, set(), set()
        )
        if not np.isfinite(bed).all():
            errors.append(f"non-finite mix at {step}")
        if step and objects and any(
            abs(o.x - previous[i].x) + abs(o.y - previous[i].y) > 0.02
            for i, o in enumerate(objects)
            if i < len(previous)
        ):
            moved += 1
        previous = objects
    frames, eof, error = progress.state()
    grew = {
        i: len(adm.tracks[i].blocks) - initial_blocks.get(i, 0)
        for i, t in enumerate(adm.tracks)
        if t.is_object
    }
    print(
        f"mixed {blocks} blocks, objects moved in {moved} of them, "
        f"track blocks grew {sum(grew.values())} total, "
        f"decoded {frames} frames (eof={eof}, error={error}), "
        f"worst silence gap {gaps / rate:.2f}s"
    )
    for message in errors:
        print("mix error:", message)
    adm.close()
    progress.close()
    return 1 if error or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
