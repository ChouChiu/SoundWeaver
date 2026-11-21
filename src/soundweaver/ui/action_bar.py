"""Persistent export actions, current status, and run progress."""

from __future__ import annotations

from PySide6.QtCore import QSize, Signal
from PySide6.QtWidgets import QBoxLayout, QHBoxLayout, QLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, PrimaryPushButton, ProgressBar, PushButton

from soundweaver.core.i18n import t
from soundweaver.ui import fluent, widgets


class ActionBar(widgets.Card):
    """Keep the primary action visible while long status text wraps."""

    open_dir_requested = Signal()
    cancel_requested = Signal()
    start_requested = Signal()

    def __init__(self, start_tooltip: str, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        self._body = QHBoxLayout()
        self._body.setSpacing(widgets.FIELD_SPACING)
        self._body.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

        info = QWidget(self)
        text = QVBoxLayout(info)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(4)
        self.summary = BodyLabel()
        self.summary.setWordWrap(True)
        self.summary.setMinimumWidth(1)
        self.status = widgets.hint_label()
        self.status.hide()
        text.addWidget(self.summary)
        text.addWidget(self.status)

        self.progress = ProgressBar()
        self.progress.setAccessibleName(t("tasks.col_progress"))
        self.progress.hide()
        text.addWidget(self.progress)
        self._body.addWidget(info, 1)

        self.open_dir_btn = PushButton(fluent.icon("folder-open"), t("action.open_output_dir"))
        self.open_dir_btn.clicked.connect(self.open_dir_requested)
        self.cancel_btn = PushButton(fluent.icon("process-stop"), t("action.cancel"))
        self.cancel_btn.clicked.connect(self.cancel_requested)
        self.cancel_btn.hide()
        self.start_btn = PrimaryPushButton(fluent.icon("media-playback-start"), t("action.start"))
        self.start_btn.setToolTip(start_tooltip)
        self.start_btn.setMinimumWidth(112)
        self.start_btn.clicked.connect(self.start_requested)

        self._commands = QWidget(self)
        buttons = QHBoxLayout(self._commands)
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(widgets.INLINE_SPACING)
        buttons.addStretch(1)
        for button in (self.open_dir_btn, self.cancel_btn, self.start_btn):
            buttons.addWidget(button)
        self._body.addWidget(self._commands)
        self.set_body(self._body)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        if self._body.direction() == QBoxLayout.Direction.LeftToRight:
            left, _, right, _ = widgets.CARD_MARGINS
            hint.setWidth(self._commands.minimumSizeHint().width() + left + right)
        return hint

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self) -> None:
        left, _, right, _ = widgets.CARD_MARGINS
        text_width = 200
        available = self.width() - left - right
        inline = text_width + self._body.spacing() + self._commands.sizeHint().width()
        direction = (
            QBoxLayout.Direction.LeftToRight if available >= inline else QBoxLayout.Direction.TopToBottom
        )
        if self._body.direction() != direction:
            self._body.setDirection(direction)
            self.updateGeometry()

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)
        self.summary.setVisible(bool(text))

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_ready(self, *, can_open_dir: bool, can_start: bool) -> None:
        self.open_dir_btn.setEnabled(can_open_dir)
        self.start_btn.setEnabled(can_start)

    def set_running(self, running: bool) -> None:
        self.start_btn.setVisible(not running)
        self.cancel_btn.setVisible(running)
        self.cancel_btn.setEnabled(running)
        self.progress.setVisible(running)
        self.progress.setValue(0)
        if running:
            self.set_summary("")
            self.set_status("")
        self._reflow()

    def set_progress(self, overall: int, title: str, message: str) -> None:
        self.progress.setValue(overall)
        if self.cancel_btn.isEnabled():
            self.set_status(t("common.labeled", label=title, text=message) if message else title)

    def set_cancelling(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.set_status(t("action.cancelling"))
