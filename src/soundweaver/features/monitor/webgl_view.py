"""Three.js WebGL monitor bridge embedding the local renderer bundle."""

from __future__ import annotations

import json

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QColor
from PySide6.QtWebEngineCore import QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QWidget

from soundweaver.core.playback_mix import ObjectSnapshot
from soundweaver.core.renderer import RENDERER_DIR
from soundweaver.features.monitor.speakers import MonitorSpeaker

MONITOR_PAGE = RENDERER_DIR / "dist" / "monitor.html"


class WebGLMonitorWidget(QWebEngineView):
    """Send monitor state to the browser once its Three.js scene is ready."""

    render_failed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ready = False
        self._options = {"showLabels": True, "showSpeakers": False, "showDrop": False}
        self._view = "iso"
        self._style = "person"
        self._selected_object: int | None = None
        self._telemetry: tuple[list[float], list[ObjectSnapshot]] = ([], [])
        # at most one telemetry script in flight; a newer one replaces the pending copy
        self._telemetry_busy = False
        self._pending_telemetry: tuple[str, str] | None = None
        self._speakers: list[MonitorSpeaker] = []
        self._mode = "room"
        self._layers = (True, True, True)
        self.setMinimumSize(360, 300)
        self.settings().setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True
        )
        self.page().setBackgroundColor(QColor(26, 25, 29))
        self.page().renderProcessTerminated.connect(self._on_process_terminated)
        self.loadFinished.connect(self._on_loaded)
        self.load(QUrl.fromLocalFile(str(MONITOR_PAGE)))

    def _on_process_terminated(self, _status, _code: int) -> None:
        self._ready = False
        self.render_failed.emit()

    def _on_loaded(self, ok: bool) -> None:
        if ok:
            self.page().runJavaScript("typeof window.liveMonitor === 'object'", self._initialize)
        else:
            self.render_failed.emit()

    def _initialize(self, ready: bool | None) -> None:
        # Qt may invoke an outstanding script callback with no result during page teardown.
        if ready is None:
            return
        if not ready:
            self.render_failed.emit()
            return
        self._ready = True
        self.set_mode(self._mode)
        self.set_style(self._style)
        self.set_view(self._view)
        self.set_speakers(self._speakers)
        self._send("setOptions", self._options)
        self.set_layers(*self._layers)
        self.set_selected_object(self._selected_object)
        self.update_telemetry(*self._telemetry)

    def _send(self, method: str, *args: object) -> None:
        if self._ready:
            payload = ", ".join(json.dumps(arg, separators=(",", ":")) for arg in args)
            self.page().runJavaScript(f"window.liveMonitor.{method}({payload});")

    def set_speakers(self, speakers: list[MonitorSpeaker]) -> None:
        self._speakers = speakers
        if self._ready:
            self._send(
                "setSpeakers",
                [
                    {
                        "name": sp.name,
                        "channel": sp.channel,
                        "layer": sp.layer,
                        "position": [round(value, 4) for value in sp.position],
                    }
                    for sp in speakers
                ],
            )

    def set_view(self, preset: str) -> None:
        self._view = preset
        self._send("setView", preset)

    def _set_option(self, key: str, on: bool) -> None:
        self._options[key] = on
        self._send("setOptions", {key: on})

    def set_show_labels(self, on: bool) -> None:
        self._set_option("showLabels", on)

    def set_show_speakers(self, on: bool) -> None:
        self._set_option("showSpeakers", on)

    def set_show_drop(self, on: bool) -> None:
        self._set_option("showDrop", on)

    def set_style(self, style: str) -> None:
        self._style = style
        self._send("setStyle", style)

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._send("setMode", mode)

    def set_layers(self, upper: bool, middle: bool, lower: bool) -> None:
        self._layers = (upper, middle, lower)
        self._send("setLayers", *self._layers)

    def set_selected_object(self, index: int | None) -> None:
        self._selected_object = index
        self._send("setSelectedObject", index)

    def update_telemetry(self, ch_dbs: list[float], objs: list[ObjectSnapshot]) -> None:
        self._telemetry = (ch_dbs, objs)
        if not self._ready:
            return
        payload = json.dumps(
            [
                {
                    "id": o.index,
                    "x": round(o.x, 4),
                    "y": round(o.y, 4),
                    "z": round(o.z, 4),
                    "db": round(o.level_db, 1),
                    "active": o.active,
                    "size": round(o.size, 4),
                }
                for o in objs
            ],
            separators=(",", ":"),
        )
        levels = json.dumps([round(db, 1) for db in ch_dbs], separators=(",", ":"))
        # A stalled renderer must drop stale frames instead of queueing them up;
        # one more update always follows while playback runs.
        if self._telemetry_busy:
            self._pending_telemetry = (payload, levels)
            return
        self._telemetry_busy = True
        self.page().runJavaScript(
            f"window.liveMonitor.updateFrame({payload}, {levels});", self._telemetry_flushed
        )

    def _telemetry_flushed(self, _result: object) -> None:
        self._telemetry_busy = False
        if self._pending_telemetry is None:
            return
        payload, levels = self._pending_telemetry
        self._pending_telemetry = None
        self._telemetry_busy = True
        self.page().runJavaScript(
            f"window.liveMonitor.updateFrame({payload}, {levels});", self._telemetry_flushed
        )
