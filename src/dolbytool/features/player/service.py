"""Player-page video from TTML, cover and audio, drawn by AMLL's PrebuiltLyricPlayer.

The rendering lives in `renderer/` (Node + headless Chromium, entry point
`src/cli/player.ts`), run through `core.renderer`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from dolbytool.core.cancel import CancellationToken
from dolbytool.core.paths import atomic_output
from dolbytool.core.renderer import renderer_script, run_renderer
from dolbytool.core.task import Report, Step
from dolbytool.features.lyrics.service import css_font_list
from dolbytool.features.waveform.encoders import FALLBACK_ENCODER, encoder_available

BACKGROUNDS = {
    "mesh": "流体渐变 (Mesh Gradient)",
    "isolation": "隔离色块 (Isolation)",
    "solid": "纯色",
    "none": "透明 (仅 VP9)",
}
CODECS = {
    "libx264": "H.264 (软件编码)",
    "h264_nvenc": "H.264 (NVIDIA 硬件编码)",
    "hevc_nvenc": "HEVC (NVIDIA 硬件编码)",
    "vp9": "VP9 + Alpha (透明 WebM)",
}
# a sensible quality per encoder: CRF for the software ones, CQ for NVENC
DEFAULT_QUALITY = {"libx264": 18, "h264_nvenc": 19, "hevc_nvenc": 21, "vp9": 30}
LYRIC_SIZES = {
    "tiny": "极小",
    "extra-small": "很小",
    "small": "小",
    "medium": "中（默认）",
    "large": "大",
    "extra-large": "很大",
    "huge": "巨大",
}
CONTROLS = {"controls": "播放按钮", "fft": "音频频谱", "none": "不显示"}
AUDIO_MODES = {"encode": "转码 (AAC / Opus)", "copy": "复制原音轨", "none": "不混入"}


@dataclass(frozen=True, slots=True)
class PlayerSpec:
    width: int = 1920
    height: int = 1080
    fps: int = 60
    codec: str = "libx264"
    quality: int = DEFAULT_QUALITY["libx264"]
    audio_mode: str = "encode"
    # percent device scale of the page; 0 picks one from the frame (1080p landscape 100 %, portrait as a phone)
    ui_scale: int = 0
    # background
    background: str = "mesh"
    # percent of AMLL's flow speed and of the frame resolution
    flow_speed: int = 100
    render_scale: int = 50
    color: str = "#111111"
    seed: int = 1
    light_wave: bool = False
    beat: bool = True
    # lyrics; font weight 0 keeps AMLL's own
    font_families: tuple[str, ...] = ()
    font_weight: int = 0
    # hundredths of an em
    letter_spacing: int = 0
    lyric_size: str = "medium"
    # percent of the line height
    word_fade: int = 50
    blur: bool = True
    scale: bool = True
    spring: bool = True
    translation: bool = True
    romanization: bool = False
    swap_trans_roman: bool = False
    background_vocals: bool = True
    # wrapped lines get rows of even length
    balance: bool = True
    # landscape: percent of the lyric column left empty on its right; -1 keeps AMLL's 15 % / 8 %
    lyric_padding: int = 3
    # song info and controls
    title: str = ""
    artists: tuple[str, ...] = ()
    album: str = ""
    show_name: bool = True
    show_artists: bool = True
    show_album: bool = False
    controls: str = "controls"
    show_volume: bool = False
    volume: int = 100
    bottom_controls: bool = False
    show_remaining: bool = False
    # "" hides the badge
    quality_tag: str = ""
    dolby_atmos: bool = False

    @property
    def suffix(self) -> str:
        return ".webm" if self.codec == "vp9" else ".mp4"


def _flag(on: bool, name: str) -> list[str]:
    return [name] if on else []


def player_args(spec: PlayerSpec, duration: float | None) -> list[str]:
    args = [
        "--width", str(spec.width // 2 * 2), "--height", str(spec.height // 2 * 2),
        "--fps", str(spec.fps), "--codec", spec.codec, "--crf", str(spec.quality),
        "--background", spec.background, "--flow-speed", f"{spec.flow_speed / 100:g}",
        "--render-scale", f"{spec.render_scale / 100:g}", "--color", spec.color, "--seed", str(spec.seed),
        "--font-weight", str(spec.font_weight), "--letter-spacing", f"{spec.letter_spacing / 100:g}",
        "--lyric-size", spec.lyric_size, "--word-fade", f"{max(spec.word_fade, 1) / 100:g}",
        "--controls", spec.controls, "--ui-scale", f"{spec.ui_scale / 100:g}",
        "--lyric-padding", str(spec.lyric_padding), *_flag(not spec.balance, "--no-balance"),
        *_flag(spec.light_wave, "--light-wave"),
        *_flag(not spec.blur, "--no-blur"), *_flag(not spec.scale, "--no-scale"),
        *_flag(not spec.spring, "--no-spring"), *_flag(not spec.translation, "--no-translation"),
        *_flag(spec.romanization, "--romanization"), *_flag(spec.swap_trans_roman, "--swap-trans-roman"),
        *_flag(not spec.background_vocals, "--no-bg-vocals"),
        *_flag(not spec.show_name, "--no-name"), *_flag(not spec.show_artists, "--no-artists"),
        *_flag(spec.show_album, "--show-album"), *_flag(spec.bottom_controls, "--bottom-controls"),
        *_flag(spec.show_remaining, "--remaining"), *_flag(spec.dolby_atmos, "--atmos"),
    ]  # fmt: skip
    if spec.font_families:
        args += ["--font", css_font_list(spec.font_families)]
    if spec.show_volume:
        args += ["--volume", str(spec.volume)]
    if spec.quality_tag:
        args += ["--quality", spec.quality_tag]
    if spec.title:
        args += ["--title", spec.title]
    for artist in spec.artists:
        args += ["--artist", artist]
    if spec.album:
        args += ["--album", spec.album]
    if duration:
        args += ["--duration", f"{duration:.3f}"]
    return args


def render_player_video(
    ttml: Path,
    cover: Path | None,
    audio: Path | None,
    output: Path,
    spec: PlayerSpec,
    duration: float | None,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    on_status: Callable[[str], None],
    excerpt: tuple[float, float] | None = None,
) -> None:
    """`duration` is the song's; `excerpt` (start, length in seconds) renders only that part."""
    script = renderer_script("player.mjs")
    args = ["--ttml", str(ttml), *player_args(spec, duration)]
    if excerpt:
        start, length = excerpt
        args += ["--start", f"{start:.3f}", "--duration", f"{length:.3f}", "--jobs", "1"]
        if duration:
            args += ["--song-length", f"{duration:.3f}"]
    if cover:
        args += ["--cover", str(cover)]
    if audio:
        args += ["--audio", str(audio), "--audio-mode", spec.audio_mode]
        if spec.beat and spec.background == "mesh":
            args.append("--beat")
    on_fraction(0.0)
    with atomic_output(output) as partial:
        run_renderer(
            script, [*args, "--out", str(partial)], token, on_fraction, on_status,
            "播放器渲染器退出码 {code}",
        )  # fmt: skip
    on_fraction(1.0)


# what a preview gives up for speed: half the size at 30 fps, a quick encode
PREVIEW_SCALE = 0.5
PREVIEW_FPS = 30


def preview_spec(spec: PlayerSpec) -> PlayerSpec:
    """`spec` at half size, laid out the same: the page scale halves with the frame."""
    vp9 = spec.background == "none"
    return replace(
        spec,
        width=max(64, round(spec.width * PREVIEW_SCALE)),
        height=max(64, round(spec.height * PREVIEW_SCALE)),
        ui_scale=round(spec.ui_scale * PREVIEW_SCALE),
        fps=min(spec.fps, PREVIEW_FPS),
        codec="vp9" if vp9 else "libx264",
        quality=33 if vp9 else 23,
        audio_mode="encode" if spec.audio_mode != "none" else "none",
    )


def player_target(output_dir: Path, name_source: Path, spec: PlayerSpec) -> Path:
    return output_dir / f"{name_source.stem}_player{spec.suffix}"


def player_step(
    ttml: Path,
    cover: Path | None,
    audio: Path | None,
    target: Path,
    spec: PlayerSpec,
    duration: float | None,
) -> Step:
    """`duration` is the song's, so the progress bar and the video run its whole length."""

    def run(token: CancellationToken, report: Report) -> str:
        use = spec
        if use.codec.endswith("_nvenc") and not encoder_available(use.codec):
            report(0, f"{use.codec} 不可用，改用 {FALLBACK_ENCODER}")
            use = replace(use, codec=FALLBACK_ENCODER, quality=DEFAULT_QUALITY[FALLBACK_ENCODER])
        done = 0

        def on_fraction(fraction: float) -> None:
            nonlocal done
            done = round(fraction * 100)
            report(done, "逐帧渲染与编码")

        # a status line must not rewind the bar
        render_player_video(
            ttml, cover, audio, target, use, duration, token, on_fraction,
            lambda message: report(done, message),
        )  # fmt: skip
        return target.name

    return Step("播放器视频", run, (target,))
