"""Lyric video settings: frame, timing, translation brightness and the font fallback list."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QVBoxLayout

from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.project import PlanContext
from dolbytool.core.renderer import renderer_problem
from dolbytool.core.task import Step
from dolbytool.features.lyrics.service import SubtitleSpec, lyrics_step, lyrics_target
from dolbytool.ui import widgets
from dolbytool.ui.feature import FeaturePanel
from dolbytool.ui.fonts import CREDITS_TIP, FontList


class LyricsPanel(FeaturePanel):
    title = "字幕视频"
    icon = "view-media-lyrics"
    description = (
        "基于 AMLL 渲染 TTML 逐字动态歌词，输出带 Alpha 透明通道的单行居中 WebM (VP9) 视频"
    )
    enabled_key = "do_lyrics"
    enabled_default = False
    needs_audio = False

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        d = SubtitleSpec()
        self.width_spin = self.spin("lyr_w", 64, MAX_FRAME_SIDE, d.width, " px", step=2)
        self.height_spin = self.spin("lyr_h", 32, MAX_FRAME_SIDE, d.height, " px", step=2)
        self.fps = self.spin("lyr_fps", 10, 120, d.fps, " fps")
        self.font_size = self.spin("lyr_font_size", 8, 512, d.font_size, " px")
        self.fade = self.spin("lyr_fade", 0, 2000, d.fade_ms, " ms", step=50)
        self.fade.setToolTip("歌词换行时新旧行的淡入淡出过渡时长")
        self.translation = self.spin("lyr_sub", 0, 100, d.translation_opacity, " %")
        self.translation.setToolTip("翻译行文本的不透明度（0% 为隐藏）")
        self.filter_credits = self.check(
            "lyrics_filter_credits", "过滤标题与制作信息", d.filter_credits, CREDITS_TIP
        )
        self.fonts = FontList()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            widgets.section(
                "画面",
                widgets.two_column_grid([
                    ("尺寸：", widgets.size_row(self.width_spin, self.height_spin), "帧率：", self.fps),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "排版与文字",
                widgets.two_column_grid([
                    ("字号：", self.font_size, "淡入淡出：", self.fade),
                    ("翻译不透明度：", self.translation, None, None),
                    ("内容：", self.filter_credits, None, None),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(widgets.section("字体回退顺序", self.fonts))
        layout.addStretch(1)

    def spec(self) -> SubtitleSpec:
        return SubtitleSpec(
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=self.fps.value(),
            font_families=tuple(self.fonts.families()),
            font_size=self.font_size.value(),
            fade_ms=self.fade.value(),
            translation_opacity=self.translation.value(),
            filter_credits=self.filter_credits.isChecked(),
        )

    # ---- hooks -----------------------------------------------------------------------

    def problem(self, ctx: PlanContext) -> str | None:
        too_big = frame_problem(self.width_spin.value(), self.height_spin.value())
        if too_big:
            return too_big
        ttml = ctx.project.ttml
        if not ttml:
            return "请选择 TTML 逐字歌词文件"
        if not ttml.is_file():
            return "指定的歌词文件不存在"
        return renderer_problem("render.mjs")

    def summary(self, ctx: PlanContext) -> str | None:
        return f"字幕视频 {self.width_spin.value()}×{self.height_spin.value()}"

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        ttml, out = project.ttml, project.output_dir
        if ttml is None or out is None:
            return []
        # Follow the audio's length so the overlay lines up with the waveform video.
        seconds = project.info.duration if project.info else None
        target = lyrics_target(out, project.audio or ttml)
        return [lyrics_step(ttml, target, self.spec(), seconds)]

    def save_extra(self) -> None:
        self.settings.setValue("lyrics_fonts", self.fonts.families())

    def restore_extra(self) -> None:
        self.fonts.set_families(self.settings.value("lyrics_fonts", [], type=list))
