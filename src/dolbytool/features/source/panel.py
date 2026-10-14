"""The inputs: audio, cover, TTML lyrics and output directory.

Reads the audio in the background and fills in the cover and lyrics it finds
(see `service.inspect`) unless the user picked their own; what was filled in
follows the audio when another one is picked.  All of it becomes a `Project`
for the output panels.
"""

from __future__ import annotations

import logging
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

logger = logging.getLogger(__name__)

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

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        from dolbytool.core.i18n import t

        super().__init__(t("source.title"), parent)
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
        self.cover_preview.setText(t("source.no_cover"))
        self.cover_preview.setToolTip(t("source.click_to_pick_cover"))
        self.cover_preview.clicked.connect(self.pick_cover)
        grid.addWidget(self.cover_preview, 0, 0, 5, 1, Qt.AlignmentFlag.AlignTop)

        self.audio_edit = self._row(
            grid, 0, t("source.audio_label"), t("source.audio_placeholder"), self.pick_audio,
            t("source.audio_tip"),
        )  # fmt: skip
        self.audio_info = widgets.hint_label()
        grid.addWidget(self.audio_info, 1, 2)
        self.audio_info.hide()
        self.cover_edit = self._row(
            grid, 2, t("source.cover_label"), t("source.cover_placeholder"), self.pick_cover,
            t("source.cover_tip"),
        )  # fmt: skip
        self.ttml_edit = self._row(
            grid, 3, t("source.ttml_label"), t("source.ttml_placeholder"), self.pick_ttml,
            t("source.ttml_tip"),
        )  # fmt: skip
        self.out_edit = self._row(
            grid, 4, t("source.output_label"), t("source.output_placeholder"), self.pick_output, t("source.output_tip")
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
        from dolbytool.core.i18n import t

        path, _ = QFileDialog.getOpenFileName(self, t("source.dialog_open_audio"), self.start_dir(), t("source.filter_audio"))
        if path:
            self.set_audio(Path(path))

    def pick_cover(self) -> None:
        from dolbytool.core.i18n import t

        path, _ = QFileDialog.getOpenFileName(self, t("source.dialog_pick_cover"), self.start_dir(), t("source.filter_image"))
        if path:
            self.set_cover(Path(path))

    def pick_ttml(self) -> None:
        from dolbytool.core.i18n import t

        path, _ = QFileDialog.getOpenFileName(self, t("source.dialog_pick_lyrics"), self.start_dir(), t("source.filter_ttml"))
        if path:
            self.set_ttml(Path(path))

    def pick_output(self) -> None:
        from dolbytool.core.i18n import t

        start = str(self.output_dir() or self.start_dir())
        path = QFileDialog.getExistingDirectory(self, t("source.dialog_pick_output"), start)
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
        from dolbytool.core.i18n import t

        audio = self.audio_path()
        self.out_edit.setPlaceholderText(
            str(default_output_dir(audio)) if audio else t("source.output_placeholder")
        )
        stamp = file_stamp(audio) if audio and audio.is_file() else None
        if stamp != self._probed:
            self._probed = stamp
            self._read_audio(audio if stamp else None)
        self._show_cover()
        self.changed.emit()

    def _read_audio(self, audio: Path | None) -> None:
        from dolbytool.core.i18n import t

        self.info, self._error = None, None
        if audio is None:
            self._probe.cancel()
            self._error = t("error.file_not_found") if self.audio_path() else None
            self._set_info(self._error or "")
            self._fill(None, None)
            return
        self.settings.setValue("last_dir", str(audio.parent))
        self._set_info(t("status.reading"))
        self._probe.run(lambda: inspect(audio), self._audio_read)

    def _audio_read(self, result: Inspection | Exception) -> None:
        from dolbytool.core.i18n import t

        if isinstance(result, Exception):
            self._error = str(result) or type(result).__name__
            self._set_info(t("error.cannot_read", error=self._error))
        else:
            self.info, self._error = result.info, result.error
            self._set_info(
                self.info.describe(self.atmos_layout()) if self.info else t("error.cannot_read", error=self._error)
            )
            self._fill(result.cover, result.ttml)
        self._show_cover()
        self.changed.emit()

    def _fill(self, cover: Path | None, ttml: Path | None) -> None:
        from dolbytool.core.i18n import t

        self._auto_cover = self._fill_field(self.cover_edit, self._auto_cover, cover, t("source.static_cover_tip"))
        self._auto_ttml = self._fill_field(self.ttml_edit, self._auto_ttml, ttml, t("section.lyrics"))

    def _fill_field(
        self, edit: QLineEdit, auto: Path | None, found: Path | None, what: str
    ) -> Path | None:
        """Show `found` in `edit` unless the user set it themselves; returns what is filled in now."""
        from dolbytool.core.i18n import t

        current = path_from_text(edit.text())
        if current is not None and current != auto:
            return auto
        if found != current:
            edit.setText(str(found) if found else "")
            if found:
                logger.info(t("source.auto_used_log", what=what, name=found.name))
        return found

    def _show_cover(self) -> None:
        from dolbytool.core.i18n import t

        cover = self.cover_path()
        stamp = file_stamp(cover)
        if stamp == self._shown_cover:
            return
        self._shown_cover = stamp
        self.cover_preview.setPixmap(QPixmap())
        if cover is None or not cover.is_file():
            self._thumb.cancel()
            self.cover_preview.setText(t("error.file_not_found") if cover else t("source.no_cover"))
            self.cover_preview.setToolTip(t("source.click_to_pick_cover"))
            return
        self.cover_preview.setText(t("status.reading"))
        side = _COVER_SIDE - 4
        self._thumb.run(lambda: _thumbnail(cover, side), lambda r: self._cover_read(cover, r))

    def _cover_read(self, cover: Path, result: tuple[QImage, bool] | Exception) -> None:
        from dolbytool.core.i18n import t

        image, moving = result if not isinstance(result, Exception) else (QImage(), False)
        if image.isNull():
            self.cover_preview.setText(t("error.cannot_read", error=""))
            if isinstance(result, Exception):
                logger.warning("封面无法读取：%s", result)
            return
        self.cover_preview.setPixmap(QPixmap.fromImage(image))
        kind = t("source.motion_cover_tip") if moving else t("source.static_cover_tip")
        self.cover_preview.setToolTip(f"{kind}\n{cover}\n{t('source.click_to_change')}")
