"""Live dBFS meter widgets, mini top-down XY radar, and M/S row controls."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, ToggleButton

from soundweaver.core.playback import SILENCE_DB
from soundweaver.ui import fluent

OBJECT_PALETTE = (
    "#ff4d4d", "#ff884d", "#ffc24d", "#e6ff4d", "#88ff4d",
    "#4dff79", "#4dffcc", "#4dd2ff", "#4d88ff", "#5c4dff",
    "#a64dff", "#f04dff", "#ff4da6", "#ff6b8b", "#ffd166",
)


def object_color(index: int) -> QColor:
    """Deterministic rainbow accent color matching the WebGL object spheres."""
    return QColor(OBJECT_PALETTE[index % len(OBJECT_PALETTE)])


class MiniXYView(QWidget):
    """24x24 top-down XY projection box matching ADMObjectXYView."""

    def __init__(self, color: QColor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self._x = 0.5
        self._y = 0.5
        self.setFixedSize(24, 24)

    def set_xy(self, x: float, y: float) -> None:
        self._x = max(0.0, min(1.0, x))
        self._y = max(0.0, min(1.0, y))
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setBrush(QColor(128, 128, 128, 22))
        p.setPen(QPen(QColor(128, 128, 128, 90), 1.0))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 3, 3)
        p.setPen(QPen(QColor(128, 128, 128, 60), 0.7))
        p.drawLine(w // 2, 2, w // 2, h - 2)
        p.drawLine(2, h // 2, w - 2, h // 2)
        px = 3.0 + self._x * (w - 6.0)
        py = 3.0 + self._y * (h - 6.0)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(QPointF(px, py), 2.5, 2.5)


class LevelBar(QWidget):
    """Fluent horizontal dBFS bar meter (-60..0 dBFS with red clip warning)."""

    def __init__(self, color: QColor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color
        self._db = SILENCE_DB
        self.setMinimumWidth(80)
        self.setFixedHeight(16)

    def set_db(self, db: float) -> None:
        if db != self._db:
            self._db = db
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        track_rect = QRectF(0, 2, w, h - 4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(fluent.surface_subtle())
        p.drawRoundedRect(track_rect, 3, 3)
        frac = max(0.0, min(1.0, (self._db + 60.0) / 60.0))
        if frac > 0.0:
            col = QColor(229, 72, 77) if self._db > -1.0 else self._color
            p.setBrush(col)
            p.drawRoundedRect(QRectF(0, 2, w * frac, h - 4), 3, 3)


class MeterRow(QWidget):
    """Single channel or object meter row with optional XY radar, dBFS bar, and M / S toggles."""

    mute_clicked = Signal(int)
    solo_clicked = Signal(int)

    def __init__(
        self,
        index: int,
        label: str,
        color: QColor | None = None,
        is_object: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.index = index
        self._color = color or QColor(77, 166, 255)
        self.setFixedHeight(30)

        row = QHBoxLayout(self)
        row.setContentsMargins(2, 1, 2, 1)
        row.setSpacing(6)
        self.xy_view: MiniXYView | None = None
        if is_object:
            self.xy_view = MiniXYView(self._color, self)
            row.addWidget(self.xy_view)

        lbl = CaptionLabel(label, self)
        lbl.setFixedWidth(42)
        row.addWidget(lbl)

        self.bar = LevelBar(self._color, self)
        row.addWidget(self.bar, 1)

        self.db_lbl = CaptionLabel("-∞", self)
        self.db_lbl.setFixedWidth(36)
        self.db_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.db_lbl)

        self.btn_m = ToggleButton("M", self)
        self.btn_m.setFixedSize(26, 22)
        self.btn_m.setStyleSheet("padding: 0px;")
        self.btn_m.clicked.connect(lambda: self.mute_clicked.emit(self.index))
        row.addWidget(self.btn_m)

        self.btn_s = ToggleButton("S", self)
        self.btn_s.setFixedSize(26, 22)
        self.btn_s.setStyleSheet("padding: 0px;")
        self.btn_s.clicked.connect(lambda: self.solo_clicked.emit(self.index))
        row.addWidget(self.btn_s)

    def set_state(
        self,
        db: float,
        muted: bool,
        soloed: bool,
        xy: tuple[float, float] | None = None,
    ) -> None:
        self.db_lbl.setText("-∞" if db <= -80.0 else f"{db:.0f}")
        self.btn_m.blockSignals(True)
        self.btn_m.setChecked(muted)
        self.btn_m.blockSignals(False)
        self.btn_s.blockSignals(True)
        self.btn_s.setChecked(soloed)
        self.btn_s.blockSignals(False)
        if self.xy_view is not None and xy is not None:
            self.xy_view.set_xy(*xy)
        self.bar.set_db(db)
