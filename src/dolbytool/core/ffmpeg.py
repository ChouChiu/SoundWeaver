"""ffmpeg / ffprobe helpers shared by every feature."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.process import NO_WINDOW, run_streaming


class FFmpegError(RuntimeError):
    pass


def missing_tools() -> list[str]:
    """The ffmpeg tools this machine lacks, for a warning before anything runs."""
    return [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]


def probe(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
             str(path)],
            check=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            creationflags=NO_WINDOW,
        )  # fmt: skip
    except FileNotFoundError as error:
        raise FFmpegError("找不到 ffprobe，请先安装 ffmpeg") from error
    except subprocess.TimeoutExpired as error:
        raise FFmpegError(f"ffprobe 读取超时：{path.name}") from error
    if result.returncode != 0:
        raise FFmpegError(result.stderr.strip() or f"ffprobe exited with {result.returncode}")
    return json.loads(result.stdout)


def first_audio_stream(data: dict) -> dict:
    stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if stream is None:
        raise FFmpegError("没有音频流")
    return stream


def duration(path: Path) -> float:
    """Container duration in seconds; raw streams without one are rejected."""
    try:
        return float(probe(path)["format"]["duration"])
    except (KeyError, ValueError) as error:
        raise FFmpegError(f"无法读取时长：{path}") from error


def run_ffmpeg(
    args: list[str],
    token: CancellationToken | None = None,
    total_seconds: float | None = None,
    on_fraction: Callable[[float], None] | None = None,
) -> None:
    """Run ffmpeg, reporting `-progress` as a 0..1 fraction and honouring cancellation."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    track = on_fraction is not None and bool(total_seconds)
    if track:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd += args

    def on_line(line: str) -> None:
        key, _, value = line.strip().partition("=")
        if track and key == "out_time_us" and value.isdigit():
            on_fraction(min(1.0, int(value) / 1e6 / total_seconds))

    try:
        run_streaming(cmd, on_line, token, error=FFmpegError)
    except FileNotFoundError as error:
        raise FFmpegError("找不到 ffmpeg，请先安装") from error


def extract_frame(source: Path, stream: int, target: Path) -> None:
    """Write one picture of `stream` (an embedded cover) to `target`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(["-i", str(source), "-map", f"0:{stream}", "-frames:v", "1", str(target)])
