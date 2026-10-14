"""Main window: pick the inputs, tick the outputs, press Start.

Built from Fluent Design widgets (PySide6-Fluent-Widgets) whose theme and
accent follow the desktop, see `ui.fluent`; file dialogs stay the desktop's
own.  The window only composes: the source panel owns the inputs, each
output panel owns its settings and its steps, the worker runs them and the
task list shows how they go.
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
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
    QMainWindow,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    BodyLabel,
    CaptionLabel,
    IconWidget,
    InfoBar,
    InfoBarPosition,
    NavigationInterface,
    NavigationItemPosition,
    PlainTextEdit,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    SegmentedWidget,
    SwitchButton,
    TitleLabel,
)

from dolbytool.core import ffmpeg
from dolbytool.core.i18n import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    get_language,
    set_language,
    t,
)
from dolbytool.core.project import PlanContext, Project
from dolbytool.core.task import Step
from dolbytool.features.batch.panel import BatchPanel
from dolbytool.features.registry import output_panels
from dolbytool.features.source.lyrics import without_number
from dolbytool.features.source.panel import SourcePanel
from dolbytool.features.source.service import kind_of
from dolbytool.ui import fluent, widgets
from dolbytool.ui.feature import FeaturePanel
from dolbytool.ui.logs import LogPanelHandler
from dolbytool.ui.outputs import OutputList
from dolbytool.ui.settings import SettingsPage
from dolbytool.ui.tasks import TaskView
from dolbytool.ui.worker import Outcome, Worker

logger = logging.getLogger(__name__)

SINGLE, BATCH = 0, 1
MODES = ("single", "batch")
ROUTE_INPUTS, ROUTE_OUTPUT, ROUTE_TASKS, ROUTE_SETTINGS = "inputs", "output", "tasks", "settings"
OUTPUTS_WIDTH = (280, 380)


def app_title() -> str:
    return t("app.title")


class _PageHeader(QWidget):
    """An output page's title with its on/off switch and what it will make."""

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
        title.setSpacing(12)
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
        # the switch beside the title already says when it is off
        self.status.setVisible(enabled and bool(text))
        fluent.secondary(self.status, problem)


def _scroll_page(*items: QWidget) -> ScrollArea:
    content = QWidget()
    content.setObjectName("page")
    content.setStyleSheet("QWidget#page { background: transparent; }")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 12, 6)
    layout.setSpacing(10)
    for item in items:
        layout.addWidget(item)
    layout.addStretch(1)
    scroll = ScrollArea()
    scroll.setWidgetResizable(True)
    scroll.enableTransparentBackground()
    scroll.setWidget(content)
    return scroll


@dataclass(slots=True)
class BatchPlan:
    """Every track of a batch planned with the current settings."""

    steps: list[Step] = field(default_factory=list)
    # per track: what it will make or why it cannot, and whether that is a problem
    status: list[tuple[str, bool]] = field(default_factory=list)
    # log lines for outputs a track had to leave out
    skipped: list[str] = field(default_factory=list)
    output_dirs: list[Path] = field(default_factory=list)
    inputs: set[Path] = field(default_factory=set)
    tracks: int = 0


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(app_title())
        self.setAcceptDrops(True)
        self.settings = QSettings()
        self.worker: Worker | None = None
        self._ask_on_close = True
        self._restoring = True
        self._missing_tools = ffmpeg.missing_tools()

        self.source = SourcePanel(self.settings)
        self.batch = BatchPanel(self.settings)
        self.panels = output_panels(self.settings)
        for inputs in (self.source, self.batch):
            inputs.changed.connect(self._sources_changed)
        for panel in self.panels:
            panel.changed.connect(self._update_ready)
            panel.tracks_changed.connect(self._tracks_changed)

        self._build_actions()
        self.setCentralWidget(self._build_body())

        self._restore_settings()
        self._restoring = False
        self._sources_changed()

    # ---- layout ----------------------------------------------------------------------

    def _build_actions(self) -> None:
        def action(text: str, icon: str, slot, shortcut=None) -> Action:
            act = Action(fluent.icon(icon), text, self)
            if shortcut is not None:
                act.setShortcut(shortcut)
                act.setToolTip(f"{text}（{QKeySequence(shortcut).toString()}）")
            act.triggered.connect(slot)
            # shortcuts fire from anywhere in the window, not only from the bar's buttons
            self.addAction(act)
            return act

        self.act_audio = action(t("action.open_audio"), "document-open", self.source.pick_audio,
                                QKeySequence.StandardKey.Open)  # fmt: skip
        self.act_batch_files = action(t("action.batch_add"), "list-add", self._batch_files,
                                      QKeySequence("Ctrl+Shift+B"))  # fmt: skip
        self.act_batch_folder = action(t("action.add_album_folder"), "folder-add", self._batch_folder,
                                       QKeySequence("Ctrl+Shift+M"))  # fmt: skip
        self.act_cover = action(t("action.select_cover"), "insert-image", self.source.pick_cover,
                                QKeySequence("Ctrl+Shift+O"))  # fmt: skip
        self.act_ttml = action(t("action.select_lyrics"), "text-xml", self.source.pick_ttml,
                                QKeySequence("Ctrl+L"))  # fmt: skip
        self.act_open_dir = action(t("action.open_output_dir"), "folder-open", self._open_output_dir,
                                   QKeySequence("Ctrl+E"))  # fmt: skip
        self.act_quit = action(
            t("action.quit"), "application-exit", self.close, QKeySequence.StandardKey.Quit
        )
        self.act_start = action(
            t("action.start"), "media-playback-start", self._start, QKeySequence("Ctrl+Return")
        )
        self.act_cancel = action(t("action.cancel"), "process-stop", self._cancel, QKeySequence("Escape"))
        self.act_cancel.setEnabled(False)
        self.input_actions = (
            self.act_audio, self.act_cover, self.act_ttml,
            self.act_batch_files, self.act_batch_folder,
        )  # fmt: skip

    def _build_body(self) -> QWidget:
        """Navigation on the left; on the right the page it shows above the action bar."""
        self.stack = QStackedWidget()
        self.nav = NavigationInterface(self, showMenuButton=False)
        # the width first: turning collapsing off expands the panel to the width set by then
        self.nav.setExpandWidth(200)
        self.nav.setCollapsible(False)
        self._routes: dict[str, QWidget] = {}

        self._add_route(ROUTE_INPUTS, "home", t("nav.materials"), self._build_inputs_page())
        self.nav.addItemHeader(t("nav.outputs_header"))
        self.headers: list[_PageHeader] = []
        for index, panel in enumerate(self.panels):
            header = _PageHeader(panel)
            self.headers.append(header)
            page = _scroll_page(header, widgets.hint_label(panel.description), panel)
            self._add_route(f"{ROUTE_OUTPUT}{index}", panel.icon, panel.title, page)
        self._add_route(
            ROUTE_TASKS, "view-list-details", t("nav.tasks_logs"), self._build_tasks_page(),
            NavigationItemPosition.BOTTOM,
        )  # fmt: skip
        self.settings_page = SettingsPage(self)
        self._add_route(
            ROUTE_SETTINGS, "preferences-system", t("nav.settings"), self.settings_page,
            NavigationItemPosition.BOTTOM,
        )  # fmt: skip

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(4, 10, 14, 12)
        right_layout.setSpacing(10)
        if self._missing_tools:
            right_layout.addWidget(
                InfoBar.error(
                    t("status.missing_dependencies_title"),
                    t("status.missing_dependencies_banner", tools="、".join(self._missing_tools)),
                    isClosable=False,
                    duration=-1,
                    position=InfoBarPosition.NONE,
                    parent=right,
                )
            )
        right_layout.addWidget(self.stack, 1)
        right_layout.addWidget(self._build_action_bar())

        central = QWidget()
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self.nav)
        outer.addWidget(right, 1)
        return central

    def _add_route(
        self, key: str, icon: str, text: str, page: QWidget,
        position: NavigationItemPosition = NavigationItemPosition.TOP,
    ) -> None:  # fmt: skip
        self._routes[key] = page
        self.stack.addWidget(page)
        self.nav.addItem(key, fluent.icon(icon), text, lambda: self._go(key), position=position)

    def _build_inputs_page(self) -> QWidget:
        """The sources, single or batch, and every output with its switch and what it makes."""
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

        # as wide as it needs on a large window, narrower before the inputs get cramped
        outputs.setMinimumWidth(OUTPUTS_WIDTH[0])
        outputs.setMaximumWidth(OUTPUTS_WIDTH[1])

        # the inputs on the left, the outputs beside them at their full height
        left = self._inputs_layout = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(self.mode_bar)
        left.addWidget(self.mode)
        left.addStretch(1)
        body = QHBoxLayout()
        body.setSpacing(12)
        body.addLayout(left, 3)
        body.addWidget(outputs, 1)
        title = QHBoxLayout()
        title.addWidget(TitleLabel(t("inputs.title")))
        title.addSpacing(12)
        title.addWidget(
            widgets.hint_label(t("inputs.hint"), wrap=False),
            0,
            Qt.AlignmentFlag.AlignBottom,
        )
        title.addStretch(1)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
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
        # every log record from now on, from any thread, also lands here
        self._log_handler = LogPanelHandler()
        self._log_handler.lines.line.connect(self.log.appendPlainText)
        logging.getLogger().addHandler(self._log_handler)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(widgets.section(t("tasks.section_tasks"), self.tasks))
        self.splitter.addWidget(widgets.section(t("tasks.section_logs"), self.log))
        self.splitter.setChildrenCollapsible(False)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(TitleLabel(t("tasks.title")))
        layout.addWidget(self.splitter, 1)
        return page

    def _go(self, key: str) -> None:
        self.stack.setCurrentWidget(self._routes[key])
        self.nav.setCurrentItem(key)

    def _current_route(self) -> str:
        page = self.stack.currentWidget()
        return next((k for k, w in self._routes.items() if w is page), ROUTE_INPUTS)

    def _build_action_bar(self) -> QWidget:
        bar = widgets.Card()
        row = QHBoxLayout()
        row.setSpacing(10)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.summary = BodyLabel()
        self.summary.setWordWrap(True)
        self.status = CaptionLabel()
        self.status.setTextColor(fluent.dim(), fluent.dim())
        self.status.setWordWrap(True)
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
        self.start_btn.setToolTip("Ctrl+Enter")
        self.start_btn.setMinimumWidth(110)
        self.start_btn.clicked.connect(self._start)
        for button in (self.open_dir_btn, self.cancel_btn, self.start_btn):
            row.addWidget(button)
        bar.set_body(row)
        bar.layout().setContentsMargins(16, 10, 12, 10)
        return bar

    def _mode_changed(self, index: int) -> None:
        self.mode_bar.setCurrentItem(MODES[index])
        self._fit_mode()
        self._sources_changed()

    def _fit_mode(self) -> None:
        """Size the input stack to the shown mode: the single source keeps its own height,
        the batch's track list takes the whole column.
        """
        current = self.mode.currentWidget()
        for index in range(self.mode.count()):
            page = self.mode.widget(index)
            shown = page is current
            policy = QSizePolicy.Policy.Preferred if shown else QSizePolicy.Policy.Ignored
            page.setSizePolicy(policy, policy)
        batch = current is self.batch
        # the stack, then the stretch below it
        self._inputs_layout.setStretch(1, 1 if batch else 0)
        self._inputs_layout.setStretch(2, 0 if batch else 1)
        self.mode.updateGeometry()

    def _show_status(self, text: str) -> None:
        # the summary above already says what is missing before a run
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def _toast(self, text: str) -> None:
        InfoBar.warning(
            "", text, duration=4000, position=InfoBarPosition.TOP, parent=self.centralWidget()
        )

    # ---- public entry points ---------------------------------------------------------

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

    # ---- planning --------------------------------------------------------------------

    @property
    def _is_batch(self) -> bool:
        return self.mode.currentIndex() == BATCH

    def _project(self) -> Project:
        """The project the output pages show: the single source, or the batch's selected track."""
        return self.batch.current_project() if self._is_batch else self.source.project()

    def _context(
        self, project: Project | None = None, batch: bool | None = None, album: bool = False
    ) -> PlanContext:
        project = self._project() if project is None else project
        required: set[str] = set()
        requested_by: list[str] = []
        for panel in self.panels:
            if panel.enabled and (need := panel.required_stems(project)):
                required |= need
                requested_by.append(panel.title)
        batch = self._is_batch if batch is None else batch
        return PlanContext(project, frozenset(required), tuple(requested_by), batch, album)

    def _active(self, ctx: PlanContext) -> list[FeaturePanel]:
        return [panel for panel in self.panels if panel.is_active(ctx)]

    @staticmethod
    def _why_not(ctx: PlanContext, panel: FeaturePanel) -> str | None:
        """Why `panel` cannot make its output for `ctx`: the shared inputs first, then its own."""
        project = ctx.project
        if panel.needs_audio:
            if project.audio is None:
                return t("source.prompt_add_audio") if ctx.batch else t("source.prompt_pick_audio")
            if project.probing:
                return t("status.parsing_audio")
            if project.info is None:
                return t("error.audio_parse_failed", error=project.audio_error or t("error.unknown"))
        return panel.problem(ctx)

    def _problems(self, ctx: PlanContext) -> dict[int, str]:
        """Why each enabled output cannot run, by panel index."""
        return {
            index: problem
            for index, panel in enumerate(self.panels)
            if panel.enabled and (problem := self._why_not(ctx, panel))
        }

    @staticmethod
    def _dir_problem(out: Path | None) -> str | None:
        if out is None:
            return t("error.invalid_output_dir")
        if out.exists() and not out.is_dir():
            return t("error.output_not_dir")
        existing = next((p for p in (out, *out.parents) if p.exists()), None)
        if existing is None or not os.access(existing, os.W_OK):
            return t("error.output_no_write")
        return None

    def _blocker(self, ctx: PlanContext, problems: dict[int, str]) -> str | None:
        """Why Start cannot run right now, or None when it can."""
        if self._missing_tools:
            return t("status.missing_dependencies_blocker", tools="、".join(self._missing_tools))
        if not any(panel.enabled for panel in self.panels):
            return t("status.no_output_selected")
        if self._is_batch:
            return self._batch_blocker()
        if problems:
            index, text = next(iter(problems.items()))
            return f"{self.panels[index].title}：{text}"
        return self._dir_problem(ctx.project.output_dir)

    def _batch_blocker(self) -> str | None:
        if not self.batch.tracks:
            return t("batch.blocker_no_tracks")
        if self.batch.busy:
            return t("batch.reading_track_info")
        if not self._batch_plan().steps:
            return t("batch.blocker_no_runnable")
        return None

    def _plan_track(
        self, project: Project, album: bool
    ) -> tuple[dict[FeaturePanel, list[Step]], list[str]]:
        """One batch track's steps by panel, and the outputs it leaves out with the reason."""
        ctx = self._context(project, batch=True, album=album)
        steps: dict[FeaturePanel, list[Step]] = {}
        left_out: list[str] = []
        for panel in self._active(ctx):
            if album and panel.per_album:
                continue
            if panel.enabled and (p := self._why_not(ctx, panel)):
                left_out.append(f"{panel.title}（{p}）")
                continue
            steps[panel] = panel.plan(ctx)
        return steps, left_out

    def _batch_plan(self) -> BatchPlan:
        plan = BatchPlan()
        album = self.batch.is_album
        album_prefix = t("batch.album_prefix")
        album_project = self.batch.album_project() if album else None
        # what each panel planned for each track, for the steps that close the album
        made: dict[FeaturePanel, list[tuple[Project, list[Step]]]] = {}
        if album_project:
            project = album_project
            ctx = self._context(project, batch=True)
            for panel in self.panels:
                if not (panel.enabled and panel.per_album):
                    continue
                if p := panel.problem(ctx):
                    plan.skipped.append(f"{album_prefix} · {panel.title}：{p}")
                    continue
                steps = panel.plan(ctx)
                plan.steps += [replace(s, title=f"{album_prefix} · {s.title}") for s in steps]
            if plan.steps and project.output_dir:
                plan.output_dirs.append(project.output_dir)
                plan.inputs |= set(project.inputs()) - {project.audio}
        for number, project in enumerate(self.batch.projects(), 1):
            assert project.audio is not None
            plan.inputs |= set(project.inputs())
            name = f"{number:02d} {without_number(project.audio.stem)}"
            if project.probing:
                plan.status.append((t("status.reading"), False))
                continue
            if project.info is None:
                plan.status.append(
                    (t("batch.skipped_prefix", reason=project.audio_error or t("error.cannot_read_short")), True)
                )
                continue
            if dir_problem := self._dir_problem(project.output_dir):
                plan.status.append((t("batch.skipped_prefix", reason=dir_problem), True))
                continue
            by_panel, left_out = self._plan_track(project, album)
            for panel, panel_steps in by_panel.items():
                made.setdefault(panel, []).append((project, panel_steps))
            steps = [step for panel_steps in by_panel.values() for step in panel_steps]
            for note in left_out:
                plan.skipped.append(t("batch.will_not_generate", name=name, note=note))
            if not steps and album and not left_out:
                # all it makes is part of what the album makes as a whole
                plan.tracks += 1
                plan.status.append((t("batch.merged_into_album"), False))
                continue
            if not steps:
                reason = "；".join(left_out) or t("batch.no_outputs_to_generate")
                plan.status.append((t("batch.skipped_prefix", reason=reason), True))
                continue
            plan.steps += [replace(s, title=f"{name} · {s.title}") for s in steps]
            plan.tracks += 1
            assert project.output_dir is not None
            plan.output_dirs.append(project.output_dir)
            text = t("batch.tasks_count", count=len(steps))
            if left_out:
                text += t("batch.not_generating_suffix", notes="、".join(left_out))
            plan.status.append((text, bool(left_out)))
        if album_project:
            ctx = self._context(album_project, batch=True)
            for panel, tracks in made.items():
                closing = panel.album_plan(ctx, tracks)
                plan.steps += [replace(s, title=f"专辑 · {s.title}") for s in closing]
                if closing and album_project.output_dir not in plan.output_dirs:
                    assert album_project.output_dir is not None
                    plan.output_dirs.append(album_project.output_dir)
        return plan

    def _batch_shares(self) -> dict[int, tuple[str, bool]]:
        """For outputs only some tracks can make: how many, and why not the rest.

        The selected track alone would make an output look impossible for the
        whole batch when it is just that track lacking, say, lyrics.
        """
        projects = [p for p in self.batch.projects() if p.info is not None]
        if not projects:
            return {}
        album = self.batch.is_album
        shares: dict[int, tuple[str, bool]] = {}
        for index, panel in enumerate(self.panels):
            if not panel.enabled or album and panel.per_album:
                continue
            missing: dict[str, int] = {}
            for project in projects:
                ctx = self._context(project, batch=True)
                if p := self._why_not(ctx, panel):
                    missing[p] = missing.get(p, 0) + 1
            if not missing:
                continue
            ok = len(projects) - sum(missing.values())
            why = "；".join(t("batch.shares_track_reason", count=n, reason=p) for p, n in missing.items())
            shares[index] = (t("batch.shares_status", ok=ok, total=len(projects), why=why), ok == 0)
        return shares

    def _tracks_changed(self) -> None:
        """A panel setting changed the tracks, as the Atmos render layout does."""
        self.source.layout_changed()
        self.batch.layout_changed()
        self._sources_changed()

    def _sources_changed(self) -> None:
        """Inputs or files on disk changed: let every panel re-read the project."""
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
        shares = self._batch_shares() if self._is_batch else {}
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
        self.summary.setText(t("inputs.to_generate", items="、".join(parts)))

    def _show_page(self, index: int) -> None:
        if index >= 0:
            self._go(f"{ROUTE_OUTPUT}{index}")

    def _output_dir(self) -> Path | None:
        """The folder the open-output button shows."""
        if not self._is_batch:
            return self.source.output_dir()
        return self.batch.output_root() or self.batch.current_project().output_dir

    # ---- running ---------------------------------------------------------------------

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
            assert ctx.project.output_dir is not None  # _blocker() passed
            dirs = [ctx.project.output_dir]
            notes = [note for panel in active for note in panel.notes(ctx)]
        if not steps:
            fluent.tell(self, t("status.no_valid_tasks_title"), t("status.no_valid_tasks_body"))
            return
        if clash := self._clash(inputs, steps):
            fluent.tell(self, t("status.cannot_start"), clash)
            return
        if not self._confirm_overwrite([t for step in steps for t in step.targets]):
            return
        try:
            for out in dirs:
                out.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            fluent.tell(self, t("status.cannot_start"), t("error.cannot_create_dir", error=error))
            return
        self._save_settings()
        logger.info("—— 开始执行 %d 项任务，输出目录：%s", len(steps), self._output_label(dirs))
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
        """Say which tracks or outputs a batch leaves out before it starts."""
        left = len(self.batch.tracks) - plan.tracks
        if not left and not plan.skipped:
            return True
        lines = [t("batch.confirm_summary", tracks=plan.tracks, steps=len(plan.steps))]
        if left:
            lines.append(t("batch.confirm_skipped_tracks", count=left))
        if plan.skipped:
            lines.append(t("batch.confirm_skipped_outputs", count=len(plan.skipped)))
        details = [
            f"{index:02d} {track.audio.name}：{text}"
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

    @staticmethod
    def _clash(inputs: set[Path], steps: list[Step]) -> str | None:
        """Outputs that would overwrite an input, or that two steps would both write."""
        targets = [t.resolve() for step in steps for t in step.targets]
        resolved = {p.resolve() for p in inputs}
        if hit := next((t for t in targets if t in resolved), None):
            return t("error.clash_input", name=hit.name)
        if twice := [t for t, n in Counter(targets).items() if n > 1]:
            return t("error.clash_steps", name=twice[0].name)
        return None

    def _confirm_overwrite(self, targets: list[Path]) -> bool:
        existing = [t for t in targets if t.exists()]
        if not existing:
            return True
        return fluent.ask(
            self,
            t("status.confirm_overwrite_title"),
            t("status.confirm_overwrite_body", count=len(existing)),
            "\n".join(p.name for p in existing),
            t("action.overwrite"),
        )

    def _cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.act_cancel.setEnabled(False)
            self._show_status(t("action.cancelling"))

    def _on_progress(self, index: int, value: int, message: str) -> None:
        self.tasks.progress(index, value, message)
        overall = self.tasks.overall()
        self.progress.setValue(overall)
        self.setWindowTitle(f"{overall}% · {app_title()}")
        self._show_status(f"{self.tasks.topLevelItem(index).text(0)}：{message}")

    def _on_step_finished(self, index: int, outcome: str, note: str, seconds: float) -> None:
        self.tasks.finished(index, outcome, note, seconds)
        self.progress.setValue(self.tasks.overall())
        if outcome == Outcome.DONE:
            # a step's files may be what a later output lists, e.g. stems for the videos
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
            t("status.done"), message, duration=8000, position=InfoBarPosition.TOP,
            parent=self.centralWidget(),
        )  # fmt: skip
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
        for panel, header in zip(self.panels, self.headers, strict=True):
            panel.setEnabled(not running)
            header.check.setEnabled(not running)
        for action in self.input_actions:
            action.setEnabled(not running)
        self.start_btn.setVisible(not running)
        self.act_start.setEnabled(not running)
        self.cancel_btn.setVisible(running)
        self.cancel_btn.setEnabled(running)
        self.act_cancel.setEnabled(running)
        self.progress.setVisible(running)
        self.progress.setValue(0)
        if running:
            self.summary.setText("")

    def _output_label(self, dirs: list[Path] | None = None) -> str:
        """Where a run writes, in words."""
        if self._is_batch and not self.batch.output_root():
            return t("batch.each_output_dir")
        out = self._output_dir() if dirs is None or self._is_batch else dirs[0]
        return str(out)

    def _open_output_dir(self) -> None:
        out = self._output_dir()
        if out and out.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    def _choose_language(self) -> None:
        from qfluentwidgets import RoundMenu
        menu = RoundMenu(parent=self)
        for code, name in LANGUAGE_NAMES.items():
            act = Action(name, self)
            if code == get_language():
                act.setCheckable(True)
                act.setChecked(True)
            act.triggered.connect(lambda _=False, c=code: self._set_language(c))
            menu.addAction(act)
        menu.exec(self.nav.mapToGlobal(self.nav.rect().bottomLeft()))

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
            parent=self.centralWidget(),
        )

    def _about(self) -> None:
        fluent.tell(
            self,
            t("app.about_title"),
            t("app.description"),
        )

    # ---- drag and drop ---------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self.worker and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        audio = [p for p in paths if kind_of(p) == "audio"]
        if self._is_batch and all(kind_of(p) == "ttml" for p in paths):
            if not self.batch.import_lyrics(paths):
                self._toast(t("status.lyrics_already_imported"))
        # a folder, several tracks, or anything dropped on the batch tab goes to the batch
        elif self._is_batch or any(p.is_dir() for p in paths) or len(audio) > 1:
            if not self.add_batch(paths):
                self._toast(t("status.no_new_audio"))
        elif not self.source.drop(paths):
            self._toast(t("status.unsupported_format"))

    # ---- settings --------------------------------------------------------------------

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
        """Close, cancelling a run without the confirmation (Ctrl+C in the terminal)."""
        self._ask_on_close = False
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            if self._ask_on_close and not fluent.ask(
                self, t("status.task_running_title"), t("status.task_running_prompt"), accept=t("action.quit")
            ):
                event.ignore()
                return
            # every step stops its children on cancel, so this does not hang;
            # a QThread destroyed while still running would abort the process
            self.worker.cancel()
            self.worker.wait()
        for panel in self.panels:
            panel.shutdown()
        self._save_settings()
        logging.getLogger().removeHandler(self._log_handler)
        super().closeEvent(event)
