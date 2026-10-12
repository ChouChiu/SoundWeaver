"""Vertical two-colour gradient, the maskedmerge + smoothstep command parameterised."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from dolbytool.core import motion
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.ffmpeg import run_ffmpeg
from dolbytool.core.paths import atomic_output
from dolbytool.core.task import Report, Step
from dolbytool.features.gradient import palette

# The reference: 4000x4000 with the blend running from y=1200 to y=2800
REFERENCE_SIZE = 4000
REFERENCE_START = 1200
REFERENCE_SPAN = 1600


@dataclass(frozen=True, slots=True)
class GradientSpec:
    top: str
    bottom: str
    width: int = 4000
    height: int = 4000
    start: float = REFERENCE_START / REFERENCE_SIZE
    span: float = REFERENCE_SPAN / REFERENCE_SIZE

    def weight(self, y: float) -> float:
        """Share of the bottom colour at relative height `y` (0 top, 1 bottom), smoothstepped."""
        t = min(1.0, max(0.0, (y - self.start) / self.span))
        return t * t * (3 - 2 * t)


def gradient_target(output_dir: Path, audio: Path) -> Path:
    return output_dir / f"{audio.stem}_gradient.png"


def render_gradient(spec: GradientSpec, output: Path, token: CancellationToken) -> None:
    """Same smoothstep as `GradientSpec.weight`, evaluated by ffmpeg once per row."""
    w, h = spec.width, spec.height
    y0 = round(spec.start * h)
    span = max(1, round(spec.span * h))
    t = f"clip((Y-{y0})/{span},0,1)"
    # one column of the mask, widened: the blend only changes from row to row
    mask = f"nullsrc=s=1x{h},geq=lum='{t}*{t}*(3-2*{t})*255',scale={w}:{h}:flags=neighbor"
    with atomic_output(output) as partial:
        run_ffmpeg(
            [
                "-f", "lavfi", "-i", f"color=c={spec.top}:s={w}x{h}",
                "-f", "lavfi", "-i", f"color=c={spec.bottom}:s={w}x{h}",
                "-f", "lavfi", "-i", mask,
                "-filter_complex", "[0][1][2]maskedmerge",
                "-frames:v", "1", str(partial),
            ],
            token,
        )  # fmt: skip


def cover_spec(spec: GradientSpec, cover: Path) -> GradientSpec:
    """`spec` with its colours taken from the dominant colour of `cover` (motion artwork's first frame)."""
    swatches = palette.palette(motion.still(cover))
    top, bottom = palette.gradient_pair(palette.dominant(swatches).rgb)
    return replace(spec, top=top, bottom=bottom)


def gradient_step(spec: GradientSpec, target: Path, cover: Path | None = None) -> Step:
    """With `cover`, the colours are picked from it when the step runs."""

    def run(token: CancellationToken, report: Report) -> str:
        used = spec
        if cover is not None:
            report(5, "提取封面主色调")
            used = cover_spec(spec, cover)
        report(10, f"{used.top} → {used.bottom}")
        render_gradient(used, target, token)
        return target.name

    return Step("渐变图", run, (target,))
