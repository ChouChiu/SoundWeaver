"""QPainter software fallback 3D monitor canvas (supports Atmos room + 360RA dome modes)."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from soundweaver.core.codecs import mpegh
from soundweaver.core.playback import SILENCE_DB, ObjectSnapshot, PlaybackEngine
from soundweaver.features.monitor.meters import object_color


class Room3DCanvas(QWidget):
    """Interactive QPainter 3D room/spherical monitor used when WebEngine monitor HTML is absent."""

    def __init__(self, engine: PlaybackEngine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.setMinimumSize(360, 300)
        self._yaw = math.radians(38)
        self._pitch = math.radians(26)
        self._drag_pos: QPointF | None = None
        self._show_labels = True
        self._show_speakers = True
        self._show_drop = False
        self._show_xy = True
        self._style = "person"
        self._mode = "room"
        self._layers = {"upper": True, "middle": True, "lower": True}
        self._objs: list[ObjectSnapshot] = []
        self._ch_dbs: list[float] = []

    def set_view(self, preset: str) -> None:
        presets = {
            "iso": (math.radians(38), math.radians(26)),
            "top": (0.0, math.radians(85)),
            "front": (0.0, math.radians(8)),
            "back": (math.pi, math.radians(8)),
            "left": (math.radians(-90), math.radians(8)),
            "right": (math.radians(90), math.radians(8)),
        }
        self._yaw, self._pitch = presets.get(preset, presets["iso"])
        self.update()

    def set_show_labels(self, on: bool) -> None:
        self._show_labels = on
        self.update()

    def set_show_speakers(self, on: bool) -> None:
        self._show_speakers = on
        self.update()

    def set_show_drop(self, on: bool) -> None:
        self._show_drop = on
        self.update()

    def set_show_xy(self, on: bool) -> None:
        self._show_xy = on
        self.update()

    def set_style(self, style: str) -> None:
        self._style = style
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def set_layers(self, upper: bool, middle: bool, lower: bool) -> None:
        self._layers = {"upper": upper, "middle": middle, "lower": lower}
        self.update()

    def sync_speakers(self) -> None:
        self.update()

    def update_telemetry(self, ch_dbs: list[float], objs: list[ObjectSnapshot]) -> None:
        self._ch_dbs = ch_dbs
        self._objs = objs
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.position()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None:
            delta = event.position() - self._drag_pos
            self._drag_pos = event.position()
            self._yaw += delta.x() * 0.008
            self._pitch = max(math.radians(-85), min(math.radians(85), self._pitch + delta.y() * 0.008))
            self.update()

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._drag_pos = None

    def _project(self, x: float, y: float, z: float, cx: float, cy: float, scale: float) -> tuple[float, float, float]:
        rx = (x - 0.5) * 2.0
        ry = (z - 0.5) * 1.15
        rz = (y - 0.5) * 2.0

        cy_rot, sy_rot = math.cos(self._yaw), math.sin(self._yaw)
        x1 = rx * cy_rot - rz * sy_rot
        z1 = rx * sy_rot + rz * cy_rot

        cp, sp = math.cos(self._pitch), math.sin(self._pitch)
        y2 = ry * cp - z1 * sp
        z2 = ry * sp + z1 * cp

        dist = 4.6
        persp = dist / max(0.8, dist + z2)
        return cx + x1 * scale * persp, cy - y2 * scale * persp, z2

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        bg = QRadialGradient(w * 0.5, h * 0.45, max(w, h) * 0.75)
        bg.setColorAt(0.0, QColor(24, 34, 48))
        bg.setColorAt(1.0, QColor(12, 17, 25))
        p.fillRect(0, 0, w, h, bg)

        cx, cy = w * 0.5, h * 0.52
        scale = min(w, h) * 0.29

        corners = [
            (0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
            (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1),
        ]
        proj = [self._project(x, y, z, cx, cy, scale) for x, y, z in corners]
        faces = (
            ((0, 1, 2, 3), QColor(34, 54, 76, 65)),
            ((4, 5, 6, 7), QColor(38, 60, 86, 28)),
            ((0, 1, 5, 4), QColor(42, 66, 94, 42)),
            ((2, 3, 7, 6), QColor(36, 56, 80, 42)),
            ((0, 3, 7, 4), QColor(40, 62, 88, 36)),
            ((1, 2, 6, 5), QColor(40, 62, 88, 36)),
        )
        p.setPen(Qt.PenStyle.NoPen)
        for idxs, fcol in sorted(faces, key=lambda f: -sum(proj[i][2] for i in f[0]) / 4.0):
            p.setBrush(fcol)
            p.drawPolygon(QPolygonF([QPointF(proj[i][0], proj[i][1]) for i in idxs]))

        p.setPen(QPen(QColor(126, 180, 216, 210), 1.4))
        for a, b in (
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ):
            p.drawLine(QPointF(proj[a][0], proj[a][1]), QPointF(proj[b][0], proj[b][1]))

        # Listener head
        if self._style != "room" or self._mode == "spherical":
            sx, sy, _ = self._project(0.5, 0.5, 0.05, cx, cy, scale)
            hx, hy, _ = self._project(0.5, 0.5, 0.19, cx, cy, scale)
            nx, ny, _ = self._project(0.5, 0.44, 0.18, cx, cy, scale)
            p.setBrush(QColor(92, 122, 153, 210))
            p.drawEllipse(QPointF(sx, sy), 14, 7)
            p.drawEllipse(QPointF(hx, hy), 9, 9)
            p.setBrush(QColor(126, 172, 220, 230))
            p.drawEllipse(QPointF(nx, ny), 3.2, 3.2)

        # Speakers
        spk_items: list[tuple[float, str, float, float, float]] = []
        if self._mode == "spherical":
            lay = mpegh.cicp_layout(self.engine.cicp)
            for idx, sp in enumerate(lay.speakers):
                if sp.is_lfe or not self._layers.get(sp.layer, True):
                    continue
                kx, ky, kz = sp.cartesian()
                px, py, depth = self._project((kx + 1.0) * 0.5, (1.0 - ky) * 0.5, max(0.0, kz), cx, cy, scale)
                db = self._ch_dbs[idx] if idx < len(self._ch_dbs) else SILENCE_DB
                spk_items.append((depth, sp.name, px, py, db))
        elif self._show_speakers:
            layout = self.engine.layout
            for name, (lx, ly, lz) in layout.positions.items():
                px, py, depth = self._project(lx, ly, max(0.0, lz), cx, cy, scale)
                idx = layout.index.get(name, -1)
                db = self._ch_dbs[idx] if 0 <= idx < len(self._ch_dbs) else SILENCE_DB
                spk_items.append((depth, name, px, py, db))

        for _, name, px, py, db in sorted(spk_items, key=lambda it: -it[0]):
            norm = max(0.0, min(1.0, (db + 60.0) / 55.0))
            if norm > 0.05:
                sglow = QRadialGradient(px, py, 16)
                sglow.setColorAt(0.0, QColor(110, 195, 255, int(140 * norm)))
                sglow.setColorAt(1.0, QColor(110, 195, 255, 0))
                p.setBrush(sglow)
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(px, py), 16, 16)
            p.setBrush(QColor(95 + int(90 * norm), 155 + int(80 * norm), 220 + int(35 * norm), 170 + int(85 * norm)))
            p.setPen(QPen(QColor(180, 220, 255, 190), 1.0))
            p.drawRoundedRect(QRectF(px - 5.5, py - 5.5, 11, 11), 2.5, 2.5)
            p.setPen(QColor(195, 220, 245, 175))
            p.setFont(QFont("sans-serif", 7))
            p.drawText(QPointF(px + 8, py + 3), name)

        # Objects
        draw_objs = []
        for o in self._objs:
            if not o.active:
                continue
            px, py, depth = self._project(o.x, o.y, o.z, cx, cy, scale)
            fx, fy, _ = self._project(o.x, o.y, 0.0, cx, cy, scale)
            draw_objs.append((depth, o, px, py, fx, fy))
        for _, o, px, py, fx, fy in sorted(draw_objs, key=lambda item: -item[0]):
            norm = max(0.0, min(1.0, (o.level_db + 60.0) / 55.0))
            col = object_color(o.index)
            radius = 7.5 + 6.5 * norm
            if self._show_drop:
                p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 110), 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(px, py), QPointF(fx, fy))
                p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 140), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(fx, fy), 6.0, 3.2)
            sphere_grad = QRadialGradient(px - radius * 0.32, py - radius * 0.32, radius * 1.35)
            sphere_grad.setColorAt(0.0, col.lighter(165))
            sphere_grad.setColorAt(0.45, col)
            sphere_grad.setColorAt(1.0, col.darker(190))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(sphere_grad)
            p.drawEllipse(QPointF(px, py), radius, radius)
            if self._show_labels:
                label = str(o.index + 1)
                p.setFont(QFont("sans-serif", 8, QFont.Weight.Bold))
                path = QPainterPath()
                path.addText(px - (4 if len(label) == 1 else 7), py - radius - 4, p.font(), label)
                p.strokePath(path, QPen(QColor(0, 0, 0, 210), 2.8))
                p.fillPath(path, QColor(255, 255, 255))

        if self._show_xy and w > 180 and h > 180:
            rs = 88
            rx0, ry0 = w - rs - 14, h - rs - 14
            p.setBrush(QColor(12, 18, 28, 200))
            p.setPen(QPen(QColor(100, 155, 210, 130), 1.0))
            p.drawRoundedRect(rx0, ry0, rs, rs, 8, 8)
            p.setPen(QPen(QColor(100, 155, 210, 60), 1.0))
            p.drawLine(rx0 + rs // 2, ry0 + 6, rx0 + rs // 2, ry0 + rs - 6)
            p.drawLine(rx0 + 6, ry0 + rs // 2, rx0 + rs - 6, ry0 + rs // 2)
            p.setPen(Qt.PenStyle.NoPen)
            for o in self._objs:
                if o.active:
                    p.setBrush(object_color(o.index))
                    p.drawEllipse(QPointF(rx0 + 8 + o.x * (rs - 16), ry0 + 8 + o.y * (rs - 16)), 3.5, 3.5)
