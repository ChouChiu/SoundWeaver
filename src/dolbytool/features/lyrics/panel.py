"""Lyric video settings: frame, timing, translation brightness and the font fallback list."""

from __future__ import annotations

import shutil
import subprocess
from functools import cache

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CheckBox,
    EditableComboBox,
    ListWidget,
    PushButton,
    ToolButton,
    setCustomStyleSheet,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType

from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.project import PlanContext
from dolbytool.core.renderer import renderer_problem
from dolbytool.core.task import Step
from dolbytool.features.lyrics.service import SubtitleSpec, lyrics_step, lyrics_target
from dolbytool.ui import fluent, widgets
from dolbytool.ui.feature import FeaturePanel

# what the completer matches on: every name of the family, not just the one shown
_SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1


@cache
def font_names() -> dict[str, frozenset[str]]:
    """Installed families, by the name to show and hand the browser, with every name they go by.

    Qt lists one name per family, the localised one under a Chinese locale (苹方-简, not
    PingFang SC); fontconfig knows them all.  The shown name is the first Latin one, which
    the browser resolves the same way and which reads the same in every locale.
    """
    qt = set(QFontDatabase.families())
    aliases: dict[str, set[str]] = {family: {family} for family in qt}
    if shutil.which("fc-list"):
        try:
            out = subprocess.run(
                ["fc-list", "--format", "%{family}\\n"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            names = [name.strip() for name in line.split(",") if name.strip()]
            for family in (name for name in names if name in qt):
                aliases[family].update(names)
    shown: dict[str, set[str]] = {}
    for family, names in aliases.items():
        # fontconfig puts the base family before style variants (PingFang SC before PingFang SC Light)
        latin = [name for name in names if name.isascii()]
        name = family if family.isascii() else min(latin, key=len, default=family)
        shown.setdefault(name, set()).update(names)
    return {
        name: frozenset(names)
        for name, names in sorted(shown.items(), key=lambda kv: kv[0].casefold())
    }


class _FontMenu(ComboBoxMenu):
    """The drop-down, built in linear time.

    RoundMenu re-measures the whole list and rescans every action for an icon on each
    item it adds: with a thousand families the menu took a second or more to open.
    Fonts carry no icons, and exec() measures the list once before showing it.
    """

    def _hasItemIcon(self) -> bool:
        return False

    def addAction(self, action) -> None:
        QListWidget.addItem(self.view, self._createActionItem(action))

    # PySide resolves exec() on a second-level subclass to QMenu's own, which has no aniType
    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        return ComboBoxMenu.exec(self, pos, ani, aniType)


class _FontPicker(EditableComboBox):
    """Enter adds the family instead of putting whatever was typed into the drop-down."""

    submitted = Signal()

    def _createComboMenu(self) -> ComboBoxMenu:
        return _FontMenu(self)

    def _onReturnPressed(self) -> None:
        # with the completion menu open, Enter picks from it; that key press reaches here first
        menu = self._completerMenu
        if not (menu and menu.isVisible()):
            self.submitted.emit()


class FontList(QWidget):
    """Ordered font fallback list: pick a family, add it, drag or move to reorder."""

    def __init__(self) -> None:
        super().__init__()
        self.list = ListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setMaximumHeight(132)
        self.list.setMinimumHeight(96)
        # an empty Fluent list draws nothing at all; outline it so it reads as a box
        setCustomStyleSheet(
            self.list,
            "ListWidget { border: 1px solid rgba(0, 0, 0, 0.12); border-radius: 6px;"
            " background: rgba(0, 0, 0, 0.02); }",
            "ListWidget { border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 6px;"
            " background: rgba(255, 255, 255, 0.03); }",
        )
        self.list.setToolTip(
            "字体按从上到下顺序回退：每个字符优先使用列表中首个支持该字形的字体。\n"
            "如需中西文字体分流，建议将西文字体置于中文字体上方。\n"
            "列表为空时使用系统默认无衬线字体。"
        )
        # type to filter: there can be hundreds of families
        names = font_names()
        # any name of a family finds it, in any case: 苹方, pingfang, PingFang SC
        self._lookup = {
            alias.casefold(): name for name, aliases in names.items() for alias in aliases
        }
        # a shown name always means itself, whichever family also lists it as an alias
        self._lookup.update({name.casefold(): name for name in names})
        self.pick = _FontPicker()
        self.pick.setPlaceholderText("输入或选择字体，中英文名均可")
        self.pick.addItems(list(names))
        self.pick.setMaxVisibleItems(12)
        model = QStandardItemModel(self.pick)
        for name, aliases in names.items():
            item = QStandardItem(name)
            item.setData(" / ".join(sorted(aliases)), _SEARCH_ROLE)
            model.appendRow(item)
        completer = QCompleter(model, self.pick)
        completer.setCompletionRole(_SEARCH_ROLE)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        self.pick.setCompleter(completer)
        self.pick.setText(self._resolve(QFont().defaultFamily()) or "")
        self.pick.submitted.connect(self._add)
        add = PushButton(fluent.icon("list-add"), "添加")
        add.clicked.connect(self._add)
        buttons = QVBoxLayout()
        buttons.setSpacing(8)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.pick, 1)
        pick_row.addWidget(add)
        buttons.addLayout(pick_row)
        moves = QHBoxLayout()
        for icon, tip, step in (("go-up", "上移", -1), ("go-down", "下移", 1)):
            button = ToolButton(fluent.icon(icon))
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, step=step: self._move(step))
            moves.addWidget(button)
        remove = PushButton(fluent.icon("list-remove"), "移除")
        remove.clicked.connect(self._remove)
        moves.addWidget(remove)
        moves.addStretch(1)
        buttons.addLayout(moves)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
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

    def _resolve(self, typed: str) -> str | None:
        return self._lookup.get(typed.strip().casefold())

    def _add(self) -> None:
        family = self._resolve(self.pick.currentText())
        if family is None:
            return
        self.pick.setText(family)
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
        self.filter_credits = CheckBox("过滤标题与制作信息")
        self.filter_credits.setChecked(d.filter_credits)
        self.filter_credits.setToolTip(
            "去掉歌词开头的「歌名 - 艺人」标题行、翻译来源说明与作词作曲等制作信息，以及结尾的版权声明\n只处理首尾连续的信息行，正文中带冒号的句子不受影响；不修改 TTML 文件"
        )
        self.filter_credits.toggled.connect(self.changed)
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
        self.settings.setValue("lyrics_filter_credits", self.filter_credits.isChecked())

    def restore_extra(self) -> None:
        self.fonts.set_families(self.settings.value("lyrics_fonts", [], type=list))
        self.filter_credits.setChecked(
            self.settings.value("lyrics_filter_credits", True, type=bool)
        )
