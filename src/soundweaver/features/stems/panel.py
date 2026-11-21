"""Stem split settings: which stems to write, the crossover, and the ADM BWF."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSettings, QSignalBlocker
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout
from qfluentwidgets import CheckBox, CompactDoubleSpinBox, PushButton

from soundweaver.core.audio import AudioInfo
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.i18n import t
from soundweaver.core.project import ATMOS_LAYOUT_KEY, PlanContext, Project
from soundweaver.core.speakers import DEFAULT_ATMOS, LAYOUTS, atmos_layout
from soundweaver.core.stems import StemKind
from soundweaver.core.task import Step
from soundweaver.features.stems.pipeline import CROSSOVER_RANGE, DEFAULT_CROSSOVER_HZ
from soundweaver.features.stems.service import adm_target, av3a_target, split_step
from soundweaver.ui import fluent, widgets
from soundweaver.ui.feature import FeaturePanel

COLUMNS = 3


@dataclass(frozen=True, slots=True)
class SplitSelection:
    kinds: tuple[StemKind, ...]
    adm: bool
    multichannel: str
    mono: tuple[str, ...]
    av3a: bool

    def has_outputs(self) -> bool:
        return bool(
            self.kinds or self.adm or self.multichannel != "none"
            or self.mono or self.av3a
        )


def _adm_supported(info: AudioInfo | None) -> bool:
    """ADM can describe a Dolby Atmos track's objects and a 360RA track's objects alike."""
    source = info is not None and (info.atmos or info.is_mpegh)
    return source and not info.is_av3a and not info.adm_bwf


def _av3a_supported(project: Project) -> bool:
    info = project.info
    if info is None:
        return False
    layout = project.speakers
    channels = len(layout.channels) if layout else info.channels
    return (info.hoa_order > 0 or channels in (1, 2, 4, 6, 8, 10, 12)) and av3a.find_encoder() is not None


class StemsPanel(FeaturePanel):
    title_key = "stems.title"
    description_key = "stems.description"
    icon = "view-media-equalizer"
    enabled_key = "do_stems"

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        self.checks: dict[str, CheckBox] = {}
        # unticked stems, including those of layouts not shown right now
        self._skip: set[str] = set()
        self.grid = QGridLayout()
        self.grid.setHorizontalSpacing(widgets.PAIR_GAP // 2)
        self.grid.setVerticalSpacing(widgets.ROW_SPACING)
        self.info = widgets.hint_label(wrap=False)
        buttons = widgets.select_all_row(self._check_all, self.info)
        stems = QVBoxLayout()
        stems.addLayout(self.grid)
        stems.addLayout(buttons)

        self.crossover = CompactDoubleSpinBox()
        self.crossover.setRange(*CROSSOVER_RANGE)
        self.crossover.setDecimals(0)
        self.crossover.setSingleStep(10)
        self.crossover.setSuffix(" Hz")
        self.crossover.setValue(DEFAULT_CROSSOVER_HZ)
        widgets.fixed_width(self.crossover)
        self.crossover.setToolTip(t("stems.crossover_tip"))
        self.remember("crossover", self.crossover)
        self.adm = self.check("stem_adm", t("stems.adm_text"), False)
        self.remove_bed_placeholders = self.check(
            "stem_adm_remove_bed_placeholders",
            t("stems.adm_remove_bed_placeholders"),
            False,
            t("stems.adm_remove_bed_placeholders_tip"),
        )
        self._project = Project()
        self.remove_bed_placeholders.setEnabled(False)
        self.adm.toggled.connect(self._refresh_adm_options)
        self.remove_bed_placeholders.toggled.connect(self._refresh_adm_options)
        self.av3a_encode = self.check("stem_av3a_encode", t("stems.av3a_text"), False)
        self.av3a_encode.setToolTip(t("stems.av3a_tip"))
        self.multichannel_box = self.combo(
            "stem_multichannel",
            {
                "none": t("stems.mc_none"),
                "wav": t("stems.mc_wav"),
                "caf": t("stems.mc_caf"),
            },
            "none",
        )
        self.layout_box = fluent.ComboBox()
        for speakers in LAYOUTS:
            self.layout_box.addItem(speakers.name, speakers.name)
        self.layout_box.setCurrentIndex(self.layout_box.findData(DEFAULT_ATMOS.name))
        self.layout_box.setToolTip(t("stems.layout_tip"))
        self.layout_box.currentIndexChanged.connect(self._layout_picked)

        self.cicp_box = fluent.ComboBox()
        for cid in mpegh.SUPPORTED_CICP:
            lay = mpegh.CICP_LAYOUTS[cid]
            self.cicp_box.addItem(f"CICP {cid}: {lay.label}", cid)
        self.cicp_box.setCurrentIndex(self.cicp_box.findData(13))
        self.cicp_box.setToolTip(t("stems.cicp_tip"))
        self.cicp_box.currentIndexChanged.connect(lambda: self.changed.emit())

        self._mono_selected: set[str] = set()
        self.mono_checks: dict[str, CheckBox] = {}
        self.mono_grid = QGridLayout()
        self.mono_grid.setHorizontalSpacing(widgets.PAIR_GAP // 2)
        self.mono_grid.setVerticalSpacing(widgets.ROW_SPACING)
        mono_btns = QHBoxLayout()
        mono_btns.setSpacing(6)
        self.btn_mono_all = PushButton(t("stems.mono_all"))
        self.btn_mono_ear = PushButton(t("stems.mono_ear"))
        self.btn_mono_top = PushButton(t("stems.mono_height"))
        self.btn_mono_none = PushButton(t("stems.mono_none"))
        self.btn_mono_all.clicked.connect(lambda: self._select_mono("all"))
        self.btn_mono_ear.clicked.connect(lambda: self._select_mono("ear"))
        self.btn_mono_top.clicked.connect(lambda: self._select_mono("top"))
        self.btn_mono_none.clicked.connect(lambda: self._select_mono("none"))
        for b in (self.btn_mono_all, self.btn_mono_ear, self.btn_mono_top, self.btn_mono_none):
            mono_btns.addWidget(b)
        mono_btns.addStretch(1)
        mono_box = QVBoxLayout()
        mono_box.addLayout(mono_btns)
        mono_box.addLayout(self.mono_grid)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.section(t("section.stems_output"), stems))
        layout.addWidget(widgets.section(t("section.mono_channels"), mono_box))
        layout.addWidget(
            widgets.section(
                t("section.settings"),
                widgets.form(
                    [
                        (t("label.atmos_render_layout"), self.layout_box),
                        (t("label.cicp_layout"), self.cicp_box),
                        (t("label.crossover"), self.crossover),
                        (t("label.multichannel_export"), self.multichannel_box),
                        # the ADM switches are one setting split over two rows, so the
                        # label lines up with the first checkbox instead of the stack's middle
                        (t("label.atmos_metadata"), self.adm),
                        ("", self.remove_bed_placeholders),
                        (t("label.av3a_export"), self.av3a_encode),
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
            with QSignalBlocker(box):
                box.setChecked(on)
        self._remember()

    def _remember(self) -> None:
        unticked = {k for k, box in self.checks.items() if not box.isChecked()}
        self._skip = (self._skip - self.checks.keys()) | unticked
        self.changed.emit()

    def _remember_mono(self) -> None:
        checked = {k for k, box in self.mono_checks.items() if box.isChecked()}
        self._mono_selected = (self._mono_selected - self.mono_checks.keys()) | checked
        self.changed.emit()

    def _select_mono(self, mode: str) -> None:
        speakers = self._project.speakers
        heights = set(speakers.heights) if speakers else set()
        channels = self.mono_checks.keys()
        selected = {
            "all": channels,
            "none": (),
            "ear": channels - heights,
            "top": channels & heights,
        }[mode]
        for channel, box in self.mono_checks.items():
            with QSignalBlocker(box):
                box.setChecked(channel in selected)
        self._remember_mono()

    def _cicp(self) -> int:
        return self.cicp_box.currentData()

    def _chosen(self, project: Project) -> set[str]:
        return {track.key for track in project.stems if track.key not in self._skip}

    def _selection(self, ctx: PlanContext) -> SplitSelection:
        project = ctx.project
        chosen = self._chosen(project) if self.enabled else set()
        kinds = ctx.required_stems | chosen
        layout = project.speakers
        mono = (
            tuple(channel for channel in layout.channels if channel in self._mono_selected)
            if self.enabled and layout else ()
        )
        return SplitSelection(
            kinds=tuple(StemKind(track.key) for track in project.stems if track.key in kinds),
            adm=self.enabled and self.adm.isChecked() and _adm_supported(project.info),
            multichannel=self.multichannel_box.currentData() if self.enabled else "none",
            mono=mono,
            av3a=self.enabled and self.av3a_encode.isChecked() and _av3a_supported(project),
        )

    def _refresh_adm_options(self) -> None:
        # Only the Dolby writer pads a bed; 360RA writes the objects the stream carries.
        info = self._project.info
        padding = bool(info is not None and info.atmos)
        self.remove_bed_placeholders.setEnabled(
            padding and self.adm.isEnabled() and self.adm.isChecked()
        )
        project = self._project
        exists = bool(
            project.audio
            and project.output_dir
            and adm_target(
                project.output_dir, project.audio,
                remove_bed_placeholders=self.remove_bed_placeholders.isChecked(),
            ).is_file()
        )
        self.adm.setText(t("stems.adm_text") + (t("stems.existing_suffix") if exists else ""))

    def _implicit(self, ctx: PlanContext, selection: SplitSelection) -> bool:
        return bool(selection.kinds) and not (
            self.enabled and (self._chosen(ctx.project) or selection.adm)
        )

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        # Remember unticked stems rather than ticked ones, so a layout seen for the
        # first time starts with everything ticked.
        for box in self.checks.values():
            self.grid.removeWidget(box)
            box.deleteLater()
        self.checks = {}
        info = project.info
        atmos = _adm_supported(info)
        self._project = project
        self.adm.setEnabled(atmos)
        self._refresh_adm_options()
        self.adm.setToolTip(t("stems.adm_tip") if atmos else t("stems.adm_disabled_tip"))

        is_mpegh = project.info is not None and project.info.is_mpegh
        self.cicp_box.setEnabled(is_mpegh)

        self.av3a_encode.setEnabled(_av3a_supported(project))
        av3a_exists = bool(
            project.audio and project.output_dir and av3a_target(project.output_dir, project.audio).is_file()
        )
        self.av3a_encode.setText(t("stems.av3a_text") + (t("stems.existing_suffix") if av3a_exists else ""))
        stems = project.stems
        for index, track in enumerate(stems):
            box = CheckBox(track.label + (t("stems.existing_suffix") if track.exists else ""))
            box.setChecked(track.key not in self._skip)
            box.setToolTip(f"{track.hint}\n{track.path}")
            box.toggled.connect(self._remember)
            self.grid.addWidget(box, index // COLUMNS, index % COLUMNS)
            self.checks[track.key] = box
        if not stems:
            self.info.setText(t("stems.hint_empty"))
        else:
            done = sum(track.exists for track in stems)
            self.info.setText(
                t("stems.done_status", done=done, total=len(stems))
                if done
                else t("stems.not_done_status")
            )
        self._refresh_mono_grid(project)

    def _refresh_mono_grid(self, project: Project) -> None:
        for box in self.mono_checks.values():
            self.mono_grid.removeWidget(box)
            box.deleteLater()
        self.mono_checks = {}
        speakers = project.speakers
        channels = speakers.channels if speakers is not None else ()
        for i, ch in enumerate(channels):
            box = CheckBox(ch)
            box.setChecked(ch in self._mono_selected)
            box.toggled.connect(self._remember_mono)
            self.mono_grid.addWidget(box, i // 6, i % 6)
            self.mono_checks[ch] = box
        has_ch = bool(channels)
        for b in (self.btn_mono_all, self.btn_mono_ear, self.btn_mono_top, self.btn_mono_none):
            b.setEnabled(has_ch)

    def is_active(self, ctx: PlanContext) -> bool:
        return self._selection(ctx).has_outputs()

    def problem(self, ctx: PlanContext) -> str | None:
        if ctx.project.info is not None and not self._selection(ctx).has_outputs():
            return t("stems.prompt_pick_one")
        return None

    def summary(self, ctx: PlanContext) -> str | None:
        selection = self._selection(ctx)
        parts = []
        if selection.kinds:
            parts.append(t("stems.summary", count=len(selection.kinds)))
        if selection.adm:
            parts.append("ADM BWF")
        if selection.multichannel != "none":
            parts.append(selection.multichannel.upper())
        if selection.mono:
            parts.append(t("stems.mono_channels_count", count=len(selection.mono)))
        if selection.av3a:
            parts.append("AV3A")
        if not parts:
            return None
        sep = t("common.list_sep")
        summary = sep.join(parts)
        if self._implicit(ctx, selection):
            summary += t("stems.needed_by", by=sep.join(ctx.requested_by))
        return summary

    def notes(self, ctx: PlanContext) -> list[str]:
        return [t("stems.implicit_note")] if self._implicit(ctx, self._selection(ctx)) else []

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        audio, out, info = project.audio, project.output_dir, project.info
        if audio is None or out is None or info is None:
            return []
        selection = self._selection(ctx)
        if not selection.has_outputs():
            return []
        compact = (
            selection.adm and info.atmos and self.remove_bed_placeholders.isChecked()
        )
        # The container's own length: a 360RA master follows it where the decoder pads.
        length = round(info.duration * info.sample_rate) or None if info.duration else None
        adm = adm_target(out, audio, remove_bed_placeholders=compact) if selection.adm else None
        return [
            split_step(
                audio, out, list(selection.kinds), self.crossover.value(),
                atmos=info.atmos, adm=adm, speakers=project.speakers,
                remove_bed_placeholders=compact,
                multichannel=selection.multichannel, mono_channels=selection.mono,
                cicp=self._cicp(),
                av3a_encode=selection.av3a, hoa_order=info.hoa_order,
                length=length,
            )
        ]  # fmt: skip

    def save_extra(self) -> None:
        self.settings.setValue("stem_skip", sorted(self._skip))
        self.settings.setValue("stem_mono", sorted(self._mono_selected))
        self.settings.setValue("stem_cicp", self._cicp())

    def restore_extra(self) -> None:
        self._skip = set(self.settings.value("stem_skip", [], type=list))
        self._mono_selected = set(self.settings.value("stem_mono", [], type=list))
        name = atmos_layout(str(self.settings.value(ATMOS_LAYOUT_KEY, ""))).name
        # the window reads the projects once everything is restored
        self.layout_box.blockSignals(True)
        self.layout_box.setCurrentIndex(self.layout_box.findData(name))
        self.layout_box.blockSignals(False)
        cicp_idx = self.cicp_box.findData(self.settings.value("stem_cicp", 13, type=int))
        self.cicp_box.blockSignals(True)
        if cicp_idx >= 0:
            self.cicp_box.setCurrentIndex(cicp_idx)
        self.cicp_box.blockSignals(False)
