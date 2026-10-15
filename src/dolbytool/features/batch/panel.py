"""The batch inputs: a list of tracks, optionally treated as one album.

Each track is read in the background like the single source; its cover is
the album's (picked, or the motion artwork or image in the folder), else its
embedded one or the art in its folder.  Its lyrics are one picked for it,
else the lyric file found nearby or imported whose name fits it (see
`source.lyrics`).  Every track becomes its own `Project` for the output
panels to plan, with an output folder of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    LineEdit,
    PushButton,
    RoundMenu,
    SwitchButton,
    TransparentPushButton,
)

from dolbytool.core.i18n import t
from dolbytool.core.paths import path_from_text
from dolbytool.core.project import ATMOS_LAYOUT_KEY, Project, expected_tracks
from dolbytool.core.speakers import SpeakerLayout, atmos_layout
from dolbytool.features.batch.service import (
    BatchTrack,
    album_name,
    album_order,
    album_root,
    audio_files,
    common_folder,
    safe_name,
    track_output_dir,
)
from dolbytool.features.source import lyrics as lyric_match
from dolbytool.features.source.service import Inspection, folder_cover, inspect
from dolbytool.ui import fluent, widgets
from dolbytool.ui.background import Each

logger = logging.getLogger(__name__)

_LYRICS, _STATUS = 2, 3


class BatchPanel(widgets.Card):
    changed = Signal()

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(t("batch.title"), parent)
        self.settings = settings
        self.tracks: list[BatchTrack] = []
        # lyrics picked for one track, by its audio; None turns its lyrics off
        self._ttml: dict[Path, Path | None] = {}
        # lyric files imported for the whole batch, and those found beside its tracks
        self._imported: list[Path] = []
        self._nearby: list[Path] = []
        # lyrics matched to the tracks by name
        self._auto: dict[Path, Path] = {}
        self._probe = Each(self)
        self._running = False
        # the art found in a folder, by folder, with the folder's mtime it was found at
        self._folder_covers: dict[Path, tuple[int, Path | None]] = {}

        self.list = widgets.HintTree(t("batch.hint"))
        self.list.setColumnCount(4)
        self.list.setHeaderLabels(
            [
                t("batch.col_track"),
                t("batch.col_info"),
                t("batch.col_lyrics"),
                t("batch.col_status"),
            ]
        )
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = self.list.header()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(0, 240)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(1, 180)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.resizeSection(2, 140)
        self.list.setMinimumHeight(120)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        remove = QShortcut(QKeySequence.StandardKey.Delete, self.list)
        remove.setContext(Qt.ShortcutContext.WidgetShortcut)
        remove.activated.connect(self.remove_selected)

        self._buttons: list[QWidget] = []
        add_files = self._button(t("batch.add_files"), "list-add", self.pick_files)
        add_folder = self._button(t("batch.add_folder"), "folder-add", self.pick_folder)
        import_lyrics = self._button(t("batch.import_lyrics"), "document-import", self.pick_lyrics)
        import_lyrics.setToolTip(t("batch.import_lyrics_tip"))
        self.lyrics_btn = self._button(t("batch.set_lyrics"), "text-xml", self.pick_ttml)
        self.remove_btn = self._button(t("batch.remove"), "list-remove", self.remove_selected)
        self.remove_btn.setToolTip(t("batch.remove_tip"))
        clear = self._button(t("batch.clear"), "edit-clear-all", self.clear, flat=True)
        buttons = QHBoxLayout()
        buttons.setSpacing(widgets.INLINE_SPACING)
        for button in (add_files, add_folder, import_lyrics, self.lyrics_btn, self.remove_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(clear)

        self.album = SwitchButton(t("batch.album_mode"))
        self.album.setOnText(t("batch.album_mode"))
        self.album.setToolTip(t("batch.album_mode_tip"))
        self.album.checkedChanged.connect(lambda _on: self._sync())
        self.album_name = LineEdit()
        self.album_name.setClearButtonEnabled(True)
        self.album_name.setPlaceholderText(t("batch.album_name_placeholder"))
        self.album_cover, cover_btn, cover_row = widgets.browse_row(
            t("batch.album_cover_placeholder"), self.pick_cover
        )
        self.out_edit, out_btn, out_row = widgets.browse_row("", self.pick_output)
        self._buttons += [cover_btn, out_btn]
        # one form, so the labels line up whether the album rows show or not
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(widgets.FIELD_SPACING)
        self.form.setVerticalSpacing(widgets.ROW_SPACING)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.form.addRow(
            widgets.field_label(t("batch.album_name"), self.album_name), self.album_name
        )
        self.form.addRow(widgets.field_label(t("batch.album_cover"), self.album_cover), cover_row)
        self.form.addRow(widgets.field_label(t("batch.output_root"), self.out_edit), out_row)

        layout = QVBoxLayout()
        layout.setSpacing(widgets.ROW_SPACING)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.album)
        layout.addLayout(self.form)
        self.set_body(layout)

        # edits settle before the list is redrawn, so typing a path reads it once
        self._settle = widgets.Debouncer(self._sync, parent=self)
        for edit in (self.album_name, self.album_cover, self.out_edit):
            edit.textChanged.connect(lambda _t: self._settle.poke())
            edit.editingFinished.connect(self._settle.now)
        fluent.on_theme_change(self._sync)
        self._sync()

    def _button(self, text: str, icon: str, slot, flat: bool = False) -> PushButton:
        kind = TransparentPushButton if flat else PushButton
        button = kind(fluent.icon(icon), text)
        button.clicked.connect(slot)
        self._buttons.append(button)
        return button

    # ---- state -----------------------------------------------------------------------

    @property
    def is_album(self) -> bool:
        return self.album.isChecked()

    @property
    def busy(self) -> bool:
        return self._probe.busy

    def album_title(self) -> str:
        return (
            self.album_name.text().strip()
            or album_name(self.tracks)
            or t("batch.default_album_name")
        )

    def output_root(self) -> Path | None:
        """Where the tracks' folders go; None leaves each track's next to it."""
        if root := path_from_text(self.out_edit.text()):
            return root
        return album_root(self.tracks, self.album_title()) if self.is_album else None

    def album_cover_path(self) -> Path | None:
        """The cover every track of the album shares."""
        if cover := path_from_text(self.album_cover.text()):
            return cover
        folder = common_folder(self.tracks)
        if folder and (found := self._folder_cover(folder)):
            return found
        return next((track.cover for track in self.tracks if track.cover), None)

    def _folder_cover(self, folder: Path) -> Path | None:
        """`folder_cover`, read again only once the folder changes: it is asked on every redraw."""
        try:
            stamp = folder.stat().st_mtime_ns
        except OSError:
            return None
        cached = self._folder_covers.get(folder)
        if cached is None or cached[0] != stamp:
            cached = self._folder_covers[folder] = (stamp, folder_cover(folder))
        return cached[1]

    def _cover(self, track: BatchTrack) -> Path | None:
        if self.is_album:
            return self.album_cover_path() or track.cover
        return track.cover

    def project(self, track: BatchTrack) -> Project:
        out = track_output_dir(track.audio, self.output_root())
        info, render = track.info, self.atmos_layout()
        tracks = expected_tracks(track.audio, out, info, render) if info else ()
        ttml = self.lyrics_for(track)
        return Project(
            track.audio, self._cover(track), ttml, out, info, tracks,
            probing=track.probing, audio_error=track.error, atmos_layout=render,
        )  # fmt: skip

    def atmos_layout(self) -> SpeakerLayout:
        return atmos_layout(str(self.settings.value(ATMOS_LAYOUT_KEY, "")))

    def projects(self) -> list[Project]:
        return [self.project(track) for track in self.tracks]

    def album_project(self) -> Project | None:
        """The album as one input, for the outputs made once per album."""
        root = self.output_root()
        if not self.is_album or root is None or not self.tracks:
            return None
        # a stand-in audio path so the per-album outputs are named after the album
        stand_in = root / f"{safe_name(self.album_title())}.album"
        return Project(stand_in, self.album_cover_path(), None, root)

    def lyrics_for(self, track: BatchTrack) -> Path | None:
        if track.audio in self._ttml:
            return self._ttml[track.audio]
        return self._auto.get(track.audio)

    def _selected_rows(self) -> list[int]:
        return sorted(self.list.indexOfTopLevelItem(i) for i in self.list.selectedItems())

    def current_project(self) -> Project:
        """The selected track, or the first, for the output pages to show."""
        if not self.tracks:
            return Project()
        rows = self._selected_rows()
        return self.project(self.tracks[rows[0] if rows else 0])

    def set_status(self, index: int, text: str, problem: bool = False) -> None:
        item = self.list.topLevelItem(index)
        item.setText(_STATUS, text)
        item.setToolTip(_STATUS, text)
        if problem:
            item.setForeground(_STATUS, fluent.negative())
        else:
            item.setData(_STATUS, Qt.ItemDataRole.ForegroundRole, None)

    def set_running(self, running: bool) -> None:
        self._running = running
        for widget in (
            *self._buttons, self.album, self.album_name, self.album_cover, self.out_edit
        ):  # fmt: skip
            widget.setEnabled(not running)
        if not running:
            self._update_buttons()

    def start_dir(self) -> str:
        if self.tracks:
            return str(self.tracks[-1].audio.parent)
        return str(self.settings.value("last_dir", str(Path.home())))

    # ---- picking ---------------------------------------------------------------------

    def pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, t("batch.dialog_add_audio"), self.start_dir(), t("source.filter_audio")
        )
        self.add(Path(p) for p in paths)

    def pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, t("batch.dialog_add_folder"), self.start_dir()
        )
        if path:
            self.add([Path(path)], album=True)

    def pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, t("batch.dialog_pick_album_cover"), self.start_dir(), t("source.filter_image")
        )
        if path:
            self.album_cover.setText(path)
            self._settle.now()

    def pick_output(self) -> None:
        start = str(self.output_root() or self.start_dir())
        path = QFileDialog.getExistingDirectory(self, t("batch.dialog_pick_output_root"), start)
        if path:
            self.out_edit.setText(path)
            self._settle.now()

    def pick_ttml(self) -> None:
        rows = self._selected_rows()
        if len(rows) != 1:
            return
        track = self.tracks[rows[0]]
        path, _ = QFileDialog.getOpenFileName(
            self,
            t("batch.dialog_assign_lyrics_for", name=track.audio.name),
            str(track.audio.parent),
            t("source.filter_ttml"),
        )
        if path:
            self._ttml[track.audio] = Path(path)
            self._sync()

    def pick_lyrics(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, t("batch.dialog_import_lyrics"), self.start_dir(), t("source.filter_ttml")
        )
        self.import_lyrics(Path(p) for p in paths)

    def import_lyrics(self, paths: Iterable[Path]) -> int:
        """Add lyric files, folders searched, for the tracks to be matched against."""
        new = [p for p in lyric_match.lyric_files(paths) if p not in self._imported]
        if not new:
            return 0
        self._imported += new
        self._match()
        used = set(self._auto.values()) | {p for p in self._ttml.values() if p}
        left = [p.name for p in new if p not in used]
        message = t(
            "batch.lyrics_imported_summary", imported=len(new), matched=len(new) - len(left)
        )
        if left:
            message += t("batch.lyrics_unmatched_note", names=t("common.list_sep").join(left))
        logger.info(message)
        self._sync()
        return len(new)

    def _set_lyrics(self, rows: Iterable[int], path: Path | None, manual: bool) -> None:
        for row in rows:
            audio = self.tracks[row].audio
            if manual:
                self._ttml[audio] = path
            else:
                self._ttml.pop(audio, None)
        self._match()
        self._sync()

    def _context_menu(self, pos) -> None:
        rows = self._selected_rows()
        if not rows or self._running:
            return
        menu = RoundMenu(parent=self)
        pick = Action(fluent.icon("text-xml"), t("batch.menu_set_lyrics"), menu)
        pick.setEnabled(len(rows) == 1)
        pick.triggered.connect(self.pick_ttml)
        none = Action(fluent.icon("edit-clear"), t("batch.menu_no_lyrics"), menu)
        none.triggered.connect(lambda: self._set_lyrics(rows, None, manual=True))
        auto = Action(fluent.icon("view-refresh"), t("batch.menu_restore_auto"), menu)
        auto.setEnabled(any(self.tracks[r].audio in self._ttml for r in rows))
        auto.triggered.connect(lambda: self._set_lyrics(rows, None, manual=False))
        menu.addActions([pick, none, auto])
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def add(self, paths: Iterable[Path], album: bool = False) -> int:
        """Queue the audio among `paths`, folders included; a folder turns album mode on."""
        paths = list(paths)
        known = {track.audio.resolve() for track in self.tracks}
        added = 0
        # lyrics dropped along with the tracks count as imported, folders' as found
        self._imported += [
            p
            for p in lyric_match.lyric_files(p for p in paths if p.is_file())
            if p not in self._imported
        ]
        lyrics = lyric_match.lyric_files(p for p in paths if p.is_dir())
        for audio in audio_files(paths):
            if audio.resolve() in known:
                continue
            known.add(audio.resolve())
            lyrics += lyric_match.lyrics_near(audio)
            self.tracks.append(BatchTrack(audio))
            self._probe.run(lambda a=audio: inspect(a, True), lambda r, a=audio: self._read(a, r))
            added += 1
        for lyric in dict.fromkeys(lyrics):
            if lyric not in self._nearby and lyric not in self._imported:
                self._nearby.append(lyric)
        self._match()
        if added:
            self.settings.setValue("last_dir", str(self.tracks[-1].audio.parent))
            if (album or any(p.is_dir() for p in paths)) and not self.is_album:
                self.album.setChecked(True)
            logger.info("added %d tracks, %d in all", added, len(self.tracks))
        self._sync()
        return added

    def remove_selected(self) -> None:
        if self._running:
            return  # the run's plan refers to the tracks by row
        rows = set(self._selected_rows())
        self.tracks = [track for i, track in enumerate(self.tracks) if i not in rows]
        # a removed track's lyrics may be the next best for another
        self._match()
        self._sync()

    def clear(self) -> None:
        self._probe.drop()
        self.tracks = []
        self._ttml = {}
        self._imported, self._nearby, self._auto = [], [], {}
        self._sync()

    # ---- reactions -------------------------------------------------------------------

    def _read(self, audio: Path, result: Inspection | Exception) -> None:
        index = next((i for i, track in enumerate(self.tracks) if track.audio == audio), None)
        if index is None:
            return  # removed while it was being read
        if isinstance(result, Exception):
            result = Inspection(audio, error=str(result) or type(result).__name__)
        error = result.error
        if error:
            logger.warning("cannot read %s: %s", audio.name, error)
            # ffmpeg says where it failed first and why last
            error = error.strip().splitlines()[-1].removeprefix(f"{audio}: ")
        self.tracks[index] = replace(
            self.tracks[index], info=result.info, error=error, cover=result.cover, probing=False
        )
        # the title tag is one more name to match lyrics by
        self._match()
        self._sync()

    def _match(self) -> None:
        """Pair the tracks without picked lyrics with the lyric files not picked."""
        picked = set(self._ttml.values())
        pool = [p for p in (*self._imported, *self._nearby) if p not in picked and p.is_file()]
        open_tracks = [
            (track.audio, track.info.title if track.info else "")
            for track in self.tracks
            if track.audio not in self._ttml
        ]
        self._auto = lyric_match.match(open_tracks, pool)

    def layout_changed(self) -> None:
        """The Atmos render layout changed: redraw what each track is rendered to."""
        self._sync()

    def _sync(self) -> None:
        """Order the tracks, redraw the list and tell the window."""
        # by audio, kept on each row: the rows may be stale after a removal or reorder
        selected = {i.data(0, Qt.ItemDataRole.UserRole) for i in self.list.selectedItems()}
        if self.is_album and not self._probe.busy:
            self.tracks = album_order(self.tracks)
        for row in (0, 1):
            self.form.setRowVisible(row, self.is_album)
        self.album_name.setPlaceholderText(
            album_name(self.tracks) or t("batch.album_name_placeholder")
        )
        root = self.output_root()
        self.out_edit.setPlaceholderText(str(root) if root else t("batch.output_root_placeholder"))
        self.list.blockSignals(True)
        # rows are updated in place while the tracks stay the same, so the scroll
        # position and the focus survive every probe result coming in
        same = self.list.topLevelItemCount() == len(self.tracks) and all(
            self.list.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole) == track.audio
            for i, track in enumerate(self.tracks)
        )
        if not same:
            self.list.clear()
        for number, track in enumerate(self.tracks, 1):
            item = self.list.topLevelItem(number - 1) if same else QTreeWidgetItem()
            self._fill_row(item, number, track)
            if not same:
                self.list.addTopLevelItem(item)
                item.setSelected(track.audio in selected)
        self.list.blockSignals(False)
        self._update_buttons()
        self.changed.emit()

    def _fill_row(self, item: QTreeWidgetItem, number: int, track: BatchTrack) -> None:
        info = track.info
        if track.probing:
            detail = t("status.reading")
        elif info is None:
            detail = t("error.cannot_read", error=track.error)
        else:
            detail = info.describe(self.atmos_layout())
        # the list numbers the tracks itself
        stem = lyric_match.without_number(track.audio.stem)
        name = (info.title if info and info.title else "") or stem
        lyrics = self.lyrics_for(track)
        manual = track.audio in self._ttml
        if lyrics:
            shown = lyrics.name
        else:
            shown = t("batch.lyrics_disabled") if manual else t("batch.lyrics_none")
        item.setText(0, f"{number:02d}. {name}")
        item.setText(1, detail)
        item.setText(_LYRICS, shown)
        item.setData(0, Qt.ItemDataRole.UserRole, track.audio)
        item.setToolTip(0, str(track.audio))
        item.setToolTip(1, detail)
        if manual:
            how = t("batch.lyrics_manual")
        else:
            how = t("batch.lyrics_auto") if lyrics else t("batch.lyrics_not_found")
        item.setToolTip(_LYRICS, t("common.with_note", text=lyrics or shown, note=how))
        for column, color in (
            (_LYRICS, None if lyrics else fluent.dim()),
            (1, fluent.negative() if info is None and not track.probing else None),
        ):
            if color is None:
                item.setData(column, Qt.ItemDataRole.ForegroundRole, None)
            else:
                item.setForeground(column, color)

    def _selection_changed(self) -> None:
        self._update_buttons()
        self.changed.emit()

    def _update_buttons(self) -> None:
        count = len(self.list.selectedItems())
        self.lyrics_btn.setEnabled(count == 1)
        self.remove_btn.setEnabled(count > 0)

    # ---- settings --------------------------------------------------------------------

    def save(self) -> None:
        self.settings.setValue("batch_album", self.is_album)

    def restore(self) -> None:
        self.album.setChecked(self.settings.value("batch_album", False, type=bool))
