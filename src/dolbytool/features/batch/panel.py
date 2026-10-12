"""The batch inputs: a list of tracks, optionally treated as one album.

Each track is read in the background like the single source; its cover is
the album's (picked, or the motion artwork or image in the folder), else its
embedded one or the art in its folder.  Its lyrics are one picked for it,
else the lyric file found nearby or imported whose name fits it (see
`batch.lyrics`).  Every track becomes its own `Project` for the output
panels to plan, with an output folder of its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
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
    BodyLabel,
    LineEdit,
    PushButton,
    RoundMenu,
    SwitchButton,
    TransparentPushButton,
    TreeWidget,
)

from dolbytool.core.project import ATMOS_LAYOUT_KEY, Project, expected_tracks
from dolbytool.core.speakers import SpeakerLayout, atmos_layout
from dolbytool.features.batch import lyrics as lyric_match
from dolbytool.features.batch.service import (
    BatchTrack,
    album_name,
    album_order,
    album_root,
    audio_files,
    common_folder,
    folder_cover,
    safe_name,
    track_output_dir,
)
from dolbytool.features.source.panel import AUDIO_FILTER, IMAGE_FILTER, TTML_FILTER
from dolbytool.features.source.service import Inspection, inspect
from dolbytool.ui import fluent, widgets
from dolbytool.ui.background import Each

_LYRICS, _STATUS = 2, 3
_SETTLE_MS = 400
ALBUM_TIP = (
    "按碟号与音轨号排序，所有音轨共用专辑封面，\n"
    "输出统一保存到「<专辑名>_stems/<音轨名>」，渐变图整张专辑只生成一张"
)


def _path(text: str) -> Path | None:
    text = text.strip()
    return Path(text).expanduser() if text else None


class BatchPanel(widgets.Card):
    changed = Signal()
    log = Signal(str)

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__("批量处理", parent)
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

        self.list = TreeWidget()
        self.list.setColumnCount(4)
        self.list.setHeaderLabels(["音轨", "信息", "歌词", "状态"])
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

        self._buttons: list[QWidget] = []
        add_files = self._button("添加文件…", "list-add", self.pick_files)
        add_folder = self._button("添加文件夹…", "folder-add", self.pick_folder)
        import_lyrics = self._button("导入歌词…", "document-import", self.pick_lyrics)
        import_lyrics.setToolTip(
            "一次选择多个 .ttml，按文件名、音频标题与歌词内的曲名自动分配给对应音轨"
        )
        self.lyrics_btn = self._button("指定歌词…", "text-xml", self.pick_ttml)
        self.remove_btn = self._button("移除", "list-remove", self.remove_selected)
        clear = self._button("清空", "edit-clear-all", self.clear, flat=True)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for button in (add_files, add_folder, import_lyrics, self.lyrics_btn, self.remove_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(clear)

        self.album = SwitchButton("专辑模式")
        self.album.setOnText("专辑模式")
        self.album.setToolTip(ALBUM_TIP)
        self.album.checkedChanged.connect(lambda _on: self._sync())
        self.album_name = LineEdit()
        self.album_name.setClearButtonEnabled(True)
        self.album_name.setPlaceholderText("留空则使用音频标签中的专辑名")
        self.album_cover, cover_btn, cover_row = widgets.browse_row(
            "图片或动态封面；留空则依次使用文件夹内的动态封面、封面图与内嵌封面", self.pick_cover
        )
        self.out_edit, out_btn, out_row = widgets.browse_row("", self.pick_output)
        self._buttons += [cover_btn, out_btn]
        self.album_rows = QWidget()
        album_form = QFormLayout(self.album_rows)
        album_form.setContentsMargins(0, 0, 0, 0)
        album_form.setVerticalSpacing(8)
        album_form.addRow(BodyLabel("专辑名："), self.album_name)
        album_form.addRow(BodyLabel("专辑封面："), cover_row)
        options = QFormLayout()
        options.addRow(BodyLabel("输出根目录："), out_row)

        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.album)
        layout.addWidget(self.album_rows)
        layout.addLayout(options)
        self.set_body(layout)

        self._settle = QTimer(self, singleShot=True, interval=_SETTLE_MS)
        self._settle.timeout.connect(self._sync)
        for edit in (self.album_name, self.album_cover, self.out_edit):
            edit.textChanged.connect(lambda _t: self._settle.start())
            edit.editingFinished.connect(self._flush)
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
        return self.album_name.text().strip() or album_name(self.tracks) or "专辑"

    def output_root(self) -> Path | None:
        """Where the tracks' folders go; None leaves each track's next to it."""
        if root := _path(self.out_edit.text()):
            return root
        return album_root(self.tracks, self.album_title()) if self.is_album else None

    def album_cover_path(self) -> Path | None:
        """The cover every track of the album shares."""
        if cover := _path(self.album_cover.text()):
            return cover
        folder = common_folder(self.tracks)
        if folder and (found := folder_cover(folder)):
            return found
        return next((t.cover for t in self.tracks if t.cover), None)

    def _cover(self, track: BatchTrack) -> Path | None:
        if self.is_album:
            return self.album_cover_path() or track.cover
        return track.cover or folder_cover(track.audio.parent)

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
        return [self.project(t) for t in self.tracks]

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

    def current_index(self) -> int:
        items = self.list.selectedItems()
        return self.list.indexOfTopLevelItem(items[0]) if items else 0

    def current_project(self) -> Project:
        """The selected track, or the first, for the output pages to show."""
        if not self.tracks:
            return Project()
        return self.project(self.tracks[min(self.current_index(), len(self.tracks) - 1)])

    def set_status(self, index: int, text: str, problem: bool = False) -> None:
        item = self.list.topLevelItem(index)
        if item is None:
            return
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
        paths, _ = QFileDialog.getOpenFileNames(self, "添加音频", self.start_dir(), AUDIO_FILTER)
        self.add(Path(p) for p in paths)

    def pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "添加文件夹（专辑）", self.start_dir())
        if path:
            self.add([Path(path)], album=True)

    def pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择专辑封面", self.start_dir(), IMAGE_FILTER)
        if path:
            self.album_cover.setText(path)
            self._flush()

    def pick_output(self) -> None:
        start = str(self.output_root() or self.start_dir())
        path = QFileDialog.getExistingDirectory(self, "选择输出根目录", start)
        if path:
            self.out_edit.setText(path)
            self._flush()

    def pick_ttml(self) -> None:
        items = self.list.selectedItems()
        if len(items) != 1:
            return
        track = self.tracks[self.list.indexOfTopLevelItem(items[0])]
        path, _ = QFileDialog.getOpenFileName(
            self, f"为 {track.audio.name} 指定歌词", str(track.audio.parent), TTML_FILTER
        )
        if path:
            self._ttml[track.audio] = Path(path)
            self._sync()

    def pick_lyrics(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "导入歌词（自动分配到音轨）", self.start_dir(), TTML_FILTER
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
        message = f"已导入 {len(new)} 份歌词，{len(new) - len(left)} 份已分配"
        if left:
            message += f"；未能匹配：{'、'.join(left)}，可选中音轨后用「指定歌词」手动指定"
        self.log.emit(message)
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
        rows = sorted({self.list.indexOfTopLevelItem(i) for i in self.list.selectedItems()})
        if not rows or self._running:
            return
        menu = RoundMenu(parent=self)
        pick = Action(fluent.icon("text-xml"), "指定歌词…", menu)
        pick.setEnabled(len(rows) == 1)
        pick.triggered.connect(self.pick_ttml)
        none = Action(fluent.icon("edit-clear"), "不使用歌词", menu)
        none.triggered.connect(lambda: self._set_lyrics(rows, None, manual=True))
        auto = Action(fluent.icon("view-refresh"), "恢复自动匹配", menu)
        auto.setEnabled(any(self.tracks[r].audio in self._ttml for r in rows))
        auto.triggered.connect(lambda: self._set_lyrics(rows, None, manual=False))
        menu.addActions([pick, none, auto])
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def add(self, paths: Iterable[Path], album: bool = False) -> int:
        """Queue the audio among `paths`, folders included; a folder turns album mode on."""
        paths = list(paths)
        known = {t.audio.resolve() for t in self.tracks}
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
            self.log.emit(f"已加入 {added} 首音轨，共 {len(self.tracks)} 首")
        self._sync()
        return added

    def remove_selected(self) -> None:
        rows = {self.list.indexOfTopLevelItem(i) for i in self.list.selectedItems()}
        self.tracks = [t for i, t in enumerate(self.tracks) if i not in rows]
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
        index = next((i for i, t in enumerate(self.tracks) if t.audio == audio), None)
        if index is None:
            return  # removed while it was being read
        if isinstance(result, Exception):
            result = Inspection(audio, error=str(result) or type(result).__name__)
        error = result.error
        if error:
            self.log.emit(f"无法读取 {audio.name}：{error}")
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
            (t.audio, t.info.title if t.info else "")
            for t in self.tracks
            if t.audio not in self._ttml
        ]
        self._auto = lyric_match.match(open_tracks, pool)

    def _flush(self) -> None:
        self._settle.stop()
        self._sync()

    def layout_changed(self) -> None:
        """The Atmos render layout changed: redraw what each track is rendered to."""
        self._sync()

    def _sync(self) -> None:
        """Order the tracks, redraw the list and tell the window."""
        selected = {
            self.tracks[self.list.indexOfTopLevelItem(i)].audio
            for i in self.list.selectedItems()
            if self.list.indexOfTopLevelItem(i) < len(self.tracks)
        }
        if self.is_album and not self._probe.busy:
            self.tracks = album_order(self.tracks)
        self.album_rows.setVisible(self.is_album)
        self.album_name.setPlaceholderText(
            album_name(self.tracks) or "留空则使用音频标签中的专辑名"
        )
        root = self.output_root()
        self.out_edit.setPlaceholderText(
            str(root) if root else "留空则保存到各音频旁的「<音频名>_stems」"
        )
        self.list.blockSignals(True)
        self.list.clear()
        for number, track in enumerate(self.tracks, 1):
            info = track.info
            if track.probing:
                detail = "正在读取…"
            elif info is None:
                detail = f"无法读取：{track.error}"
            else:
                detail = info.describe(self.atmos_layout())
            # the list numbers the tracks itself
            stem = lyric_match.without_number(track.audio.stem)
            name = (info.title if info and info.title else "") or stem
            lyrics = self.lyrics_for(track)
            manual = track.audio in self._ttml
            shown = lyrics.name if lyrics else ("不使用" if manual else "无")
            item = QTreeWidgetItem([f"{number:02d}. {name}", detail, shown, ""])
            item.setToolTip(0, str(track.audio))
            item.setToolTip(1, detail)
            how = "手动指定" if manual else "自动匹配" if lyrics else "未找到，可导入或手动指定"
            item.setToolTip(_LYRICS, f"{lyrics or shown}\n（{how}）")
            if not lyrics:
                item.setForeground(_LYRICS, fluent.dim())
            if info is None and not track.probing:
                item.setForeground(1, fluent.negative())
            self.list.addTopLevelItem(item)
            item.setSelected(track.audio in selected)
        self.list.blockSignals(False)
        self._update_buttons()
        self.changed.emit()

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
