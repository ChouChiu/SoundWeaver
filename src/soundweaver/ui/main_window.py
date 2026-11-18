"""Main FluentWindow shell: composes inputs, feature pages, 3D monitor, tasks, settings, and player bar."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSettings, Qt, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    FluentWindow,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    NavigationItemPosition,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    SegmentedWidget,
    SmoothScrollArea,
    SwitchButton,
    TitleLabel,
)

from soundweaver.core import ffmpeg
from soundweaver.core.i18n import get_language, set_language, t
from soundweaver.core.playback import PlaybackEngine
from soundweaver.core.project import PlanContext, Project
from soundweaver.core.sources import kind_of
from soundweaver.core.task import Step
from soundweaver.features.batch.panel import BatchPanel
from soundweaver.features.batch.planner import (
    BatchPlan,
    batch_shares,
    build_batch_plan,
    build_context,
    dir_problem,
    find_target_clash,
    panel_why_not,
)
from soundweaver.features.monitor import MonitorPage
from soundweaver.features.registry import output_panels
from soundweaver.features.source.panel import SourcePanel
from soundweaver.ui import fluent, widgets
from soundweaver.ui.feature import FeaturePanel
from soundweaver.ui.logs import LogPanelHandler
from soundweaver.ui.outputs import OutputList
from soundweaver.ui.player_bar import PlayerBar
from soundweaver.ui.settings import SettingsPage
from soundweaver.ui.tasks import TaskView
from soundweaver.ui.worker import Outcome, Worker

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
OUTPUTS_WIDTH = (280, 380)


def app_title() -> str:
    return t("app.title")


class _PageHeader(QWidget):
    """An output page's title with its on/off switch and current status line."""

    def __init__(self, panel: FeaturePanel) -> None:
        super().__init__()
        icon = IconWidget(fluent.icon(panel.icon))
        icon.setFixedSize(26, 26)
        self.check = SwitchButton()
        self.check.setOnText(t("switch.enabled"))
        self.check.setOffText(t("switch.disabled"))
        self.check.checkedChanged.connect(panel.set_enabled)
        self.status = widgets.hint_label()
        self.status.hide()
        title = QHBoxLayout()
        title.setSpacing(widgets.FIELD_SPACING)
        title.addWidget(icon)
        title.addWidget(TitleLabel(panel.title))
        title.addStretch(1)
        title.addWidget(self.check)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(title)
        layout.addWidget(self.status)

    def sync(self, enabled: bool, text: str, problem: bool) -> None:
        self.check.blockSignals(True)
        self.check.setChecked(enabled)
        self.check.blockSignals(False)
        self.status.setText(text)
        self.status.setVisible(enabled and bool(text))
        fluent.secondary(self.status, problem)


def _scroll_page(route_key: str, *items: QWidget) -> SmoothScrollArea:
    content = QWidget()
    content.setObjectName("page")
    content.setStyleSheet("QWidget#page { background: transparent; }")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(*widgets.PAGE_MARGINS)
    layout.setSpacing(widgets.SECTION_SPACING)
    for item in items:
        layout.addWidget(item)
    layout.addStretch(1)
    scroll = SmoothScrollArea()
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
        self.worker: Worker | None = None
        self._ask_on_close = True
        self._restoring = True
        self._monitor_full = False
        self._missing_tools = ffmpeg.missing_tools()

        self.playback_engine = PlaybackEngine(self)
        self.monitor_page = MonitorPage(self.playback_engine, self)
        self.monitor_page.setObjectName(ROUTE_MONITOR)
        self.monitor_page.full_window_toggled.connect(self._set_monitor_full_window)

        self.player_bar = PlayerBar(self.playback_engine, self)
        self.player_bar.expand_requested.connect(lambda: self._go(ROUTE_MONITOR))
        self.player_bar.next_requested.connect(self.monitor_page.play_next)
        self.player_bar.prev_requested.connect(self.monitor_page.play_prev)
        self.playback_engine.track_finished.connect(self.monitor_page.play_next)

        self.source = SourcePanel(self.settings)
        self.batch = BatchPanel(self.settings)
        self.panels = output_panels(self.settings)
        for inputs in (self.source, self.batch):
            inputs.changed.connect(self._sources_changed)
        for panel in self.panels:
            panel.changed.connect(self._update_ready)
            panel.tracks_changed.connect(self._tracks_changed)

        self._build_actions()
        self._build_shell()

        self._restore_settings()
        self._restoring = False
        self._sources_changed()

    def _build_actions(self) -> None:
        def action(text: str, icon: str, slot, shortcut=None) -> Action:
            act = Action(fluent.icon(icon), text, self)
            if shortcut is not None:
                act.setShortcut(shortcut)
                keys = act.shortcut().toString(QKeySequence.SequenceFormat.NativeText)
                act.setToolTip(t("common.with_note", text=text, note=keys))
            act.triggered.connect(slot)
            self.addAction(act)
            return act

        self.act_audio = action(
            t("action.open_audio"), "document-open", self._single(self.source.pick_audio),
            QKeySequence.StandardKey.Open,
        )
        self.act_batch_files = action(
            t("action.batch_add"), "list-add", self._batch_files, QKeySequence("Ctrl+Shift+B")
        )
        self.act_batch_folder = action(
            t("action.add_album_folder"), "folder-add", self._batch_folder, QKeySequence("Ctrl+Shift+M")
        )
        self.act_cover = action(
            t("action.select_cover"), "insert-image", self._single(self.source.pick_cover),
            QKeySequence("Ctrl+Shift+O"),
        )
        self.act_ttml = action(
            t("action.select_lyrics"), "text-xml", self._single(self.source.pick_ttml),
            QKeySequence("Ctrl+L"),
        )
        self.act_open_dir = action(
            t("action.open_output_dir"), "folder-open", self._open_output_dir, QKeySequence("Ctrl+E")
        )
        self.act_quit = action(
            t("action.quit"), "application-exit", self.close, QKeySequence.StandardKey.Quit
        )
        self.act_start = action(
            t("action.start"), "media-playback-start", self._start, QKeySequence("Ctrl+Return")
        )
        self.act_cancel = action(
            t("action.cancel"), "process-stop", self._handle_escape, QKeySequence("Escape")
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
        self.navigationInterface.setCollapsible(False)
        self.navigationInterface.setMenuButtonVisible(False)
        self.navigationInterface.setReturnButtonVisible(False)
        # Clear the 48px FluentTitleBar so the first nav item doesn't overlap the app title.
        self.navigationInterface.panel.vBoxLayout.setContentsMargins(0, 46, 0, 5)
        self.widgetLayout.setContentsMargins(0, 44, 0, 0)
        self._routes: dict[str, QWidget] = {}

        inputs_page = self._build_inputs_page()
        inputs_page.setObjectName(ROUTE_INPUTS)
        self._add_route(ROUTE_INPUTS, "home", t("nav.materials"), inputs_page)
        self.navigationInterface.addItemHeader(t("nav.outputs_header"))
        self.headers: list[_PageHeader] = []
        for index, panel in enumerate(self.panels):
            header = _PageHeader(panel)
            self.headers.append(header)
            route = f"{ROUTE_OUTPUT}{index}"
            page = _scroll_page(route, header, widgets.hint_label(panel.description), panel)
            self._add_route(route, panel.icon, panel.title, page)

        self._add_route(
            ROUTE_MONITOR, "audio-speakers-symbolic", t("nav.monitor"), self.monitor_page,
            NavigationItemPosition.BOTTOM,
        )
        tasks_page = self._build_tasks_page()
        tasks_page.setObjectName(ROUTE_TASKS)
        self._add_route(
            ROUTE_TASKS, "view-list-details", t("nav.tasks_logs"), tasks_page,
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
            ROUTE_SETTINGS, "preferences-system", t("nav.settings"), self.settings_page,
            NavigationItemPosition.BOTTOM,
        )

        # Wrap stackedWidget + persistent action/player bars vertically inside FluentWindow's widgetLayout.
        self.widgetLayout.takeAt(0)
        col = QVBoxLayout()
        col.setContentsMargins(10, 4, 14, 10)
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
        self.action_bar = self._build_action_bar()
        col.addWidget(self.action_bar)
        col.addWidget(self.player_bar)
        self.widgetLayout.addLayout(col)

    def _add_route(
        self,
        key: str,
        icon: str,
        text: str,
        page: QWidget,
        position: NavigationItemPosition = NavigationItemPosition.TOP,
    ) -> None:
        self._routes[key] = page
        self.addSubInterface(page, fluent.icon(icon), text, position)
        item = self.navigationInterface.widget(key)
        if item is not None:
            item.clicked.connect(lambda _=False, k=key: self._sync_route_chrome(k))

    def _sync_route_chrome(self, key: str) -> None:
        self.action_bar.setVisible(key not in (ROUTE_MONITOR, ROUTE_SETTINGS) or self.worker is not None)
        if key != ROUTE_MONITOR and self._monitor_full:
            self.monitor_page.exit_full_window()

    def _build_inputs_page(self) -> QWidget:
        self.mode_bar = SegmentedWidget()
        self.mode = QStackedWidget()
        for key, text, icon, page in (
            (MODES[SINGLE], t("inputs.single_mode"), "audio-x-generic", self.source),
            (MODES[BATCH], t("inputs.batch_mode"), "view-media-playlist", self.batch),
        ):
            self.mode.addWidget(page)
            self.mode_bar.addItem(
                key, text, lambda _=False, p=page: self.mode.setCurrentWidget(p), fluent.icon(icon)
            )
        self.mode_bar.setCurrentItem(MODES[SINGLE])
        self.mode.currentChanged.connect(self._mode_changed)

        self.outputs = OutputList(self.panels)
        self.outputs.selected.connect(self._show_page)
        self.outputs.itemClicked.connect(lambda item: self._show_page(self.outputs.row(item)))
        outputs = widgets.section(t("inputs.outputs_section"), self.outputs)
        outputs.setMinimumWidth(OUTPUTS_WIDTH[0])
        outputs.setMaximumWidth(OUTPUTS_WIDTH[1])

        left = self._inputs_layout = QVBoxLayout()
        left.setSpacing(widgets.SECTION_SPACING)
        left.addWidget(self.mode_bar)
        left.addWidget(self.mode)
        left.addStretch(1)
        body = QHBoxLayout()
        body.setSpacing(widgets.SECTION_SPACING)
        body.addLayout(left, 3)
        body.addWidget(outputs, 1)
        title = QHBoxLayout()
        title.addWidget(TitleLabel(t("inputs.title")))
        title.addSpacing(widgets.FIELD_SPACING)
        title.addWidget(widgets.hint_label(t("inputs.hint"), wrap=False), 0, Qt.AlignmentFlag.AlignBottom)
        title.addStretch(1)
        layout = QVBoxLayout()
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.SECTION_SPACING)
        layout.addLayout(title)
        layout.addLayout(body, 1)
        self._fit_mode()
        page = QWidget()
        page.setLayout(layout)
        return page

    def _build_tasks_page(self) -> QWidget:
        self.tasks = TaskView()
        self.log = PlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText(t("tasks.log_placeholder"))
        self.log.setMaximumBlockCount(5000)
        self._log_handler = LogPanelHandler()
        self._log_handler.lines.line.connect(self.log.appendPlainText)
        logging.getLogger().addHandler(self._log_handler)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.setHandleWidth(6)
        self.splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
        self.splitter.addWidget(widgets.section(t("tasks.section_tasks"), self.tasks))
        self.splitter.addWidget(widgets.section(t("tasks.section_logs"), self.log))
        self.splitter.setChildrenCollapsible(False)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(*widgets.PAGE_MARGINS)
        layout.setSpacing(widgets.SECTION_SPACING)
        layout.addWidget(TitleLabel(t("tasks.title")))
        layout.addWidget(self.splitter, 1)
        return page

    def _go(self, key: str) -> None:
        self.switchTo(self._routes[key])
        self._sync_route_chrome(key)

    def _current_route(self) -> str:
        page = self.stackedWidget.currentWidget()
        return next((k for k, w in self._routes.items() if w is page), ROUTE_INPUTS)

    def _set_monitor_full_window(self, on: bool) -> None:
        self._monitor_full = on
        self.navigationInterface.setVisible(not on)
        self.act_cancel.setEnabled(on or self.worker is not None)

    def _handle_escape(self) -> None:
        if self._monitor_full:
            self.monitor_page.exit_full_window()
        else:
            self._ask_cancel()

    def _build_action_bar(self) -> QWidget:
        bar = widgets.Card()
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
        self.progress.setFixedWidth(220)
        self.progress.hide()
        row.addWidget(self.progress)
        self.open_dir_btn = PushButton(fluent.icon("folder-open"), t("action.open_output_dir"))
        self.open_dir_btn.clicked.connect(self._open_output_dir)
        self.cancel_btn = PushButton(fluent.icon("process-stop"), t("action.cancel"))
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        self.start_btn = PrimaryPushButton(fluent.icon("media-playback-start"), t("action.start"))
        self.start_btn.setToolTip(self.act_start.toolTip())
        self.start_btn.setMinimumWidth(110)
        self.start_btn.clicked.connect(self._start)
        for button in (self.open_dir_btn, self.cancel_btn, self.start_btn):
            row.addWidget(button)
        bar.set_body(row)
        return bar

    def _mode_changed(self, index: int) -> None:
        self.mode_bar.setCurrentItem(MODES[index])
        self._fit_mode()
        self._sources_changed()

    def _fit_mode(self) -> None:
        current = self.mode.currentWidget()
        for index in range(self.mode.count()):
            page = self.mode.widget(index)
            shown = page is current
            policy = QSizePolicy.Policy.Preferred if shown else QSizePolicy.Policy.Ignored
            page.setSizePolicy(policy, policy)
        batch = current is self.batch
        self._inputs_layout.setStretch(1, 1 if batch else 0)
        self._inputs_layout.setStretch(2, 0 if batch else 1)
        self.mode.updateGeometry()

    def _show_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _toast(self, text: str) -> None:
        InfoBar.warning("", text, duration=4000, position=InfoBarPosition.TOP, parent=self)

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

    def _context(self, project: Project | None = None, batch: bool | None = None) -> PlanContext:
        return build_context(
            self.panels,
            self._project() if project is None else project,
            batch=self._is_batch if batch is None else batch,
        )

    def _active(self, ctx: PlanContext) -> list[FeaturePanel]:
        return [panel for panel in self.panels if panel.is_active(ctx)]

    def _problems(self, ctx: PlanContext) -> dict[int, str]:
        return {
            index: problem
            for index, panel in enumerate(self.panels)
            if panel.enabled and (problem := panel_why_not(ctx, panel))
        }

    def _batch_plan(self) -> BatchPlan:
        projects = list(self.batch.projects())
        album = self.batch.is_album
        return build_batch_plan(
            self.panels,
            projects,
            self.batch.album_project() if album else None,
            is_album=album,
        )

    def _blocker(self, ctx: PlanContext, problems: dict[int, str]) -> str | None:
        if self._missing_tools:
            return t(
                "status.missing_dependencies_blocker",
                tools=t("common.list_sep").join(self._missing_tools),
            )
        if not any(panel.enabled for panel in self.panels):
            return t("status.no_output_selected")
        if self._is_batch:
            if not self.batch.tracks:
                return t("batch.blocker_no_tracks")
            if self.batch.busy:
                return t("batch.reading_track_info")
            if not self._batch_plan().steps:
                return t("batch.blocker_no_runnable")
            return None
        if problems:
            index, text = next(iter(problems.items()))
            return t("common.labeled", label=self.panels[index].title, text=text)
        return dir_problem(ctx.project.output_dir)

    def _tracks_changed(self) -> None:
        self.source.layout_changed()
        self.batch.layout_changed()
        self._sources_changed()

    def _sources_changed(self) -> None:
        project = self._project()
        for panel in self.panels:
            panel.refresh(project)
        self._update_ready()

    def _update_ready(self) -> None:
        if self._restoring:
            return
        ctx = self._context()
        out = self._output_dir()
        has_dir = bool(out and out.is_dir())
        self.open_dir_btn.setEnabled(has_dir)
        self.act_open_dir.setEnabled(has_dir)
        problems = self._problems(ctx)
        shares = (
            batch_shares(self.panels, list(self.batch.projects()), is_album=self.batch.is_album)
            if self._is_batch
            else {}
        )
        self.outputs.sync(self.panels)
        for index, panel in enumerate(self.panels):
            if index in shares:
                status = shares[index]
            elif index in problems:
                status = (problems[index], True)
            elif panel.is_active(ctx):
                status = (panel.summary(ctx) or "", False)
            else:
                status = (t("switch.disabled"), False)
            self.outputs.set_status(index, *status)
            self.headers[index].sync(panel.enabled, *status)
        if self.worker:
            return
        plan = self._batch_plan() if self._is_batch else None
        if plan is not None:
            for index, (text, problem) in enumerate(plan.status):
                self.batch.set_status(index, text, problem)
        blocker = self._blocker(ctx, problems)
        self.start_btn.setEnabled(blocker is None)
        self.act_start.setEnabled(blocker is None)
        if blocker:
            self.summary.setText(blocker)
            return
        if plan is not None:
            album = t("batch.album_prefix") if self.batch.is_album else t("batch.batch_prefix")
            self.summary.setText(
                t(
                    "batch.summary_label",
                    mode=album,
                    tracks=plan.tracks,
                    total=len(self.batch.tracks),
                    steps=len(plan.steps),
                )
            )
            return
        parts = [s for panel in self._active(ctx) if (s := panel.summary(ctx))]
        self.summary.setText(t("inputs.to_generate", items=t("common.list_sep").join(parts)))

    def _show_page(self, index: int) -> None:
        if index >= 0:
            self._go(f"{ROUTE_OUTPUT}{index}")

    def _output_dir(self) -> Path | None:
        if not self._is_batch:
            return self.source.output_dir()
        return self.batch.output_root() or self.batch.current_project().output_dir

    def _start(self) -> None:
        if self.worker:
            return
        ctx = self._context()
        blocker = self._blocker(ctx, self._problems(ctx))
        if blocker:
            fluent.tell(self, t("status.cannot_start"), blocker)
            return
        if self._is_batch:
            plan = self._batch_plan()
            steps, inputs, dirs, notes = plan.steps, plan.inputs, plan.output_dirs, plan.skipped
            if not self._confirm_batch(plan):
                return
        else:
            active = self._active(ctx)
            if not all(panel.confirm(ctx) for panel in active):
                return
            steps = [step for panel in active for step in panel.plan(ctx)]
            inputs = set(ctx.project.inputs())
            assert ctx.project.output_dir is not None
            dirs = [ctx.project.output_dir]
            notes = [note for panel in active for note in panel.notes(ctx)]
        if not steps:
            fluent.tell(self, t("status.no_valid_tasks_title"), t("status.no_valid_tasks_body"))
            return
        if clash := find_target_clash(inputs, steps):
            fluent.tell(self, t("status.cannot_start"), clash)
            return
        if not self._confirm_overwrite([target for step in steps for target in step.targets]):
            return
        try:
            for out in dirs:
                out.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            fluent.tell(self, t("status.cannot_start"), t("error.cannot_create_dir", error=error))
            return
        self._save_settings()
        logger.info("—— %s", t("status.run_started", count=len(steps), path=self._output_label(dirs)))
        for note in notes:
            logger.info(note)
        self.tasks.set_steps(steps)
        self._go(ROUTE_TASKS)
        self.worker = Worker(steps, self)
        self.worker.step_started.connect(self.tasks.started)
        self.worker.step_progress.connect(self._on_progress)
        self.worker.step_finished.connect(self._on_step_finished)
        self.worker.finished_with.connect(self._on_finished)
        self._set_running(True)
        self.worker.start()

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
                zip(self.batch.tracks, plan.status, strict=False), 1
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
        if self.worker and fluent.ask(
            self,
            t("action.cancel_confirm_title"),
            t("action.cancel_confirm_body"),
            accept=t("action.cancel"),
        ):
            self._cancel()

    def _cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.act_cancel.setEnabled(self._monitor_full)
            self._show_status(t("action.cancelling"))

    def _on_progress(self, index: int, value: int, message: str) -> None:
        self.tasks.progress(index, value, message)
        overall = self.tasks.overall()
        self.progress.setValue(overall)
        self.setWindowTitle(f"{overall}% · {app_title()}")
        self._show_status(
            t("common.labeled", label=self.tasks.topLevelItem(index).text(0), text=message)
        )

    def _on_step_finished(self, index: int, outcome: str, note: str, seconds: float) -> None:
        self.tasks.finished(index, outcome, note, seconds)
        self.progress.setValue(self.tasks.overall())
        if outcome == Outcome.DONE:
            self._sources_changed()

    def _on_finished(self, outcome: str) -> None:
        if self.worker:
            self.worker.wait()
            self.worker.deleteLater()
        self.worker = None
        self._set_running(False)
        self.setWindowTitle(app_title())
        self._sources_changed()
        failures = self.tasks.failures()
        message = {
            Outcome.DONE: t("status.all_completed", path=self._output_label()),
            Outcome.CANCELLED: t("status.cancelled"),
            Outcome.FAILED: t("status.completed_with_failures", count=len(failures)),
        }[Outcome(outcome)]
        self._show_status(message)
        logger.info("—— %s", message)
        QApplication.alert(self)
        if outcome == Outcome.DONE:
            self._notify_done(message)
        if failures:
            fluent.tell(
                self,
                t("status.part_failed_title"),
                t("status.part_failed_body", message=message),
                "\n".join(failures),
            )

    def _notify_done(self, message: str) -> None:
        bar = InfoBar.success(
            t("status.done"), message, duration=8000, position=InfoBarPosition.TOP, parent=self
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
        self.start_btn.setVisible(not running)
        self.act_start.setEnabled(not running)
        self.cancel_btn.setVisible(running)
        self.cancel_btn.setEnabled(running)
        self.act_cancel.setEnabled(running or self._monitor_full)
        self.progress.setVisible(running)
        self.progress.setValue(0)
        if running:
            self.summary.setText("")

    def _output_label(self, dirs: list[Path] | None = None) -> str:
        if self._is_batch and not self.batch.output_root():
            return t("batch.each_output_dir")
        out = self._output_dir() if dirs is None or self._is_batch else dirs[0]
        return str(out)

    def _open_output_dir(self) -> None:
        out = self._output_dir()
        if out and out.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    def _set_language(self, code: str) -> None:
        if code == get_language():
            return
        set_language(code)
        self.settings.setValue("language", code)
        self.setWindowTitle(app_title())
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
        if not self.worker and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        audio = [p for p in paths if kind_of(p) == "audio"]
        if self._is_batch and all(kind_of(p) == "ttml" for p in paths):
            if not self.batch.import_lyrics(paths):
                self._toast(t("status.lyrics_already_imported"))
        elif self._is_batch or any(p.is_dir() for p in paths) or len(audio) > 1:
            if not self.add_batch(paths):
                self._toast(t("status.no_new_audio"))
        elif not self.source.drop(paths):
            self._toast(t("status.unsupported_format"))

    def _save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("tasks_splitter", self.splitter.saveState())
        self.settings.setValue("route", self._current_route())
        self.settings.setValue("mode", self.mode.currentIndex())
        self.batch.save()
        for panel in self.panels:
            panel.save()

    def _restore_settings(self) -> None:
        if not self.restoreGeometry(self.settings.value("geometry", b"")):
            self.resize(1180, 860)
        if not self.splitter.restoreState(self.settings.value("tasks_splitter", b"")):
            self.splitter.setSizes([500, 200])
        self.batch.restore()
        self.mode.setCurrentIndex(self.settings.value("mode", SINGLE, type=int))
        for panel in self.panels:
            panel.restore()
        self.outputs.sync(self.panels)
        route = self.settings.value("route", ROUTE_INPUTS, type=str)
        self._go(route if route in self._routes else ROUTE_INPUTS)

    def close_without_asking(self) -> None:
        self._ask_on_close = False
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            if self._ask_on_close and not fluent.ask(
                self,
                t("status.task_running_title"),
                t("status.task_running_prompt"),
                accept=t("action.quit"),
            ):
                event.ignore()
                return
            self.worker.cancel()
            self.worker.wait()
        for panel in self.panels:
            panel.shutdown()
        self._save_settings()
        logging.getLogger().removeHandler(self._log_handler)
        super().closeEvent(event)
