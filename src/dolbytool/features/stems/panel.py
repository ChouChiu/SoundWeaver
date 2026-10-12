"""Stem split settings: which stems to write, the crossover, and the ADM BWF."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout
from qfluentwidgets import CheckBox, CompactDoubleSpinBox, HyperlinkButton

from dolbytool.core.project import ATMOS_LAYOUT_KEY, PlanContext, Project
from dolbytool.core.speakers import DEFAULT_ATMOS, LAYOUTS, atmos_layout
from dolbytool.core.stems import StemKind
from dolbytool.core.task import Step
from dolbytool.features.stems.pipeline import CROSSOVER_RANGE, DEFAULT_CROSSOVER_HZ
from dolbytool.features.stems.service import adm_target, split_step
from dolbytool.ui import fluent, widgets
from dolbytool.ui.feature import FeaturePanel

COLUMNS = 3
ADM_TEXT = "导出 ADM BWF（含空间对象三维轨迹）"
ADM_TIP = (
    "仅适用于杜比全景声源：将各动态对象与声床分轨导出为 24-bit 多声道 BWF WAV，\n"
    "内嵌 ITU-R BS.2076-2 axml 与 chna 元数据，记录空间三维轨迹与平移增益，\n"
    "兼容标准 ADM 查看器、杜比渲染器及专业 DAW"
)
LAYOUT_TIP = (
    "杜比全景声源渲染到的扬声器布局，决定可导出的分轨与声场视频的分轨暗化；\n"
    "床声道在没有对应扬声器的布局中按其位置摆放，ADM BWF 不受影响"
)


class StemsPanel(FeaturePanel):
    title = "分轨"
    icon = "view-media-equalizer"
    description = "按声道布局分离导出 WAV 分轨；杜比全景声源可渲染为 5.1 至 9.1.6 等布局，支持导出标准 ADM BWF"
    enabled_key = "do_stems"

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        self.checks: dict[str, CheckBox] = {}
        # unticked stems, including those of layouts not shown right now
        self._skip: set[str] = set()
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(6)
        buttons = QHBoxLayout()
        for text, state in (("全选", True), ("全不选", False)):
            button = HyperlinkButton()
            button.setText(text)
            button.clicked.connect(lambda _=False, on=state: self._check_all(on))
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.info = widgets.hint_label(wrap=False)
        buttons.addWidget(self.info)
        stems = QVBoxLayout()
        stems.addLayout(self.grid)
        stems.addLayout(buttons)

        self.crossover = CompactDoubleSpinBox()
        self.crossover.setRange(*CROSSOVER_RANGE)
        self.crossover.setDecimals(0)
        self.crossover.setSingleStep(10)
        self.crossover.setSuffix(" Hz")
        self.crossover.setValue(DEFAULT_CROSSOVER_HZ)
        self.crossover.setToolTip("低频段与高频段的分频截止频率")
        self.remember("crossover", self.crossover)
        self.adm = CheckBox(ADM_TEXT)
        self.adm.toggled.connect(self.changed)
        self.layout_box = fluent.ComboBox()
        for speakers in LAYOUTS:
            self.layout_box.addItem(speakers.name, speakers.name)
        self.layout_box.setCurrentIndex(self.layout_box.findData(DEFAULT_ATMOS.name))
        self.layout_box.setToolTip(LAYOUT_TIP)
        self.layout_box.currentIndexChanged.connect(self._layout_picked)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.section("输出分轨", stems))
        layout.addWidget(
            widgets.section(
                "设置",
                widgets.form(
                    [
                        ("全景声渲染布局：", self.layout_box),
                        ("分频点：", self.crossover),
                        ("全景声元数据：", self.adm),
                    ]
                ),
            )
        )
        layout.addStretch(1)

    def _layout_picked(self) -> None:
        # stored at once: the input panels read it to list each project's stems
        self.settings.setValue(ATMOS_LAYOUT_KEY, self.layout_box.currentData())
        self.tracks_changed.emit()

    def _check_all(self, on: bool) -> None:
        for box in self.checks.values():
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
        self._remember()

    def _remember(self) -> None:
        unticked = {k for k, box in self.checks.items() if not box.isChecked()}
        self._skip = (self._skip - self.checks.keys()) | unticked
        self.changed.emit()

    def _chosen(self, project: Project) -> set[str]:
        return {t.key for t in project.stems if t.key not in self._skip}

    def _adm(self, ctx: PlanContext) -> bool:
        info = ctx.project.info
        return self.enabled and self.adm.isChecked() and info is not None and info.atmos

    def _kinds(self, ctx: PlanContext) -> list[str]:
        """Stems the split must write: ticked outputs plus what other outputs need."""
        kinds = set(ctx.required_stems)
        if self.enabled:
            kinds |= self._chosen(ctx.project)
        return [t.key for t in ctx.project.stems if t.key in kinds]

    def _implicit(self, ctx: PlanContext) -> bool:
        """The split runs only because another output needs a stem that is not there yet."""
        chosen = self._chosen(ctx.project) or self._adm(ctx)
        return bool(self._kinds(ctx)) and not (self.enabled and chosen)

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        # Remember unticked stems rather than ticked ones, so a layout seen for the
        # first time starts with everything ticked.
        for box in self.checks.values():
            self.grid.removeWidget(box)
            box.deleteLater()
        self.checks = {}
        atmos = project.info is not None and project.info.atmos
        self.adm.setEnabled(atmos)
        exists = bool(
            project.audio
            and project.output_dir
            and adm_target(project.output_dir, project.audio).is_file()
        )
        self.adm.setText(ADM_TEXT + ("（已有）" if exists else ""))
        self.adm.setToolTip(ADM_TIP if atmos else "当前输入非杜比全景声，无法导出 ADM BWF")
        stems = project.stems
        for index, track in enumerate(stems):
            box = CheckBox(track.label + ("（已有）" if track.exists else ""))
            box.setChecked(track.key not in self._skip)
            box.setToolTip(f"{track.hint}\n{track.path}")
            box.toggled.connect(self._remember)
            self.grid.addWidget(box, index // COLUMNS, index % COLUMNS)
            self.checks[track.key] = box
        if not stems:
            self.info.setText("载入音频后显示可用分轨")
            return
        done = sum(t.exists for t in stems)
        self.info.setText(
            f"输出目录已存在 {done}/{len(stems)} 条分轨" if done else "输出目录尚未生成分轨"
        )

    def is_active(self, ctx: PlanContext) -> bool:
        return bool(self._kinds(ctx)) or self._adm(ctx)

    def problem(self, ctx: PlanContext) -> str | None:
        if ctx.project.info is not None and not self._chosen(ctx.project) and not self._adm(ctx):
            return "请至少勾选一条需要导出的分轨"
        return None

    def summary(self, ctx: PlanContext) -> str | None:
        kinds = self._kinds(ctx)
        parts = [f"{len(kinds)} 条分轨"] if kinds else []
        if self._adm(ctx):
            parts.append("ADM BWF")
        if not parts:
            return None
        note = f"（{'、'.join(ctx.requested_by)}需要）" if self._implicit(ctx) else ""
        return "、".join(parts) + note

    def notes(self, ctx: PlanContext) -> list[str]:
        return ["所选音轨尚未分轨，将自动追加前置分轨任务"] if self._implicit(ctx) else []

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        audio, out, info = project.audio, project.output_dir, project.info
        if audio is None or out is None or info is None:
            return []
        adm = adm_target(out, audio) if self._adm(ctx) else None
        kinds = [StemKind(k) for k in self._kinds(ctx)]
        if not kinds and adm is None:
            return []
        return [
            split_step(
                audio, out, kinds, self.crossover.value(),
                atmos=info.atmos, adm=adm, speakers=project.speakers,
            )
        ]  # fmt: skip

    def save_extra(self) -> None:
        self.settings.setValue("stem_adm", self.adm.isChecked())
        self.settings.setValue("stem_skip", sorted(self._skip))

    def restore_extra(self) -> None:
        self._skip = set(self.settings.value("stem_skip", [], type=list))
        self.adm.setChecked(self.settings.value("stem_adm", False, type=bool))
        name = atmos_layout(str(self.settings.value(ATMOS_LAYOUT_KEY, ""))).name
        # the window reads the projects once everything is restored
        self.layout_box.blockSignals(True)
        self.layout_box.setCurrentIndex(self.layout_box.findData(name))
        self.layout_box.blockSignals(False)
