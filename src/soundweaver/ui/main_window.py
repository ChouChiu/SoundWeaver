"""Main FluentWindow shell: composes inputs, feature pages, 3D monitor, tasks, settings, and player bar."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QSettings,
    Qt,
    QUrl,
)
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    NavigationItemPosition,
    PushButton,
    TitleLabel,
)

from soundweaver.core import ffmpeg
from soundweaver.core.i18n import get_language, set_language, t
from soundweaver.core.planning import find_target_clash
from soundweaver.core.playback import PlaybackEngine
from soundweaver.core.project import Project
from soundweaver.core.runner import Outcome
from soundweaver.core.sources import kind_of
from soundweaver.features.batch.panel import BatchPanel
from soundweaver.features.batch.planner import BatchPlan
from soundweaver.features.monitor import MonitorPage
from soundweaver.features.monitor.player_bar import PlayerBar
from soundweaver.features.source.panel import SourcePanel
from soundweaver.features.tasks.panel import TasksPage
from soundweaver.ui import fluent, widgets
from soundweaver.ui.action_bar import ActionBar
from soundweaver.ui.export_workflow import (
    BatchSelection,
    ExportAssessment,
    assess_exports,
    plan_single,
)
from soundweaver.ui.feature import PageHeader
from soundweaver.ui.feature_registry import output_panels
from soundweaver.ui.outputs import OutputList
from soundweaver.ui.scrolling import DirectScrollArea, use_direct_wheel
from soundweaver.ui.settings import SettingsPage

logger = logging.getLogger(__name__)

SINGLE, BATCH = 0, 1
MODES = ("single", "batch")
ROUTE_INPUTS, ROUTE_OUTPUT, ROUTE_MONITOR, ROUTE_TASKS, ROUTE_SETTINGS = (
    "inputs",
    "output",
    "monitor",
    "tasks",
    "settings",
)


def app_title() -> str:
    return t("app.title")


def _scroll_page(
    route_key: str, *items: QWidget, stretch_last: bool = False
) -> DirectScrollArea:
    content = QWidget()
    content.setObjectName("page")
    content.setStyleSheet("QWidget#page { background: transparent; }")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(*widgets.PAGE_MARGINS)
    layout.setSpacing(widgets.SECTION_SPACING)
    for index, item in enumerate(items):
        layout.addWidget(item, int(stretch_last and index == len(items) - 1))
    if not stretch_last:
        layout.addStretch(1)
    scroll = DirectScrollArea()
    scroll.setObjectName(route_key)
    scroll.setWidgetResizable(True)
    scroll.enableTransparentBackground()
    scroll.setWidget(content)
    return scroll


class MainWindow(FluentWindow):
    """FluentUI 2 main application window."""

    def __init__(self) -> None:
        super().__init__()
        # qframelesswindow installs an app-wide eventFilter on Linux that crashes PySide6 6.11
        # when QtWebEngine's internal QQuickItem objects dispatch hover/property events.
        if (app := QCoreApplication.instance()) is not None:
            app.removeEventFilter(self)
        self.installEventFilter(self)
        self.setWindowTitle(app_title())
        self.setAcceptDrops(True)
        self.settings = QSettings()
        self._ask_on_close = True
        self._restoring = True
        self._monitor_full = False
        self._missing_tools = ffmpeg.missing_tools()

        self.playback_engine = PlaybackEngine(self)
        self.monitor_page = MonitorPage(self.playback_engine, self)
        self.monitor_page.full_window_toggled.connect(self._set_monitor_full_window)

        self.player_bar = PlayerBar(self.playback_engine, self)
        self.player_bar.expand_requested.connect(lambda: self._go(ROUTE_MONITOR))
        self.player_bar.next_requested.connect(self.monitor_page.play_next)
        self.player_bar.prev_requested.connect(self.monitor_page.play_prev)
        self.playback_engine.track_finished.connect(self.monitor_page.play_next)
        self.playback_engine.load_failed.connect(self._playback_failed)

        self.source = SourcePanel(self.settings)
        self.batch = BatchPanel(self.settings)
        self.panels = output_panels(self.settings)
        for inputs in (self.source, self.batch):
            inputs.changed.connect(self._sources_changed)
        # Replanning walks every track x every panel; coalesce setting bursts so
        # a run of toggles recomputes once, after 150 quiet milliseconds.
        self._ready_debounce = widgets.Debouncer(self._update_ready, ms=150, parent=self)
        for panel in self.panels:
            panel.changed.connect(self._ready_debounce.poke)
            panel.tracks_changed.connect(self._tracks_changed)

        self.stackedWidget.setAnimationEnabled(False)

        self._build_actions()
        self._build_shell()

        self._restore_settings()
        self.stackedWidget.setAnimationEnabled(True)
        self._restoring = False
        self._sources_changed()

    def _build_actions(self) -> None:
        self._action_keys: list[tuple[Action, str]] = []

        def action(key: str, icon: str, slot, shortcut=None) -> Action:
            act = Action(fluent.icon(icon), t(key), self)
            if shortcut is not None:
                act.setShortcut(shortcut)
                keys = act.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
                act.setToolTip(t("common.with_note", text=t(key), note=keys))
            act.triggered.connect(slot)
            self.addAction(act)
            self._action_keys.append((act, key))
            return act

        self.act_audio = action(
            "action.open_audio", "document-open", self._single(self.source.pick_audio),
            QKeySequence.StandardKey.Open,
        )
        self.act_batch_files = action(
            "action.batch_add", "list-add", self._batch_files, QKeySequence("Ctrl+Shift+B")
        )
        self.act_batch_folder = action(
            "action.add_album_folder", "folder-add", self._batch_folder, QKeySequence("Ctrl+Shift+M")
        )
        self.act_cover = action(
            "action.select_cover", "insert-image", self._single(self.source.pick_cover),
            QKeySequence("Ctrl+Shift+O"),
        )
        self.act_ttml = action(
            "action.select_lyrics", "text-xml", self._single(self.source.pick_ttml),
            QKeySequence("Ctrl+L"),
        )
        self.act_open_dir = action(
            "action.open_output_dir", "folder-open", self._open_output_dir, QKeySequence("Ctrl+E")
        )
        self.act_quit = action(
            "action.quit", "application-exit", self.close, QKeySequence.StandardKey.Quit
        )
        self.act_start = action(
            "action.start", "media-playback-start", self._start, QKeySequence("Ctrl+Return")
        )
        self.act_cancel = action(
            "action.cancel", "process-stop", self._handle_escape, QKeySequence("Escape")
        )
        self.act_cancel.setEnabled(False)
        self.input_actions = (
            self.act_audio, self.act_cover, self.act_ttml,
            self.act_batch_files, self.act_batch_folder,
        )

    def _single(self, pick):
        def run() -> None:
            self.mode.setCurrentIndex(SINGLE)
            pick()

        return run

    def _build_shell(self) -> None:
        self.navigationInterface.setExpandWidth(200)
        nav = self.navigationInterface.panel
        # Animate the selected indicator without resizing the entire workspace every frame.
        nav.expandAni.setDuration(0)
        use_direct_wheel(nav.scrollArea)
        self.navigationInterface.setMinimumExpandWidth(1040)
        self.navigationInterface.setMenuButtonVisible(False)
        self.navigationInterface.setReturnButtonVisible(False)
        # Clear the 48px FluentTitleBar so the first nav item doesn't overlap the app title.
        nav.vBoxLayout.setContentsMargins(0, 48, 0, 4)
        self.widgetLayout.setContentsMargins(0, 44, 0, 0)
        self._routes: dict[str, QWidget] = {}
        self._nav_keys: list[tuple[QWidget, str]] = []

        inputs_page = self._build_inputs_page()
        inputs_page.setObjectName(ROUTE_INPUTS)
        self._add_route(ROUTE_INPUTS, "home", "nav.materials", inputs_page)
        self.outputs_header = self.navigationInterface.addItemHeader(t("nav.outputs_header"))
        self.headers: list[PageHeader] = []
        for index, panel in enumerate(self.panels):
            header = PageHeader(panel)
            self.headers.append(header)
            route = f"{ROUTE_OUTPUT}{index}"
            page = _scroll_page(route, header, widgets.hint_label(panel.description), panel)
            self._add_route(route, panel.icon, panel.title_key, page)

        self._add_route(
            ROUTE_MONITOR,
            "audio-speakers-symbolic",
            "nav.monitor",
            _scroll_page(ROUTE_MONITOR, self.monitor_page, stretch_last=True),
            NavigationItemPosition.BOTTOM,
        )
        self.tasks_page = TasksPage()
        self.tasks_page.setObjectName(ROUTE_TASKS)
        self.tasks_page.progress.connect(self._on_progress)
        self.tasks_page.finished.connect(self._on_finished)
        self._add_route(
            ROUTE_TASKS, "view-list-details", "nav.tasks_logs", self.tasks_page,
            NavigationItemPosition.BOTTOM,
        )
        self.settings_page = SettingsPage(
            (
                self.act_audio, self.act_cover, self.act_ttml, self.act_batch_files,
                self.act_batch_folder, self.act_open_dir, self.act_start, self.act_cancel,
                self.act_quit,
            ),
            self,
        )
        self.settings_page.setObjectName(ROUTE_SETTINGS)
        self.settings_page.language_picked.connect(self._set_language)
        self.settings_page.about_requested.connect(self._about)
        self._add_route(
            ROUTE_SETTINGS, "preferences-system", "nav.settings", self.settings_page,
            NavigationItemPosition.BOTTOM,
        )

        # Wrap stackedWidget + persistent action/player bars vertically inside FluentWindow's widgetLayout.
        self.widgetLayout.takeAt(0)
        col = QVBoxLayout()
        col.setContentsMargins(12, 4, 16, 12)
        col.setSpacing(widgets.SECTION_SPACING)
        if self._missing_tools:
            col.addWidget(
                InfoBar.error(
                    t("status.missing_dependencies_title"),
                    t(
                        "status.missing_dependencies_banner",
                        tools=t("common.list_sep").join(self._missing_tools),
                    ),
                    isClosable=False,
                    duration=-1,
                    position=InfoBarPosition.NONE,
                    parent=self,
                )
            )
        col.addWidget(self.stackedWidget, 1)
        self.action_bar = ActionBar(self.act_start.toolTip(), self)
        self.action_bar.open_dir_requested.connect(self._open_output_dir)
        self.action_bar.cancel_requested.connect(self._cancel)
        self.action_bar.start_requested.connect(self._start)
        col.addWidget(self.action_bar)
        col.addWidget(self.player_bar)
        self.widgetLayout.addLayout(col)
        self.stackedWidget.currentChanged.connect(self._on_route_changed)

    def _add_route(
        self,
        key: str,
        icon: str,
        text_key: str,
        page: QWidget,
        position: NavigationItemPosition = NavigationItemPosition.TOP,
    ) -> None:
        self._routes[key] = page
        text = t(text_key)
        self.addSubInterface(page, fluent.icon(icon), text, position)
        item = self.navigationInterface.widget(key)
        self._nav_keys.append((item, text_key))
        widgets.keyboard_activate(item, text, item.click)

    def _on_route_changed(self) -> None:
        self._sync_route_chrome(self._current_route())

    def _sync_route_chrome(self, key: str) -> None:
        self.action_bar.setVisible(key not in (ROUTE_MONITOR, ROUTE_SETTINGS) or self.tasks_page.running)
        if key != ROUTE_MONITOR and self._monitor_full:
            self.monitor_page.exit_full_window()

    def _build_inputs_page(self) -> QWidget:
        self.mode_bar = widgets.SegmentedChoice()
        self.mode = widgets.CurrentPageStack()
        for key, text, icon, page in (
            (MODES[SINGLE], t("inputs.single_mode"), "audio-x-generic", self.source),
            (MODES[BATCH], t("inputs.batch_mode"), "view-media-playlist", self.batch),
        ):
            self.mode.addWidget(page)
            self.mode_bar.add_choice(
                key, text, lambda p=page: self.mode.setCurrentWidget(p), fluent.icon(icon)
            )
        self.mode_bar.setCurrentItem(MODES[SINGLE])
        self.mode.currentChanged.connect(self._mode_changed)

        self.outputs = OutputList(self.panels)
        self.outputs.itemClicked.connect(self._open_output_settings)
        self.outputs.itemActivated.connect(self._open_output_settings)
        outputs = widgets.section(t("inputs.outputs_section"), self.outputs)

        primary = QWidget()
        input_layout = QVBoxLayout(primary)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(widgets.SECTION_SPACING)
        input_layout.addWidget(self.mode_bar)
        input_layout.addLayout(self.mode)

        heading = QWidget()
        title = QVBoxLayout(heading)
        title.setContentsMargins(0, 0, 0, 0)
        title.setSpacing(widgets.ROW_SPACING)
        title.addWidget(TitleLabel(t("inputs.title")))
        title.addWidget(widgets.hint_label(t("inputs.hint")))
        body = widgets.AdaptiveColumns(primary, outputs, breakpoint=800)
        return _scroll_page(ROUTE_INPUTS, heading, body, stretch_last=True)

    def switchTo(self, interface: QWidget) -> None:
        view = self.stackedWidget.view
        if interface is view.currentWidget():
            return
        # A page left mid-transition still has its old offset. Start from the layout origin.
        interface.move(0, 0)
        # The Fluent frame resets scrollbars. Switching through the stack keeps their positions.
        view.setCurrentWidget(interface, duration=300)

    def _go(self, key: str) -> None:
        self.switchTo(self._routes[key])

    def _current_route(self) -> str:
        page = self.stackedWidget.currentWidget()
        return page.objectName()

    def _set_monitor_full_window(self, on: bool) -> None:
        self._monitor_full = on
        self.navigationInterface.setVisible(not on)
        self.act_cancel.setEnabled(on or self.tasks_page.running)

    def _handle_escape(self) -> None:
        if self._monitor_full:
            self.monitor_page.exit_full_window()
        else:
            self._ask_cancel()

    def _mode_changed(self, index: int) -> None:
        self.mode_bar.setCurrentItem(MODES[index])
        self.mode.invalidate()
        self._sources_changed()

    def _toast(self, text: str) -> None:
        InfoBar.warning("", text, duration=4000, position=InfoBarPosition.TOP, parent=self)

    def _playback_failed(self, message: str) -> None:
        InfoBar.error(
            t("player.load_failed"), message, duration=6000,
            position=InfoBarPosition.TOP_RIGHT, parent=self,
        )

    def set_audio(self, path: Path) -> None:
        self.mode.setCurrentIndex(SINGLE)
        self.source.set_audio(path)

    def set_cover(self, path: Path) -> None:
        self.source.set_cover(path)

    def add_batch(self, paths: Iterable[Path]) -> int:
        self.mode.setCurrentIndex(BATCH)
        return self.batch.add(paths)

    def _batch_files(self) -> None:
        self.mode.setCurrentIndex(BATCH)
        self.batch.pick_files()

    def _batch_folder(self) -> None:
        self.mode.setCurrentIndex(BATCH)
        self.batch.pick_folder()

    @property
    def _is_batch(self) -> bool:
        return self.mode.currentIndex() == BATCH

    def _project(self) -> Project:
        return self.batch.current_project() if self._is_batch else self.source.project()

    def _assess_export(self) -> ExportAssessment:
        batch = None
        if self._is_batch:
            is_album = self.batch.is_album
            batch = BatchSelection(
                self.batch.projects(),
                self.batch.album_project() if is_album else None,
                is_album,
                self.batch.busy,
            )
        return assess_exports(self.panels, self._project(), self._missing_tools, batch)

    def _tracks_changed(self) -> None:
        self.source.layout_changed()
        self.batch.layout_changed()

    def _sources_changed(self) -> None:
        project = self._project()
        for panel in self.panels:
            panel.refresh(project)
        self._update_ready()

    def _update_ready(self) -> None:
        if self._restoring or self.tasks_page.running:
            return
        assessment = self._assess_export()
        self.outputs.sync(self.panels)
        for panel, header, status in zip(self.panels, self.headers, assessment.statuses, strict=True):
            header.sync(panel.enabled, *status)
        self.outputs.set_statuses(assessment.statuses)
        if (plan := assessment.batch_plan) is not None:
            for index, (text, problem) in enumerate(plan.status):
                self.batch.set_status(index, text, problem)

        out = self._output_dir()
        has_dir = out is not None and out.is_dir()
        can_start = assessment.blocker is None
        self.act_open_dir.setEnabled(has_dir)
        self.act_start.setEnabled(can_start)
        self.action_bar.set_ready(can_open_dir=has_dir, can_start=can_start)
        self.action_bar.set_summary(assessment.summary)

    def _open_output_settings(self, item) -> None:
        index = self.outputs.row(item)
        self._go(f"{ROUTE_OUTPUT}{index}")
        self.headers[index].check.indicator.setFocus(Qt.FocusReason.OtherFocusReason)

    def _output_dir(self) -> Path | None:
        if not self._is_batch:
            return self.source.output_dir()
        return self.batch.output_root() or self.batch.current_project().output_dir

    def _start(self) -> None:
        if self.tasks_page.running:
            return
        inputs_panel = self.batch if self._is_batch else self.source
        inputs_panel.flush_pending_edits()
        assessment = self._assess_export()
        if assessment.blocker:
            fluent.tell(self, t("status.cannot_start"), assessment.blocker)
            return
        if assessment.batch_plan is not None:
            plan = assessment.batch_plan
            if not self._confirm_batch(plan):
                return
            notes = plan.skipped
        else:
            ctx = assessment.context
            active = [self.panels[index] for index in assessment.active]
            if not all(panel.confirm(ctx) for panel in active):
                return
            plan = plan_single(ctx, active)
            notes = [note for panel in active for note in panel.notes(ctx)]

        if not plan.steps:
            fluent.tell(self, t("status.no_valid_tasks_title"), t("status.no_valid_tasks_body"))
            return
        if clash := find_target_clash(plan.inputs, plan.steps):
            fluent.tell(self, t("status.cannot_start"), clash)
            return
        if not self._confirm_overwrite(plan.targets):
            return
        try:
            for out in plan.output_dirs:
                out.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            fluent.tell(self, t("status.cannot_start"), t("error.cannot_create_dir", error=error))
            return
        self._save_settings()
        logger.info("—— %s", t(
            "status.run_started", count=len(plan.steps), path=self._output_label(plan.output_dirs),
        ))
        for note in notes:
            logger.info(note)
        self._go(ROUTE_TASKS)
        self._set_running(True)
        self.tasks_page.start(plan.steps)

    def _confirm_batch(self, plan: BatchPlan) -> bool:
        left = len(self.batch.tracks) - plan.tracks
        if not left and not plan.skipped:
            return True
        lines = [t("batch.confirm_summary", tracks=plan.tracks, steps=len(plan.steps))]
        if left:
            lines.append(t("batch.confirm_skipped_tracks", count=left))
        if plan.skipped:
            lines.append(t("batch.confirm_skipped_outputs", count=len(plan.skipped)))
        details = [
            t("common.labeled", label=f"{index:02d} {track.audio.name}", text=text)
            for index, (track, (text, problem)) in enumerate(
                zip(self.batch.tracks, plan.status, strict=True), 1
            )
            if problem
        ]
        return fluent.ask(
            self,
            t("batch.confirm_title"),
            "\n".join(lines),
            "\n".join(details + plan.skipped),
            t("action.continue"),
        )

    def _confirm_overwrite(self, targets: list[Path]) -> bool:
        existing = [target for target in targets if target.exists()]
        if not existing:
            return True
        return fluent.ask(
            self,
            t("status.confirm_overwrite_title"),
            t("status.confirm_overwrite_body", count=len(existing)),
            "\n".join(p.name for p in existing),
            t("action.overwrite"),
        )

    def _ask_cancel(self) -> None:
        if self.tasks_page.running and fluent.ask(
            self,
            t("action.cancel_confirm_title"),
            t("action.cancel_confirm_body"),
            accept=t("action.cancel"),
        ):
            self._cancel()

    def _cancel(self) -> None:
        if self.tasks_page.running:
            self.tasks_page.cancel()
            self.action_bar.set_cancelling()
            self.act_cancel.setEnabled(self._monitor_full)

    def _on_progress(self, overall: int, title: str, message: str) -> None:
        self.action_bar.set_progress(overall, title, message)
        self.setWindowTitle(f"{overall}% · {app_title()}")

    def _on_finished(self, outcome: str) -> None:
        self._set_running(False)
        self._sync_route_chrome(self._current_route())
        self.setWindowTitle(app_title())
        self._sources_changed()
        failures = self.tasks_page.failures()
        message = {
            Outcome.DONE: t("status.all_completed", path=self._output_label()),
            Outcome.CANCELLED: t("status.cancelled"),
            Outcome.FAILED: t("status.completed_with_failures", count=len(failures)),
        }[Outcome(outcome)]
        self.action_bar.set_status(message)
        logger.info("—— %s", message)
        QApplication.alert(self)
        if outcome == Outcome.DONE and self.action_bar.isHidden():
            self._notify_done()
        if failures:
            fluent.tell(
                self,
                t("status.part_failed_title"),
                t("status.part_failed_body", message=message),
                "\n".join(failures),
            )

    def _notify_done(self) -> None:
        bar = InfoBar.success(
            t("status.done"), "", duration=8000, position=InfoBarPosition.TOP, parent=self
        )
        out = self._output_dir()
        if out and out.is_dir():
            button = PushButton(fluent.icon("folder-open"), t("action.open_output_dir"))
            button.clicked.connect(self._open_output_dir)
            bar.addWidget(button)

    def _set_running(self, running: bool) -> None:
        self.source.set_running(running)
        self.batch.set_running(running)
        self.mode_bar.setEnabled(not running)
        self.outputs.set_locked(running)
        self.settings_page.set_running(running)
        for panel, header in zip(self.panels, self.headers, strict=True):
            if running:
                panel.cancel_background()
            panel.setEnabled(not running)
            header.check.setEnabled(not running)
        for action in self.input_actions:
            action.setEnabled(not running)
        self.act_start.setEnabled(not running)
        self.act_cancel.setEnabled(running or self._monitor_full)
        self.action_bar.set_running(running)

    def _output_label(self, dirs: list[Path] | None = None) -> str:
        if self._is_batch and not self.batch.output_root():
            return t("batch.each_output_dir")
        out = self._output_dir() if dirs is None or self._is_batch else dirs[0]
        return str(out)

    def _open_output_dir(self) -> None:
        out = self._output_dir()
        if out and out.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    def _retranslate_shell(self) -> None:
        """Switch the window chrome live; page bodies follow after a restart."""
        self.setWindowTitle(app_title())
        for act, key in self._action_keys:
            act.setText(t(key))
            if act.shortcut().isEmpty():
                continue
            keys = act.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
            act.setToolTip(t("common.with_note", text=t(key), note=keys))
        for item, text_key in self._nav_keys:
            if item is not None:
                item.setText(t(text_key))
                item.setToolTip(t(text_key))
        self.outputs_header.setText(t("nav.outputs_header"))

    def _set_language(self, code: str) -> None:
        if code == get_language():
            return
        set_language(code)
        self.settings.setValue("language", code)
        self._retranslate_shell()
        self._sources_changed()
        InfoBar.info(
            t("action.language_changed_title"),
            t("action.language_changed_tip"),
            duration=6000,
            parent=self,
        )

    def _about(self) -> None:
        fluent.tell(self, t("app.about_title"), t("app.description"))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self.tasks_page.running and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths or self.tasks_page.running:
            return
        event.acceptProposedAction()
        audio_count = sum(kind_of(path) == "audio" for path in paths)
        if self._is_batch and all(kind_of(p) == "ttml" for p in paths):
            if not self.batch.import_lyrics(paths):
                self._toast(t("status.lyrics_already_imported"))
        elif self._is_batch or any(p.is_dir() for p in paths) or audio_count > 1:
            if not self.add_batch(paths):
                self._toast(t("status.no_new_audio"))
        elif not self.source.drop(paths):
            self._toast(t("status.unsupported_format"))

    def _save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("tasks_splitter", self.tasks_page.save_layout())
        self.settings.setValue("route", self._current_route())
        self.settings.setValue("mode", self.mode.currentIndex())
        self.batch.save()
        for panel in self.panels:
            panel.save()

    def _restore_settings(self) -> None:
        if not self.restoreGeometry(self.settings.value("geometry", b"")):
            self.resize(1180, 860)
        self.tasks_page.restore_layout(self.settings.value("tasks_splitter", b""))
        self.batch.restore()
        mode = self.settings.value("mode", SINGLE, type=int)
        self.mode.setCurrentIndex(BATCH if mode == BATCH else SINGLE)
        for panel in self.panels:
            panel.restore()
        self.outputs.sync(self.panels)
        route = self.settings.value("route", ROUTE_INPUTS, type=str)
        self._go(route if route in self._routes else ROUTE_INPUTS)

    def close_without_asking(self) -> None:
        self._ask_on_close = False
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tasks_page.running and self._ask_on_close and not fluent.ask(
            self,
            t("status.task_running_title"),
            t("status.task_running_prompt"),
            accept=t("action.quit"),
        ):
            event.ignore()
            return
        self.tasks_page.shutdown()
        self.playback_engine.shutdown()
        for panel in self.panels:
            panel.shutdown()
        self._save_settings()
        super().closeEvent(event)
