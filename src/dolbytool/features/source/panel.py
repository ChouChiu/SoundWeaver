"""The inputs: audio, cover, TTML lyrics and output directory.

Reads the audio in the background, takes its embedded cover and a same-named
.ttml when the user has not picked their own, and turns all of it into a
`Project` for the output panels.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QToolButton,
    QWidget,
)

from dolbytool.core.audio import AudioInfo
from dolbytool.core.paths import FileStamp, file_stamp
from dolbytool.core.project import Project, default_output_dir, expected_tracks
from dolbytool.features.source.service import Inspection, inspect, kind_of, sibling_ttml
from dolbytool.ui import widgets
from dolbytool.ui.background import Latest

AUDIO_FILTER = (
    "音频文件 (*.m4a *.mp4 *.mka *.mkv *.ec3 *.eac3 *.ac3 *.thd *.wav *.flac);;所有文件 (*)"
)
IMAGE_FILTER = "图片 (*.png *.jpg *.jpeg *.webp *.bmp);;所有文件 (*)"
TTML_FILTER = "TTML 歌词 (*.ttml *.xml);;所有文件 (*)"
OUTPUT_PLACEHOLDER = "默认保存至：<音频目录>/<音频名>_stems"
# typing in a path field settles this long before it is acted on
_SETTLE_MS = 400


def _path(text: str) -> Path | None:
    text = text.strip()
    return Path(text).expanduser() if text else None


class SourcePanel(QGroupBox):
    changed = Signal()
    log = Signal(str)

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__("源文件", parent)
        self.settings = settings
        self.info: AudioInfo | None = None
        self._error: str | None = None
        self._probed: FileStamp | None = None
        self._auto_cover: Path | None = None
        self._shown_cover: FileStamp | None = None
        self._probe = Latest(self)
        self._buttons: list[QToolButton] = []

        grid = QGridLayout(self)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        self.cover_preview = widgets.preview_label(text="无封面", side=112)
        grid.addWidget(self.cover_preview, 0, 0, 5, 1, Qt.AlignmentFlag.AlignTop)

        self.audio_edit = self._row(grid, 0, "音频(&U)", "支持 m4a / eac3 / wav / flac 等格式", self.pick_audio)
        self.audio_info = widgets.hint_label()
        grid.addWidget(self.audio_info, 1, 2)
        self.cover_edit = self._row(
            grid, 2, "封面(&C)", "封面图片（内嵌封面将自动提取）", self.pick_cover
        )
        self.ttml_edit = self._row(grid, 3, "歌词(&L)", "TTML 逐字歌词文件", self.pick_ttml)
        self.out_edit = self._row(grid, 4, "输出(&O)", OUTPUT_PLACEHOLDER, self.pick_output)
        grid.setColumnStretch(2, 1)

        # edits settle before anything reacts, so typing a path probes it once
        self._settle = QTimer(self, singleShot=True, interval=_SETTLE_MS)
        self._settle.timeout.connect(self._fields_changed)
        for edit in (self.audio_edit, self.cover_edit, self.ttml_edit, self.out_edit):
            edit.textChanged.connect(lambda _t: self._settle.start())
            edit.editingFinished.connect(self._flush)

    def _row(self, grid: QGridLayout, row: int, label: str, placeholder: str, slot) -> QLineEdit:
        edit, button, line = widgets.browse_row(placeholder, slot)
        self._buttons.append(button)
        buddy = QLabel(label)
        buddy.setBuddy(edit)
        grid.addWidget(buddy, row, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addLayout(line, row, 2)
        return edit

    # ---- state -----------------------------------------------------------------------

    def audio_path(self) -> Path | None:
        return _path(self.audio_edit.text())

    def cover_path(self) -> Path | None:
        return _path(self.cover_edit.text())

    def ttml_path(self) -> Path | None:
        return _path(self.ttml_edit.text())

    def output_dir(self) -> Path | None:
        if out := _path(self.out_edit.text()):
            return out
        if audio := self.audio_path():
            return default_output_dir(audio)
        ttml = self.ttml_path()
        return ttml.parent if ttml else None

    def project(self) -> Project:
        audio, out, info = self.audio_path(), self.output_dir(), self.info
        tracks = expected_tracks(audio, out, info.channels) if audio and out and info else ()
        return Project(
            audio, self.cover_path(), self.ttml_path(), out, info, tracks,
            probing=self._probe.busy, audio_error=self._error,
        )  # fmt: skip

    def start_dir(self) -> str:
        audio = self.audio_path()
        if audio and audio.parent.is_dir():
            return str(audio.parent)
        return str(self.settings.value("last_dir", str(Path.home())))

    def set_running(self, running: bool) -> None:
        for widget in (self.audio_edit, self.cover_edit, self.ttml_edit, self.out_edit, *self._buttons):
            widget.setEnabled(not running)

    # ---- picking ---------------------------------------------------------------------

    def pick_audio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "打开音频", self.start_dir(), AUDIO_FILTER)
        if path:
            self.set_audio(Path(path))

    def pick_cover(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择封面", self.start_dir(), IMAGE_FILTER)
        if path:
            self.set_cover(Path(path))

    def pick_ttml(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择歌词", self.start_dir(), TTML_FILTER)
        if path:
            self.set_ttml(Path(path))

    def pick_output(self) -> None:
        start = str(self.output_dir() or self.start_dir())
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", start)
        if path:
            self.out_edit.setText(path)
            self._flush()

    def set_audio(self, path: Path) -> None:
        self.audio_edit.setText(str(path))
        self._flush()

    def set_cover(self, path: Path) -> None:
        self.cover_edit.setText(str(path))
        self._flush()

    def set_ttml(self, path: Path) -> None:
        self.ttml_edit.setText(str(path))
        self._flush()

    def drop(self, paths: Iterable[Path]) -> bool:
        """Route dropped files to the field their type belongs to; False if none fit."""
        setters = {"audio": self.set_audio, "cover": self.set_cover, "ttml": self.set_ttml}
        used = False
        for path in paths:
            if kind := kind_of(path):
                setters[kind](path)
                used = True
        return used

    # ---- reactions -------------------------------------------------------------------

    def _flush(self) -> None:
        self._settle.stop()
        self._fields_changed()

    def _fields_changed(self) -> None:
        audio = self.audio_path()
        self.out_edit.setPlaceholderText(
            str(default_output_dir(audio)) if audio else OUTPUT_PLACEHOLDER
        )
        stamp = file_stamp(audio) if audio and audio.is_file() else None
        if stamp != self._probed:
            self._probed = stamp
            self._read_audio(audio if stamp else None)
        self._show_cover()
        self.changed.emit()

    def _read_audio(self, audio: Path | None) -> None:
        self.info, self._error = None, None
        if audio is None:
            self._probe.cancel()
            path = self.audio_path()
            self._error = "文件不存在" if path else None
            self.audio_info.setText(self._error or "")
            return
        self.settings.setValue("last_dir", str(audio.parent))
        if not self.ttml_path() and (sibling := sibling_ttml(audio)):
            self.ttml_edit.setText(str(sibling))
        cover = self.cover_path()
        want_cover = cover is None or cover == self._auto_cover
        self.audio_info.setText("正在读取…")
        self._probe.run(lambda: inspect(audio, want_cover), self._audio_read)

    def _audio_read(self, result: Inspection | Exception) -> None:
        if isinstance(result, Exception):
            self._error = str(result) or type(result).__name__
        else:
            self.info, self._error = result.info, result.error
            cover = self.cover_path()
            if result.cover and (cover is None or cover == self._auto_cover):
                self._auto_cover = result.cover
                self.cover_edit.setText(str(result.cover))
                self.log.emit(f"已提取内嵌封面：{result.cover.name}")
        self.audio_info.setText(self.info.describe() if self.info else f"无法读取：{self._error}")
        self._show_cover()
        self.changed.emit()

    def _show_cover(self) -> None:
        cover = self.cover_path()
        stamp = file_stamp(cover)
        if stamp == self._shown_cover:
            return
        self._shown_cover = stamp
        pixmap = QPixmap(str(cover)) if cover and cover.is_file() else QPixmap()
        if pixmap.isNull():
            self.cover_preview.setPixmap(QPixmap())
            self.cover_preview.setText(("无法读取" if cover.is_file() else "文件不存在") if cover else "无封面")
            return
        self.cover_preview.setPixmap(
            pixmap.scaled(
                self.cover_preview.size() - QSize(4, 4),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.cover_preview.setToolTip(str(cover))
