"""Waveform video settings: which tracks to render, look and encoder."""

from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QVBoxLayout

from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.project import PlanContext, Project, Track
from dolbytool.core.task import Step
from dolbytool.features.waveform.encoders import DEFAULT_ENCODER, ENCODER_LABELS
from dolbytool.features.waveform.service import WaveformSpec, waveform_step, waveform_target
from dolbytool.ui import fluent, widgets
from dolbytool.ui.feature import FeaturePanel

DEFAULT_TRACKS = ("surround",)
# "wave2/" keys: the dark look replaced the old white-background defaults.
COLOR_KEYS = ("wave2/wave_color", "wave2/background", "wave2/playhead")


class WaveformPanel(FeaturePanel):
    title = "波形视频"
    icon = "view-media-visualization"
    description = "为所选音轨生成水平滚动波形视频 (MP4)，若所选分轨未生成将自动追加前置分轨任务"
    enabled_key = "do_waveform"

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        self.tracks = widgets.TrackList()
        self.tracks.changed.connect(self.changed)

        d = WaveformSpec()
        self.pps = self.spin("pps", 10, 1000, d.pps, " 像素/秒")
        self.fps = self.spin("fps", 10, 120, d.fps, " fps")
        self.screen_w = self.spin("screen_w", 64, MAX_FRAME_SIDE, d.screen_width, " px", step=2)
        self.screen_h = self.spin("screen_h", 32, MAX_FRAME_SIDE, d.height, " px", step=2)
        self.wave_color = widgets.ColorButton(d.wave_color, "选择波形颜色")
        self.background = widgets.ColorButton(d.background, "选择背景颜色")
        self.playhead = widgets.ColorButton(d.playhead, "选择播放头颜色")
        self.unplayed = self.spin("unplayed", 0, 100, round(d.unplayed_level * 100), " %")
        self.unplayed.setToolTip("播放头右侧未播放波形保留的不透明度（100% 为不暗化）")
        self.encoder = fluent.ComboBox()
        for name, label in ENCODER_LABELS.items():
            self.encoder.addItem(label, name)
        self.encoder.setToolTip("硬件编码器不可用时将自动回退至 libx264 软件编码")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.section("音轨", self.tracks), 1)
        layout.addWidget(
            widgets.section(
                "画面",
                widgets.two_column_grid([
                    ("尺寸：", widgets.size_row(self.screen_w, self.screen_h), "帧率：", self.fps),
                    ("滚动速度：", self.pps, "编码器：", self.encoder),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "色彩与样式",
                widgets.two_column_grid([
                    ("背景：", self.background, "波形：", self.wave_color),
                    ("播放头：", self.playhead, "未播不透明度：", self.unplayed),
                ]),
            )
        )  # fmt: skip

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
        checked = self.tracks.keys()
        items = []
        for track in project.tracks:
            if track.is_original:
                text = f"源音频（{track.path.name}）"
            else:
                text = f"{track.label}（{track.key}{'' if track.exists else '，待生成'}）"
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
        too_big = frame_problem(self.screen_w.value(), self.screen_h.value())
        if too_big:
            return too_big
        if ctx.project.info is not None and not self._selected(ctx.project, ctx.batch):
            return "请至少勾选一条需要生成波形的音轨"
        return None

    def summary(self, ctx: PlanContext) -> str | None:
        count = len(self._selected(ctx.project, ctx.batch))
        return f"{count} 个波形视频" if count else None

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
        for key, button in zip(COLOR_KEYS, self._colors(), strict=True):
            self.settings.setValue(key, button.hex())
        self.settings.setValue("encoder", self.encoder.currentData())
        self.settings.setValue("tracks", sorted(self.tracks.keys()))

    def restore_extra(self) -> None:
        self.tracks.seed(set(self.settings.value("tracks", list(DEFAULT_TRACKS), type=list)))
        d = WaveformSpec()
        defaults = (d.wave_color, d.background, d.playhead)
        for key, button, default in zip(COLOR_KEYS, self._colors(), defaults, strict=True):
            button.setColor(self.settings.value(key, default))
        encoder = self.settings.value("encoder", DEFAULT_ENCODER)
        self.encoder.setCurrentIndex(max(0, self.encoder.findData(encoder)))

    def _colors(self) -> tuple[widgets.ColorButton, ...]:
        return (self.wave_color, self.background, self.playhead)
