"""Sound field video settings: which tracks, frame, camera preset, head model, sizes and gate."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QComboBox, QFileDialog, QVBoxLayout

from dolbytool.core.project import PlanContext, Project, Track
from dolbytool.core.renderer import renderer_problem
from dolbytool.core.task import Step
from dolbytool.features.soundfield.service import (
    TRACK_SPEAKERS,
    VIEWS,
    SoundfieldSpec,
    SoundfieldVideo,
    soundfield_step,
    soundfield_target,
)
from dolbytool.ui import widgets
from dolbytool.ui.feature import FeaturePanel

GLB_FILTER = "glTF 二进制模型 (*.glb);;所有文件 (*)"
DEFAULT_TRACKS = ("original",)


class SoundfieldPanel(FeaturePanel):
    title = "声场视频"
    icon = "audio-speakers-symbolic"
    description = (
        "将全景声动态对象在 3D 空间内的运动轨迹与电平可视化，输出带 Alpha 透明通道的 WebM (VP9) 视频"
    )
    enabled_key = "do_soundfield"
    enabled_default = False

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        self.tracks = widgets.TrackList()
        self.tracks.changed.connect(self.changed)

        d = SoundfieldSpec()
        self.width_spin = self.spin("sf_w", 64, 7680, d.width, " px", step=2)
        self.height_spin = self.spin("sf_h", 64, 4320, d.height, " px", step=2)
        self.fps = self.spin("sf_fps", 10, 120, d.fps, " fps")
        self.view = QComboBox()
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
                    ("分轨外不透明度：", self.dim, None, None),
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
        keys = self.tracks.checked()
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
        )

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        if self.tracks.is_empty():
            checked = set(self.settings.value("sf_tracks", list(DEFAULT_TRACKS), type=list))
        else:
            checked = self.tracks.checked()
        items = []
        for track in project.tracks:
            if track.key not in TRACK_SPEAKERS:
                continue  # the LFE and the frequency bands have no place in the room
            text = f"源音频（{track.path.name}）" if track.is_original else f"{track.label}（{track.key}）"
            speakers = " ".join(TRACK_SPEAKERS[track.key])
            tip = f"{track.hint}\n非此音轨扬声器（{speakers}）分配范围内的对象将暗化呈现"
            items.append(widgets.TrackItem(track.key, text, tip))
        self.tracks.set_tracks(items, checked)

    def problem(self, ctx: PlanContext) -> str | None:
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
        return [soundfield_step(audio, videos, self.spec(), info.duration)]

    def save_extra(self) -> None:
        if not self.tracks.is_empty():
            self.settings.setValue("sf_tracks", sorted(self.tracks.checked()))
        self.settings.setValue("sf_view", self.view.currentData())
        self.settings.setValue("sf_head", self.head_edit.text().strip())

    def restore_extra(self) -> None:
        view = self.settings.value("sf_view", SoundfieldSpec().view)
        self.view.setCurrentIndex(max(0, self.view.findData(view)))
        self.head_edit.setText(self.settings.value("sf_head", "", type=str))
