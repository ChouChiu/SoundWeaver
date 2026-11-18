"""Video encoders for waveform & player renders, plus availability probing."""

from __future__ import annotations

import shutil
import subprocess
from functools import cache

from soundweaver.core.i18n import t
from soundweaver.core.process import NO_WINDOW

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

DEFAULT_ENCODER = "h264_nvenc"
FALLBACK_ENCODER = "libx264"
NVENC_MAX_SIDE = {"h264_nvenc": 4096, "hevc_nvenc": 8192}
_PROBE_TIMEOUT = 15


@cache
def encoder_available(name: str) -> bool:
    """Encode a tiny frame to verify a listed GPU/software encoder actually works here."""
    if shutil.which("ffmpeg") is None:
        return False
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", "color=black:s=256x256:d=0.1", "-frames:v", "1",
                "-c:v", name, "-f", "null", "-",
            ],
            check=False,
            capture_output=True,
            timeout=_PROBE_TIMEOUT,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def encoder_for(name: str, width: int, height: int) -> tuple[str, str | None]:
    """Return `name`, or the best working fallback encoder for the frame dimensions."""
    side = max(width, height)
    candidates = [name]
    if name == "h264_nvenc":
        candidates.append("hevc_nvenc")
    candidates.append(FALLBACK_ENCODER)
    usable = next(
        c
        for c in candidates
        if c == FALLBACK_ENCODER or (side <= NVENC_MAX_SIDE.get(c, side) and encoder_available(c))
    )
    if usable == name:
        return name, None
    too_big = side > NVENC_MAX_SIDE.get(name, side)
    reason = (
        t("encoder.too_large", name=name, side=NVENC_MAX_SIDE[name])
        if too_big
        else t("encoder.unavailable", name=name)
    )
    return usable, t("encoder.fallback", reason=reason, usable=usable)
