"""Sound field video settings: which tracks, frame, camera preset, head model, sizes and gate."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QFileDialog, QVBoxLayout
from qfluentwidgets import CheckBox

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
from dolbytool.ui import fluent, widgets
from dolbytool.ui.feature import FeaturePanel

GLB_FILTER = "glTF 二进制模型 (*.glb);;所有文件 (*)"
DEFAULT_TRACKS = ("original",)


class SoundfieldPanel(FeaturePanel):
    title = "声场视频"
    icon = "audio-speakers-symbolic"
    description = "将全景声动态对象在 3D 空间内的运动轨迹与电平可视化，输出带 Alpha 透明通道的 WebM (VP9) 视频"
    enabled_key = "do_soundfield"
    enabled_default = False

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        self.tracks = widgets.TrackList()
        self.tracks.changed.connect(self.changed)

        d = SoundfieldSpec()
        self.width_spin = self.spin("sf_w", 64, MAX_FRAME_SIDE, d.width, " px", step=2)
        self.height_spin = self.spin("sf_h", 64, MAX_FRAME_SIDE, d.height, " px", step=2)
        self.fps = self.spin("sf_fps", 10, 120, d.fps, " fps")
        self.view = fluent.ComboBox()
        for name, label in VIEWS.items():
            self.view.addItem(label, name)
        self.head_edit, _button, head_row = widgets.browse_row(
            "留空使用默认听者剪影", self._browse_head, "选择 3D 头模 (.glb)"
        )
        self.head_edit.textChanged.connect(lambda _t: self.changed.emit())
        self.object_scale = self.spin("sf_object_scale", 10, 1000, d.object_scale, " %")
        self.object_scale.setToolTip("动态对象小球的显示比例（100% 为标准尺寸）")
        self.head_scale = self.spin("sf_head_scale", 10, 1000, d.head_scale, " %")
        self.head_scale.setToolTip("听者模型显示比例（100% 时高度约为声场空间的 15%）")
        self.gate = self.spin("sf_gate", -120, 0, d.gate_db, " dBFS")
        self.gate.setToolTip(
            "对象电平低于该门限时平滑淡出隐藏；\n"
            "在分轨模式下，若分配至该音轨扬声器的能量低于此门限则平滑暗化"
        )
        self.fade = self.spin("sf_fade", 0, 2000, d.fade_ms, " ms", step=10)
        self.fade.setToolTip("对象显隐与明暗切换的平滑过渡时长")
        self.dim = self.spin("sf_dim", 0, 100, d.dim, " %")
        self.dim.setToolTip("分轨模式下，非本音轨声扬对象的保留不透明度（0% 为完全隐藏）")
        self.level_response = self.spin("sf_level", 0, 100, d.level_response, " %", step=10)
        self.level_response.setToolTip(
            "对象亮度、光晕与大小随其实时电平变化的程度（0% 为不随电平变化）"
        )
        self.glow = self.spin("sf_glow", 0, 100, d.glow, " %", step=10)
        self.glow.setToolTip("对象周围光晕的强度（0% 为关闭）")
        self.trail = self.spin("sf_trail", 0, 100, d.trail, " ×0.1 秒")
        self.trail.setToolTip("对象身后保留的运动轨迹时长（0 为关闭）；对象瞬移时轨迹会截断")
        self.zoom = self.spin("sf_zoom", 50, 300, d.zoom, " %", step=5)
        self.zoom.setToolTip("相对于刚好容纳整个房间的机位距离，100% 为自动取景，越大越近")
        self.color_mode = fluent.ComboBox()
        for name, label in COLOR_MODES.items():
            self.color_mode.addItem(label, name)
        self.color_mode.currentIndexChanged.connect(self.changed)
        self.drop = CheckBox("落地投影")
        self.drop.setChecked(d.drop)
        self.drop.setToolTip("从对象向地面画一条虚线并在落点标出圆环，便于判断高度")
        self.drop.toggled.connect(self.changed)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widgets.section("音轨", self.tracks), 1)
        layout.addWidget(
            widgets.section(
                "画面与视角",
                widgets.two_column_grid([
                    ("尺寸：", widgets.size_row(self.width_spin, self.height_spin), "帧率：", self.fps),
                    ("视角：", self.view, None, None),
                    ("头模：", head_row, None, None),
                ]),
            )
        )  # fmt: skip
        layout.addWidget(
            widgets.section(
                "空间对象参数",
                widgets.two_column_grid([
                    ("对象大小：", self.object_scale, "头模大小：", self.head_scale),
                    ("静音门限：", self.gate, "淡入淡出：", self.fade),
                    ("分轨外不透明度：", self.dim, "电平响应：", self.level_response),
                    ("光晕：", self.glow, "轨迹长度：", self.trail),
                    ("着色：", self.color_mode, "取景缩放：", self.zoom),
                    ("辅助：", self.drop, None, None),
                ]),
            )
        )  # fmt: skip

    def _head(self) -> Path | None:
        text = self.head_edit.text().strip()
        return Path(text).expanduser() if text else None

    def _browse_head(self) -> None:
        current = self._head()
        start = str(current.parent) if current else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "选择头模", start, GLB_FILTER)
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
        checked = self.tracks.keys()
        items = []
        for track in project.tracks:
            if track.key not in TRACK_SPEAKERS:
                continue  # the LFE and the frequency bands have no place in the room
            text = (
                f"源音频（{track.path.name}）"
                if track.is_original
                else f"{track.label}（{track.key}）"
            )
            speakers = " ".join(track_speakers(track.key, project.atmos_layout))
            tip = f"{track.hint}\n非此音轨扬声器（{speakers}）分配范围内的对象将暗化呈现"
            items.append(widgets.TrackItem(track.key, text, tip))
        self.tracks.set_tracks(items, checked)

    def problem(self, ctx: PlanContext) -> str | None:
        too_big = frame_problem(self.width_spin.value(), self.height_spin.value())
        if too_big:
            return too_big
        info = ctx.project.info
        if info is not None and not info.atmos:
            return "当前输入非杜比全景声，无法生成声场视频"
        if info is not None and not self._selected(ctx.project):
            return "请至少勾选一条需要生成声场视频的音轨"
        head = self._head()
        if head and not head.is_file():
            return "指定的 3D 头模文件不存在"
        return renderer_problem("soundfield.mjs")

    def summary(self, ctx: PlanContext) -> str | None:
        count = len(self._selected(ctx.project))
        return f"{count} 个声场视频" if count else None

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
        self.settings.setValue("sf_view", self.view.currentData())
        self.settings.setValue("sf_head", self.head_edit.text().strip())
        self.settings.setValue("sf_color_mode", self.color_mode.currentData())
        self.settings.setValue("sf_drop", self.drop.isChecked())

    def restore_extra(self) -> None:
        self.tracks.seed(set(self.settings.value("sf_tracks", list(DEFAULT_TRACKS), type=list)))
        view = self.settings.value("sf_view", SoundfieldSpec().view)
        self.view.setCurrentIndex(max(0, self.view.findData(view)))
        self.head_edit.setText(self.settings.value("sf_head", "", type=str))
        mode = self.settings.value("sf_color_mode", SoundfieldSpec().color_mode)
        self.color_mode.setCurrentIndex(max(0, self.color_mode.findData(mode)))
        self.drop.setChecked(self.settings.value("sf_drop", SoundfieldSpec().drop, type=bool))
