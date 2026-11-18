"""Interactive 3D spatial audio monitor, live meters with M/S, playlist, and synced lyrics."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QListWidgetItem,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CheckBox,
    ListWidget,
    PillPushButton,
    PushButton,
    SegmentedWidget,
    SimpleCardWidget,
    SmoothScrollArea,
    TitleLabel,
    ToolButton,
)

from soundweaver.core.audio import AudioInfo
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.i18n import t
from soundweaver.core.playback import SILENCE_DB, ObjectSnapshot, PlaybackEngine
from soundweaver.core.renderer import RENDERER_DIR
from soundweaver.core.sources import AUDIO_SUFFIXES, is_audio
from soundweaver.core.speakers import LAYOUTS, atmos_layout
from soundweaver.core.ttml import parse_ttml_lines
from soundweaver.features.monitor.meters import MeterRow, object_color
from soundweaver.features.monitor.room_canvas import Room3DCanvas
from soundweaver.features.monitor.webgl_view import WebGLMonitorWidget
from soundweaver.ui import fluent, widgets


class MonitorPage(QWidget):
    """Unified Spatial Player & 3D Monitor: playlist, lyrics, 3D room/sphere dome, and M/S meters."""

    full_window_toggled = Signal(bool)

    def __init__(self, engine: PlaybackEngine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.setAcceptDrops(True)
        self._playlist: list[Path] = []
        self._current_idx = -1
        self._lyrics: list[tuple[float, float, str]] = []
        self._meter_mode = "channels"
        self._meter_rows: list[MeterRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(*widgets.PAGE_MARGINS)
        root.setSpacing(widgets.SECTION_SPACING)

        # Header + Layout / CICP + Binaural + Full-window
        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        hdr.addWidget(TitleLabel(t("monitor.title"), self))
        hdr.addStretch(1)

        self.binaural_btn = PillPushButton(fluent.icon("headphones"), t("monitor.binaural"), self)
        self.binaural_btn.setCheckable(True)
        self.binaural_btn.setEnabled(av3a.find_binaural_renderer() is not None)
        self.binaural_btn.setToolTip(t("monitor.binaural_tip"))
        self.binaural_btn.toggled.connect(self.engine.set_binaural)
        hdr.addWidget(self.binaural_btn)

        self.mode_box = fluent.ComboBox(self)
        self.mode_box.addItem(t("monitor.mode.room"), "room")
        self.mode_box.addItem(t("monitor.mode.spherical"), "spherical")
        self.mode_box.currentIndexChanged.connect(self._on_mode_changed)
        hdr.addWidget(self.mode_box)

        self.layout_lbl = BodyLabel(t("label.atmos_render_layout"), self)
        self.layout_box = fluent.ComboBox(self)
        for spk in LAYOUTS:
            self.layout_box.addItem(spk.name, spk.name)
        self.layout_box.setCurrentIndex(self.layout_box.findData(self.engine.layout.name))
        self.layout_box.currentIndexChanged.connect(self._on_layout_changed)
        hdr.addWidget(self.layout_lbl)
        hdr.addWidget(self.layout_box)

        self.cicp_box = fluent.ComboBox(self)
        for cid in mpegh.SUPPORTED_CICP:
            lay = mpegh.CICP_LAYOUTS[cid]
            self.cicp_box.addItem(f"CICP {cid}: {lay.label}", cid)
        self.cicp_box.setCurrentIndex(self.cicp_box.findData(self.engine.cicp))
        self.cicp_box.currentIndexChanged.connect(self._on_cicp_changed)
        self.cicp_box.setVisible(False)
        hdr.addWidget(self.cicp_box)

        self.full_btn = ToolButton(fluent.icon("view-fullscreen"), self)
        self.full_btn.setCheckable(True)
        self.full_btn.setToolTip(t("monitor.fullscreen_tip"))
        self.full_btn.toggled.connect(self._on_full_toggled)
        hdr.addWidget(self.full_btn)
        root.addLayout(hdr)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setHandleWidth(8)
        splitter.setChildrenCollapsible(False)
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")

        # Left: Playlist + Synced Lyrics (Fluent ListWidget)
        left = SimpleCardWidget(splitter)
        left.setMinimumWidth(200)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(12, 10, 12, 12)
        left_lay.setSpacing(8)
        left_tabs = SegmentedWidget(left)
        left_stack = QStackedWidget(left)

        pl_page = QWidget(left_stack)
        pl_lay = QVBoxLayout(pl_page)
        pl_lay.setContentsMargins(0, 2, 0, 0)
        pl_lay.setSpacing(8)
        pl_btns = QHBoxLayout()
        pl_btns.setSpacing(6)
        btn_add = PushButton(fluent.icon("list-add"), t("monitor.add_tracks"), pl_page)
        btn_add.clicked.connect(self._browse_tracks)
        btn_clear = ToolButton(fluent.icon("edit-clear-all"), pl_page)
        btn_clear.clicked.connect(self._clear_playlist)
        pl_btns.addWidget(btn_add, 1)
        pl_btns.addWidget(btn_clear)
        self.playlist_list = ListWidget(pl_page)
        self.playlist_list.setFrameShape(QFrame.Shape.NoFrame)
        self.playlist_list.setStyleSheet("ListWidget { background: transparent; border: none; }")
        self.playlist_list.itemDoubleClicked.connect(self._play_item)
        pl_lay.addLayout(pl_btns)
        pl_lay.addWidget(self.playlist_list, 1)

        self.lyrics_list = ListWidget(left_stack)
        self.lyrics_list.setFrameShape(QFrame.Shape.NoFrame)
        self.lyrics_list.setStyleSheet("ListWidget { background: transparent; border: none; }")
        left_stack.addWidget(pl_page)
        left_stack.addWidget(self.lyrics_list)
        left_tabs.addItem("playlist", t("monitor.playlist"), lambda: left_stack.setCurrentIndex(0))
        left_tabs.addItem("lyrics", t("monitor.lyrics"), lambda: left_stack.setCurrentIndex(1))
        left_tabs.setCurrentItem("playlist")
        left_lay.addWidget(left_tabs)
        left_lay.addWidget(left_stack, 1)

        # Center: 3D Monitor + Viewpoints + Options / WalkMix Layers
        center = SimpleCardWidget(splitter)
        center.setMinimumWidth(360)
        c_lay = QVBoxLayout(center)
        c_lay.setContentsMargins(12, 10, 12, 12)
        c_lay.setSpacing(8)
        vp_row = QHBoxLayout()
        vp_row.setSpacing(8)
        vp_tabs = SegmentedWidget(center)
        for preset in ("iso", "top", "front", "back", "left", "right"):
            vp_tabs.addItem(preset, t(f"option.view.{preset}"), lambda p=preset: self.canvas_3d.set_view(p))
        vp_tabs.setCurrentItem("iso")
        vp_row.addWidget(vp_tabs)
        vp_row.addStretch(1)
        self.style_box = fluent.ComboBox(center)
        for key in ("person", "room", "logic"):
            self.style_box.addItem(t(f"monitor.style.{key}"), key)
        self.style_box.currentIndexChanged.connect(self._on_style_changed)
        vp_row.addWidget(self.style_box)

        opt_row = QHBoxLayout()
        opt_row.setSpacing(12)
        self.chk_labels = CheckBox(t("soundfield.labels"), center)
        self.chk_labels.setChecked(True)
        self.chk_labels.toggled.connect(lambda on: self.canvas_3d.set_show_labels(on))
        self.chk_speakers = CheckBox(t("soundfield.speakers"), center)
        self.chk_speakers.toggled.connect(lambda on: self.canvas_3d.set_show_speakers(on))
        self.chk_drop = CheckBox(t("soundfield.drop"), center)
        self.chk_drop.toggled.connect(lambda on: self.canvas_3d.set_show_drop(on))
        opt_row.addWidget(self.chk_labels)
        opt_row.addWidget(self.chk_speakers)
        opt_row.addWidget(self.chk_drop)
        opt_row.addStretch(1)

        self.btn_upper = PillPushButton(t("monitor.layer.upper"), center)
        self.btn_middle = PillPushButton(t("monitor.layer.middle"), center)
        self.btn_lower = PillPushButton(t("monitor.layer.lower"), center)
        for b in (self.btn_upper, self.btn_middle, self.btn_lower):
            b.setCheckable(True)
            b.setChecked(True)
            b.toggled.connect(self._sync_layers)
            b.setVisible(False)
            opt_row.addWidget(b)

        self._center_lay = c_lay
        if (
            QGuiApplication.platformName() != "offscreen"
            and (RENDERER_DIR / "dist" / "monitor.html").is_file()
        ):
            web = WebGLMonitorWidget(self.engine, center)
            web.render_failed.connect(self._fallback_to_software_canvas)
            self.canvas_3d: WebGLMonitorWidget | Room3DCanvas = web
        else:
            self.canvas_3d = Room3DCanvas(self.engine, center)
        c_lay.addLayout(vp_row)
        c_lay.addLayout(opt_row)
        c_lay.addWidget(self.canvas_3d, 1)

        # Right: Live Meters + M/S (SmoothScrollArea)
        right = SimpleCardWidget(splitter)
        right.setMinimumWidth(250)
        r_lay = QVBoxLayout(right)
        r_lay.setContentsMargins(12, 10, 12, 12)
        r_lay.setSpacing(8)
        r_hdr = QHBoxLayout()
        r_hdr.setSpacing(6)
        m_tabs = SegmentedWidget(right)
        m_tabs.addItem("channels", t("monitor.channels"), lambda: self._set_meter_mode("channels"))
        m_tabs.addItem("objects", t("monitor.objects"), lambda: self._set_meter_mode("objects"))
        m_tabs.setCurrentItem("channels")
        btn_clear_ms = ToolButton(fluent.icon("edit-clear"), right)
        btn_clear_ms.setToolTip(t("monitor.clear_ms"))
        btn_clear_ms.clicked.connect(self.engine.clear_ms)
        r_hdr.addWidget(m_tabs, 1)
        r_hdr.addWidget(btn_clear_ms)
        r_lay.addLayout(r_hdr)

        scroll = SmoothScrollArea(right)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        self._meter_container = QWidget(scroll)
        self._meter_layout = QVBoxLayout(self._meter_container)
        self._meter_layout.setContentsMargins(2, 4, 2, 4)
        self._meter_layout.setSpacing(3)
        self._meter_layout.addStretch(1)
        scroll.setWidget(self._meter_container)
        scroll.enableTransparentBackground()
        r_lay.addWidget(scroll, 1)

        splitter.addWidget(left)
        splitter.addWidget(center)
        splitter.addWidget(right)
        splitter.setSizes([240, 620, 300])
        root.addWidget(splitter, 1)

        self.engine.meters_updated.connect(self._on_meters)
        self.engine.position_changed.connect(self._on_position)
        self.engine.track_loaded.connect(self._on_track_loaded)
        self._rebuild_meters(0)

    def _fallback_to_software_canvas(self) -> None:
        if isinstance(self.canvas_3d, Room3DCanvas):
            return
        old = self.canvas_3d
        sw = Room3DCanvas(self.engine, old.parentWidget())
        sw.set_show_labels(self.chk_labels.isChecked())
        sw.set_show_speakers(self.chk_speakers.isChecked())
        sw.set_show_drop(self.chk_drop.isChecked())
        sw.set_style(str(self.style_box.currentData() or "person"))
        sw.set_mode(str(self.mode_box.currentData() or "room"))
        sw.set_layers(self.btn_upper.isChecked(), self.btn_middle.isChecked(), self.btn_lower.isChecked())
        self._center_lay.replaceWidget(old, sw)
        old.deleteLater()
        self.canvas_3d = sw

    def _on_full_toggled(self, on: bool) -> None:
        self.full_btn.setIcon(fluent.icon("view-restore" if on else "view-fullscreen"))
        self.full_window_toggled.emit(on)

    def exit_full_window(self) -> None:
        if self.full_btn.isChecked():
            self.full_btn.setChecked(False)

    def _sync_layers(self) -> None:
        self.canvas_3d.set_layers(
            self.btn_upper.isChecked(),
            self.btn_middle.isChecked(),
            self.btn_lower.isChecked(),
        )

    def _on_mode_changed(self) -> None:
        mode = str(self.mode_box.currentData() or "room")
        is_sph = mode == "spherical"
        self.canvas_3d.set_mode(mode)
        self.layout_lbl.setVisible(not is_sph)
        self.layout_box.setVisible(not is_sph)
        self.cicp_box.setVisible(is_sph)
        self.style_box.setEnabled(not is_sph)
        self.chk_speakers.setVisible(not is_sph)
        self.chk_drop.setVisible(not is_sph)
        for b in (self.btn_upper, self.btn_middle, self.btn_lower):
            b.setVisible(is_sph)

    def _on_style_changed(self) -> None:
        self.canvas_3d.set_style(str(self.style_box.currentData() or "person"))

    def _on_layout_changed(self) -> None:
        self.engine.set_layout(atmos_layout(str(self.layout_box.currentData() or "")))
        self.canvas_3d.sync_speakers()
        self._rebuild_meters(len(self._meter_rows))

    def _on_cicp_changed(self) -> None:
        self.engine.set_cicp(int(self.cicp_box.currentData() or 13))
        self.canvas_3d.sync_speakers()
        self._rebuild_meters(0)

    def _set_meter_mode(self, mode: str) -> None:
        self._meter_mode = mode
        self._rebuild_meters(0)

    def _rebuild_meters(self, obj_count: int) -> None:
        for row in self._meter_rows:
            self._meter_layout.removeWidget(row)
            row.deleteLater()
        self._meter_rows = []
        if self._meter_mode == "channels":
            channels = self.engine.active_channels
            for idx, ch in enumerate(channels):
                row = MeterRow(idx, ch, parent=self._meter_container)
                row.mute_clicked.connect(self.engine.toggle_channel_mute)
                row.solo_clicked.connect(self.engine.toggle_channel_solo)
                self._meter_layout.insertWidget(self._meter_layout.count() - 1, row)
                self._meter_rows.append(row)
        else:
            for idx in range(obj_count):
                row = MeterRow(
                    idx, f"Obj {idx + 1}", object_color(idx), is_object=True, parent=self._meter_container
                )
                row.mute_clicked.connect(self.engine.toggle_object_mute)
                row.solo_clicked.connect(self.engine.toggle_object_solo)
                self._meter_layout.insertWidget(self._meter_layout.count() - 1, row)
                self._meter_rows.append(row)

    def _on_meters(self, ch_dbs: list[float], objs: list[ObjectSnapshot]) -> None:
        self.canvas_3d.update_telemetry(ch_dbs, objs)
        if self._meter_mode == "channels":
            if len(self._meter_rows) != len(self.engine.active_channels):
                self._rebuild_meters(len(objs))
            muted, soloed = self.engine.channel_ms()
            for i, row in enumerate(self._meter_rows):
                db = ch_dbs[i] if i < len(ch_dbs) else SILENCE_DB
                row.set_state(db, i in muted, i in soloed)
        else:
            if len(self._meter_rows) != len(objs):
                self._rebuild_meters(len(objs))
            muted, soloed = self.engine.object_ms()
            for i, row in enumerate(self._meter_rows):
                if i < len(objs):
                    row.set_state(objs[i].level_db, i in muted, i in soloed, (objs[i].x, objs[i].y))
                else:
                    row.set_state(SILENCE_DB, i in muted, i in soloed)

    def _on_track_loaded(self, path: Path, info: AudioInfo) -> None:
        target_mode = "spherical" if info.is_mpegh else "room"
        mode_idx = self.mode_box.findData(target_mode)
        if mode_idx >= 0 and self.mode_box.currentIndex() != mode_idx:
            self.mode_box.setCurrentIndex(mode_idx)
        self._rebuild_meters(info.object_count)

        self.lyrics_list.clear()
        self._lyrics = []
        ttml = path.with_suffix(".ttml")
        if ttml.is_file():
            self._lyrics = parse_ttml_lines(ttml)
            for _b, _e, text in self._lyrics:
                self.lyrics_list.addItem(text)

    def _on_position(self, sec: float) -> None:
        if not self._lyrics:
            return
        active = -1
        for i, (b, e, _txt) in enumerate(self._lyrics):
            if b <= sec <= (e if e > b else b + 5.0):
                active = i
                break
        if active >= 0 and active != self.lyrics_list.currentRow():
            self.lyrics_list.setCurrentRow(active)
            item = self.lyrics_list.item(active)
            if item is not None:
                self.lyrics_list.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    def add_paths(self, paths: list[Path]) -> None:
        for p in paths:
            if p.is_dir():
                for child in sorted(p.rglob("*")):
                    if is_audio(child):
                        self._add_one(child)
            elif p.is_file():
                self._add_one(p)
        if self._current_idx < 0 and self._playlist:
            self.play_index(0)

    def _add_one(self, path: Path) -> None:
        if path in self._playlist:
            return
        self._playlist.append(path)
        self.playlist_list.addItem(QListWidgetItem(path.name))

    def play_index(self, idx: int) -> None:
        if 0 <= idx < len(self._playlist):
            self._current_idx = idx
            self.playlist_list.setCurrentRow(idx)
            self.engine.load(self._playlist[idx])

    def play_next(self) -> None:
        if self._playlist:
            self.play_index((self._current_idx + 1) % len(self._playlist))

    def play_prev(self) -> None:
        if self._playlist:
            self.play_index((self._current_idx - 1) % len(self._playlist))

    def _play_item(self, item: QListWidgetItem) -> None:
        self.play_index(self.playlist_list.row(item))

    def _clear_playlist(self) -> None:
        self.engine.stop()
        self._playlist.clear()
        self.playlist_list.clear()
        self._current_idx = -1

    def _browse_tracks(self) -> None:
        exts = " ".join(f"*{s}" for s in sorted(AUDIO_SUFFIXES))
        files, _ = QFileDialog.getOpenFileNames(
            self,
            t("monitor.add_tracks"),
            str(Path.home()),
            f"Audio ({exts});;All (*.*)",
        )
        if files:
            self.add_paths([Path(f) for f in files])

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.add_paths(paths)
