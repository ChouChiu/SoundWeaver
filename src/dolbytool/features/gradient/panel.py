"""Gradient settings: cover swatches, the two colours, size and blend band."""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, QSize
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout
from qfluentwidgets import TransparentToggleToolButton

from dolbytool.core import motion
from dolbytool.core.ffmpeg import MAX_FRAME_SIDE, frame_problem
from dolbytool.core.i18n import t
from dolbytool.core.paths import FileStamp, file_stamp
from dolbytool.core.project import PlanContext, Project
from dolbytool.core.task import Step
from dolbytool.features.gradient import palette
from dolbytool.features.gradient.service import GradientSpec, gradient_step, gradient_target
from dolbytool.ui import fluent, widgets
from dolbytool.ui.background import Latest
from dolbytool.ui.feature import FeaturePanel

logger = logging.getLogger(__name__)

# The reference pair from the original command, until a cover supplies its own.
DEFAULT_TOP = "#343b3d"
DEFAULT_BOTTOM = "#040509"


class GradientPanel(FeaturePanel):
    title_key = "gradient.title"
    description_key = "gradient.description"
    icon = "color-gradient"
    enabled_key = "do_gradient"
    per_album = True

    def __init__(self, settings: QSettings, parent=None) -> None:
        super().__init__(settings, parent)
        self._cover: FileStamp | None = None
        self._swatches = Latest(self)

        self.swatch_row = QHBoxLayout()
        self.swatch_row.setSpacing(2)
        self.swatch_hint = widgets.hint_label(t("gradient.swatch_hint"), wrap=False)
        self.swatch_row.addWidget(self.swatch_hint)
        self.swatch_row.addStretch(1)
        self.top_color = widgets.ColorButton(DEFAULT_TOP, t("gradient.pick_top"))
        self.bottom_color = widgets.ColorButton(DEFAULT_BOTTOM, t("gradient.pick_bottom"))
        colours = widgets.form([
            (t("label.cover_colors"), self.swatch_row),
            (t("label.top_base_color"), self.top_color),
            (t("label.bottom_base_color"), self.bottom_color),
        ])  # fmt: skip

        self.width_spin = self.spin("grad_w", 16, MAX_FRAME_SIDE, 4000, " px")
        self.height_spin = self.spin("grad_h", 16, MAX_FRAME_SIDE, 4000, " px")
        self.start_spin = self.spin("grad_start", 0, 100, 30, " %")
        self.span_spin = self.spin("grad_span", 1, 100, 40, " %")
        self.start_spin.setToolTip(t("gradient.start_tip"))
        self.span_spin.setToolTip(t("gradient.span_tip"))
        frame = widgets.form([
            (t("label.size"), widgets.size_row(self.width_spin, self.height_spin)),
            (t("label.grad_start"), self.start_spin),
            (t("label.grad_span"), self.span_spin),
        ])  # fmt: skip

        self.preview = widgets.preview_label(t("gradient.preview_title"), side=150)
        for button in (self.top_color, self.bottom_color):
            button.colorChanged.connect(self._update_preview)
        for box in (self.width_spin, self.height_spin, self.start_spin, self.span_spin):
            box.valueChanged.connect(self._update_preview)

        settings_column = QVBoxLayout()
        settings_column.addWidget(widgets.section(t("section.colors"), colours))
        settings_column.addWidget(widgets.section(t("section.video"), frame))
        settings_column.addStretch(1)
        preview_column = QVBoxLayout()
        preview_column.addWidget(widgets.section(t("section.preview"), self.preview))
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
            self._swatches.cancel()
            self.swatch_hint.setText(t("gradient.swatch_hint"))
            return
        self.swatch_hint.setText(t("gradient.extracting"))
        self._swatches.run(lambda: palette.palette(motion.still(cover)), self._show_swatches)

    def _show_swatches(self, result: list[palette.Swatch] | Exception) -> None:
        if isinstance(result, Exception):
            # Pillow and ffmpeg raise many kinds of error for a broken picture
            self.swatch_hint.setText(t("gradient.cover_unreadable"))
            logger.warning("cannot take the colours of the cover: %s", result)
            return
        swatches = result
        self.swatch_hint.hide()
        main = palette.dominant(swatches)
        for index, sw in enumerate(swatches):
            button = TransparentToggleToolButton(widgets.swatch_icon(QColor(sw.hex), 22))
            button.setIconSize(QSize(22, 22))
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setChecked(sw is main)
            button.setToolTip(t("gradient.swatch_tip", hex=sw.hex, share=sw.share))
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
        # in device pixels, so the preview is as sharp as the screen
        ratio = self.preview.devicePixelRatioF()
        pw, ph = max(1, round(w * ratio)), max(1, round(h * ratio))
        image = QImage(pw, ph, QImage.Format.Format_RGB32)
        painter = QPainter(image)
        for y in range(ph):
            mix = spec.weight(y / ph)
            painter.fillRect(
                0, y, pw, 1,
                QColor(
                    round(top.red() + (bottom.red() - top.red()) * mix),
                    round(top.green() + (bottom.green() - top.green()) * mix),
                    round(top.blue() + (bottom.blue() - top.blue()) * mix),
                ),
            )  # fmt: skip
        painter.end()
        image.setDevicePixelRatio(ratio)
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
        return t("gradient.summary", width=spec.width, height=spec.height)

    def confirm(self, ctx: PlanContext) -> bool:
        cover = ctx.project.cover
        if cover and cover.is_file():
            return True
        return fluent.ask(
            self,
            t("status.cannot_start"),
            t("gradient.prompt_pick_cover"),
            accept=t("action.continue"),
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
