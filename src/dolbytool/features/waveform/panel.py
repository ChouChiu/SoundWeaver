"""Waveform video settings: which tracks to render, look and encoder."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QVBoxLayout

from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.project import PlanContext, Project, Track
from dolbytool.core.task import Step
from dolbytool.features.waveform.encoders import DEFAULT_ENCODER, ENCODER_LABELS
from dolbytool.features.waveform.service import WaveformSpec, waveform_step, waveform_target
from dolbytool.ui import widgets
from dolbytool.ui.feature import FeaturePanel

DEFAULT_TRACKS = ("surround",)


class WaveformPanel(FeaturePanel):
    title_key = "waveform.title"
    description_key = "waveform.description"
    _title = "波形视频"
    icon = "view-media-visualization"
    _description = "为所选音轨生成水平滚动波形视频 (MP4)，若所选分轨未生成将自动追加前置分轨任务"
    enabled_key = "do_waveform"

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        from dolbytool.core.i18n import t

        self.tracks = widgets.TrackList()
        self.tracks.changed.connect(self.changed)

        d = WaveformSpec()
        self.pps = self.spin("pps", 10, 1000, d.pps, t("waveform.pps_suffix"))
        self.fps = self.spin("fps", 10, 120, d.fps, " fps")
        self.screen_w = self.spin("screen_w", 64, MAX_FRAME_SIDE, d.screen_width, " px", step=2)
        self.screen_h = self.spin("screen_h", 32, MAX_FRAME_SIDE, d.height, " px", step=2)
        # "wave2/" keys: the dark look replaced the old white-background defaults
        self.wave_color = self.color("wave2/wave_color", d.wave_color, t("waveform.pick_wave_color"))
        self.background = self.color("wave2/background", d.background, t("waveform.pick_bg_color"))
        self.playhead = self.color("wave2/playhead", d.playhead, t("waveform.pick_playhead_color"))
        self.unplayed = self.spin(
            "unplayed", 0, 100, round(d.unplayed_level * 100), " %",
            tip=t("waveform.unplayed_tip"),
        )  # fmt: skip
        encoder_labels = {
            "h264_nvenc": t("option.encoder.nvenc_h264"),
            "hevc_nvenc": t("option.encoder.nvenc_hevc"),
            "libx264": t("option.encoder.libx264"),
        }
        self.encoder = self.combo(
            "encoder",
            encoder_labels,
            DEFAULT_ENCODER,
            t("waveform.encoder_tip"),
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.section(t("section.tracks"), self.tracks))
        layout.addWidget(
            widgets.section(
                t("section.video"),
                widgets.two_column_grid([
                    (t("label.size"), widgets.size_row(self.screen_w, self.screen_h), t("label.fps"), self.fps),
                    (t("label.scroll_speed"), self.pps, t("label.encoder"), self.encoder),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                t("section.color_style"),
                widgets.two_column_grid([
                    (t("label.background"), self.background, t("label.waveform"), self.wave_color),
                    (t("label.playhead"), self.playhead, t("label.unplayed_opacity"), self.unplayed),
                ]),
            )
        )  # fmt: skip
        layout.addStretch(1)

    def spec(self) -> WaveformSpec:
        return WaveformSpec(
            pps=self.pps.value(),
            screen_width=self.screen_w.value(),
            height=self.screen_h.value(),
            wave_color=self.wave_color.hex(),
            background=self.background.hex(),
            playhead=self.playhead.hex(),
            unplayed_level=self.unplayed.value() / 100,
            fps=self.fps.value(),
            encoder=self.encoder.currentData(),
        )

    def _selected(self, project: Project, batch: bool = False) -> list[Track]:
        keys = self.tracks.keys()
        picked = [t for t in project.tracks if t.key in keys]
        # a batch track whose layout shares no ticked key gets its source, as refresh() would
        if batch and not picked and project.tracks:
            return [project.tracks[0]]
        return picked

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        from dolbytool.core.i18n import t

        checked = self.tracks.keys()
        items = []
        for track in project.tracks:
            if track.is_original:
                text = t("audio.original")
            else:
                text = track.label + ("" if track.exists else t("waveform.pending_suffix"))
            items.append(
                widgets.TrackItem(
                    track.key, text, f"{track.hint}\n{track.path}",
                    pending=not track.is_original and not track.exists,
                )
            )  # fmt: skip
        if items and not checked & {i.key for i in items}:
            # Nothing remembered applies to this layout (e.g. a stereo file): show the source
            # ticked, as a batch would make it, without forgetting the other layouts' picks.
            self.tracks.set_tracks(items, {items[0].key}, remember=False)
            return
        self.tracks.set_tracks(items, checked)

    def required_stems(self, project: Project) -> set[str]:
        return {t.key for t in self._selected(project) if not t.is_original and not t.exists}

    def problem(self, ctx: PlanContext) -> str | None:
        from dolbytool.core.i18n import t

        too_big = frame_problem(self.screen_w.value(), self.screen_h.value())
        if too_big:
            return too_big
        if ctx.project.info is not None and not self._selected(ctx.project, ctx.batch):
            return t("waveform.prompt_pick_track")
        return None

    def summary(self, ctx: PlanContext) -> str | None:
        from dolbytool.core.i18n import t

        count = len(self._selected(ctx.project, ctx.batch))
        return t("waveform.summary", count=count) if count else None

    def plan(self, ctx: PlanContext) -> list[Step]:
        out = ctx.project.output_dir
        if out is None:
            return []
        spec = self.spec()
        return [
            waveform_step(track, waveform_target(out, track.path), spec)
            for track in self._selected(ctx.project, ctx.batch)
        ]

    def save_extra(self) -> None:
        self.settings.setValue("tracks", sorted(self.tracks.keys()))

    def restore_extra(self) -> None:
        self.tracks.seed(set(self.settings.value("tracks", list(DEFAULT_TRACKS), type=list)))
