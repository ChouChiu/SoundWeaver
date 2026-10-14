"""Player-page video from TTML, cover and audio, drawn by AMLL's PrebuiltLyricPlayer.

The rendering lives in `renderer/` (Node + headless Chromium, entry point
`src/cli/player.ts`), run through `core.renderer`.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from dolbytool.core import ffmpeg, motion
from dolbytool.core.cancel import CancellationToken
from dolbytool.core.paths import atomic_output, work_root
from dolbytool.core.renderer import renderer_script, run_renderer
from dolbytool.core.task import Progress, Report, Step
from dolbytool.features.lyrics.service import css_font_list
from dolbytool.features.waveform.encoders import encoder_for

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
    # drop the title and credit lines around the lyrics
    filter_credits: bool = True
    # JPEG frames from the browser: about 1.5x faster, fine detail a little softer
    fast_capture: bool = True
    # draw at twice the size and scale down: smoother text edges, about 2-3x slower
    supersample: bool = False
    # the line a song without lyrics shows; empty leaves the cover alone
    instrumental_text: str = "纯音乐，请欣赏"
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
        # Matroska holds any audio as is: E-AC-3 JOC, and the PCM of a stem
        return ".webm" if self.codec == "vp9" else ".mkv"


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
        "--instrumental-text", spec.instrumental_text, *_flag(spec.fast_capture, "--fast-capture"),
        *_flag(spec.supersample, "--supersample"),
        *_flag(not spec.filter_credits, "--keep-credits"),
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
    ttml: Path | None,
    cover: Path | None,
    audio: Path | None,
    output: Path,
    spec: PlayerSpec,
    duration: float | None,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    on_status: Callable[[str], None],
    excerpt: tuple[float, float] | None = None,
    playlist: Path | None = None,
) -> None:
    """`duration` is the song's; `excerpt` (start, length in seconds) renders only that part.

    Motion artwork as `cover` loops on the cover; the background takes its first frame.
    Without `ttml` (an instrumental) the cover stands alone, for `duration`.  A
    `playlist` (see `render_queue`) plays in place of `ttml`, `audio` its songs' joined.
    """
    script = renderer_script("player.mjs")
    source = ["--playlist", str(playlist)] if playlist else ["--ttml", str(ttml)] if ttml else []
    args = [*source, *player_args(spec, duration)]
    if excerpt:
        start, length = excerpt
        args += ["--start", f"{start:.3f}", "--duration", f"{length:.3f}", "--jobs", "1"]
        if duration:
            args += ["--song-length", f"{duration:.3f}"]
    on_fraction(0.0)
    if cover and motion.is_motion(cover):
        on_status("提取动态封面帧")
        frames = motion.frames(cover, motion.side_for(spec.width, spec.height), token)
        args += ["--cover", str(motion.still(cover)), "--cover-frames", str(frames)]
    elif cover:
        args += ["--cover", str(cover)]
    if audio:
        args += ["--audio", str(audio), "--audio-mode", spec.audio_mode]
        if spec.beat and spec.background == "mesh":
            args.append("--beat")
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


def _available(spec: PlayerSpec, report: Report) -> PlayerSpec:
    """`spec` with libx264 in place of an NVENC encoder this machine lacks."""
    if spec.codec.endswith("_nvenc"):
        codec, note = encoder_for(spec.codec, spec.width, spec.height)
        if note:
            report(0, note)
            return replace(spec, codec=codec, quality=DEFAULT_QUALITY[codec])
    return spec


from dolbytool.core.i18n import t


def player_step(
    ttml: Path | None,
    cover: Path | None,
    audio: Path | None,
    target: Path,
    spec: PlayerSpec,
    duration: float | None,
) -> Step:
    """`duration` is the song's, so the progress bar and the video run its whole length."""

    def run(token: CancellationToken, report: Report) -> str:
        use = _available(spec, report)
        progress = Progress(report, t("step.frame_render_encode"))
        render_player_video(
            ttml, cover, audio, target, use, duration, token, progress.fraction, progress.status
        )
        return target.name

    return Step(t("step.player_video"), run, (target,))


@dataclass(frozen=True, slots=True)
class QueueTrack:
    """A song of a player video that plays several back to back."""

    audio: Path
    ttml: Path | None
    title: str
    artists: tuple[str, ...]
    album: str


def _needs_audio(spec: PlayerSpec) -> bool:
    return (
        spec.audio_mode != "none"
        or spec.controls == "fft"
        or (spec.beat and spec.background == "mesh")
    )


def render_queue(
    tracks: Sequence[QueueTrack],
    cover: Path | None,
    output: Path,
    spec: PlayerSpec,
    token: CancellationToken,
    on_fraction: Callable[[float], None],
    on_status: Callable[[str], None],
) -> None:
    """`tracks` played one after another as the player's queue would, an album say.

    One page plays them all, so at each song the info, progress and lyrics
    change over as in the player while the background flows on.  Their audio is
    joined without re-encoding, which suits an album's tracks of one format.
    """
    lengths = [ffmpeg.duration(track.audio) for track in tracks]
    with tempfile.TemporaryDirectory(dir=work_root()) as scratch:
        playlist = Path(scratch) / "playlist.json"
        entries = [
            {"ttml": str(t.ttml) if t.ttml else None, "title": t.title,
             "artists": list(t.artists), "album": t.album, "duration": length}
            for t, length in zip(tracks, lengths, strict=True)
        ]  # fmt: skip
        playlist.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        audio = None
        if _needs_audio(spec):
            on_status(t("step.concat_album_audio"))
            listing = Path(scratch) / "audio.txt"
            listing.write_text(
                "".join("file '{}'\n".format(str(t.audio).replace("'", "'\\''")) for t in tracks),
                encoding="utf-8",
            )
            audio = Path(scratch) / "queue.mka"
            ffmpeg.run_ffmpeg(
                ["-f", "concat", "-safe", "0", "-i", str(listing), "-map", "0:a", "-c", "copy",
                 str(audio)],
                token,
            )  # fmt: skip
        render_player_video(
            None, cover, audio, output, spec, sum(lengths), token, on_fraction, on_status,
            playlist=playlist,
        )  # fmt: skip


def queue_step(
    tracks: Sequence[QueueTrack], cover: Path | None, target: Path, spec: PlayerSpec
) -> Step:
    def run(token: CancellationToken, report: Report) -> str:
        use = _available(spec, report)
        progress = Progress(report, t("step.frame_render_encode"))
        render_queue(tracks, cover, target, use, token, progress.fraction, progress.status)
        return t("step.album_continuous_title", name=target.name, count=len(tracks))

    return Step(t("step.album_player_video"), run, (target,))

