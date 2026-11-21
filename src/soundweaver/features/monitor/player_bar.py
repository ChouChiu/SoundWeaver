"""Persistent bottom playback bar shared across the whole application."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    SimpleCardWidget,
    Slider,
    ToolButton,
    TransparentToolButton,
)

from soundweaver.core.audio import AudioInfo
from soundweaver.core.i18n import t
from soundweaver.core.playback import PlaybackEngine
from soundweaver.core.sources import sidecar_cover
from soundweaver.ui import fluent, widgets


class _PlaybackSlider(Slider):
    def __init__(self, name: str, parent: QWidget) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        widgets.name_control(self, name)
        self.setMinimumHeight(28)

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.hasFocus():
            with QPainter(self) as painter:
                painter.setPen(QPen(self.palette().text().color(), 2))
                painter.drawRoundedRect(self.rect().adjusted(2, 2, -2, -2), 4, 4)


class _HoverSeekSlider(_PlaybackSlider):
    """Seek slider that shows the hovered timestamp in a tooltip without seeking on hover."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(t("player.seek"), parent)
        self.setMouseTracking(True)
        self._duration = 0.0

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._duration > 0 and self.width() > 0:
            ratio = max(0.0, min(1.0, event.position().x() / self.width()))
            self.setToolTip(widgets.format_time(ratio * self._duration))
        super().mouseMoveEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        value = self.value()
        super().keyPressEvent(event)
        if self.value() != value:
            self.clicked.emit(self.value())


class PlayerBar(QWidget):
    """Compact bottom bar for transport, seeking, volume, and opening the 3D monitor."""

    expand_requested = Signal()
    prev_requested = Signal()
    next_requested = Signal()

    def __init__(self, engine: PlaybackEngine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._seeking = False
        self._snapping_vol = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        card = SimpleCardWidget(self)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.cover_lbl = ToolButton(fluent.icon("audio-x-generic"), card)
        self.cover_lbl.setFixedSize(44, 44)
        self.cover_lbl.setIconSize(QSize(20, 20))
        widgets.name_control(self.cover_lbl, t("player.open_monitor"))
        self.cover_lbl.clicked.connect(self.expand_requested.emit)

        meta_box = QWidget(card)
        meta_box.setMinimumWidth(96)
        meta_box.setMaximumWidth(240)
        self.title_lbl = BodyLabel(t("player.idle"), meta_box)
        self.sub_lbl = CaptionLabel("", meta_box)
        for label in (self.title_lbl, self.sub_lbl):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        fluent.secondary(self.sub_lbl)
        meta = QVBoxLayout(meta_box)
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(1)
        meta.addWidget(self.title_lbl)
        meta.addWidget(self.sub_lbl)

        self.btn_prev = TransparentToolButton(fluent.icon("media-skip-backward"), card)
        widgets.name_control(self.btn_prev, t("player.prev"))
        self.btn_prev.clicked.connect(self.prev_requested.emit)

        self.btn_play = ToolButton(fluent.icon("media-playback-start"), card)
        widgets.name_control(self.btn_play, t("player.play_pause"))
        self.btn_play.clicked.connect(self.engine.toggle_play)

        self.btn_next = TransparentToolButton(fluent.icon("media-skip-forward"), card)
        widgets.name_control(self.btn_next, t("player.next"))
        self.btn_next.clicked.connect(self.next_requested.emit)

        self.pos_lbl = CaptionLabel("0:00", card)
        self.pos_lbl.setFixedWidth(38)
        self.pos_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.dur_lbl = CaptionLabel("0:00", card)
        self.dur_lbl.setFixedWidth(38)
        self.dur_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.seek_slider = _HoverSeekSlider(card)
        self.seek_slider.setMinimumWidth(112)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderPressed.connect(self._on_seek_start)
        self.seek_slider.sliderReleased.connect(self._on_seek_end)
        self.seek_slider.clicked.connect(self._on_seek_click)

        self.vol_slider = _PlaybackSlider(t("player.volume"), card)
        self.vol_slider.setRange(0, 200)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(100)
        self.vol_lbl = CaptionLabel("100%", card)
        self.vol_lbl.setFixedWidth(38)
        self.vol_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.vol_slider.valueChanged.connect(self._on_volume)

        self.btn_expand = TransparentToolButton(fluent.icon("audio-speakers-symbolic"), card)
        widgets.name_control(self.btn_expand, t("player.open_monitor"))
        self.btn_expand.clicked.connect(self.expand_requested.emit)

        self._info = self._group(card, self.cover_lbl, meta_box, stretch=meta_box)
        self._transport = self._group(card, self.btn_prev, self.btn_play, self.btn_next)
        self._timeline = self._group(
            card,
            self.pos_lbl,
            self.seek_slider,
            self.dur_lbl,
            stretch=self.seek_slider,
        )
        self._volume = self._group(card, self.vol_slider, self.vol_lbl, self.btn_expand)
        self._grid = QGridLayout(card)
        self._grid.setContentsMargins(12, 8, 12, 8)
        self._grid.setSpacing(widgets.INLINE_SPACING)
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._wide = False
        self._place_groups()

        self._track_subtitle = ""
        self.engine.buffering_changed.connect(self._on_buffering)
        self.engine.track_loaded.connect(self._on_track_loaded)
        self.engine.track_cleared.connect(self._on_track_cleared)
        self.engine.state_changed.connect(self._on_state)
        self.engine.position_changed.connect(self._on_position)

    @staticmethod
    def _group(parent: QWidget, *controls: QWidget, stretch: QWidget | None = None) -> QWidget:
        group = QWidget(parent)
        layout = QHBoxLayout(group)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(widgets.INLINE_SPACING)
        for control in controls:
            layout.addWidget(control, int(control is stretch))
        return group

    def _place_groups(self) -> None:
        grid = self._grid
        while grid.count():
            grid.takeAt(0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 2 if self._wide else 0)
        if self._wide:
            for column, group in enumerate(
                (self._info, self._transport, self._timeline, self._volume)
            ):
                grid.addWidget(group, 0, column)
        else:
            grid.addWidget(self._info, 0, 0, 1, 3)
            grid.addWidget(self._transport, 0, 3, Qt.AlignmentFlag.AlignRight)
            grid.addWidget(self._timeline, 1, 0, 1, 3)
            grid.addWidget(self._volume, 1, 3)
        self.updateGeometry()

    def minimumSizeHint(self) -> QSize:
        widths = [
            first.minimumSizeHint().width() + second.minimumSizeHint().width()
            for first, second in ((self._info, self._transport), (self._timeline, self._volume))
        ]
        margins = self._grid.contentsMargins()
        hint = super().minimumSizeHint()
        hint.setWidth(max(widths) + margins.left() + margins.right() + self._grid.spacing())
        return hint

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        margins = self._grid.contentsMargins()
        groups = (self._info, self._transport, self._timeline, self._volume)
        inline_width = sum(group.minimumSizeHint().width() for group in groups)
        inline_width += margins.left() + margins.right() + self._grid.spacing() * 3
        wide = self.width() >= inline_width
        if wide != self._wide:
            self._wide = wide
            self._place_groups()

    def _set_subtitle(self, text: str) -> None:
        self.sub_lbl.setText(text)
        self.sub_lbl.setToolTip(text)

    def _on_track_loaded(self, path: Path, info: AudioInfo) -> None:
        title = info.title or path.stem
        self.title_lbl.setText(title)
        self.title_lbl.setToolTip(title)
        self._track_subtitle = info.artist or info.codec
        self._set_subtitle(self._track_subtitle)
        self.seek_slider.set_duration(info.duration)
        self.seek_slider.setEnabled(info.duration > 0)
        self.seek_slider.setValue(0)
        self.dur_lbl.setText(widgets.format_time(info.duration))
        self.cover_lbl.setIcon(fluent.icon("audio-x-generic"))
        self.cover_lbl.setIconSize(QSize(20, 20))
        cover = sidecar_cover(path)
        if cover is not None:
            pix = QPixmap(str(cover))
            if not pix.isNull():
                badge = pix.scaled(
                    36,
                    36,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                left, top = (badge.width() - 36) // 2, (badge.height() - 36) // 2
                self.cover_lbl.setIcon(QIcon(badge.copy(left, top, 36, 36)))
                self.cover_lbl.setIconSize(QSize(36, 36))

    def _on_track_cleared(self) -> None:
        self._track_subtitle = ""
        self.title_lbl.setText(t("player.idle"))
        self.title_lbl.setToolTip("")
        self._set_subtitle("")
        self.pos_lbl.setText("0:00")
        self.dur_lbl.setText("0:00")
        self.seek_slider.set_duration(0)
        self.seek_slider.setValue(0)
        self.seek_slider.setEnabled(False)
        self.cover_lbl.setIcon(fluent.icon("audio-x-generic"))
        self.cover_lbl.setIconSize(QSize(20, 20))

    def _on_buffering(self, buffering: bool) -> None:
        self._set_subtitle(t("player.buffering") if buffering else self._track_subtitle)

    def _on_state(self, playing: bool) -> None:
        self.btn_play.setIcon(
            fluent.icon("media-playback-pause" if playing else "media-playback-start")
        )

    def _on_position(self, seconds: float) -> None:
        if self._seeking:
            return
        self.pos_lbl.setText(widgets.format_time(seconds))
        dur = self.engine.duration
        if dur > 0:
            self.seek_slider.set_duration(dur)
            duration_label = widgets.format_time(dur)
            if duration_label != self.dur_lbl.text():
                self.dur_lbl.setText(duration_label)
            val = max(0, min(1000, int(1000 * seconds / dur)))
            if val != self.seek_slider.value():
                self.seek_slider.setValue(val)

    def _on_seek_start(self) -> None:
        self._seeking = True

    def _on_seek_end(self) -> None:
        self._seeking = False
        self._seek_to_slider(self.seek_slider.value())

    def _on_seek_click(self, value: int) -> None:
        if not self._seeking:
            self._seek_to_slider(value)

    def _seek_to_slider(self, value: int) -> None:
        dur = self.engine.duration
        if dur > 0:
            self.engine.seek(dur * value / 1000.0)

    def _on_volume(self, val: int) -> None:
        if self._snapping_vol:
            return
        # Snap near 100% without blocking Slider's internal _adjustHandlePos signal
        if 96 <= val <= 104 and val != 100:
            val = 100
            self._snapping_vol = True
            self.vol_slider.setValue(100)
            self._snapping_vol = False
        self.vol_lbl.setText(f"{val}%")
        self.engine.set_volume(val / 100.0)
