"""Transparent single-line lyric video from TTML, drawn by AMLL.

The rendering lives in `renderer/` (Node + headless Chromium, entry point
`src/cli/main.ts`), run through `core.renderer`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.paths import atomic_output
from dolbytool.core.renderer import renderer_script, run_renderer
from dolbytool.core.task import Report, Step


@dataclass(frozen=True, slots=True)
class SubtitleSpec:
    width: int = 1920
    height: int = 240
    fps: int = 60
    # Fallback order: each glyph comes from the first family that has it.
    font_families: tuple[str, ...] = ()
    font_size: int = 64
    fade_ms: int = 200
    # translation line opacity in percent; AMLL's own 30 % is too faint over video
    translation_opacity: int = 70
    # drop the title and credit lines around the lyrics
    filter_credits: bool = True
    crf: int = 30


def css_font_list(families: tuple[str, ...]) -> str:
    """Quoted CSS font-family list; the page appends sans-serif as the last resort."""
    names = [json.dumps(f.strip()) for f in families if f.strip()]
    return ", ".join(names) or "sans-serif"


def render_subtitle_video(
    ttml: Path,
    output: Path,
    spec: SubtitleSpec,
    duration: float | None,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    on_status: Callable[[str], None],
) -> None:
    script = renderer_script("render.mjs")
    # VP9 with yuva420p needs even dimensions
    args = [
        "--ttml", str(ttml),
        "--width", str(spec.width // 2 * 2), "--height", str(spec.height // 2 * 2),
        "--fps", str(spec.fps), "--font", css_font_list(spec.font_families), "--font-size", str(spec.font_size),
        "--fade", str(spec.fade_ms),
        "--sub-opacity", str(spec.translation_opacity), "--crf", str(spec.crf),
        *(() if spec.filter_credits else ("--keep-credits",)),
    ]  # fmt: skip
    if duration:
        args += ["--duration", f"{duration:.3f}"]

    on_fraction(0.0)
    with atomic_output(output) as partial:
        run_renderer(
            script, [*args, "--out", str(partial)], token, on_fraction, on_status,
            "字幕渲染器退出码 {code}",
        )  # fmt: skip
    on_fraction(1.0)


def lyrics_target(output_dir: Path, name_source: Path) -> Path:
    return output_dir / f"{name_source.stem}_lyrics.webm"


def lyrics_step(ttml: Path, target: Path, spec: SubtitleSpec, duration: float | None) -> Step:
    """`duration` follows the audio so the overlay lines up with the waveform video."""

    def run(token: CancellationToken, report: Report) -> str:
        done = 0

        def on_fraction(fraction: float) -> None:
            nonlocal done
            done = round(fraction * 100)
            report(done, "逐帧渲染与编码")

        # a status line must not rewind the bar
        render_subtitle_video(
            ttml, target, spec, duration, token, on_fraction, lambda message: report(done, message)
        )
        return target.name

    return Step("字幕视频", run, (target,))
