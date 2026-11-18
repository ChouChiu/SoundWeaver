"""Persistent bottom playback bar shared across the whole application."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMouseEvent, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget
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
from soundweaver.ui import fluent


def _fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


class _HoverSeekSlider(Slider):
    """Seek slider that shows the hovered timestamp in a tooltip without seeking on hover."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setMouseTracking(True)
        self._duration = 0.0

    def set_duration(self, seconds: float) -> None:
        self._duration = max(0.0, seconds)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._duration > 0 and self.width() > 0:
            ratio = max(0.0, min(1.0, event.position().x() / self.width()))
            self.setToolTip(_fmt_time(ratio * self._duration))
        super().mouseMoveEvent(event)


class _CoverBadge(QLabel):
    """Clickable album artwork thumbnail."""

    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(44, 44)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("border-radius: 6px; background: rgba(128,128,128,0.15);")

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


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
        self.setFixedHeight(64)

        card = SimpleCardWidget(self)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        self.cover_lbl = _CoverBadge(card)
        self.cover_lbl.clicked.connect(self.expand_requested.emit)

        meta_box = QWidget(card)
        meta_box.setMinimumWidth(120)
        meta_box.setMaximumWidth(240)
        self.title_lbl = BodyLabel(t("player.idle"), meta_box)
        self.sub_lbl = CaptionLabel("", meta_box)
        meta = QVBoxLayout(meta_box)
        meta.setContentsMargins(0, 0, 0, 0)
        meta.setSpacing(1)
        meta.addWidget(self.title_lbl)
        meta.addWidget(self.sub_lbl)

        self.btn_prev = TransparentToolButton(fluent.icon("media-skip-backward"), card)
        self.btn_prev.setToolTip(t("player.prev"))
        self.btn_prev.clicked.connect(self.prev_requested.emit)

        self.btn_play = ToolButton(fluent.icon("media-playback-start"), card)
        self.btn_play.setToolTip(t("player.play_pause"))
        self.btn_play.clicked.connect(self.engine.toggle_play)

        self.btn_next = TransparentToolButton(fluent.icon("media-skip-forward"), card)
        self.btn_next.setToolTip(t("player.next"))
        self.btn_next.clicked.connect(self.next_requested.emit)

        self.pos_lbl = CaptionLabel("0:00", card)
        self.pos_lbl.setFixedWidth(38)
        self.pos_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.dur_lbl = CaptionLabel("0:00", card)
        self.dur_lbl.setFixedWidth(38)
        self.dur_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.seek_slider = _HoverSeekSlider(card)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.setEnabled(False)
        self.seek_slider.sliderPressed.connect(self._on_seek_start)
        self.seek_slider.sliderReleased.connect(self._on_seek_end)
        self.seek_slider.clicked.connect(self._on_seek_click)

        self.vol_slider = Slider(Qt.Orientation.Horizontal, card)
        self.vol_slider.setRange(0, 200)
        self.vol_slider.setValue(100)
        self.vol_slider.setFixedWidth(100)
        self.vol_lbl = CaptionLabel("100%", card)
        self.vol_lbl.setFixedWidth(38)
        self.vol_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.vol_slider.valueChanged.connect(self._on_volume)

        self.btn_expand = TransparentToolButton(fluent.icon("audio-speakers-symbolic"), card)
        self.btn_expand.setToolTip(t("player.open_monitor"))
        self.btn_expand.clicked.connect(self.expand_requested.emit)

        row = QHBoxLayout(card)
        row.setContentsMargins(14, 6, 14, 6)
        row.setSpacing(10)
        row.addWidget(self.cover_lbl)
        row.addWidget(meta_box, 1)
        row.addWidget(self.btn_prev)
        row.addWidget(self.btn_play)
        row.addWidget(self.btn_next)
        row.addWidget(self.pos_lbl)
        row.addWidget(self.seek_slider, 2)
        row.addWidget(self.dur_lbl)
        row.addWidget(self.vol_slider)
        row.addWidget(self.vol_lbl)
        row.addWidget(self.btn_expand)

        self.engine.track_loaded.connect(self._on_track_loaded)
        self.engine.state_changed.connect(self._on_state)
        self.engine.position_changed.connect(self._on_position)

    def _on_track_loaded(self, path: Path, info: AudioInfo) -> None:
        self.title_lbl.setText(info.title or path.stem)
        self.sub_lbl.setText(info.artist or info.codec)
        self.seek_slider.set_duration(info.duration)
        self.seek_slider.setEnabled(info.duration > 0)
        self.seek_slider.setValue(0)
        self.dur_lbl.setText(_fmt_time(info.duration))
        self.cover_lbl.setPixmap(QPixmap())
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            cand = path.with_suffix(ext)
            if cand.is_file():
                pix = QPixmap(str(cand)).scaled(
                    44, 44, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
                )
                self.cover_lbl.setPixmap(pix)
                break

    def _on_state(self, playing: bool) -> None:
        self.btn_play.setIcon(fluent.icon("media-playback-pause" if playing else "media-playback-start"))

    def _on_position(self, seconds: float) -> None:
        if self._seeking:
            return
        self.pos_lbl.setText(_fmt_time(seconds))
        dur = self.engine.duration
        if dur > 0:
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
