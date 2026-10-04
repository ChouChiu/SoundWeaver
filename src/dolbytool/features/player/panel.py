"""Player video settings: frame and encoder, background, lyrics, song info and controls, fonts."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QThread, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from dolbytool.core.cancel import CancellationToken, ProcessingCancelled
from dolbytool.core.paths import work_root
from dolbytool.core.project import PlanContext, Project
from dolbytool.core.renderer import renderer_problem
from dolbytool.core.task import Step
from dolbytool.features.lyrics.panel import FontList
from dolbytool.features.player.service import (
    AUDIO_MODES,
    BACKGROUNDS,
    CODECS,
    CONTROLS,
    DEFAULT_QUALITY,
    LYRIC_SIZES,
    PlayerSpec,
    player_step,
    player_target,
    preview_spec,
    render_player_video,
)
from dolbytool.ui import widgets
from dolbytool.ui.feature import FeaturePanel
from dolbytool.ui.video_preview import VideoPreview

ATMOS_TAG = "Dolby Atmos"


def _split_artists(text: str) -> tuple[str, ...]:
    return tuple(a.strip() for a in text.replace("、", "/").split("/") if a.strip())


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
        status = ""

        def on_fraction(fraction: float) -> None:
            self.progress.emit(round(fraction * 100), status or "逐帧渲染")

        def on_status(message: str) -> None:
            nonlocal status
            status = message
            self.progress.emit(-1, message)

        try:
            self._render(self.target, self.token, on_fraction, on_status)
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
        self._checks: dict[str, QCheckBox] = {}
        self._combos: dict[str, QComboBox] = {}
        self._edits: dict[str, QLineEdit] = {}
        self._tag_title = self._tag_artist = self._tag_album = ""

        # ---- frame and encoding
        self.width_spin = self.spin("pl_w", 64, 7680, d.width, " px", step=2)
        self.height_spin = self.spin("pl_h", 64, 7680, d.height, " px", step=2)
        self.height_spin.setToolTip("高度大于宽度时自动使用竖屏（移动端）布局")
        self.fps = self.spin("pl_fps", 10, 120, d.fps, " fps")
        self.codec = self._combo("pl_codec", CODECS, d.codec)
        self.codec.setToolTip(
            "硬件编码器不可用时将自动回退至 libx264 软件编码；透明背景必须使用 VP9"
        )
        self.quality = self.spin("pl_quality", 0, 63, d.quality)
        self.quality.setToolTip("软件编码为 CRF，NVENC 为 CQ；数值越小画质越高、文件越大")
        self.codec.currentIndexChanged.connect(self._codec_changed)
        self.audio_mode = self._combo("pl_audio", AUDIO_MODES, d.audio_mode)
        self.audio_mode.setToolTip(
            "将源音频混入视频；WebM 只能容纳 Opus，选择复制时也会转码为 Opus"
        )
        self.ui_scale = self.spin("pl_ui_scale", 0, 800, d.ui_scale, " %", step=25)
        self.ui_scale.setSpecialValueText("自动")
        self.ui_scale.setToolTip(
            "页面整体缩放（设备像素比）\n"
            "自动：横屏按 1080p 窗口排版，竖屏按约 400 px 宽的手机排版，再放大到输出尺寸\n"
            "数值越大，界面与歌词越大"
        )

        # ---- background
        self.background = self._combo("pl_bg", BACKGROUNDS, d.background)
        self.background.currentIndexChanged.connect(self._sync_enabled)
        self.flow = self.spin("pl_flow", 0, 1000, d.flow_speed, " %", step=10)
        self.flow.setToolTip("背景流动速度，100% 为 AMLL 默认值")
        self.render_scale = self.spin("pl_bg_scale", 5, 200, d.render_scale, " %", step=5)
        self.render_scale.setToolTip("背景画布相对画面的渲染分辨率；背景本身很模糊，50% 已足够")
        self.color = widgets.ColorButton(d.color, "选择背景颜色")
        self.seed = self.spin("pl_seed", 0, 999999, d.seed)
        self.seed.setToolTip("背景的随机形态由此种子决定，换一个数字可得到不同的流动图案")
        self.light_wave = self._check(
            "pl_light_wave", "明暗光波", d.light_wave, "仅 Isolation 背景"
        )
        self.beat = self._check(
            "pl_beat",
            "随低频律动",
            d.beat,
            "仅 Mesh 背景：按源音频 50–120 Hz 的能量让背景随鼓点起伏",
        )

        # ---- lyrics
        self.lyric_size = self._combo("pl_lyric_size", LYRIC_SIZES, d.lyric_size)
        self.font_weight = self.spin("pl_font_weight", 0, 1000, d.font_weight, step=100)
        self.font_weight.setSpecialValueText("默认")
        self.letter_spacing = self.spin("pl_letter_spacing", -50, 50, d.letter_spacing, " %em")
        self.word_fade = self.spin("pl_word_fade", 1, 500, d.word_fade, " %", step=5)
        self.word_fade.setToolTip("逐字高亮边缘的柔和宽度（相对行高）")
        self.blur = self._check("pl_blur", "非当前行模糊", d.blur)
        self.scale = self._check("pl_scale", "非当前行缩小", d.scale)
        self.spring = self._check("pl_spring", "弹簧滚动动画", d.spring)
        self.translation = self._check("pl_translation", "显示翻译", d.translation)
        self.romanization = self._check("pl_roman", "显示音译", d.romanization)
        self.swap = self._check("pl_swap", "交换翻译与音译", d.swap_trans_roman)
        self.bg_vocals = self._check("pl_bg_vocals", "显示和声", d.background_vocals)
        self.balance = self._check(
            "pl_balance", "均衡换行", d.balance, "长句需要折行时让各行长度接近，而不是第一行塞满"
        )
        self.lyric_padding = self.spin("pl_lyric_padding", -1, 50, d.lyric_padding, " %")
        self.lyric_padding.setSpecialValueText("AMLL 默认")
        self.lyric_padding.setToolTip(
            "横屏时歌词栏右侧留白（占栏宽）；AMLL 默认为 15%，过宽会让本可一行显示的歌词折行"
        )

        # ---- song info and controls
        self.title_edit = self._edit("pl_title", "留空则使用音频标签或 TTML 元数据")
        self.artist_edit = self._edit("pl_artist", "多位艺人用 / 分隔")
        self.album_edit = self._edit("pl_album", "")
        self.show_name = self._check("pl_show_name", "曲名", d.show_name)
        self.show_artists = self._check("pl_show_artists", "艺人", d.show_artists)
        self.show_album = self._check("pl_show_album", "专辑", d.show_album)
        self.controls = self._combo("pl_controls", CONTROLS, d.controls)
        self.controls.setToolTip("音频频谱需要源音频")
        self.show_volume = self._check("pl_show_volume", "音量条", d.show_volume)
        self.volume = self.spin("pl_volume", 0, 100, d.volume, " %", step=5)
        self.show_volume.toggled.connect(self._sync_enabled)
        self.bottom = self._check(
            "pl_bottom", "底部按钮", d.bottom_controls, "歌词 / 隔空播放 / 播放列表按钮"
        )
        self.remaining = self._check("pl_remaining", "显示剩余时间", d.show_remaining)
        self.quality_tag = self._edit("pl_quality_tag", "留空：全景声显示 Dolby Atmos，其余不显示")
        self.show_tag = self._check("pl_show_tag", "音质标签", True)
        self.show_tag.toggled.connect(self._sync_enabled)

        self.fonts = FontList()

        # ---- preview
        self._project: Project | None = None
        self._job: PreviewJob | None = None
        self._dialog: VideoPreview | None = None
        self.preview_start = self._seconds("pl_preview_start", 30.0, "从歌曲的第几秒开始预览")
        self.preview_length = self._seconds(
            "pl_preview_length", 8.0, "预览片段长度", low=1.0, high=60.0
        )
        self.preview_button = QPushButton(QIcon.fromTheme("media-playback-start"), "渲染预览")
        self.preview_button.setToolTip("按当前参数以一半分辨率、30 fps 渲染一小段并播放")
        self.preview_button.clicked.connect(self._preview)
        self.preview_progress = QProgressBar()
        self.preview_progress.setTextVisible(False)
        self.preview_progress.setMaximumWidth(160)
        self.preview_progress.hide()
        self.preview_status = QLabel()
        self.preview_status.setEnabled(False)

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
                    ("音频：", self.audio_mode, "界面缩放：", self.ui_scale),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "背景",
                widgets.two_column_grid([
                    ("渲染器：", self.background, "颜色：", self.color),
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
                    ("效果：", widgets.hbox(self.blur, self.scale, self.spring), None, None),
                    ("内容：", widgets.hbox(self.translation, self.romanization, self.swap, self.bg_vocals),
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

    # ---- persisted controls ------------------------------------------------------------

    def _check(self, key: str, text: str, value: bool, tip: str = "") -> QCheckBox:
        box = QCheckBox(text)
        box.setChecked(value)
        if tip:
            box.setToolTip(tip)
        box.toggled.connect(self.changed)
        self._checks[key] = box
        return box

    def _combo(self, key: str, items: dict[str, str], value: str) -> QComboBox:
        box = QComboBox()
        for name, label in items.items():
            box.addItem(label, name)
        box.setCurrentIndex(max(0, box.findData(value)))
        box.currentIndexChanged.connect(self.changed)
        self._combos[key] = box
        return box

    def _edit(self, key: str, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setClearButtonEnabled(True)
        edit.textChanged.connect(lambda _t: self.changed.emit())
        self._edits[key] = edit
        return edit

    def _seconds(
        self, key: str, value: float, tip: str, low: float = 0.0, high: float = 3600.0
    ) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(1)
        box.setSingleStep(1.0)
        box.setValue(value)
        box.setSuffix(" 秒")
        box.setToolTip(tip)
        # not part of the output: saved, but no reason to re-plan the run
        self._spins[key] = box
        return box

    def _codec_changed(self) -> None:
        self.quality.setValue(DEFAULT_QUALITY[self.codec.currentData()])
        self._sync_enabled()

    def _sync_enabled(self) -> None:
        bg = self.background.currentData()
        webgl = bg in ("mesh", "isolation")
        for w in (self.flow, self.render_scale, self.seed):
            w.setEnabled(webgl)
        self.color.setEnabled(bg == "solid")
        self.beat.setEnabled(bg == "mesh")
        self.light_wave.setEnabled(bg == "isolation")
        self.volume.setEnabled(self.show_volume.isChecked())
        self.quality_tag.setEnabled(self.show_tag.isChecked())

    def _info_placeholders(self, project: Project) -> None:
        info = project.info
        self._tag_title = info.title if info else ""
        self._tag_artist = info.artist if info else ""
        self._tag_album = info.album if info else ""
        fallback = "留空则使用 TTML 元数据"
        self.title_edit.setPlaceholderText(self._tag_title or fallback)
        self.artist_edit.setPlaceholderText(self._tag_artist or f"{fallback}，多位艺人用 / 分隔")
        self.album_edit.setPlaceholderText(self._tag_album or fallback)

    def spec(self, project: Project | None = None) -> PlayerSpec:
        info = project.info if project else None
        atmos = bool(info and info.atmos)
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
            color=self.color.hex(),
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
            title=self.title_edit.text().strip() or self._tag_title,
            artists=_split_artists(self.artist_edit.text() or self._tag_artist),
            album=self.album_edit.text().strip() or self._tag_album,
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
        assert project is not None and project.ttml is not None
        spec = preview_spec(self.spec(project))
        ttml = project.ttml
        cover = project.cover if project.cover and project.cover.is_file() else None
        audio = project.audio if project.info else None
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
        self.preview_button.setIcon(QIcon.fromTheme("process-stop"))
        self.preview_button.setText("取消")
        self.preview_progress.setValue(0)
        self.preview_progress.show()
        self.preview_status.setText("准备中")
        self._job.start()

    def _preview_progress(self, value: int, message: str) -> None:
        if value >= 0:
            self.preview_progress.setValue(value)
        self.preview_status.setText(message)

    def _preview_done(self, path: Path | None, error: str) -> None:
        self._job = None
        self.preview_button.setIcon(QIcon.fromTheme("media-playback-start"))
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
        project = ctx.project
        ttml = project.ttml
        if not ttml:
            return "请选择 TTML 逐字歌词文件"
        if not ttml.is_file():
            return "指定的歌词文件不存在"
        if self.background.currentData() == "none" and self.codec.currentData() != "vp9":
            return "透明背景需要 VP9 编码器（唯一支持 Alpha 通道的选项）"
        needs_audio = (
            self.controls.currentData() == "fft" or self.audio_mode.currentData() != "none"
        )
        if needs_audio and project.info is None:
            return "音频频谱与混入音频需要可读取的源音频"
        return renderer_problem("player.mjs")

    def summary(self, ctx: PlanContext) -> str | None:
        return f"播放器视频 {self.width_spin.value()}×{self.height_spin.value()}"

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        ttml, out = project.ttml, project.output_dir
        if ttml is None or out is None:
            return []
        spec = self.spec(project)
        seconds = project.info.duration if project.info else None
        audio = project.audio if project.info else None
        cover = project.cover if project.cover and project.cover.is_file() else None
        target = player_target(out, project.audio or ttml, spec)
        return [player_step(ttml, cover, audio, target, spec, seconds)]

    # ---- settings --------------------------------------------------------------------

    def save_extra(self) -> None:
        for key, box in self._checks.items():
            self.settings.setValue(key, box.isChecked())
        for key, combo in self._combos.items():
            self.settings.setValue(key, combo.currentData())
        for key, edit in self._edits.items():
            self.settings.setValue(key, edit.text())
        self.settings.setValue("pl_color", self.color.hex())
        self.settings.setValue("pl_fonts", self.fonts.families())

    def restore_extra(self) -> None:
        # the saved quality must win over the codec's default
        quality = self.quality.value()
        for key, combo in self._combos.items():
            value = self.settings.value(key, combo.currentData())
            combo.setCurrentIndex(max(0, combo.findData(value)))
        self.quality.setValue(quality)
        for key, box in self._checks.items():
            box.setChecked(self.settings.value(key, box.isChecked(), type=bool))
        for key, edit in self._edits.items():
            edit.setText(self.settings.value(key, "", type=str))
        self.color.setColor(self.settings.value("pl_color", self.color.hex()))
        self.fonts.set_families(self.settings.value("pl_fonts", [], type=list))
        self._sync_enabled()
