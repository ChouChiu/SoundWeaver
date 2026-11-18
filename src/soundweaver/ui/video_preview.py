"""A looping video player dialog for render previews."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import PrimaryPushButton, PushButton
from qframelesswindow import FramelessDialog, StandardTitleBar

from soundweaver.core.i18n import t
from soundweaver.ui import fluent, widgets


class VideoPreview(FramelessDialog):
    """Plays one file on a loop; reopening it with another file replaces the video."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setTitleBar(StandardTitleBar(self))
        self.titleBar.raise_()
        self.setWindowTitle(t("action.preview"))
        self.resize(1000, 620)
        self.video = QVideoWidget(self)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        self.player.setAudioOutput(self.audio)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.toggle = PrimaryPushButton(self)
        self.toggle.clicked.connect(self._toggle)
        self.player.playbackStateChanged.connect(self._sync)
        close = PushButton(fluent.icon("window-close"), t("action.close"), self)
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.setSpacing(widgets.INLINE_SPACING)
        buttons.addWidget(self.toggle)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        left, top, right, bottom = widgets.PAGE_MARGINS
        layout.setContentsMargins(left, self.titleBar.height() + top, right, bottom)
        layout.setSpacing(widgets.SECTION_SPACING)
        layout.addWidget(self.video, 1)
        layout.addLayout(buttons)
        self._sync()

    def play(self, path: Path) -> None:
        self.player.stop()
        # a fresh URL each time: the same path rewritten would otherwise stay cached
        self.player.setSource(QUrl())
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.show()
        self.raise_()
        self.player.play()

    def _toggle(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _sync(self) -> None:
        playing = self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        self.toggle.setIcon(
            fluent.icon("media-playback-pause" if playing else "media-playback-start")
        )
        self.toggle.setText(t("action.pause") if playing else t("action.play"))

    def closeEvent(self, event) -> None:
        self.player.stop()
        super().closeEvent(event)
