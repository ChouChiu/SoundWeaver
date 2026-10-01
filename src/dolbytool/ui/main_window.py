"""Main window: pick the inputs, tick the outputs, press Start.

Plain Qt widgets only, so the app takes the desktop's own style, colours and
file dialogs (Breeze on KDE).  The window only composes: the source panel
owns the inputs, each output panel owns its settings and its steps, the
worker runs them and the task list shows how they go.
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dolbytool.core import ffmpeg
from dolbytool.core.project import PlanContext
from dolbytool.core.task import Step
from dolbytool.features.registry import output_panels
from dolbytool.features.source.panel import SourcePanel
from dolbytool.ui import widgets
from dolbytool.ui.feature import FeaturePanel
from dolbytool.ui.outputs import OutputList
from dolbytool.ui.tasks import TaskView
from dolbytool.ui.worker import Outcome, Worker

APP_TITLE = "杜比视频素材工具"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setAcceptDrops(True)
        self.settings = QSettings()
        self.worker: Worker | None = None
        self._restoring = True
        self._missing_tools = ffmpeg.missing_tools()

        self.source = SourcePanel(self.settings)
        self.panels = output_panels(self.settings)
        self.source.changed.connect(self._sources_changed)
        self.source.log.connect(self._log)
        for panel in self.panels:
            panel.changed.connect(self._update_ready)
            panel.log.connect(self._log)

        self._build_actions()
        self._build_menu()
        self.setCentralWidget(self._build_body())
        self.statusBar().showMessage("准备就绪。请选择或拖入音频文件")

        self._restore_settings()
        self._restoring = False
        self._sources_changed()

    # ---- layout ----------------------------------------------------------------------

    def _build_actions(self) -> None:
        def action(text: str, icon: str, slot, shortcut=None) -> QAction:
            act = QAction(QIcon.fromTheme(icon), text, self)
            if shortcut is not None:
                act.setShortcut(shortcut)
            act.triggered.connect(slot)
            return act

        self.act_audio = action("打开音频(&A)…", "document-open", self.source.pick_audio,
                                QKeySequence.StandardKey.Open)  # fmt: skip
        self.act_cover = action("选择封面(&C)…", "insert-image", self.source.pick_cover,
                                QKeySequence("Ctrl+Shift+O"))  # fmt: skip
        self.act_ttml = action("选择歌词(&L)…", "text-xml", self.source.pick_ttml,
                                QKeySequence("Ctrl+L"))  # fmt: skip
        self.act_open_dir = action("打开输出目录(&D)", "folder-open", self._open_output_dir,
                                   QKeySequence("Ctrl+E"))  # fmt: skip
        self.act_quit = action(
            "退出(&Q)", "application-exit", self.close, QKeySequence.StandardKey.Quit
        )
        self.act_start = action(
            "开始(&S)", "media-playback-start", self._start, QKeySequence("Ctrl+Return")
        )
        self.act_cancel = action("取消(&X)", "process-stop", self._cancel, QKeySequence("Escape"))
        self.act_cancel.setEnabled(False)
        self.act_about = action("关于(&A)", "help-about", self._about)
        self.input_actions = (self.act_audio, self.act_cover, self.act_ttml)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")
        file_menu.addActions([*self.input_actions, self.act_open_dir])
        file_menu.addSeparator()
        file_menu.addAction(self.act_quit)
        run_menu = self.menuBar().addMenu("运行(&R)")
        run_menu.addActions([self.act_start, self.act_cancel])
        self.menuBar().addMenu("帮助(&H)").addAction(self.act_about)

    def _build_body(self) -> QWidget:
        # left: inputs and the output checklist; right: the selected output's page
        self.outputs = OutputList(self.panels)
        self.outputs.selected.connect(self._show_page)
        left = QWidget()
        left.setMinimumWidth(420)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.source)
        outputs_label = QLabel("输出")
        bold = QFont(outputs_label.font())
        bold.setBold(True)
        outputs_label.setFont(bold)
        left_layout.addWidget(outputs_label)
        left_layout.addWidget(self.outputs, 1)

        self.pages = QStackedWidget()
        for panel in self.panels:
            self.pages.addWidget(self._page(panel))

        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(left)
        top.addWidget(self.pages)
        top.setStretchFactor(0, 1)
        top.setStretchFactor(1, 1)
        top.setChildrenCollapsible(False)

        # bottom: what the run does and what it said
        self.tasks = TaskView()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("运行日志")
        self.log.setMaximumBlockCount(5000)
        self.bottom = QTabWidget()
        self.bottom.addTab(self.tasks, QIcon.fromTheme("view-list-details"), "任务")
        self.bottom.addTab(self.log, QIcon.fromTheme("text-x-log"), "日志")

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(top)
        self.splitter.addWidget(self.bottom)
        self.splitter.setStretchFactor(0, 4)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setChildrenCollapsible(False)
        self.top_splitter = top

        central = QWidget()
        outer = QVBoxLayout(central)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet("QLabel { background: #da4453; color: white; padding: 6px; }")
        self.banner.setVisible(bool(self._missing_tools))
        self.banner.setText(
            f"未检测到系统依赖组件 {'、'.join(self._missing_tools)}，请确保已安装 FFmpeg 并将其加入系统 PATH 环境变量"
        )
        outer.addWidget(self.banner)
        outer.addWidget(self.splitter, 1)
        outer.addLayout(self._build_action_row())
        return central

    def _page(self, panel: FeaturePanel) -> QWidget:
        title = QLabel(panel.title)
        font = QFont(title.font())
        font.setPointSizeF(font.pointSizeF() * 1.3)
        font.setBold(True)
        title.setFont(font)
        icon = QLabel()
        icon.setPixmap(QIcon.fromTheme(panel.icon).pixmap(32, 32))
        header = QHBoxLayout()
        header.addWidget(icon)
        header.addWidget(title)
        header.addStretch(1)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addLayout(header)
        layout.addWidget(widgets.hint_label(panel.description))
        layout.addWidget(panel, 1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(content)
        return scroll

    def _build_action_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        row.addWidget(self.summary, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.hide()
        row.addWidget(self.progress)
        self.open_dir_btn = QPushButton(QIcon.fromTheme("folder-open"), "打开输出目录")
        self.open_dir_btn.clicked.connect(self._open_output_dir)
        self.cancel_btn = QPushButton(QIcon.fromTheme("process-stop"), "取消")
        self.cancel_btn.clicked.connect(self._cancel)
        self.cancel_btn.hide()
        self.start_btn = QPushButton(QIcon.fromTheme("media-playback-start"), "开始")
        self.start_btn.setToolTip("Ctrl+Enter")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._start)
        for button in (self.open_dir_btn, self.cancel_btn, self.start_btn):
            row.addWidget(button)
        return row

    # ---- public entry points ---------------------------------------------------------

    def set_audio(self, path: Path) -> None:
        self.source.set_audio(path)

    def set_cover(self, path: Path) -> None:
        self.source.set_cover(path)

    # ---- planning --------------------------------------------------------------------

    def _context(self) -> PlanContext:
        project = self.source.project()
        required: set[str] = set()
        requested_by: list[str] = []
        for panel in self.panels:
            if panel.enabled and (need := panel.required_stems(project)):
                required |= need
                requested_by.append(panel.title)
        return PlanContext(project, frozenset(required), tuple(requested_by))

    def _active(self, ctx: PlanContext) -> list[FeaturePanel]:
        return [panel for panel in self.panels if panel.is_active(ctx)]

    def _input_problem(self, ctx: PlanContext, panel: FeaturePanel) -> str | None:
        """What the shared inputs lack for `panel`."""
        project = ctx.project
        if not panel.needs_audio:
            return None
        if project.audio is None:
            return "请选择音频文件"
        if project.probing:
            return "正在解析音频…"
        if project.info is None:
            return f"无法解析音频：{project.audio_error}" if project.audio_error else "无法解析音频"
        return None

    def _problems(self, ctx: PlanContext) -> dict[int, str]:
        """Why each enabled output cannot run, by panel index."""
        out = {}
        for index, panel in enumerate(self.panels):
            if panel.enabled and (p := self._input_problem(ctx, panel) or panel.problem(ctx)):
                out[index] = p
        return out

    def _blocker(self, ctx: PlanContext, problems: dict[int, str]) -> str | None:
        """Why Start cannot run right now, or None when it can."""
        if self._missing_tools:
            return f"缺少系统依赖 {'、'.join(self._missing_tools)}"
        if not any(panel.enabled for panel in self.panels):
            return "请勾选至少一项输出内容"
        if problems:
            index, text = next(iter(problems.items()))
            return f"{self.panels[index].title}：{text}"
        out = ctx.project.output_dir
        if out is None:
            return "请指定有效的音频文件或输出目录"
        if out.exists() and not out.is_dir():
            return "指定的输出路径不是目录"
        existing = next((p for p in (out, *out.parents) if p.exists()), None)
        if existing is None or not os.access(existing, os.W_OK):
            return "输出目录无写入权限"
        return None

    def _sources_changed(self) -> None:
        """Inputs or files on disk changed: let every panel re-read the project."""
        project = self.source.project()
        for panel in self.panels:
            panel.refresh(project)
        self._update_ready()

    def _update_ready(self) -> None:
        if self._restoring:
            return
        ctx = self._context()
        out = ctx.project.output_dir
        has_dir = bool(out and out.is_dir())
        self.open_dir_btn.setEnabled(has_dir)
        self.act_open_dir.setEnabled(has_dir)
        problems = self._problems(ctx)
        for index, panel in enumerate(self.panels):
            if index in problems:
                self.outputs.set_status(index, problems[index], problem=True)
            elif panel.is_active(ctx):
                self.outputs.set_status(index, panel.summary(ctx) or "")
            else:
                self.outputs.set_status(index, "未启用")
        if self.worker:
            return
        blocker = self._blocker(ctx, problems)
        self.start_btn.setEnabled(blocker is None)
        self.act_start.setEnabled(blocker is None)
        if blocker:
            self.summary.setText(blocker)
            return
        parts = [s for panel in self._active(ctx) if (s := panel.summary(ctx))]
        self.summary.setText("待生成：" + "、".join(parts))

    def _show_page(self, index: int) -> None:
        if index >= 0:
            self.pages.setCurrentIndex(index)

    # ---- running ---------------------------------------------------------------------

    def _start(self) -> None:
        if self.worker:
            return
        ctx = self._context()
        blocker = self._blocker(ctx, self._problems(ctx))
        if blocker:
            QMessageBox.warning(self, "无法开始", blocker)
            return
        active = self._active(ctx)
        if not all(panel.confirm(ctx) for panel in active):
            return
        steps = [step for panel in active for step in panel.plan(ctx)]
        if not steps:
            QMessageBox.information(self, "未选择有效任务", "当前配置未勾选任何需要生成的输出项")
            return
        if clash := self._clash(ctx, steps):
            QMessageBox.warning(self, "无法开始", clash)
            return
        if not self._confirm_overwrite([t for step in steps for t in step.targets]):
            return
        out = ctx.project.output_dir
        assert out is not None  # _blocker() passed
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            QMessageBox.warning(self, "无法开始", f"无法创建输出目录：{error}")
            return
        self._save_settings()
        self._log(f"—— 开始执行 {len(steps)} 项任务，输出目录：{out}")
        for panel in active:
            for note in panel.notes(ctx):
                self._log(note)
        self.tasks.set_steps(steps)
        self.bottom.setCurrentWidget(self.tasks)
        self.worker = Worker(steps, self)
        self.worker.step_started.connect(self.tasks.started)
        self.worker.step_progress.connect(self._on_progress)
        self.worker.step_finished.connect(self._on_step_finished)
        self.worker.log.connect(self._log)
        self.worker.finished_with.connect(self._on_finished)
        self._set_running(True)
        self.worker.start()

    @staticmethod
    def _clash(ctx: PlanContext, steps: list[Step]) -> str | None:
        """Outputs that would overwrite an input, or that two steps would both write."""
        targets = [t.resolve() for step in steps for t in step.targets]
        inputs = {p.resolve() for p in ctx.project.inputs()}
        if hit := next((t for t in targets if t in inputs), None):
            return f"输出文件与源输入文件重名（{hit.name}），请指定其他输出目录"
        if twice := [t for t, n in Counter(targets).items() if n > 1]:
            return f"多个输出任务指向相同的文件（{twice[0].name}），请调整配置"
        return None

    def _confirm_overwrite(self, targets: list[Path]) -> bool:
        existing = [t for t in targets if t.exists()]
        if not existing:
            return True
        box = QMessageBox(
            QMessageBox.Icon.Question,
            "确认覆盖文件",
            f"检测到 {len(existing)} 个目标文件已存在，是否确认覆盖？",
            QMessageBox.StandardButton.Cancel,
            self,
        )
        box.setDetailedText("\n".join(p.name for p in existing))
        overwrite = box.addButton("覆盖(&O)", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is overwrite

    def _cancel(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.act_cancel.setEnabled(False)
            self.statusBar().showMessage("正在取消…")

    def _on_progress(self, index: int, value: int, message: str) -> None:
        self.tasks.progress(index, value, message)
        overall = self.tasks.overall()
        self.progress.setValue(overall)
        self.setWindowTitle(f"{overall}% · {APP_TITLE}")
        self.statusBar().showMessage(f"{self.tasks.topLevelItem(index).text(0)}：{message}")

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
        self.setWindowTitle(APP_TITLE)
        self._sources_changed()
        failures = self.tasks.failures()
        message = {
            Outcome.DONE: f"全部任务完成，保存在 {self.source.output_dir()}",
            Outcome.CANCELLED: "任务已取消",
            Outcome.FAILED: f"处理完成（{len(failures)} 项失败）",
        }[Outcome(outcome)]
        self.statusBar().showMessage(message)
        self._log(f"—— {message}")
        QApplication.alert(self)
        if failures:
            box = QMessageBox(QMessageBox.Icon.Warning, "部分任务执行失败", message, parent=self)
            box.setInformativeText("其余任务已完成，详细失败信息请查看下方列表与运行日志")
            box.setDetailedText("\n".join(failures))
            box.exec()

    def _set_running(self, running: bool) -> None:
        self.source.set_running(running)
        self.outputs.set_locked(running)
        for index in range(self.pages.count()):
            self.pages.widget(index).setEnabled(not running)
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

    def _open_output_dir(self) -> None:
        out = self.source.output_dir()
        if out and out.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    def _log(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"关于 {APP_TITLE}",
            "杜比全景声与多声道视频素材制作工具\n\n"
            "支持分轨导出、ADM BWF、封面渐变图、滚动波形视频、动态字幕与 3D 声场可视化。\n\n"
            "基于 AGPL-3.0-or-later 协议开源",
        )

    # ---- drag and drop ---------------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self.worker and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if not self.source.drop(paths):
            self.statusBar().showMessage("不支持的文件格式", 5000)

    # ---- settings --------------------------------------------------------------------

    def _save_settings(self) -> None:
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.settings.setValue("top_splitter", self.top_splitter.saveState())
        self.settings.setValue("page", self.outputs.currentRow())
        for panel in self.panels:
            panel.save()

    def _restore_settings(self) -> None:
        if not self.restoreGeometry(self.settings.value("geometry", b"")):
            self.resize(1180, 860)
        if not self.splitter.restoreState(self.settings.value("splitter", b"")):
            self.splitter.setSizes([620, 200])
        self.top_splitter.restoreState(self.settings.value("top_splitter", b""))
        for panel in self.panels:
            panel.restore()
        self.outputs.sync(self.panels)
        page = self.settings.value("page", 0, type=int)
        self.outputs.setCurrentRow(page if 0 <= page < len(self.panels) else 0)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker:
            answer = QMessageBox.question(
                self, "任务运行中", "后台任务尚未完成，确定要取消任务并退出吗？"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # every step stops its children on cancel, so this does not hang;
            # a QThread destroyed while still running would abort the process
            self.worker.cancel()
            self.worker.wait()
        self._save_settings()
        super().closeEvent(event)
