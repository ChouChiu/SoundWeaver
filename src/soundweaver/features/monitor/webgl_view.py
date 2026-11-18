"""Three.js WebGL monitor bridge embedding `renderer/dist/monitor.html`."""

from __future__ import annotations

import json

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget

from soundweaver.core.codecs import mpegh
from soundweaver.core.playback import ObjectSnapshot, PlaybackEngine
from soundweaver.core.renderer import RENDERER_DIR


class WebGLMonitorWidget(QWebEngineView):
    """True Three.js WebGL 3D monitor supporting Dolby/AV3A room and 360RA WalkMix dome modes."""

    render_failed = Signal()

    def __init__(self, engine: PlaybackEngine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._ready = False
        self._show_labels = True
        self._show_speakers = False
        self._show_drop = False
        self._style = "person"
        self._mode = "room"
        self._layers = (True, True, True)
        self.setMinimumSize(360, 300)
        self.settings().setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        self.settings().setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        self.page().setBackgroundColor(QColor(26, 25, 29))
        self.page().renderProcessTerminated.connect(lambda _s, _c: self.render_failed.emit())
        self.loadFinished.connect(self._on_loaded)
        self.load(QUrl.fromLocalFile(str(RENDERER_DIR / "dist" / "monitor.html")))

    def _on_loaded(self, ok: bool) -> None:
        self._ready = ok
        if not ok:
            return
        self.set_mode(self._mode)
        self.sync_speakers()
        self.set_show_labels(self._show_labels)
        self.set_show_speakers(self._show_speakers)
        self.set_show_drop(self._show_drop)
        self.set_style(self._style)
        self.set_layers(*self._layers)

    def sync_speakers(self) -> None:
        if not self._ready:
            return
        if self._mode == "spherical":
            lay = mpegh.cicp_layout(self.engine.cicp)
            spks = [
                {
                    "name": sp.name,
                    "pos": [0.5, 0.5, 0.5],
                    "spherical": [round(v, 4) for v in sp.cartesian()],
                    "layer": sp.layer,
                    "isLfe": sp.is_lfe,
                }
                for sp in lay.speakers
            ]
        else:
            spks = [
                {"name": n, "pos": [round(p[0], 4), round(p[1], 4), round(max(0.0, p[2]), 4)]}
                for n, p in self.engine.layout.positions.items()
            ]
        self.page().runJavaScript(f"window.liveMonitor?.setSpeakers({json.dumps(spks)});")

    def set_view(self, preset: str) -> None:
        if self._ready:
            self.page().runJavaScript(f"window.liveMonitor?.setView({json.dumps(preset)});")

    def set_show_labels(self, on: bool) -> None:
        self._show_labels = on
        if self._ready:
            self.page().runJavaScript(f"window.liveMonitor?.setOptions({{showLabels:{'true' if on else 'false'}}});")

    def set_show_speakers(self, on: bool) -> None:
        self._show_speakers = on
        if self._ready:
            self.page().runJavaScript(f"window.liveMonitor?.setOptions({{showSpeakers:{'true' if on else 'false'}}});")

    def set_show_drop(self, on: bool) -> None:
        self._show_drop = on
        if self._ready:
            self.page().runJavaScript(f"window.liveMonitor?.setOptions({{showDrop:{'true' if on else 'false'}}});")

    def set_show_xy(self, _on: bool) -> None:
        pass

    def set_style(self, style: str) -> None:
        self._style = style
        if self._ready:
            self.page().runJavaScript(f"window.liveMonitor?.setStyle({json.dumps(style)});")

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        if self._ready:
            self.page().runJavaScript(f"window.liveMonitor?.setMode({json.dumps(mode)});")
            self.sync_speakers()

    def set_layers(self, upper: bool, middle: bool, lower: bool) -> None:
        self._layers = (upper, middle, lower)
        if self._ready:
            u = "true" if upper else "false"
            m = "true" if middle else "false"
            l = "true" if lower else "false"
            self.page().runJavaScript(f"window.liveMonitor?.setLayers({u}, {m}, {l});")

    def update_telemetry(self, ch_dbs: list[float], objs: list[ObjectSnapshot]) -> None:
        if not self._ready:
            return
        payload = [
            {"id": o.index, "x": round(o.x, 4), "y": round(o.y, 4), "z": round(o.z, 4), "db": round(o.level_db, 1), "active": o.active}
            for o in objs
        ]
        self.page().runJavaScript(
            f"window.liveMonitor?.updateFrame({json.dumps(payload)}, {json.dumps([round(d, 1) for d in ch_dbs])});"
        )
