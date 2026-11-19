"""Stem split settings: which stems to write, the crossover, and the ADM BWF."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout
from qfluentwidgets import CheckBox, CompactDoubleSpinBox, PushButton

from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.i18n import t
from soundweaver.core.project import ATMOS_LAYOUT_KEY, PlanContext, Project
from soundweaver.core.speakers import DEFAULT_ATMOS, LAYOUTS, atmos_layout
from soundweaver.core.stems import StemKind
from soundweaver.core.task import Step
from soundweaver.features.stems.pipeline import CROSSOVER_RANGE, DEFAULT_CROSSOVER_HZ
from soundweaver.features.stems.service import adm_target, av3a_target, binaural_target, split_step
from soundweaver.ui import fluent, widgets
from soundweaver.ui.feature import FeaturePanel

COLUMNS = 3


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
        self.crossover.setToolTip(t("stems.crossover_tip"))
        self.remember("crossover", self.crossover)
        self.adm = self.check("stem_adm", t("stems.adm_text"), False)
        self.remove_bed_placeholders = self.check(
            "stem_adm_remove_bed_placeholders",
            t("stems.adm_remove_bed_placeholders"),
            False,
            t("stems.adm_remove_bed_placeholders_tip"),
        )
        self._adm_project: Project | None = None
        self.remove_bed_placeholders.setEnabled(False)
        self.adm.toggled.connect(self._refresh_adm_options)
        self.remove_bed_placeholders.toggled.connect(self._refresh_adm_options)
        adm_options = QVBoxLayout()
        adm_options.addWidget(self.adm)
        adm_options.addWidget(self.remove_bed_placeholders)
        self.binaural = self.check("stem_binaural", t("stems.binaural_text"), False)
        self.binaural.setToolTip(t("stems.binaural_tip"))
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
                        (t("label.atmos_metadata"), adm_options),
                        (t("label.binaural_export"), self.binaural),
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
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
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
        layout = atmos_layout(str(self.layout_box.currentData() or DEFAULT_ATMOS.name))
        heights = set(layout.heights)
        for ch, box in self.mono_checks.items():
            box.blockSignals(True)
            if mode == "all":
                box.setChecked(True)
            elif mode == "none":
                box.setChecked(False)
            elif mode == "ear":
                box.setChecked(ch not in heights)
            elif mode == "top":
                box.setChecked(ch in heights)
            box.blockSignals(False)
        self._remember_mono()

    def _chosen_mono(self, project: Project) -> tuple[str, ...]:
        if project.speakers is None or not self.enabled:
            return ()
        return tuple(ch for ch in project.speakers.channels if ch in self._mono_selected)

    def _multichannel(self) -> str:
        return str(self.multichannel_box.currentData() or "none") if self.enabled else "none"

    def _cicp(self) -> int:
        return int(self.cicp_box.currentData() or 13)

    def _binaural(self, ctx: PlanContext) -> bool:
        info = ctx.project.info
        return (
            self.enabled
            and self.binaural.isChecked()
            and info is not None
            and av3a.find_binaural_renderer() is not None
            and (info.atmos or info.adm_bwf or info.channels >= 4 or info.is_mpegh)
        )

    def _av3a_encode(self, ctx: PlanContext) -> bool:
        info = ctx.project.info
        if not self.enabled or not self.av3a_encode.isChecked() or info is None:
            return False
        if av3a.find_encoder() is None:
            return False
        ch = info.channels if info.hoa_order > 0 else (len(ctx.project.speakers.channels) if ctx.project.speakers else info.channels)
        return info.hoa_order > 0 or ch in (1, 2, 4, 6, 8, 10, 12)

    def _chosen(self, project: Project) -> set[str]:
        return {track.key for track in project.stems if track.key not in self._skip}

    def _adm(self, ctx: PlanContext) -> bool:
        info = ctx.project.info
        return (
            self.enabled
            and self.adm.isChecked()
            and info is not None
            and info.atmos
            and not info.is_av3a
            and not info.adm_bwf
        )

    def _refresh_adm_options(self) -> None:
        self.remove_bed_placeholders.setEnabled(self.adm.isEnabled() and self.adm.isChecked())
        project = self._adm_project
        exists = bool(
            project
            and project.audio
            and project.output_dir
            and adm_target(
                project.output_dir, project.audio,
                remove_bed_placeholders=self.remove_bed_placeholders.isChecked(),
            ).is_file()
        )
        self.adm.setText(t("stems.adm_text") + (t("stems.existing_suffix") if exists else ""))

    def _kinds(self, ctx: PlanContext) -> list[str]:
        """Stems the split must write: ticked outputs plus what other outputs need."""
        kinds = set(ctx.required_stems)
        if self.enabled:
            kinds |= self._chosen(ctx.project)
        return [track.key for track in ctx.project.stems if track.key in kinds]

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
        info = project.info
        atmos = info is not None and info.atmos and not info.is_av3a and not info.adm_bwf
        self._adm_project = project
        self.adm.setEnabled(atmos)
        self._refresh_adm_options()
        self.adm.setToolTip(t("stems.adm_tip") if atmos else t("stems.adm_disabled_tip"))

        is_mpegh = project.info is not None and project.info.is_mpegh
        self.cicp_box.setEnabled(is_mpegh)

        has_binaural = av3a.find_binaural_renderer() is not None and (
            project.info is not None and (project.info.atmos or project.info.adm_bwf or project.info.channels >= 4 or project.info.is_mpegh)
        )
        self.binaural.setEnabled(has_binaural)
        bin_exists = bool(
            project.audio and project.output_dir and binaural_target(project.output_dir, project.audio).is_file()
        )
        self.binaural.setText(t("stems.binaural_text") + (t("stems.existing_suffix") if bin_exists else ""))

        has_enc = av3a.find_encoder() is not None and project.info is not None
        self.av3a_encode.setEnabled(has_enc)
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
        channels = project.speakers.channels if project.speakers is not None else ()
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
        return (
            bool(self._kinds(ctx))
            or self._adm(ctx)
            or self._multichannel() != "none"
            or bool(self._chosen_mono(ctx.project))
            or self._binaural(ctx)
            or self._av3a_encode(ctx)
        )

    def problem(self, ctx: PlanContext) -> str | None:
        if (
            ctx.project.info is not None
            and not self._chosen(ctx.project)
            and not self._adm(ctx)
            and self._multichannel() == "none"
            and not self._chosen_mono(ctx.project)
            and not self._binaural(ctx)
            and not self._av3a_encode(ctx)
        ):
            return t("stems.prompt_pick_one")
        return None

    def summary(self, ctx: PlanContext) -> str | None:
        kinds = self._kinds(ctx)
        parts = [t("stems.summary", count=len(kinds))] if kinds else []
        if self._adm(ctx):
            parts.append("ADM BWF")
        mc = self._multichannel()
        if mc != "none":
            parts.append(mc.upper())
        mono = self._chosen_mono(ctx.project)
        if mono:
            parts.append(t("stems.mono_channels_count", count=len(mono)))
        if self._binaural(ctx):
            parts.append("Binaural")
        if self._av3a_encode(ctx):
            parts.append("AV3A")
        if not parts:
            return None
        sep = t("common.list_sep")
        note = t("stems.needed_by", by=sep.join(ctx.requested_by)) if self._implicit(ctx) else ""
        return sep.join(parts) + note

    def notes(self, ctx: PlanContext) -> list[str]:
        return [t("stems.implicit_note")] if self._implicit(ctx) else []

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        audio, out, info = project.audio, project.output_dir, project.info
        if audio is None or out is None or info is None:
            return []
        export_adm = self._adm(ctx)
        compact_adm = export_adm and self.remove_bed_placeholders.isChecked()
        adm = adm_target(out, audio, remove_bed_placeholders=compact_adm) if export_adm else None
        kinds = [StemKind(k) for k in self._kinds(ctx)]
        mc = self._multichannel()
        mono = self._chosen_mono(project)
        binaural = self._binaural(ctx)
        av3a_enc = self._av3a_encode(ctx)
        if not kinds and adm is None and mc == "none" and not mono and not binaural and not av3a_enc:
            return []
        return [
            split_step(
                audio, out, kinds, self.crossover.value(),
                atmos=info.atmos, adm=adm, speakers=project.speakers,
                remove_bed_placeholders=compact_adm,
                multichannel=mc, mono_channels=mono,
                cicp=self._cicp(), binaural=binaural,
                av3a_encode=av3a_enc, hoa_order=info.hoa_order,
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
