"""Sound field video settings: which tracks, frame, camera preset, head model, sizes and gate."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog, QVBoxLayout

from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.project import PlanContext, Project, Track
from dolbytool.core.renderer import renderer_problem
from dolbytool.core.task import Step
from dolbytool.features.soundfield.service import (
    COLOR_MODES,
    TRACK_SPEAKERS,
    VIEWS,
    SoundfieldSpec,
    SoundfieldVideo,
    soundfield_step,
    soundfield_target,
    track_speakers,
)
from dolbytool.ui import widgets
from dolbytool.ui.feature import FeaturePanel

GLB_FILTER = "glTF 二进制模型 (*.glb);;所有文件 (*)"
DEFAULT_TRACKS = ("original",)


class SoundfieldPanel(FeaturePanel):
    title_key = "soundfield.title"
    description_key = "soundfield.description"
    _title = "声场视频"
    icon = "audio-speakers-symbolic"
    _description = "将全景声动态对象在 3D 空间内的运动轨迹与电平可视化，输出带 Alpha 透明通道的 WebM (VP9) 视频"
    enabled_key = "do_soundfield"
    enabled_default = False

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        from dolbytool.core.i18n import t

        self.tracks = widgets.TrackList()
        self.tracks.changed.connect(self.changed)

        d = SoundfieldSpec()
        self.width_spin = self.spin("sf_w", 64, MAX_FRAME_SIDE, d.width, " px", step=2)
        self.height_spin = self.spin("sf_h", 64, MAX_FRAME_SIDE, d.height, " px", step=2)
        self.fps = self.spin("sf_fps", 10, 120, d.fps, " fps")
        view_labels = {
            "iso": t("option.view.iso"),
            "top": t("option.view.top"),
            "angle": t("option.view.angle"),
        }
        self.view = self.combo("sf_view", view_labels, d.view)
        self.head_edit, _button, head_row = widgets.browse_row(
            t("soundfield.head_placeholder"), self._browse_head, t("soundfield.pick_head_model")
        )
        self.persist_edit("sf_head", self.head_edit)
        self.object_scale = self.spin("sf_object_scale", 10, 1000, d.object_scale, " %")
        self.object_scale.setToolTip(t("soundfield.object_scale_tip"))
        self.head_scale = self.spin("sf_head_scale", 10, 1000, d.head_scale, " %")
        self.head_scale.setToolTip(t("soundfield.head_scale_tip"))
        self.gate = self.spin("sf_gate", -120, 0, d.gate_db, " dBFS")
        self.gate.setToolTip(t("soundfield.gate_tip"))
        self.fade = self.spin("sf_fade", 0, 2000, d.fade_ms, " ms", step=10)
        self.fade.setToolTip(t("soundfield.fade_tip"))
        self.dim = self.spin("sf_dim", 0, 100, d.dim, " %")
        self.dim.setToolTip(t("soundfield.dim_tip"))
        self.level_response = self.spin("sf_level", 0, 100, d.level_response, " %", step=10)
        self.level_response.setToolTip(t("soundfield.level_response_tip"))
        self.glow = self.spin("sf_glow", 0, 100, d.glow, " %", step=10)
        self.glow.setToolTip(t("soundfield.glow_tip"))
        self.trail = self.seconds(
            "sf_trail_seconds", d.trail, 0.0, 10.0, 0.1,
            t("soundfield.trail_tip"),
        )  # fmt: skip
        self.zoom = self.spin("sf_zoom", 50, 300, d.zoom, " %", step=5)
        self.zoom.setToolTip(t("soundfield.zoom_tip"))
        color_modes = {
            "tint": t("option.color_mode.tint"),
            "height": t("option.color_mode.height"),
            "object": t("option.color_mode.object"),
        }
        self.color_mode = self.combo("sf_color_mode", color_modes, d.color_mode)
        self.drop = self.check(
            "sf_drop", t("soundfield.drop"), d.drop, t("soundfield.drop_tip")
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.section(t("section.tracks"), self.tracks))
        layout.addWidget(
            widgets.section(
                t("section.frame_camera"),
                widgets.two_column_grid([
                    (t("label.size"), widgets.size_row(self.width_spin, self.height_spin), t("label.fps"), self.fps),
                    (t("label.view"), self.view, None, None),
                    (t("label.head_model"), head_row, None, None),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                t("section.space_objects"),
                widgets.two_column_grid([
                    (t("label.object_size"), self.object_scale, t("label.head_size"), self.head_scale),
                    (t("label.mute_gate"), self.gate, t("label.fade"), self.fade),
                    (t("label.outside_opacity"), self.dim, t("label.level_response"), self.level_response),
                    (t("label.glow"), self.glow, t("label.trail_length"), self.trail),
                    (t("label.coloring"), self.color_mode, t("label.framing_zoom"), self.zoom),
                    (t("label.assist"), self.drop, None, None),
                ]),
            )
        )  # fmt: skip
        layout.addStretch(1)

    def _head(self) -> Path | None:
        text = self.head_edit.text().strip()
        return Path(text).expanduser() if text else None

    def _browse_head(self) -> None:
        current = self._head()
        from dolbytool.core.i18n import t

        start = str(current.parent) if current else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, t("soundfield.dialog_pick_head"), start, t("soundfield.filter_glb"))
        if path:
            self.head_edit.setText(path)

    def _selected(self, project: Project) -> list[Track]:
        keys = self.tracks.keys()
        return [t for t in project.tracks if t.key in keys and t.key in TRACK_SPEAKERS]

    def spec(self) -> SoundfieldSpec:
        return SoundfieldSpec(
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            fps=self.fps.value(),
            view=self.view.currentData(),
            head=self._head(),
            object_scale=self.object_scale.value(),
            head_scale=self.head_scale.value(),
            gate_db=self.gate.value(),
            fade_ms=self.fade.value(),
            dim=self.dim.value(),
            level_response=self.level_response.value(),
            glow=self.glow.value(),
            trail=self.trail.value(),
            drop=self.drop.isChecked(),
            color_mode=self.color_mode.currentData(),
            zoom=self.zoom.value(),
        )

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        from dolbytool.core.i18n import t

        checked = self.tracks.keys()
        items = []
        for track in project.tracks:
            if track.key not in TRACK_SPEAKERS:
                continue  # the LFE and the frequency bands have no place in the room
            text = t("audio.original") if track.is_original else track.label
            speakers = " ".join(track_speakers(track.key, project.atmos_layout))
            tip = t("soundfield.track_tip", hint=track.hint, speakers=speakers)
            items.append(widgets.TrackItem(track.key, text, tip))
        self.tracks.set_tracks(items, checked)

    def problem(self, ctx: PlanContext) -> str | None:
        from dolbytool.core.i18n import t

        too_big = frame_problem(self.width_spin.value(), self.height_spin.value())
        if too_big:
            return too_big
        info = ctx.project.info
        if info is not None and not info.atmos:
            return t("soundfield.not_atmos")
        if info is not None and not self._selected(ctx.project):
            return t("soundfield.prompt_pick_track")
        head = self._head()
        if head and not head.is_file():
            return t("soundfield.head_not_found")
        return renderer_problem("soundfield.mjs")

    def summary(self, ctx: PlanContext) -> str | None:
        from dolbytool.core.i18n import t

        count = len(self._selected(ctx.project))
        return t("soundfield.summary", count=count) if count else None

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        audio, out, info = project.audio, project.output_dir, project.info
        if audio is None or out is None or info is None:
            return []
        videos = [
            SoundfieldVideo(t.key, t.label, soundfield_target(out, t.path))
            for t in self._selected(project)
        ]
        return [soundfield_step(audio, videos, self.spec(), info.duration, project.atmos_layout)]

    def save_extra(self) -> None:
        self.settings.setValue("sf_tracks", sorted(self.tracks.keys()))

    def restore_extra(self) -> None:
        self.tracks.seed(set(self.settings.value("sf_tracks", list(DEFAULT_TRACKS), type=list)))
        if not self.settings.contains("sf_trail_seconds") and self.settings.contains("sf_trail"):
            # stored in tenths of a second before
            self.trail.setValue(self.settings.value("sf_trail", 0, type=int) / 10)
