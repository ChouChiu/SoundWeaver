"""Build all bundled native spatial audio engines into `src/soundweaver/bin/`.

Builds:
  - `soundweaver_av3a`: Audio Vivid (AVS3-P3) frame decoder
  - `soundweaver_mpegh`: MPEG-H 3D Audio (360RA) frame decoder
  - `vivid_encode`: Audio Vivid (AVS3-P3) encoder + `model.bin`
  - `ia_mpeghd_testbench`: MPEG-H 3D Audio (360RA) decoder (LC Level 4)

Run: `uv run python scripts/build_native.py`
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NATIVE_DIR = ROOT / "native"
BUILD_DIR = NATIVE_DIR / ".build"
BIN_DIR = ROOT / "src" / "soundweaver" / "bin"

TARGETS = ("soundweaver_av3a", "soundweaver_mpegh", "vivid_encode", "ia_mpeghd_testbench")


def stage(source: Path, dest: Path) -> None:
    """Copy a built artifact into place atomically.

    A running SoundWeaver holds its worker binaries open, and writing onto them
    in place fails with "Text file busy"; replacing the directory entry leaves
    the running process on the inode it already has.
    """
    partial = dest.with_name(f"{dest.name}.tmp")
    shutil.copy2(source, partial)
    os.replace(partial, dest)


def main() -> int:
    cmake = shutil.which("cmake")
    if not cmake:
        print("error: cmake is required to build native engines.", file=sys.stderr)
        return 1

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)

    cfg = [
        cmake,
        "-S",
        str(NATIVE_DIR),
        "-B",
        str(BUILD_DIR),
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
    ]
    if shutil.which("ninja"):
        cfg += ["-G", "Ninja"]
    subprocess.run(cfg, check=True)

    jobs = str(max(1, os.cpu_count() or 4))
    subprocess.run(
        [cmake, "--build", str(BUILD_DIR), "--target", *TARGETS, "--parallel", jobs],
        check=True,
    )

    exe_suffix = ".exe" if sys.platform == "win32" else ""
    for name in TARGETS:
        found = next(BUILD_DIR.rglob(f"{name}{exe_suffix}"), None)
        if not found or not found.is_file():
            print(f"error: built target {name!r} not found in {BUILD_DIR}", file=sys.stderr)
            return 1
        dest = BIN_DIR / found.name
        stage(found, dest)
        print(f"staged {dest.relative_to(ROOT)}")

    model = NATIVE_DIR / "av3a" / "model.bin"
    if model.is_file():
        stage(model, BIN_DIR / "model.bin")
        print(f"staged {(BIN_DIR / 'model.bin').relative_to(ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
