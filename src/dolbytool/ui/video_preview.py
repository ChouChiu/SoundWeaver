"""A looping video player dialog for render previews."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QIcon
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import QDialog, QHBoxLayout, QPushButton, QVBoxLayout, QWidget


class VideoPreview(QDialog):
    """Plays one file on a loop; reopening it with another file replaces the video."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("预览")
        self.resize(1000, 620)
        self.video = QVideoWidget()
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(self.video)
        self.player.setAudioOutput(self.audio)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.toggle = QPushButton()
        self.toggle.clicked.connect(self._toggle)
        self.player.playbackStateChanged.connect(self._sync)
        close = QPushButton(QIcon.fromTheme("window-close"), "关闭")
        close.clicked.connect(self.close)
        buttons = QHBoxLayout()
        buttons.addWidget(self.toggle)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
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
            QIcon.fromTheme("media-playback-pause" if playing else "media-playback-start")
        )
        self.toggle.setText("暂停" if playing else "播放")

    def closeEvent(self, event) -> None:
        self.player.stop()
        super().closeEvent(event)
