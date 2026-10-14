"""Player video settings: frame and encoder, background, lyrics, song info and controls, fonts."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QThread, Signal
from PySide6.QtWidgets import QVBoxLayout
from qfluentwidgets import (
    CaptionLabel,
    PrimaryPushButton,
    ProgressBar,
)

from dolbytool.core.cancel import CancellationToken, ProcessingCancelled
from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.paths import work_root
from dolbytool.core.project import ORIGINAL, PlanContext, Project
from dolbytool.core.renderer import renderer_problem
from dolbytool.core.stems import stem_label
from dolbytool.core.task import Progress, Step
from dolbytool.features.player.service import (
    AUDIO_MODES,
    BACKGROUNDS,
    CODECS,
    CONTROLS,
    DEFAULT_QUALITY,
    LYRIC_SIZES,
    PlayerSpec,
    QueueTrack,
    player_step,
    player_target,
    preview_spec,
    queue_step,
    render_player_video,
)
from dolbytool.features.source.lyrics import without_number
from dolbytool.ui import fluent, widgets
from dolbytool.ui.feature import FeaturePanel
from dolbytool.ui.fonts import CREDITS_TIP, FontList
from dolbytool.ui.video_preview import VideoPreview

logger = logging.getLogger(__name__)

ATMOS_TAG = "Dolby Atmos"


def _split_artists(text: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in text.replace("、", "/").split("/") if a.strip())


QUALITY_RANGE = {"libx264": (0, 51), "h264_nvenc": (1, 51), "hevc_nvenc": (1, 51), "vp9": (0, 63)}
ALBUM_MODES = {
    "tracks": "每首单独的视频",
    "queue": "整张专辑连续播放",
    "both": "两者都生成",
}


class PreviewJob(QThread):
    """Renders a few seconds of the player video off the UI thread."""

    progress = Signal(int, str)
    # the preview file, or None with the error (empty when cancelled)
    done = Signal(object, str)

    def __init__(self, render, target: Path, parent=None) -> None:
        super().__init__(parent)
        self._render = render
        self.target = target
        self.token = CancellationToken()

    def run(self) -> None:
        progress = Progress(self.progress.emit, "逐帧渲染")
        try:
            self._render(self.target, self.token, progress.fraction, progress.status)
        except ProcessingCancelled:
            self.done.emit(None, "")
            return
        except Exception as error:  # noqa: BLE001 - shown to the user, the app stays up
            self.done.emit(None, str(error) or type(error).__name__)
            return
        self.done.emit(self.target, "")


class PlayerPanel(FeaturePanel):
    title_key = "player.title"
    description_key = "player.description"
    _title = "播放器视频"
    icon = "media-playback-start"
    _description = (
        "基于 AMLL Prebuilt Player 渲染完整播放器页面：封面、曲目信息、进度条、播放控件、"
        "流动背景与逐字滚动歌词，横屏 / 竖屏自动切换布局，可混入音频"
    )
    enabled_key = "do_player"
    enabled_default = False
    needs_audio = False

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        from dolbytool.core.i18n import t

        d = PlayerSpec()

        # ---- frame and encoding
        self.width_spin = self.spin("pl_w", 64, MAX_FRAME_SIDE, d.width, " px", step=2)
        self.height_spin = self.spin("pl_h", 64, MAX_FRAME_SIDE, d.height, " px", step=2)
        self.height_spin.setToolTip(t("player.height_tip"))
        self.fps = self.spin("pl_fps", 10, 240, d.fps, " fps")
        self.fps.setToolTip(t("player.fps_tip"))
        codecs_labels = {
            "libx264": t("option.encoder.libx264"),
            "h264_nvenc": t("option.encoder.nvenc_h264"),
            "hevc_nvenc": t("option.encoder.nvenc_hevc"),
            "vp9": t("option.encoder.vp9"),
        }
        self.codec = self.combo("pl_codec", codecs_labels, d.codec)
        self.codec.setToolTip(t("player.codec_tip"))
        self.quality = self.spin("pl_quality", 0, 63, d.quality)
        self.quality.setToolTip(t("player.quality_tip"))
        self.codec.currentIndexChanged.connect(self._codec_changed)
        audio_modes_labels = {
            "encode": t("option.audio_mode.encode"),
            "copy": t("option.audio_mode.copy"),
            "none": t("option.audio_mode.none"),
        }
        self.audio_mode = self.combo("pl_audio", audio_modes_labels, d.audio_mode)
        self.audio_mode.setToolTip(t("player.audio_mode_tip"))
        self.audio_mode.currentIndexChanged.connect(self._sync_enabled)
        # the stem to mix in, by kind, so every track of a batch takes the same one
        self._audio_key = ORIGINAL
        self.audio_track = fluent.ComboBox()

        self.audio_track.setToolTip(t("player.audio_track_tip"))
        self.audio_track.activated.connect(self._audio_track_picked)
        self.fast_capture = self.check(
            "pl_fast_capture", t("player.fast_capture"), d.fast_capture,
            t("player.fast_capture_tip"),
        )  # fmt: skip
        self.supersample = self.check(
            "pl_supersample", t("player.supersample"), d.supersample,
            t("player.supersample_tip"),
        )  # fmt: skip
        album_modes_labels = {
            "tracks": t("option.album_mode.tracks"),
            "queue": t("option.album_mode.queue"),
            "both": t("option.album_mode.both"),
        }
        self.album_mode = self.combo("pl_album_mode", album_modes_labels, "tracks")
        self.album_mode.setToolTip(t("player.album_mode_tip"))
        self.ui_scale = self.spin("pl_ui_scale", 0, 800, d.ui_scale, " %", step=25)
        self.ui_scale.setSpecialValueText(t("action.confirm") if False else "自动")
        self.ui_scale.setToolTip(t("player.ui_scale_tip"))

        # ---- background
        bg_labels = {
            "mesh": t("option.bg.mesh"),
            "isolation": t("option.bg.isolation"),
            "solid": t("option.bg.solid"),
            "none": t("option.bg.none"),
        }
        self.background = self.combo("pl_bg", bg_labels, d.background)
        self.background.currentIndexChanged.connect(self._sync_enabled)
        self.flow = self.spin("pl_flow", 0, 1000, d.flow_speed, " %", step=10)
        self.flow.setToolTip(t("player.flow_tip"))
        self.render_scale = self.spin("pl_bg_scale", 5, 200, d.render_scale, " %", step=5)
        self.render_scale.setToolTip(t("player.render_scale_tip"))
        self.bg_color = self.color("pl_color", d.color, t("player.pick_bg_color"))
        self.seed = self.spin("pl_seed", 0, 999999, d.seed)
        self.seed.setToolTip(t("player.seed_tip"))
        self.light_wave = self.check("pl_light_wave", t("player.light_wave"), d.light_wave, t("player.light_wave_tip"))
        self.beat = self.check(
            "pl_beat",
            t("player.beat"),
            d.beat,
            t("player.beat_tip"),
        )

        # ---- lyrics
        lyric_size_labels = {
            "tiny": t("option.size.tiny"),
            "extra-small": t("option.size.extra_small"),
            "small": t("option.size.small"),
            "medium": t("option.size.medium"),
            "large": t("option.size.large"),
            "extra-large": t("option.size.extra_large"),
            "huge": t("option.size.huge"),
        }
        self.lyric_size = self.combo("pl_lyric_size", lyric_size_labels, d.lyric_size)
        self.font_weight = self.spin("pl_font_weight", 0, 1000, d.font_weight, step=100)
        self.font_weight.setSpecialValueText("默认")
        self.letter_spacing = self.spin("pl_letter_spacing", -50, 50, d.letter_spacing, " %em")
        self.word_fade = self.spin("pl_word_fade", 1, 500, d.word_fade, " %", step=5)
        self.word_fade.setToolTip(t("player.word_fade_tip"))
        self.blur = self.check("pl_blur", t("player.blur"), d.blur)
        self.scale = self.check("pl_scale", t("player.scale"), d.scale)
        self.spring = self.check("pl_spring", t("player.spring"), d.spring)
        self.translation = self.check("pl_translation", t("player.translation"), d.translation)
        self.romanization = self.check("pl_roman", t("player.romanization"), d.romanization)
        self.swap = self.check("pl_swap", t("player.swap"), d.swap_trans_roman)
        self.bg_vocals = self.check("pl_bg_vocals", t("player.bg_vocals"), d.background_vocals)
        self.filter_credits = self.check(
            "pl_filter_credits",
            t("lyrics.filter_credits"),
            d.filter_credits,
            t("lyrics.credits_tip"),
        )
        self.balance = self.check(
            "pl_balance", t("player.balance"), d.balance, t("player.balance_tip")
        )
        self.lyric_padding = self.spin("pl_lyric_padding", -1, 50, d.lyric_padding, " %")
        self.lyric_padding.setSpecialValueText("AMLL")
        self.lyric_padding.setToolTip(t("player.lyric_padding_tip"))

        # ---- song info and controls
        self.title_edit = self.edit("pl_title", t("player.title_placeholder"))
        self.artist_edit = self.edit("pl_artist", t("player.artist_placeholder"))
        self.album_edit = self.edit("pl_album", "")
        self.show_name = self.check("pl_show_name", t("player.show_name"), d.show_name)
        self.show_artists = self.check("pl_show_artists", t("player.show_artists"), d.show_artists)
        self.show_album = self.check("pl_show_album", t("player.show_album"), d.show_album)
        controls_labels = {
            "controls": t("option.controls.controls"),
            "fft": t("option.controls.fft"),
            "none": t("option.controls.none"),
        }
        self.controls = self.combo("pl_controls", controls_labels, d.controls)
        self.controls.setToolTip(t("player.controls_tip"))
        self.show_volume = self.check("pl_show_volume", t("player.show_volume"), d.show_volume)
        self.volume = self.spin("pl_volume", 0, 100, d.volume, " %", step=5)
        self.show_volume.toggled.connect(self._sync_enabled)
        self.bottom = self.check(
            "pl_bottom", t("player.bottom_buttons"), d.bottom_controls, t("player.bottom_buttons_tip")
        )
        self.remaining = self.check("pl_remaining", t("player.show_remaining"), d.show_remaining)
        self.instrumental = self.edit(
            "pl_instrumental", t("player.instrumental_placeholder"), t("player.instrumental_default_text")
        )
        self.instrumental.setToolTip(t("player.instrumental_tip"))
        self.quality_tag = self.edit("pl_quality_tag", t("player.quality_tag_placeholder"))
        self.quality_tag.setMinimumWidth(320)
        self.show_tag = self.check("pl_show_tag", t("player.show_tag"), True)
        self.show_tag.toggled.connect(self._sync_enabled)

        self.fonts = FontList()

        # ---- preview
        self._project: Project | None = None
        self._job: PreviewJob | None = None
        self._dialog: VideoPreview | None = None
        # not part of the output: saved, but no reason to re-plan the run
        self.preview_start = self.seconds(
            "pl_preview_start", 30.0, 0.0, 3600.0, tip=t("player.preview_start_tip"), replan=False
        )
        self.preview_length = self.seconds(
            "pl_preview_length", 8.0, 1.0, 60.0, tip=t("player.preview_length_tip"), replan=False
        )
        self.preview_button = PrimaryPushButton(fluent.icon("media-playback-start"), t("action.render_preview"))
        self.preview_button.setToolTip(t("player.preview_button_tip"))
        self.preview_button.clicked.connect(self._preview)
        self.preview_progress = ProgressBar()
        self.preview_progress.setFixedWidth(140)
        self.preview_progress.hide()
        self.preview_status = CaptionLabel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            widgets.section(
                t("section.preview"),
                widgets.hbox(
                    t("label.preview_start"), self.preview_start, 12, t("label.preview_length"), self.preview_length, 12,
                    self.preview_button, self.preview_progress, self.preview_status,
                ),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                t("section.frame_encoding"),
                widgets.two_column_grid([
                    (t("label.size"), widgets.size_row(self.width_spin, self.height_spin), t("label.fps"), self.fps),
                    (t("label.encoder"), self.codec, t("label.quality"), self.quality),
                    (t("label.audio"), widgets.hbox(self.audio_mode, self.audio_track), t("label.ui_scale"), self.ui_scale),
                    (t("label.speed"), widgets.hbox(self.fast_capture, self.supersample), None, None),
                    (t("label.album_mode_label"), self.album_mode, None, None),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                t("section.background"),
                widgets.two_column_grid([
                    (t("label.renderer"), self.background, t("label.color"), self.bg_color),
                    (t("label.flow_speed"), self.flow, t("label.render_scale"), self.render_scale),
                    (t("label.random_seed"), self.seed, None, widgets.hbox(self.beat, self.light_wave)),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                t("section.lyrics"),
                widgets.two_column_grid([
                    (t("label.font_size"), self.lyric_size, t("label.font_weight"), self.font_weight),
                    (t("label.letter_spacing"), self.letter_spacing, t("label.word_fade"), self.word_fade),
                    (t("label.right_padding"), self.lyric_padding, None, widgets.hbox(self.balance)),
                    (t("label.instrumental"), self.instrumental, None, None),
                    (t("label.effects"), widgets.hbox(self.blur, self.scale, self.spring), None, None),
                    (t("label.content"), widgets.hbox(self.translation, self.romanization, self.swap, self.bg_vocals,
                                         self.filter_credits),
                     None, None),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                t("section.track_info_controls"),
                widgets.two_column_grid([
                    (t("label.track_title"), self.title_edit, None, None),
                    (t("label.artist"), self.artist_edit, None, None),
                    (t("label.album"), self.album_edit, None, None),
                    (t("label.display"), widgets.hbox(self.show_name, self.show_artists, self.show_album, self.remaining),
                     None, None),
                    (t("label.controls"), self.controls, t("label.volume"), widgets.hbox(self.show_volume, self.volume)),
                    (t("label.tags"), widgets.hbox(self.show_tag, self.quality_tag, stretch=False), None,
                     widgets.hbox(self.bottom)),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(widgets.section(t("section.fonts_fallback"), self.fonts))
        layout.addStretch(1)
        self._sync_enabled()

    def _quality_range(self) -> None:
        # NVENC reads CQ 0 as "automatic", not as the best quality
        self.quality.setRange(*QUALITY_RANGE.get(self.codec.currentData(), (0, 51)))

    def _codec_changed(self) -> None:
        self._quality_range()
        self.quality.setValue(DEFAULT_QUALITY[self.codec.currentData()])
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        bg = self.background.currentData()
        webgl = bg in ("mesh", "isolation")
        for w in (self.flow, self.render_scale, self.seed):
            w.setEnabled(webgl)
        self.bg_color.setEnabled(bg == "solid")
        self.beat.setEnabled(bg == "mesh")
        self.light_wave.setEnabled(bg == "isolation")
        self.volume.setEnabled(self.show_volume.isChecked())
        self.quality_tag.setEnabled(self.show_tag.isChecked())
        self.audio_track.setEnabled(self.audio_mode.currentData() != "none")

    # ---- the audio mixed in ------------------------------------------------------------

    def _fill_audio_tracks(self, project: Project) -> None:
        from dolbytool.core.i18n import t

        self.audio_track.clear()
        for track in project.tracks:
            # the file is named on the inputs page; here it would only crowd the row
            text = t("audio.original") if track.is_original else track.label
            self.audio_track.addItem(text, track.key)
            self.audio_track.setItemData(
                self.audio_track.count() - 1, track.hint, Qt.ItemDataRole.ToolTipRole
            )
        if not project.tracks:
            self.audio_track.addItem(t("audio.original"), ORIGINAL)
        index = self.audio_track.findData(self._audio_key)
        if index < 0:
            # kept for the tracks that have it; this one falls back to its source
            self.audio_track.addItem(
                t("player.stem_not_found_fallback", label=stem_label(self._audio_key)), self._audio_key
            )
            index = self.audio_track.count() - 1
        self.audio_track.setCurrentIndex(index)

    def _audio_track_picked(self, index: int) -> None:
        self._audio_key = self.audio_track.itemData(index) or ORIGINAL
        self.changed.emit()

    def _mix_audio(self, project: Project) -> Path | None:
        """The audio the video takes: the chosen stem when there is one to mix in, else the source."""
        if project.info is None:
            return None
        if self.audio_mode.currentData() != "none":
            for track in project.tracks:
                if track.key == self._audio_key:
                    return track.path
        return project.audio

    def _info_placeholders(self, project: Project) -> None:
        info = project.info
        fallback = "留空则使用 TTML 元数据"
        self.title_edit.setPlaceholderText((info and info.title) or fallback)
        self.artist_edit.setPlaceholderText(
            (info and info.artist) or f"{fallback}，多位艺人用 / 分隔"
        )
        self.album_edit.setPlaceholderText((info and info.album) or fallback)

    def spec(self, project: Project | None = None, batch: bool = False) -> PlayerSpec:
        """`batch` leaves the title to each track: one typed title cannot name them all."""
        info = project.info if project else None
        atmos = bool(info and info.atmos)
        typed_title = "" if batch else self.title_edit.text().strip()
        tag = ""
        if self.show_tag.isChecked():
            tag = self.quality_tag.text().strip() or (ATMOS_TAG if atmos else "")
        return PlayerSpec(
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=self.fps.value(),
            codec=self.codec.currentData(),
            quality=self.quality.value(),
            audio_mode=self.audio_mode.currentData(),
            ui_scale=self.ui_scale.value(),
            background=self.background.currentData(),
            flow_speed=self.flow.value(),
            render_scale=self.render_scale.value(),
            color=self.bg_color.hex(),
            seed=self.seed.value(),
            light_wave=self.light_wave.isChecked(),
            beat=self.beat.isChecked(),
            font_families=tuple(self.fonts.families()),
            font_weight=self.font_weight.value(),
            letter_spacing=self.letter_spacing.value(),
            lyric_size=self.lyric_size.currentData(),
            word_fade=self.word_fade.value(),
            blur=self.blur.isChecked(),
            scale=self.scale.isChecked(),
            spring=self.spring.isChecked(),
            translation=self.translation.isChecked(),
            romanization=self.romanization.isChecked(),
            swap_trans_roman=self.swap.isChecked(),
            background_vocals=self.bg_vocals.isChecked(),
            balance=self.balance.isChecked(),
            lyric_padding=self.lyric_padding.value(),
            instrumental_text=self.instrumental.text().strip(),
            fast_capture=self.fast_capture.isChecked(),
            supersample=self.supersample.isChecked(),
            filter_credits=self.filter_credits.isChecked(),
            title=typed_title or (info.title if info else ""),
            artists=_split_artists(self.artist_edit.text() or (info.artist if info else "")),
            album=self.album_edit.text().strip() or (info.album if info else ""),
            show_name=self.show_name.isChecked(),
            show_artists=self.show_artists.isChecked(),
            show_album=self.show_album.isChecked(),
            controls=self.controls.currentData(),
            show_volume=self.show_volume.isChecked(),
            volume=self.volume.value(),
            bottom_controls=self.bottom.isChecked(),
            show_remaining=self.remaining.isChecked(),
            quality_tag=tag,
            dolby_atmos=bool(tag) and (atmos or "atmos" in tag.lower()),
        )

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        self._project = project
        self._info_placeholders(project)
        self._fill_audio_tracks(project)

    def required_stems(self, project: Project) -> set[str]:
        audio = self._mix_audio(project)
        if audio is None or audio == project.audio or audio.is_file():
            return set()
        return {self._audio_key}

    # ---- preview -----------------------------------------------------------------------

    def _preview(self) -> None:
        from dolbytool.core.i18n import t

        if self._job is not None:
            self._job.token.cancel()
            return
        project = self._project
        problem = self.problem(PlanContext(project)) if project else t("player.prompt_pick_input")
        if problem:
            self.preview_status.setText(problem)
            return
        assert project is not None
        spec = preview_spec(self.spec(project))
        ttml = project.ttml
        cover = project.cover if project.cover and project.cover.is_file() else None
        audio = self._mix_audio(project)
        if audio is not None and not audio.is_file():
            audio = project.audio  # a preview does not wait for the stem to be split
        song = project.info.duration if project.info else None
        start = self.preview_start.value()
        if song is not None and start >= song:
            start = max(0.0, song - self.preview_length.value())
        excerpt = (start, self.preview_length.value())
        target = work_root() / f"player_preview{spec.suffix}"

        def render(out: Path, token, on_fraction, on_status) -> None:
            render_player_video(
                ttml, cover, audio, out, spec, song, token, on_fraction, on_status, excerpt
            )

        self._job = PreviewJob(render, target, self)
        self._job.progress.connect(self._preview_progress)
        self._job.done.connect(self._preview_done)
        self.preview_button.setIcon(fluent.icon("process-stop"))
        self.preview_button.setText(t("action.cancel"))
        self.preview_progress.setValue(0)
        self.preview_progress.show()
        self.preview_status.setText(t("status.preparing"))
        self._job.start()

    def _preview_progress(self, value: int, message: str) -> None:
        self.preview_progress.setValue(value)
        self.preview_status.setText(message)

    def _preview_done(self, path: Path | None, error: str) -> None:
        from dolbytool.core.i18n import t

        self._job = None
        self.preview_button.setIcon(fluent.icon("media-playback-start"))
        self.preview_button.setText(t("action.render_preview"))
        self.preview_progress.hide()
        if path is None:
            self.preview_status.setText(t("player.preview_failed", error=error) if error else t("status.cancelled"))
            if error:
                logger.error("播放器预览失败：%s", error)
            return
        self.preview_status.setText("")
        if self._dialog is None:
            self._dialog = VideoPreview(self.window())
        self._dialog.play(path)

    def shutdown(self) -> None:
        """Cancel a running preview and wait for it, before the window goes away."""
        if self._job is not None:
            self._job.token.cancel()
            self._job.wait(15000)

    def problem(self, ctx: PlanContext) -> str | None:
        from dolbytool.core.i18n import t

        too_big = frame_problem(self.width_spin.value(), self.height_spin.value())
        if too_big:
            return too_big
        project = ctx.project
        ttml = project.ttml
        if ttml and not ttml.is_file():
            return t("lyrics.ttml_not_found")
        # an instrumental runs for as long as its audio
        if not ttml and project.info is None:
            return t("player.prompt_pick_lyrics_or_audio")
        if self.background.currentData() == "none" and self.codec.currentData() != "vp9":
            return t("player.vp9_alpha_required")
        needs_audio = (
            self.controls.currentData() == "fft" or self.audio_mode.currentData() != "none"
        )
        if needs_audio and project.info is None:
            return t("player.spectrum_audio_required")
        return renderer_problem("player.mjs")

    def summary(self, ctx: PlanContext) -> str | None:
        from dolbytool.core.i18n import t

        size = t("player.summary_size", width=self.width_spin.value(), height=self.height_spin.value())
        return size if ctx.project.ttml else t("player.summary_no_lyrics", size=size)

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        ttml, out, name = project.ttml, project.output_dir, project.audio or project.ttml
        if out is None or name is None:
            return []
        if ctx.album and self.album_mode.currentData() == "queue":
            return []  # played as part of the whole album instead
        spec = self.spec(project, ctx.batch)
        seconds = project.info.duration if project.info else None
        audio = self._mix_audio(project)
        cover = project.cover if project.cover and project.cover.is_file() else None
        target = player_target(out, name, spec)
        return [player_step(ttml, cover, audio, target, spec, seconds)]

    def album_plan(
        self, ctx: PlanContext, tracks: Sequence[tuple[Project, list[Step]]]
    ) -> list[Step]:
        album = ctx.project
        if self.album_mode.currentData() == "tracks" or not tracks:
            return []
        if album.audio is None or album.output_dir is None:
            return []
        queue = []
        for project, _steps in tracks:
            assert project.audio is not None
            tags = self.spec(project, True)
            queue.append(
                QueueTrack(
                    self._mix_audio(project) or project.audio, project.ttml,
                    tags.title or without_number(project.audio.stem), tags.artists, tags.album,
                )
            )  # fmt: skip
        # the first track's spec: its format picks the quality tag, the title goes unused
        spec = self.spec(tracks[0][0], True)
        target = player_target(album.output_dir, album.audio, spec)
        cover = album.cover if album.cover and album.cover.is_file() else None
        return [queue_step(queue, cover, target, spec)]

    # ---- settings --------------------------------------------------------------------

    def save_extra(self) -> None:
        self.settings.setValue("pl_audio_track", self._audio_key)
        self.settings.setValue("pl_fonts", self.fonts.families())

    def restore_extra(self) -> None:
        self._audio_key = self.settings.value("pl_audio_track", ORIGINAL, type=str) or ORIGINAL
        if self._project is not None:
            self._fill_audio_tracks(self._project)
        self.fonts.set_families(self.settings.value("pl_fonts", [], type=list))
        self._quality_range()
        self._sync_enabled()
