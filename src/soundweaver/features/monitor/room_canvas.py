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

from soundweaver.core.playback_mix import SILENCE_DB, ObjectSnapshot
from soundweaver.features.monitor.meters import (
    SELECTED_OBJECT_COLOR,
    object_color,
)
from soundweaver.features.monitor.speakers import MonitorSpeaker

LOGIC_OBJECT_COLOR = "#2965a6"
LOGIC_RIM_COLOR = "#539beb"
STUDIO_SIGNAL_FLOOR_DB = -90
STUDIO_EAR_HEIGHT = 0.25
STUDIO_SELECTED_COLOR = "#56acff"
SIZE_HALO_RADIUS_GAIN = 3


def studio_object_color(level_db: float) -> QColor:
    norm = max(0.0, min(1.0, (level_db - STUDIO_SIGNAL_FLOOR_DB) / (-5.0 - STUDIO_SIGNAL_FLOOR_DB)))
    return QColor(33 + round(3 * norm), 112 + round(87 * norm), 76 + round(41 * norm))


class Room3DCanvas(QWidget):
    """Interactive QPainter 3D room/spherical monitor used when WebEngine monitor HTML is absent."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._speakers: list[MonitorSpeaker] = []
        self.setMinimumSize(360, 300)
        self._yaw = math.radians(38)
        self._pitch = math.radians(26)
        self._drag_pos: QPointF | None = None
        self._show_labels = True
        self._show_speakers = False
        self._show_drop = False
        self._style = "person"
        self._selected_object: int | None = None
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

    def set_style(self, style: str) -> None:
        self._style = style
        self.update()

    def set_selected_object(self, index: int | None) -> None:
        self._selected_object = index
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.update()

    def set_layers(self, upper: bool, middle: bool, lower: bool) -> None:
        self._layers = {"upper": upper, "middle": middle, "lower": lower}
        self.update()

    def set_speakers(self, speakers: list[MonitorSpeaker]) -> None:
        self._speakers = speakers
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
        if self._mode == "room" and self._style != "logic":
            rx, ry, rz = (x - 0.5) * 1.5, z - 0.5, (0.5 - y) * 2.0
        else:
            rx, ry, rz = (x - 0.5) * 2.0, (z - 0.5) * 1.15, (y - 0.5) * 2.0

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
        room_mode = self._mode == "room"
        logic = room_mode and self._style == "logic"
        studio = room_mode and not logic
        person = studio and self._style == "person"

        if room_mode:
            p.fillRect(0, 0, w, h, QColor("#161e29" if logic else "#1a191d"))
        else:
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
        if room_mode:
            # Outward winding draws only the interior faces facing the camera.
            floor, wall, side_wall = (
                ("#233242", "#27384a", "#202f40") if logic else ("#3f4045", "#54555a", "#404147")
            )
            faces = (
                ((0, 1, 2, 3), QColor(floor)),
                ((7, 6, 5, 4), QColor(wall)),
                ((4, 5, 1, 0), QColor(wall)),
                ((6, 7, 3, 2), QColor(wall)),
                ((0, 3, 7, 4), QColor(side_wall)),
                ((5, 6, 2, 1), QColor(side_wall)),
            )
        visible_edges: set[tuple[int, int]] = set()
        floor_visible = False
        for idxs, fcol in sorted(faces, key=lambda f: -sum(proj[i][2] for i in f[0]) / 4.0):
            if room_mode:
                (ax, ay, _), (bx, by, _), (cx_face, cy_face, _) = (proj[i] for i in idxs[:3])
                cross = (bx - ax) * (cy_face - ay) - (by - ay) * (cx_face - ax)
                # The studio projection reverses the depth axis.
                if cross == 0 or (cross > 0) == studio:
                    continue
                if idxs == (0, 1, 2, 3):
                    floor_visible = True
                for a, b in zip(idxs, idxs[1:] + idxs[:1], strict=True):
                    visible_edges.add((min(a, b), max(a, b)))
            p.setBrush(fcol)
            p.drawPolygon(QPolygonF([QPointF(proj[i][0], proj[i][1]) for i in idxs]))

        if logic and floor_visible:
            p.setPen(QPen(QColor(88, 107, 128, 165), 0.9))
            for step in (0.25, 0.5, 0.75):
                for start, end in (((step, 0), (step, 1)), ((0, step), (1, step))):
                    gx, gy, _ = self._project(*start, 0, cx, cy, scale)
                    hx, hy, _ = self._project(*end, 0, cx, cy, scale)
                    p.drawLine(QPointF(gx, gy), QPointF(hx, hy))

        if studio and self._style == "room":
            screen = [
                QPointF(*self._project(sx, 0.004, sz, cx, cy, scale)[:2])
                for sx, sz in ((0.17, 0.23), (0.83, 0.23), (0.83, 0.77), (0.17, 0.77))
            ]
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(205, 208, 210, 197 if math.cos(self._yaw) > 0 else 26))
            p.drawPolygon(QPolygonF(screen))

        edge_color = QColor(104, 121, 137, 191) if logic else QColor(126, 180, 216, 210)
        if studio:
            edge_color = QColor("#24252a")
        p.setPen(QPen(edge_color, 1.25 if logic else 1.15 if studio else 1.4))
        for a, b in (
            (0, 1), (1, 2), (2, 3), (3, 0),
            (4, 5), (5, 6), (6, 7), (7, 4),
            (0, 4), (1, 5), (2, 6), (3, 7),
        ):
            if room_mode and (min(a, b), max(a, b)) not in visible_edges:
                continue
            p.drawLine(QPointF(proj[a][0], proj[a][1]), QPointF(proj[b][0], proj[b][1]))

        # Listener head
        if self._style != "room" or self._mode == "spherical":
            sx, sy, _ = self._project(0.5, 0.5, 0.05, cx, cy, scale)
            ear = STUDIO_EAR_HEIGHT if person else 0.19
            hx, hy, _ = self._project(0.5, 0.5, ear, cx, cy, scale)
            nx, ny, _ = self._project(0.5, 0.44, ear - 0.01, cx, cy, scale)
            if person:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor("#92969d"))
                p.drawEllipse(QPointF(sx, sy), scale * 0.08, scale * 0.04)
                neck = QRectF(hx - scale * 0.02, min(hy, sy), scale * 0.04, abs(sy - hy))
                p.drawRoundedRect(neck, 3, 3)
                p.setBrush(QColor("#acb0b7"))
                p.drawEllipse(QPointF(hx, hy), scale * 0.049, scale * 0.064)
            else:
                p.setBrush(QColor(92, 122, 153, 210))
                p.drawEllipse(QPointF(sx, sy), 14, 7)
                p.drawEllipse(QPointF(hx, hy), 9, 9)
                p.setBrush(QColor(126, 172, 220, 230))
                p.drawEllipse(QPointF(nx, ny), 3.2, 3.2)

        # Speakers
        spk_items: list[tuple[float, str, float, float, float]] = []
        spherical = self._mode == "spherical"
        if spherical or self._show_speakers:
            for speaker in self._speakers:
                x, y, z = speaker.position
                if spherical:
                    if not self._layers[speaker.layer]:
                        continue
                    x, y = (x + 1.0) * 0.5, (1.0 - y) * 0.5
                elif person:
                    z = STUDIO_EAR_HEIGHT + (1 - STUDIO_EAR_HEIGHT) * z
                px, py, depth = self._project(x, y, max(0.0, z), cx, cy, scale)
                idx = speaker.channel
                db = self._ch_dbs[idx] if idx < len(self._ch_dbs) else SILENCE_DB
                spk_items.append((depth, speaker.name, px, py, db))

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
            selected = o.index == self._selected_object
            audible = o.active and (o.level_db >= STUDIO_SIGNAL_FLOOR_DB if studio else o.level_db > -80)
            if not (studio or selected or audible):
                continue
            z = STUDIO_EAR_HEIGHT + (1 - STUDIO_EAR_HEIGHT) * o.z if person else o.z
            px, py, depth = self._project(o.x, o.y, z, cx, cy, scale)
            fx, fy, _ = self._project(o.x, o.y, 0.0, cx, cy, scale)
            draw_objs.append((depth, o, audible, px, py, fx, fy))
        for depth, o, audible, px, py, fx, fy in sorted(draw_objs, key=lambda item: -item[0]):
            selected = o.index == self._selected_object
            norm = max(0.0, min(1.0, (o.level_db + 60.0) / 55.0))
            if logic:
                col = QColor("#ffffff" if selected else LOGIC_RIM_COLOR)
                radius = scale * 0.047 * 4.6 / max(0.8, 4.6 + depth)
            elif studio:
                col = QColor(STUDIO_SELECTED_COLOR) if selected else studio_object_color(o.level_db)
                radius = scale * 0.045 * 4.6 / max(0.8, 4.6 + depth)
            else:
                col = QColor(SELECTED_OBJECT_COLOR) if selected else object_color(o.index)
                radius = 7.5 + 6.5 * norm + (1.5 if selected else 0.0)
            if self._show_drop and (audible or selected):
                p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 110), 1.0, Qt.PenStyle.DashLine))
                p.drawLine(QPointF(px, py), QPointF(fx, fy))
                p.setPen(QPen(QColor(col.red(), col.green(), col.blue(), 140), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(QPointF(fx, fy), 6.0, 3.2)
            if room_mode and o.size > 0:
                halo_radius = radius * (1 + SIZE_HALO_RADIUS_GAIN * min(1.0, o.size))
                fill = QColor(col if audible or selected else "#89939d")
                stroke = QColor(fill)
                alpha = 0.65 if logic else 1.0 if audible or selected else 0.45
                fill.setAlpha(round(41 * alpha))
                stroke.setAlpha(round(133 * alpha))
                p.setBrush(fill)
                p.setPen(QPen(stroke, max(0.8, halo_radius * 0.05)))
                p.drawEllipse(QPointF(px, py), halo_radius, halo_radius)
            idle = studio and not (audible or selected)
            p.setPen(Qt.PenStyle.NoPen)
            if idle:
                p.setBrush(QColor(137, 147, 157, 69))
            elif logic:
                sphere_grad = QRadialGradient(px, py, radius)
                sphere_grad.setColorAt(0.46, QColor("#e8f2ff" if selected else LOGIC_OBJECT_COLOR))
                sphere_grad.setColorAt(1.0, col)
                p.setBrush(sphere_grad)
            else:
                sphere_grad = QRadialGradient(px - radius * 0.32, py - radius * 0.32, radius * 1.35)
                sphere_grad.setColorAt(0.0, col.lighter(165))
                sphere_grad.setColorAt(0.45, col)
                sphere_grad.setColorAt(1.0, col.darker(190))
                p.setBrush(sphere_grad)
            p.drawEllipse(QPointF(px, py), radius, radius)
            if selected:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(col, 1.5))
                p.drawEllipse(QPointF(px, py), radius + 4, radius + 4)
            if not logic and (self._show_labels or selected):
                label = str(o.index + 1)
                font = QFont("sans-serif", 8, QFont.Weight.Normal if studio else QFont.Weight.Bold)
                path = QPainterPath()
                path.addText(0, 0, font, label)
                bounds = path.boundingRect()
                marker_radius = radius + 4 if selected else radius
                gap = 2 if studio else 4
                path.translate(px - bounds.center().x(), py + marker_radius + gap - bounds.top())
                if studio:
                    p.fillPath(path, QColor(224, 226, 230, 115 if idle else 255))
                else:
                    p.strokePath(path, QPen(QColor(0, 0, 0, 210), 2.8))
                    p.fillPath(path, QColor(255, 255, 255))

        if w > 180 and h > 180:
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
                    color = QColor(LOGIC_RIM_COLOR) if logic else object_color(o.index)
                    if studio:
                        color = (
                            studio_object_color(o.level_db) if o.level_db >= STUDIO_SIGNAL_FLOOR_DB
                            else QColor(137, 147, 157, 69)
                        )
                    p.setBrush(color)
                    p.drawEllipse(QPointF(rx0 + 8 + o.x * (rs - 16), ry0 + 8 + o.y * (rs - 16)), 3.5, 3.5)
