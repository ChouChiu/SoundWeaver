"""Scrolling waveform video, a port of the two-step ffmpeg script.

Step 1 draws the whole track as one wide strip (`duration * pps` pixels), left
and right channels as two rows, with half a screen of background on either
side; step 2 decodes it once and slides a screen-sized window across it at
`pps` pixels per second, with a fixed playhead in the middle.  Everything
left of the playhead has been played and keeps the full waveform colour, the
right half is faded toward the background.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from dolbytool.core import ac4
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.ffmpeg import duration, run_ffmpeg
from dolbytool.core.i18n import t
from dolbytool.core.paths import atomic_output, work_root
from dolbytool.core.project import Track
from dolbytool.core.task import Report, Step, percent
from dolbytool.features.waveform.encoders import DEFAULT_ENCODER, ENCODERS, encoder_for


@dataclass(frozen=True, slots=True)
class WaveformSpec:
    pps: int = 100
    screen_width: int = 1320
    height: int = 180
    # Dark stage, cool low-saturation silver waveform, red playhead.
    background: str = "#101216"
    wave_color: str = "#c5ccd4"
    playhead: str = "#e5484d"
    # Share of the waveform colour left in the unplayed half, the rest is background.
    unplayed_level: float = 0.35
    fps: int = 30
    encoder: str = DEFAULT_ENCODER
    audio_bitrate: str = "320k"


def _fade_to_background(background: str, level: float) -> str:
    """lutrgb mixing each pixel toward the background, so the stage colour stays put."""
    rgb = [int(background.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    return "lutrgb=" + ":".join(
        f"{c}='val*{level:.4f}+{v * (1 - level):.4f}'" for c, v in zip("rgb", rgb, strict=True)
    )


def _render_strip(
    audio: Path, output: Path, width: int, spec: WaveformSpec, token: CancellationToken
) -> None:
    """The track as a `width` strip, left channel above right, padded by half a screen each side."""
    colour, h = spec.wave_color, spec.height
    graph = (
        f"color={spec.background}:s={width}x{h}:d=1[bg];"
        "[0:a]compand=attacks=0.01:decays=0.2:points=-90/-90|-30/-10|0/-3:soft-knee=6,"
        "volume=1.5,"
        f"showwavespic=s={width}x{h}:split_channels=1:colors={colour}|{colour}[w];"
        "[bg][w]overlay=format=auto,format=rgb24,"
        f"pad={width + spec.screen_width}:{h}:{spec.screen_width // 2}:0:{spec.background}"
    )
    run_ffmpeg(
        ["-i", str(audio), "-filter_complex", graph, "-frames:v", "1", "-update", "1", str(output)],
        token,
    )


def render_waveform_video(
    audio: Path,
    output: Path,
    spec: WaveformSpec,
    token: CancellationToken,
    on_fraction: Callable[[float], None] = lambda _f: None,
) -> None:
    on_fraction(0.0)
    audio = ac4.readable(audio, token)
    # yuv420p needs even dimensions
    spec = replace(spec, screen_width=spec.screen_width // 2 * 2, height=spec.height // 2 * 2)
    seconds = duration(audio)
    width = max(1, int(seconds * spec.pps))
    centre, w, h = spec.screen_width // 2, spec.screen_width, spec.height
    # The strip is decoded once and held by `loop`; every frame is a crop of it at
    # the frame's own position, worked out from the frame number so it is exact.
    video = (
        f"[0:v]loop=loop=-1:size=1,setpts=N/{spec.fps}/TB,"
        f"crop={w}:{h}:x='n*{spec.pps}/{spec.fps}':y=0,split[played][rest];"
        f"[rest]crop={w - centre}:{h}:{centre}:0,"
        f"{_fade_to_background(spec.background, spec.unplayed_level)}[unplayed];"
        f"[played][unplayed]overlay=x={centre}:y=0,"
        f"drawbox=x={centre - 1}:y=0:w=2:h={h}:color={spec.playhead}:t=fill,"
        "format=yuv420p[v]"
    )
    with tempfile.TemporaryDirectory(dir=work_root()) as scratch:
        strip = Path(scratch) / "strip.png"
        _render_strip(audio, strip, width, spec, token)
        on_fraction(0.05)
        with atomic_output(output) as partial:
            run_ffmpeg(
                [
                    "-framerate", str(spec.fps), "-i", str(strip), "-i", str(audio),
                    "-filter_complex", video, "-map", "[v]", "-map", "1:a",
                    *ENCODERS[spec.encoder], "-r", str(spec.fps),
                    "-c:a", "aac", "-b:a", spec.audio_bitrate,
                    "-t", f"{seconds:.3f}", "-shortest", "-movflags", "+faststart", str(partial),
                ],
                token,
                seconds,
                lambda f: on_fraction(0.05 + 0.95 * f),
            )  # fmt: skip
    on_fraction(1.0)


def waveform_target(output_dir: Path, source: Path) -> Path:
    return output_dir / f"{source.stem}.mp4"


def waveform_step(track: Track, target: Path, spec: WaveformSpec) -> Step:
    def run(token: CancellationToken, report: Report) -> str:
        encoder, note = encoder_for(spec.encoder, spec.screen_width, spec.height)
        if note:
            report(0, note)
        use = replace(spec, encoder=encoder)
        if not track.path.is_file():
            raise FileNotFoundError(
                t("common.labeled", label=t("error.file_not_found"), text=track.path)
            )
        render_waveform_video(
            track.path, target, use, token, percent(report, t("step.render_and_encode"))
        )
        return target.name

    # each video is light on the CPU, so several encode side by side
    return Step(t("step.waveform_video_track", label=track.label), run, (target,), parallel=True)
