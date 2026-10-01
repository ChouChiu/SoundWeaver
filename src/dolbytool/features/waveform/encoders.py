"""Video encoders the waveform video can use, and a probe for whether one works here."""

from __future__ import annotations

import subprocess
from functools import cache

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


@cache
def encoder_available(name: str) -> bool:
    """Encode one tiny frame; a listed NVENC encoder still fails without a usable GPU/driver."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=black:s=256x256:d=0.1", "-frames:v", "1", "-c:v", name, "-f", "null", "-"],
        check=False,
        capture_output=True,
    )  # fmt: skip
    return result.returncode == 0
