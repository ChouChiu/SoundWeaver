"""Gradient settings: cover swatches, the two colours, size and blend band."""

from __future__ import annotations

from PySide6.QtCore import QSettings, QSize
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import TransparentToggleToolButton

from dolbytool.core import motion
from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.paths import FileStamp, file_stamp
from dolbytool.core.project import PlanContext, Project
from dolbytool.core.task import Step
from dolbytool.features.gradient import palette
from dolbytool.features.gradient.service import GradientSpec, gradient_step, gradient_target
from dolbytool.ui import fluent, widgets
from dolbytool.ui.feature import FeaturePanel

# The reference pair from the original command, until a cover supplies its own.
DEFAULT_TOP = "#343b3d"
DEFAULT_BOTTOM = "#040509"


class GradientPanel(FeaturePanel):
    title = "渐变图"
    icon = "color-gradient"
    description = "提取封面主色调并向下平滑压暗过渡，生成背景渐变图 (PNG)"
    enabled_key = "do_gradient"
    per_album = True

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        self._cover: FileStamp | None = None

        self.swatch_row = QHBoxLayout()
        self.swatch_row.setSpacing(2)
        self.swatch_hint = widgets.hint_label("载入封面后自动提取主色调", wrap=False)
        self.swatch_row.addWidget(self.swatch_hint)
        self.swatch_row.addStretch(1)
        self.top_color = widgets.ColorButton(DEFAULT_TOP, "选择上部基色")
        self.bottom_color = widgets.ColorButton(DEFAULT_BOTTOM, "选择下部基色")
        colours = widgets.form([
            ("封面色彩：", self.swatch_row),
            ("上部基色：", self.top_color),
            ("下部基色：", self.bottom_color),
        ])  # fmt: skip

        self.width_spin = self.spin("grad_w", 16, MAX_FRAME_SIDE, 4000, " px")
        self.height_spin = self.spin("grad_h", 16, MAX_FRAME_SIDE, 4000, " px")
        self.start_spin = self.spin("grad_start", 0, 100, 30, " %")
        self.span_spin = self.spin("grad_span", 1, 100, 40, " %")
        self.start_spin.setToolTip("渐变过渡起始纵向位置（占画面高度比例）")
        self.span_spin.setToolTip("渐变过渡区间的跨度（占画面高度比例）")
        frame = widgets.form([
            ("尺寸：", widgets.size_row(self.width_spin, self.height_spin)),
            ("过渡起点：", self.start_spin),
            ("过渡跨度：", self.span_spin),
        ])  # fmt: skip

        self.preview = widgets.preview_label("渐变预览", side=150)
        for button in (self.top_color, self.bottom_color):
            button.colorChanged.connect(self._update_preview)
        for box in (self.width_spin, self.height_spin, self.start_spin, self.span_spin):
            box.valueChanged.connect(self._update_preview)

        settings_column = QVBoxLayout()
        settings_column.addWidget(widgets.section("颜色", colours))
        settings_column.addWidget(widgets.section("画面", frame))
        settings_column.addStretch(1)
        preview_column = QVBoxLayout()
        preview_column.addWidget(widgets.section("预览", self.preview))
        preview_column.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addLayout(settings_column, 1)
        layout.addLayout(preview_column)

    def spec(self) -> GradientSpec:
        return GradientSpec(
            self.top_color.hex(),
            self.bottom_color.hex(),
            self.width_spin.value(),
            self.height_spin.value(),
            self.start_spin.value() / 100,
            self.span_spin.value() / 100,
        )

    # ---- cover colours ---------------------------------------------------------------

    def _load_cover(self, project: Project) -> None:
        cover = project.cover
        self._clear_swatches()
        self.swatch_hint.show()
        if not cover or not cover.is_file():
            self.swatch_hint.setText("选择封面后显示封面主要颜色")
            return
        try:
            swatches = palette.palette(motion.still(cover))
        except Exception as error:  # noqa: BLE001 - Pillow raises many types for a broken image
            self.swatch_hint.setText("封面无法读取")
            self.log.emit(f"封面取色失败：{error}")
            return
        self.swatch_hint.hide()
        main = palette.dominant(swatches)
        for index, sw in enumerate(swatches):
            button = TransparentToggleToolButton(widgets.swatch_icon(QColor(sw.hex), 22))
            button.setIconSize(QSize(22, 22))
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setChecked(sw is main)
            button.setToolTip(f"{sw.hex}（色彩占比约 {sw.share:.0%}）\n点击应用为渐变主色")
            button.clicked.connect(lambda _=False, rgb=sw.rgb: self._apply_main(rgb))
            self.swatch_row.insertWidget(index, button)
        self._apply_main(main.rgb)

    def _clear_swatches(self) -> None:
        for index in reversed(range(self.swatch_row.count())):
            widget = self.swatch_row.itemAt(index).widget()
            if isinstance(widget, TransparentToggleToolButton):
                self.swatch_row.takeAt(index)
                widget.deleteLater()

    def _apply_main(self, rgb: tuple[int, int, int]) -> None:
        top, bottom = palette.gradient_pair(rgb)
        self.top_color.setColor(top)
        self.bottom_color.setColor(bottom)

    def _update_preview(self) -> None:
        spec = self.spec()
        side = self.preview.width() - 4
        aspect = spec.width / spec.height
        if aspect >= 1:
            w, h = side, max(1, round(side / aspect))
        else:
            w, h = max(1, round(side * aspect)), side
        top, bottom = QColor(spec.top), QColor(spec.bottom)
        image = QImage(w, h, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        for y in range(h):
            t = spec.weight(y / h)
            painter.fillRect(
                0, y, w, 1,
                QColor(
                    round(top.red() + (bottom.red() - top.red()) * t),
                    round(top.green() + (bottom.green() - top.green()) * t),
                    round(top.blue() + (bottom.blue() - top.blue()) * t),
                ),
            )  # fmt: skip
        painter.end()
        self.preview.setPixmap(QPixmap.fromImage(image))

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        stamp = file_stamp(project.cover)
        if stamp != self._cover:
            self._cover = stamp
            self._load_cover(project)

    def problem(self, ctx: PlanContext) -> str | None:
        return frame_problem(self.width_spin.value(), self.height_spin.value())

    def summary(self, ctx: PlanContext) -> str | None:
        spec = self.spec()
        return f"渐变图 {spec.width}×{spec.height}"

    def confirm(self, ctx: PlanContext) -> bool:
        cover = ctx.project.cover
        if cover and cover.is_file():
            return True
        return fluent.ask(
            self, "未载入封面", "当前未选择封面图片，渐变图将直接使用预设基色生成，是否继续？"
        )

    def plan(self, ctx: PlanContext) -> list[Step]:
        project = ctx.project
        if project.audio is None or project.output_dir is None:
            return []
        target = gradient_target(project.output_dir, project.audio)
        # a batch has no swatch to pick from: each track takes its own cover's colours
        cover = project.cover if ctx.batch and project.cover and project.cover.is_file() else None
        return [gradient_step(self.spec(), target, cover)]

    def restore_extra(self) -> None:
        self._update_preview()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._update_preview()
