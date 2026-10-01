"""Lyric video settings: frame, timing, translation brightness and the font fallback list."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFontComboBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from dolbytool.core.project import PlanContext
from dolbytool.core.renderer import renderer_problem
from dolbytool.core.task import Step
from dolbytool.features.lyrics.service import SubtitleSpec, lyrics_step, lyrics_target
from dolbytool.ui import widgets
from dolbytool.ui.feature import FeaturePanel


class FontList(QWidget):
    """Ordered font fallback list: pick a family, add it, drag or move to reorder."""

    def __init__(self) -> None:
        super().__init__()
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setMaximumHeight(96)
        self.list.setToolTip(
            "按顺序回退：每个字取列表中第一个含该字形的字体\n"
            "中文字体通常也带西文字形，想让西文用别的字体就把西文字体放在前面\n"
            "列表为空时使用系统默认无衬线字体"
        )
        self.pick = QFontComboBox()
        add = QPushButton(QIcon.fromTheme("list-add"), "添加")
        add.clicked.connect(self._add)
        buttons = QVBoxLayout()
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.pick, 1)
        pick_row.addWidget(add)
        buttons.addLayout(pick_row)
        moves = QHBoxLayout()
        for icon, text, step in (("go-up", "上移", -1), ("go-down", "下移", 1)):
            button = QPushButton(QIcon.fromTheme(icon), text)
            button.clicked.connect(lambda _=False, step=step: self._move(step))
            moves.addWidget(button)
        remove = QPushButton(QIcon.fromTheme("list-remove"), "移除")
        remove.clicked.connect(self._remove)
        moves.addWidget(remove)
        moves.addStretch(1)
        buttons.addLayout(moves)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons, 1)

    def families(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_families(self, families: list[str]) -> None:
        self.list.clear()
        for family in families:
            item = QListWidgetItem(family)
            item.setFont(QFont(family))
            self.list.addItem(item)

    def _add(self) -> None:
        family = self.pick.currentFont().family()
        if family not in self.families():
            self.set_families([*self.families(), family])
        self.list.setCurrentRow(self.families().index(family))

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.list.count():
            return
        self.list.insertItem(target, self.list.takeItem(row))
        self.list.setCurrentRow(target)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)


class LyricsPanel(FeaturePanel):
    title = "字幕视频"
    icon = "view-media-lyrics"
    description = (
        "用 AMLL 渲染 TTML 逐字歌词，输出无背景的单行字幕 WebM（VP9 带透明通道）；"
        "只显示翻译，不显示音译与背景人声，时长跟随音频"
    )
    enabled_key = "do_lyrics"
    enabled_default = False
    needs_audio = False

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        d = SubtitleSpec()
        self.width_spin = self.spin("lyr_w", 64, 7680, d.width, " px", step=2)
        self.height_spin = self.spin("lyr_h", 32, 4320, d.height, " px", step=2)
        self.fps = self.spin("lyr_fps", 10, 120, d.fps, " fps")
        self.font_size = self.spin("lyr_font_size", 8, 512, d.font_size, " px")
        self.fade = self.spin("lyr_fade", 0, 2000, d.fade_ms, " ms", step=50)
        self.fade.setToolTip("换行时旧行淡出、新行淡入各用的时长")
        self.translation = self.spin("lyr_sub", 0, 100, d.translation_opacity, " %")
        self.translation.setToolTip("翻译行的不透明度，AMLL 原样式为 30%")
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
                "文字",
                widgets.two_column_grid([
                    ("字号：", self.font_size, "淡入淡出：", self.fade),
                    ("翻译亮度：", self.translation, None, None),
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
        )

    # ---- hooks -----------------------------------------------------------------------

    def problem(self, ctx: PlanContext) -> str | None:
        ttml = ctx.project.ttml
        if not ttml:
            return "需要选择 TTML 歌词"
        if not ttml.is_file():
            return "歌词文件不存在"
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
