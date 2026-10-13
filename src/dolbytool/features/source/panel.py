"""The inputs: audio, cover, TTML lyrics and output directory.

Reads the audio in the background and fills in the cover and lyrics it finds
(see `service.inspect`) unless the user picked their own; what was filled in
follows the audio when another one is picked.  All of it becomes a `Project`
for the output panels.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QImage, QMouseEvent, QPixmap
from PySide6.QtWidgets import QFileDialog, QGridLayout, QLineEdit, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, ToolButton

from dolbytool.core import motion
from dolbytool.core.audio import AudioInfo
from dolbytool.core.paths import FileStamp, file_stamp, path_from_text
from dolbytool.core.project import ATMOS_LAYOUT_KEY, Project, default_output_dir, expected_tracks
from dolbytool.core.speakers import SpeakerLayout, atmos_layout
from dolbytool.features.source.service import Inspection, inspect, kind_of
from dolbytool.ui import widgets
from dolbytool.ui.background import Latest

AUDIO_FILTER = (
    "音频文件 (*.m4a *.mp4 *.mka *.mkv *.ec3 *.eac3 *.ac3 *.thd *.wav *.flac);;所有文件 (*)"
)
IMAGE_FILTER = (
    "图片或动态封面 (*.png *.jpg *.jpeg *.webp *.bmp *.mp4 *.m4v *.mov *.webm *.mkv);;所有文件 (*)"
)
TTML_FILTER = "TTML 歌词 (*.ttml *.xml);;所有文件 (*)"
OUTPUT_PLACEHOLDER = "默认保存至：<音频目录>/<音频名>_stems"
# typing in a path field settles this long before it is acted on
_SETTLE_MS = 400
_COVER_SIDE = 112


class _CoverPreview(CaptionLabel):
    """The cover as a thumbnail; clicking it picks another."""

    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setText("无封面\n点击选择")
        self.setFixedSize(_COVER_SIDE, _COVER_SIDE)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(widgets.PREVIEW_STYLE)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def _thumbnail(cover: Path, side: int) -> tuple[QImage, bool]:
    """`cover` scaled to fit `side` (motion artwork by its first frame), and whether it moves."""
    moving = motion.is_motion(cover)
    image = QImage(str(motion.still(cover)))
    if not image.isNull():
        image = image.scaled(
            side,
            side,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    return image, moving


class SourcePanel(widgets.Card):
    changed = Signal()
    log = Signal(str)

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__("源文件", parent)
        self.settings = settings
        self.info: AudioInfo | None = None
        self._error: str | None = None
        self._probed: FileStamp | None = None
        # what was filled in for the audio, replaced along with it unless the user changed it
        self._auto_cover: Path | None = None
        self._auto_ttml: Path | None = None
        self._shown_cover: FileStamp | None = None
        self._probe = Latest(self)
        self._thumb = Latest(self)
        self._buttons: list[ToolButton] = []

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        self.cover_preview = _CoverPreview()
        self.cover_preview.clicked.connect(self.pick_cover)
        grid.addWidget(self.cover_preview, 0, 0, 5, 1, Qt.AlignmentFlag.AlignTop)

        self.audio_edit = self._row(
            grid, 0, "音频(&U)", "拖入或选择 m4a / eac3 / wav / flac 等音频", self.pick_audio,
            "选择音频（Ctrl+O）",
        )  # fmt: skip
        self.audio_info = widgets.hint_label()
        grid.addWidget(self.audio_info, 1, 2)
        self.audio_info.hide()
        self.cover_edit = self._row(
            grid, 2, "封面(&C)", "自动使用内嵌封面或同文件夹的封面图", self.pick_cover,
            "选择封面图片或动态封面（Ctrl+Shift+O）",
        )  # fmt: skip
        self.ttml_edit = self._row(
            grid, 3, "歌词(&L)", "自动查找同名或同文件夹的 TTML 逐字歌词", self.pick_ttml,
            "选择 TTML 歌词（Ctrl+L）",
        )  # fmt: skip
        self.out_edit = self._row(
            grid, 4, "输出(&O)", OUTPUT_PLACEHOLDER, self.pick_output, "选择输出目录"
        )
        grid.setColumnStretch(2, 1)
        self.set_body(grid)

        # edits settle before anything reacts, so typing a path probes it once
        self._settle = QTimer(self, singleShot=True, interval=_SETTLE_MS)
        self._settle.timeout.connect(self._fields_changed)
        for edit in (self.audio_edit, self.cover_edit, self.ttml_edit, self.out_edit):
            edit.textChanged.connect(lambda _t: self._settle.start())
            edit.editingFinished.connect(self._flush)

    def _row(
        self, grid: QGridLayout, row: int, label: str, placeholder: str, slot, tip: str
    ) -> QLineEdit:
        edit, button, line = widgets.browse_row(placeholder, slot, tip)
        self._buttons.append(button)
        buddy = BodyLabel(label)
        buddy.setBuddy(edit)
        grid.addWidget(buddy, row, 1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        grid.addLayout(line, row, 2)
        return edit

    def _set_info(self, text: str) -> None:
        # an empty line would still leave a gap between the audio and cover rows
        self.audio_info.setText(text)
        self.audio_info.setVisible(bool(text))

    # ---- state -----------------------------------------------------------------------

    def audio_path(self) -> Path | None:
        return path_from_text(self.audio_edit.text())

    def cover_path(self) -> Path | None:
        return path_from_text(self.cover_edit.text())

    def ttml_path(self) -> Path | None:
        return path_from_text(self.ttml_edit.text())

    def output_dir(self) -> Path | None:
        if out := path_from_text(self.out_edit.text()):
            return out
        if audio := self.audio_path():
            return default_output_dir(audio)
        ttml = self.ttml_path()
        return ttml.parent if ttml else None

    def atmos_layout(self) -> SpeakerLayout:
        return atmos_layout(str(self.settings.value(ATMOS_LAYOUT_KEY, "")))

    def project(self) -> Project:
        audio, out, info = self.audio_path(), self.output_dir(), self.info
        render = self.atmos_layout()
        tracks = expected_tracks(audio, out, info, render) if audio and out and info else ()
        return Project(
            audio, self.cover_path(), self.ttml_path(), out, info, tracks,
            probing=self._probe.busy, audio_error=self._error, atmos_layout=render,
        )  # fmt: skip

    def layout_changed(self) -> None:
        """The Atmos render layout changed: say what the audio is rendered to now."""
        if self.info is not None:
            self._set_info(self.info.describe(self.atmos_layout()))

    def start_dir(self) -> str:
        audio = self.audio_path()
        if audio and audio.parent.is_dir():
            return str(audio.parent)
        return str(self.settings.value("last_dir", str(Path.home())))

    def set_running(self, running: bool) -> None:
        for widget in (
            self.audio_edit, self.cover_edit, self.ttml_edit, self.out_edit,
            self.cover_preview, *self._buttons,
        ):  # fmt: skip
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
            self._error = "文件不存在" if self.audio_path() else None
            self._set_info(self._error or "")
            self._fill(None, None)
            return
        self.settings.setValue("last_dir", str(audio.parent))
        self._set_info("正在读取…")
        self._probe.run(lambda: inspect(audio), self._audio_read)

    def _audio_read(self, result: Inspection | Exception) -> None:
        if isinstance(result, Exception):
            self._error = str(result) or type(result).__name__
            self._set_info(f"无法读取：{self._error}")
        else:
            self.info, self._error = result.info, result.error
            self._set_info(
                self.info.describe(self.atmos_layout()) if self.info else f"无法读取：{self._error}"
            )
            self._fill(result.cover, result.ttml)
        self._show_cover()
        self.changed.emit()

    def _fill(self, cover: Path | None, ttml: Path | None) -> None:
        self._auto_cover = self._fill_field(self.cover_edit, self._auto_cover, cover, "封面")
        self._auto_ttml = self._fill_field(self.ttml_edit, self._auto_ttml, ttml, "歌词")

    def _fill_field(
        self, edit: QLineEdit, auto: Path | None, found: Path | None, what: str
    ) -> Path | None:
        """Show `found` in `edit` unless the user set it themselves; returns what is filled in now."""
        current = path_from_text(edit.text())
        if current is not None and current != auto:
            return auto
        if found != current:
            edit.setText(str(found) if found else "")
            if found:
                self.log.emit(f"已自动使用{what}：{found.name}")
        return found

    def _show_cover(self) -> None:
        cover = self.cover_path()
        stamp = file_stamp(cover)
        if stamp == self._shown_cover:
            return
        self._shown_cover = stamp
        self.cover_preview.setPixmap(QPixmap())
        if cover is None or not cover.is_file():
            self._thumb.cancel()
            self.cover_preview.setText("文件不存在" if cover else "无封面\n点击选择")
            self.cover_preview.setToolTip("点击选择封面")
            return
        self.cover_preview.setText("正在读取…")
        side = _COVER_SIDE - 4
        self._thumb.run(lambda: _thumbnail(cover, side), lambda r: self._cover_read(cover, r))

    def _cover_read(self, cover: Path, result: tuple[QImage, bool] | Exception) -> None:
        image, moving = result if not isinstance(result, Exception) else (QImage(), False)
        if image.isNull():
            self.cover_preview.setText("无法读取")
            if isinstance(result, Exception):
                self.log.emit(f"封面无法读取：{result}")
            return
        self.cover_preview.setPixmap(QPixmap.fromImage(image))
        kind = "动态封面（播放器视频中循环播放）" if moving else "封面"
        self.cover_preview.setToolTip(f"{kind}\n{cover}\n点击更换")
