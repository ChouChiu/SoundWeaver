"""Player video settings: frame and encoder, background, lyrics, song info and controls, fonts."""

from __future__ import annotations

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
    title = "播放器视频"
    icon = "media-playback-start"
    description = (
        "基于 AMLL Prebuilt Player 渲染完整播放器页面：封面、曲目信息、进度条、播放控件、"
        "流动背景与逐字滚动歌词，横屏 / 竖屏自动切换布局，可混入音频"
    )
    enabled_key = "do_player"
    enabled_default = False
    needs_audio = False

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        d = PlayerSpec()

        # ---- frame and encoding
        self.width_spin = self.spin("pl_w", 64, MAX_FRAME_SIDE, d.width, " px", step=2)
        self.height_spin = self.spin("pl_h", 64, MAX_FRAME_SIDE, d.height, " px", step=2)
        self.height_spin.setToolTip("高度大于宽度时自动使用竖屏（移动端）布局")
        self.fps = self.spin("pl_fps", 10, 240, d.fps, " fps")
        self.fps.setToolTip(
            "高帧率下逐字扫光与滚动更顺滑，适合高刷新率屏幕观看；渲染耗时随帧率成正比增加\n"
            "上传平台多数只支持到 60 fps，部分支持 120 fps"
        )
        self.codec = self.combo("pl_codec", CODECS, d.codec)
        self.codec.setToolTip(
            "硬件编码器不可用时将自动回退至 libx264 软件编码；透明背景必须使用 VP9"
        )
        self.quality = self.spin("pl_quality", 0, 63, d.quality)
        self.quality.setToolTip(
            "数值越小画质越高、文件越大；软件编码为 CRF（0 为无损），NVENC 为 CQ（最小 1）\n"
            "NVENC 19 与无损相比约 54 dB，肉眼已难分辨；更清晰优先关闭“快速截图”"
        )
        self.codec.currentIndexChanged.connect(self._codec_changed)
        self.audio_mode = self.combo("pl_audio", AUDIO_MODES, d.audio_mode)
        self.audio_mode.setToolTip(
            "将所选音轨混入视频；MKV 原样复制任何音频（含全景声与 PCM 分轨），\n"
            "WebM 只能容纳 Opus，选择复制时也会转码为 Opus"
        )
        self.audio_mode.currentIndexChanged.connect(self._sync_enabled)
        # the stem to mix in, by kind, so every track of a batch takes the same one
        self._audio_key = ORIGINAL
        self.audio_track = fluent.ComboBox()

        self.audio_track.setToolTip(
            "混入视频的音轨：源音频或任一分轨\n"
            "分轨尚未导出时会自动补上分轨任务；批量与专辑中每首都取同一种分轨"
        )
        self.audio_track.activated.connect(self._audio_track_picked)
        self.fast_capture = self.check(
            "pl_fast_capture", "快速截图", d.fast_capture,
            "浏览器取帧改用 JPEG 而非无损 PNG，渲染约快 1.5 倍，细节略软；透明背景不受影响",
        )  # fmt: skip
        self.supersample = self.check(
            "pl_supersample", "超采样", d.supersample,
            "按两倍尺寸绘制再缩小到输出尺寸，文字与图标边缘更平滑；渲染约慢 2 到 3 倍",
        )  # fmt: skip
        self.album_mode = self.combo("pl_album_mode", ALBUM_MODES, "tracks")
        self.album_mode.setToolTip(
            "专辑模式下的输出方式\n"
            "整张专辑连续播放：像歌单一样按专辑顺序从头播到尾，生成「<专辑名>_player」，\n"
            "切歌时曲目信息、进度与歌词随之切换，背景与动态封面连续流动"
        )
        self.ui_scale = self.spin("pl_ui_scale", 0, 800, d.ui_scale, " %", step=25)
        self.ui_scale.setSpecialValueText("自动")
        self.ui_scale.setToolTip(
            "页面整体缩放（设备像素比）\n"
            "自动：横屏按 1080p 窗口排版，竖屏按约 400 px 宽的手机排版，再放大到输出尺寸\n"
            "数值越大，界面与歌词越大"
        )

        # ---- background
        self.background = self.combo("pl_bg", BACKGROUNDS, d.background)
        self.background.currentIndexChanged.connect(self._sync_enabled)
        self.flow = self.spin("pl_flow", 0, 1000, d.flow_speed, " %", step=10)
        self.flow.setToolTip("背景流动速度，100% 为 AMLL 默认值")
        self.render_scale = self.spin("pl_bg_scale", 5, 200, d.render_scale, " %", step=5)
        self.render_scale.setToolTip("背景画布相对画面的渲染分辨率；背景本身很模糊，50% 已足够")
        self.bg_color = self.color("pl_color", d.color, "选择背景颜色")
        self.seed = self.spin("pl_seed", 0, 999999, d.seed)
        self.seed.setToolTip("背景的随机形态由此种子决定，换一个数字可得到不同的流动图案")
        self.light_wave = self.check("pl_light_wave", "明暗光波", d.light_wave, "仅 Isolation 背景")
        self.beat = self.check(
            "pl_beat",
            "随低频律动",
            d.beat,
            "仅 Mesh 背景：按源音频 50–120 Hz 的能量让背景随鼓点起伏",
        )

        # ---- lyrics
        self.lyric_size = self.combo("pl_lyric_size", LYRIC_SIZES, d.lyric_size)
        self.font_weight = self.spin("pl_font_weight", 0, 1000, d.font_weight, step=100)
        self.font_weight.setSpecialValueText("默认")
        self.letter_spacing = self.spin("pl_letter_spacing", -50, 50, d.letter_spacing, " %em")
        self.word_fade = self.spin("pl_word_fade", 1, 500, d.word_fade, " %", step=5)
        self.word_fade.setToolTip("逐字高亮边缘的柔和宽度（相对行高）")
        self.blur = self.check("pl_blur", "非当前行模糊", d.blur)
        self.scale = self.check("pl_scale", "非当前行缩小", d.scale)
        self.spring = self.check("pl_spring", "弹簧滚动动画", d.spring)
        self.translation = self.check("pl_translation", "显示翻译", d.translation)
        self.romanization = self.check("pl_roman", "显示音译", d.romanization)
        self.swap = self.check("pl_swap", "交换翻译与音译", d.swap_trans_roman)
        self.bg_vocals = self.check("pl_bg_vocals", "显示和声", d.background_vocals)
        self.filter_credits = self.check(
            "pl_filter_credits",
            "过滤标题与制作信息",
            d.filter_credits,
            CREDITS_TIP,
        )
        self.balance = self.check(
            "pl_balance", "均衡换行", d.balance, "长句需要折行时让各行长度接近，而不是第一行塞满"
        )
        self.lyric_padding = self.spin("pl_lyric_padding", -1, 50, d.lyric_padding, " %")
        self.lyric_padding.setSpecialValueText("AMLL 默认")
        self.lyric_padding.setToolTip(
            "横屏时歌词栏右侧留白（占栏宽）；AMLL 默认为 15%，过宽会让本可一行显示的歌词折行"
        )

        # ---- song info and controls
        self.title_edit = self.edit("pl_title", "留空则使用音频标签或 TTML 元数据")
        self.artist_edit = self.edit("pl_artist", "多位艺人用 / 分隔")
        self.album_edit = self.edit("pl_album", "")
        self.show_name = self.check("pl_show_name", "曲名", d.show_name)
        self.show_artists = self.check("pl_show_artists", "艺人", d.show_artists)
        self.show_album = self.check("pl_show_album", "专辑", d.show_album)
        self.controls = self.combo("pl_controls", CONTROLS, d.controls)
        self.controls.setToolTip("音频频谱需要源音频")
        self.show_volume = self.check("pl_show_volume", "音量条", d.show_volume)
        self.volume = self.spin("pl_volume", 0, 100, d.volume, " %", step=5)
        self.show_volume.toggled.connect(self._sync_enabled)
        self.bottom = self.check(
            "pl_bottom", "底部按钮", d.bottom_controls, "歌词 / 隔空播放 / 播放列表按钮"
        )
        self.remaining = self.check("pl_remaining", "显示剩余时间", d.show_remaining)
        self.instrumental = self.edit(
            "pl_instrumental", "留空则只显示居中的封面（AMLL 无歌词布局）", d.instrumental_text
        )
        self.instrumental.setToolTip("没有歌词的音轨（纯音乐）在歌词区显示的一行文字")
        self.quality_tag = self.edit("pl_quality_tag", "留空：全景声显示 Dolby Atmos，其余不显示")
        self.quality_tag.setMinimumWidth(320)
        self.show_tag = self.check("pl_show_tag", "音质标签", True)
        self.show_tag.toggled.connect(self._sync_enabled)

        self.fonts = FontList()

        # ---- preview
        self._project: Project | None = None
        self._job: PreviewJob | None = None
        self._dialog: VideoPreview | None = None
        # not part of the output: saved, but no reason to re-plan the run
        self.preview_start = self.seconds(
            "pl_preview_start", 30.0, 0.0, 3600.0, tip="从歌曲的第几秒开始预览", replan=False
        )
        self.preview_length = self.seconds(
            "pl_preview_length", 8.0, 1.0, 60.0, tip="预览片段长度", replan=False
        )
        self.preview_button = PrimaryPushButton(fluent.icon("media-playback-start"), "渲染预览")
        self.preview_button.setToolTip("按当前参数以一半分辨率、30 fps 渲染一小段并播放")
        self.preview_button.clicked.connect(self._preview)
        self.preview_progress = ProgressBar()
        self.preview_progress.setFixedWidth(140)
        self.preview_progress.hide()
        self.preview_status = CaptionLabel()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            widgets.section(
                "预览",
                widgets.hbox(
                    "起点：", self.preview_start, 12, "时长：", self.preview_length, 12,
                    self.preview_button, self.preview_progress, self.preview_status,
                ),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "画面与编码",
                widgets.two_column_grid([
                    ("尺寸：", widgets.size_row(self.width_spin, self.height_spin), "帧率：", self.fps),
                    ("编码器：", self.codec, "画质：", self.quality),
                    ("音频：", widgets.hbox(self.audio_mode, self.audio_track), "界面缩放：", self.ui_scale),
                    ("速度：", widgets.hbox(self.fast_capture, self.supersample), None, None),
                    ("专辑模式：", self.album_mode, None, None),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "背景",
                widgets.two_column_grid([
                    ("渲染器：", self.background, "颜色：", self.bg_color),
                    ("流动速度：", self.flow, "渲染比例：", self.render_scale),
                    ("随机种子：", self.seed, None, widgets.hbox(self.beat, self.light_wave)),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "歌词",
                widgets.two_column_grid([
                    ("字号：", self.lyric_size, "字重：", self.font_weight),
                    ("字间距：", self.letter_spacing, "渐变宽度：", self.word_fade),
                    ("右侧留白：", self.lyric_padding, None, widgets.hbox(self.balance)),
                    ("纯音乐：", self.instrumental, None, None),
                    ("效果：", widgets.hbox(self.blur, self.scale, self.spring), None, None),
                    ("内容：", widgets.hbox(self.translation, self.romanization, self.swap, self.bg_vocals,
                                         self.filter_credits),
                     None, None),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "曲目信息与控件",
                widgets.two_column_grid([
                    ("曲名：", self.title_edit, None, None),
                    ("艺人：", self.artist_edit, None, None),
                    ("专辑：", self.album_edit, None, None),
                    ("显示：", widgets.hbox(self.show_name, self.show_artists, self.show_album, self.remaining),
                     None, None),
                    ("控件：", self.controls, "音量：", widgets.hbox(self.show_volume, self.volume)),
                    ("标签：", widgets.hbox(self.show_tag, self.quality_tag, stretch=False), None,
                     widgets.hbox(self.bottom)),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(widgets.section("字体回退顺序", self.fonts))
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
        self.audio_track.clear()
        for track in project.tracks:
            # the file is named on the inputs page; here it would only crowd the row
            text = "源音频" if track.is_original else track.label
            self.audio_track.addItem(text, track.key)
            self.audio_track.setItemData(
                self.audio_track.count() - 1, track.hint, Qt.ItemDataRole.ToolTipRole
            )
        if not project.tracks:
            self.audio_track.addItem("源音频", ORIGINAL)
        index = self.audio_track.findData(self._audio_key)
        if index < 0:
            # kept for the tracks that have it; this one falls back to its source
            self.audio_track.addItem(
                f"{stem_label(self._audio_key)}（此音频没有，改用源音频）", self._audio_key
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
        if self._job is not None:
            self._job.token.cancel()
            return
        project = self._project
        problem = self.problem(PlanContext(project)) if project else "请先选择输入文件"
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
        self.preview_button.setText("取消")
        self.preview_progress.setValue(0)
        self.preview_progress.show()
        self.preview_status.setText("准备中")
        self._job.start()

    def _preview_progress(self, value: int, message: str) -> None:
        self.preview_progress.setValue(value)
        self.preview_status.setText(message)

    def _preview_done(self, path: Path | None, error: str) -> None:
        self._job = None
        self.preview_button.setIcon(fluent.icon("media-playback-start"))
        self.preview_button.setText("渲染预览")
        self.preview_progress.hide()
        if path is None:
            self.preview_status.setText(f"预览失败：{error}" if error else "已取消")
            if error:
                self.log.emit(f"播放器预览失败：{error}")
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
        too_big = frame_problem(self.width_spin.value(), self.height_spin.value())
        if too_big:
            return too_big
        project = ctx.project
        ttml = project.ttml
        if ttml and not ttml.is_file():
            return "指定的歌词文件不存在"
        # an instrumental runs for as long as its audio
        if not ttml and project.info is None:
            return "请选择 TTML 逐字歌词文件，纯音乐需要可读取的源音频"
        if self.background.currentData() == "none" and self.codec.currentData() != "vp9":
            return "透明背景需要 VP9 编码器（唯一支持 Alpha 通道的选项）"
        needs_audio = (
            self.controls.currentData() == "fft" or self.audio_mode.currentData() != "none"
        )
        if needs_audio and project.info is None:
            return "音频频谱与混入音频需要可读取的源音频"
        return renderer_problem("player.mjs")

    def summary(self, ctx: PlanContext) -> str | None:
        size = f"播放器视频 {self.width_spin.value()}×{self.height_spin.value()}"
        return size if ctx.project.ttml else f"{size}（无歌词，按纯音乐只显示封面）"

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
