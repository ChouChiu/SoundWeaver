"""Persistent bottom action bar: summary/status, progress, open folder, cancel, and start."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, PrimaryPushButton, ProgressBar, PushButton

from soundweaver.core.i18n import t
from soundweaver.ui import fluent, widgets


class ActionBar(widgets.Card):
    """Fluent 2 bottom action bar shared across input and output pages."""

    open_dir_requested = Signal()
    cancel_requested = Signal()
    start_requested = Signal()

    def __init__(self, start_tooltip: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent=parent)
        row = QHBoxLayout()
        row.setSpacing(widgets.FIELD_SPACING)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.summary = BodyLabel()
        self.summary.setWordWrap(True)
        self.status = widgets.hint_label()
        self.status.hide()
        text.addWidget(self.summary)
        text.addWidget(self.status)
        row.addLayout(text, 1)

        self.progress = ProgressBar()
        self.progress.setAccessibleName(t("tasks.col_progress"))
        self.progress.setMinimumWidth(120)
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        row.addWidget(self.progress)

        self.open_dir_btn = PushButton(fluent.icon("folder-open"), t("action.open_output_dir"))
        self.open_dir_btn.clicked.connect(self.open_dir_requested)
        self.cancel_btn = PushButton(fluent.icon("process-stop"), t("action.cancel"))
        self.cancel_btn.clicked.connect(self.cancel_requested)
        self.cancel_btn.hide()
        self.start_btn = PrimaryPushButton(fluent.icon("media-playback-start"), t("action.start"))
        if start_tooltip:
            self.start_btn.setToolTip(start_tooltip)
        self.start_btn.setMinimumWidth(110)
        self.start_btn.clicked.connect(self.start_requested)
        for button in (self.open_dir_btn, self.cancel_btn, self.start_btn):
            row.addWidget(button)
        self.set_body(row)

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)

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
            self.summary.setText("")

    def set_progress(self, overall: int, title: str, message: str) -> None:
        self.progress.setValue(overall)
        if message and self.cancel_btn.isEnabled():
            self.set_status(t("common.labeled", label=title, text=message))

    def set_cancelling(self) -> None:
        self.cancel_btn.setEnabled(False)
        self.set_status(t("action.cancelling"))
