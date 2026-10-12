"""Transparent sound field videos of a Dolby Atmos track, one per chosen track.

The object positions and levels come from the Atmos decoder's trace
(`core.atmos.trace_atmos`); the room, the listener and the objects are drawn
by three.js in the headless-browser renderer (`renderer/src/soundfield/`),
frame by frame on the container's timeline, so the video lines up with the
audio, the stems and the other videos.

A video for a stem still shows every audible object, so a moving one never
jumps; it is dimmed while its sound does not reach that stem's speakers, judged
by the share of its energy the render to the chosen layout gives them against
the same gate.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dolbytool.core.atmos import trace_atmos
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.paths import atomic_output, work_root
from dolbytool.core.renderer import renderer_script, run_renderer
from dolbytool.core.speakers import DEFAULT_ATMOS, SpeakerLayout
from dolbytool.core.task import Report, Step

# The speakers behind each stem, as far as the stem is a set of speakers; the
# source itself and the fold-down hold all of them (None).  A layout without
# some of them simply has fewer.
ALL_SPEAKERS = None
TRACK_SPEAKERS: dict[str, tuple[str, ...] | None] = {
    "original": ALL_SPEAKERS,
    "downmix": ALL_SPEAKERS,
    "surround": ("Ls", "Rs", "Lb", "Rb"),
    "back": ("Lb", "Rb"),
    "wide": ("Lw", "Rw"),
    "center": ("C",),
    "front": ("L", "R"),
    "front_no_center": ("L", "R"),
    "height": ("Tfl", "Tfr", "Tsl", "Tsr", "Tbl", "Tbr"),
    "height_front": ("Tfl", "Tfr"),
    "height_rear": ("Tbl", "Tbr"),
    "height_side": ("Tsl", "Tsr"),
}


def track_speakers(key: str, layout: SpeakerLayout) -> tuple[str, ...]:
    """The speakers of `layout` a track's objects are judged by, every one but the LFE for all."""
    wanted = TRACK_SPEAKERS[key]
    return tuple(s for s in layout.positions if wanted is None or s in wanted)


# camera presets the renderer knows, with their labels
VIEWS = {"iso": "等轴测", "top": "顶部", "angle": "Angle（对面低角度）"}
COLOR_MODES = {"tint": "统一色调", "height": "按高度（地面冷、顶部暖）", "object": "按对象区分"}
# share of the progress bar the object trace takes; drawing is the rest
TRACE_SHARE = 0.3


@dataclass(frozen=True, slots=True)
class SoundfieldSpec:
    width: int = 1920
    height: int = 1080
    fps: int = 60
    view: str = "iso"
    # GLB head model; None draws the built-in silhouette
    head: Path | None = None
    # percent of the default sizes
    object_scale: int = 130
    head_scale: int = 200
    # objects quieter than this fade out
    gate_db: int = -90
    fade_ms: int = 180
    # opacity, in percent, of an object whose sound is outside the video's track
    dim: int = 25
    # percent: how much brightness, halo and size follow each object's level
    level_response: int = 0
    # percent strength of the halo, 0 for none
    glow: int = 0
    # tenths of a second of path drawn behind each object, 0 for none
    trail: int = 0
    drop: bool = False
    color_mode: str = "tint"
    # percent of the distance that just fits the room; above 100 is closer
    zoom: int = 100
    crf: int = 30


@dataclass(frozen=True, slots=True)
class SoundfieldVideo:
    key: str  # track key, see TRACK_SPEAKERS
    label: str
    target: Path


def render_soundfield_videos(
    audio: Path,
    videos: list[SoundfieldVideo],
    spec: SoundfieldSpec,
    token: CancellationToken,
    duration: float | None = None,
    on_fraction: Callable[[float], None] = lambda _f: None,
    on_status: Callable[[str], None] = lambda _m: None,
    layout: SpeakerLayout = DEFAULT_ATMOS,
) -> list[Path]:
    """Trace the objects once, then draw one video per entry of `videos`."""
    script = renderer_script("soundfield.mjs")
    on_fraction(0.0)
    with tempfile.TemporaryDirectory(dir=work_root()) as work:
        on_status("解析全景声对象")
        trace = trace_atmos(audio, token, lambda f: on_fraction(f * TRACE_SHARE), layout)
        share = (1 - TRACE_SHARE) / max(1, len(videos))
        for i, video in enumerate(videos):
            start = TRACE_SHARE + i * share
            prefix = f"{video.label}：" if len(videos) > 1 else ""
            scene = Path(work) / f"scene-{i}.json"
            wanted = TRACK_SPEAKERS[video.key]
            data = trace.to_json(None if wanted is ALL_SPEAKERS else wanted)
            scene.write_text(json.dumps(data, separators=(",", ":")))
            with atomic_output(video.target) as partial:
                run_renderer(
                    script, _args(scene, partial, spec, duration), token,
                    lambda f, start=start: on_fraction(start + share * f),
                    lambda m, prefix=prefix: on_status(prefix + m),
                    "声场渲染器退出码 {code}",
                )  # fmt: skip
    on_fraction(1.0)
    return [v.target for v in videos]


def _args(scene: Path, output: Path, spec: SoundfieldSpec, duration: float | None) -> list[str]:
    # VP9 with yuva420p needs even dimensions
    args = [
        "--scene", str(scene), "--out", str(output),
        "--width", str(spec.width // 2 * 2), "--height", str(spec.height // 2 * 2),
        "--fps", str(spec.fps), "--view", spec.view,
        "--object-scale", str(spec.object_scale / 100), "--head-scale", str(spec.head_scale / 100),
        # a negative value has to be attached to its flag for Node's parseArgs
        f"--gate-db={spec.gate_db}", "--fade", str(spec.fade_ms), "--crf", str(spec.crf),
        "--dim", str(spec.dim / 100),
        "--level-response", str(spec.level_response / 100), "--glow", str(spec.glow / 100),
        "--trail", str(spec.trail / 10), "--color", spec.color_mode, "--zoom", str(spec.zoom / 100),
        *(["--drop"] if spec.drop else []),
    ]  # fmt: skip
    if spec.head:
        args += ["--head", str(spec.head)]
    if duration:
        args += ["--duration", f"{duration:.3f}"]
    return args


def soundfield_target(output_dir: Path, track_path: Path) -> Path:
    """Named after the track it shows, like the waveform videos."""
    return output_dir / f"{track_path.stem}_soundfield.webm"


def soundfield_step(
    audio: Path,
    videos: list[SoundfieldVideo],
    spec: SoundfieldSpec,
    duration: float | None,
    layout: SpeakerLayout = DEFAULT_ATMOS,
) -> Step:
    """`duration` follows the probed audio length, as the lyric video does."""

    def run(token: CancellationToken, report: Report) -> str:
        done = 0
        message = "解析全景声对象"

        def on_fraction(fraction: float) -> None:
            nonlocal done
            done = round(fraction * 100)
            report(done, message if fraction < TRACE_SHARE else "逐帧渲染与编码")

        def on_status(text: str) -> None:
            nonlocal message
            message = text
            # a status line must not rewind the bar
            report(done, text)

        paths = render_soundfield_videos(
            audio, videos, spec, token, duration, on_fraction, on_status, layout
        )
        return "、".join(p.name for p in paths)

    title = "声场视频" if len(videos) == 1 else f"声场视频（{len(videos)} 条）"
    return Step(title, run, tuple(v.target for v in videos))
