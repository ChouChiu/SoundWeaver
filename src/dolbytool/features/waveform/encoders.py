"""Video encoders the waveform video can use, and a probe for whether one works here."""

from __future__ import annotations

import subprocess
from functools import cache

from dolbytool.core.process import NO_WINDOW

ENCODERS: dict[str, list[str]] = {
    "h264_nvenc": [
        "-c:v", "h264_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "19",
        "-b:v", "0", "-multipass", "fullres", "-spatial-aq", "1", "-temporal-aq", "1",
        "-bf", "3", "-g", "60",
    ],
    "hevc_nvenc": [
        "-c:v", "hevc_nvenc", "-preset", "p7", "-tune", "hq", "-rc", "vbr", "-cq", "21",
        "-b:v", "0", "-multipass", "fullres", "-spatial-aq", "1", "-temporal-aq", "1",
        "-bf", "3", "-g", "60", "-tag:v", "hvc1",
    ],
    "libx264": ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-g", "60"],
}  # fmt: skip


ENCODER_LABELS = {
    "h264_nvenc": "H.264 (NVIDIA 硬件编码)",
    "hevc_nvenc": "HEVC (NVIDIA 硬件编码)",
    "libx264": "H.264 (软件编码)",
}
DEFAULT_ENCODER = "h264_nvenc"
FALLBACK_ENCODER = "libx264"
# Largest side NVENC encodes on this generation of cards (H.264 level 6.2 / HEVC);
# beyond it the session fails with "No capable devices found".
NVENC_MAX_SIDE = {"h264_nvenc": 4096, "hevc_nvenc": 8192}


@cache
def encoder_available(name: str) -> bool:
    """Encode one tiny frame; a listed NVENC encoder still fails without a usable GPU/driver."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=black:s=256x256:d=0.1", "-frames:v", "1", "-c:v", name, "-f", "null", "-"],
        check=False,
        capture_output=True,
        creationflags=NO_WINDOW,
    )  # fmt: skip
    return result.returncode == 0


def encoder_for(name: str, width: int, height: int) -> tuple[str, str | None]:
    """`name`, or the next encoder that works here and takes frames this large, with why it changed.

    H.264 NVENC hands over to HEVC NVENC, which keeps the GPU, before the software encoder.
    """
    side = max(width, height)
    candidates = [name]
    if name == "h264_nvenc":
        candidates.append("hevc_nvenc")
    candidates.append(FALLBACK_ENCODER)
    usable = next(
        c
        for c in candidates
        if c == FALLBACK_ENCODER or side <= NVENC_MAX_SIDE.get(c, side) and encoder_available(c)
    )
    if usable == name:
        return name, None
    too_big = side > NVENC_MAX_SIDE.get(name, side)
    reason = f"超出 {name} 最大 {NVENC_MAX_SIDE[name]} px" if too_big else f"{name} 不可用"
    return usable, f"{reason}，改用 {usable}"
