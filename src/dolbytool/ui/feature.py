"""The contract every output panel follows, so the main window never needs to know which is which."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import QDoubleSpinBox, QLineEdit, QSpinBox, QWidget
from qfluentwidgets import CheckBox, CompactDoubleSpinBox, LineEdit

from dolbytool.core.project import PlanContext, Project
from dolbytool.core.task import Step
from dolbytool.ui import fluent, widgets


class FeaturePanel(QWidget):
    """The settings page of one output, which turns the current project into steps.

    Subclasses set the class attributes, build their controls in `__init__`
    and override the hooks they need.  Whether the output is on is the main
    window's switch, read here as `enabled`.  Controls made through `spin()`,
    `check()`, `combo()`, `edit()` and `color()` report changes and are saved
    and restored under their key.
    """

    changed = Signal()
    # a setting of this panel changed which tracks every project has
    tracks_changed = Signal()

    title = ""
    icon = ""
    # one line under the title, saying what the output is
    description = ""
    enabled_key = ""
    enabled_default = True
    # whether ticking this output requires a readable source audio
    needs_audio = True
    # made once per album from what the tracks share (the cover), not once per track
    per_album = False

    def __init__(self, settings: QSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._spins: dict[str, QSpinBox | QDoubleSpinBox] = {}
        self._checks: dict[str, CheckBox] = {}
        self._combos: dict[str, fluent.ComboBox] = {}
        self._edits: dict[str, QLineEdit] = {}
        self._colors: dict[str, widgets.ColorButton] = {}
        self._enabled = self.enabled_default

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        if on != self._enabled:
            self._enabled = on
            self.changed.emit()

    # ---- persisted controls ----------------------------------------------------------

    def spin(
        self, key: str, low: int, high: int, value: int, suffix: str = "", step: int = 1,
        tip: str = "",
    ) -> QSpinBox:  # fmt: skip
        box = widgets.spin(low, high, value, suffix, step)
        box.setToolTip(tip)
        self.remember(key, box)
        return box

    def seconds(
        self, key: str, value: float, low: float, high: float, step: float = 1.0, tip: str = "",
        replan: bool = True,
    ) -> CompactDoubleSpinBox:  # fmt: skip
        """A spin box in seconds, to one decimal; `replan=False` for one that is not part of the output."""
        box = CompactDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(1)
        box.setSingleStep(step)
        box.setValue(value)
        box.setSuffix(" 秒")
        box.setToolTip(tip)
        self.remember(key, box, replan)
        return box

    def remember(self, key: str, box: QSpinBox | QDoubleSpinBox, replan: bool = True) -> None:
        self._spins[key] = box
        if replan:
            box.valueChanged.connect(self.changed)

    def check(self, key: str, text: str, value: bool, tip: str = "") -> CheckBox:
        box = CheckBox(text)
        box.setChecked(value)
        box.setToolTip(tip)
        box.toggled.connect(self.changed)
        self._checks[key] = box
        return box

    def combo(self, key: str, items: dict[str, str], value: str, tip: str = "") -> fluent.ComboBox:
        """A choice among `items` (stored name: label), saved by name."""
        box = fluent.ComboBox()
        for name, label in items.items():
            box.addItem(label, name)
        box.setCurrentIndex(max(0, box.findData(value)))
        box.setToolTip(tip)
        box.currentIndexChanged.connect(self.changed)
        self._combos[key] = box
        return box

    def edit(self, key: str, placeholder: str = "", value: str = "") -> LineEdit:
        line = LineEdit()
        line.setPlaceholderText(placeholder)
        line.setClearButtonEnabled(True)
        line.setText(value)
        self.persist_edit(key, line)
        return line

    def persist_edit(self, key: str, line: QLineEdit) -> None:
        line.textChanged.connect(lambda _t: self.changed.emit())
        self._edits[key] = line

    def color(self, key: str, value: str, title: str) -> widgets.ColorButton:
        button = widgets.ColorButton(value, title)
        button.colorChanged.connect(lambda _c: self.changed.emit())
        self._colors[key] = button
        return button

    # ---- hooks -----------------------------------------------------------------------

    def refresh(self, project: Project) -> None:
        """The sources or the files on disk changed."""

    def required_stems(self, project: Project) -> set[str]:
        """Stems this output needs that are not on disk yet (asked only while enabled)."""
        return set()

    def is_active(self, ctx: PlanContext) -> bool:
        """Whether this panel contributes steps; enabled by default."""
        return self.enabled

    def problem(self, ctx: PlanContext) -> str | None:
        """Why this output cannot run as set up, asked only while enabled."""
        return None

    def summary(self, ctx: PlanContext) -> str | None:
        """What a run would produce, for the output list and the action bar."""
        return None

    def plan(self, ctx: PlanContext) -> list[Step]:
        return []

    def album_plan(
        self, ctx: PlanContext, tracks: Sequence[tuple[Project, list[Step]]]
    ) -> list[Step]:
        """Steps to run once every track of an album is done, given what this panel
        planned for each; `ctx` is the album's.
        """
        return []

    def confirm(self, ctx: PlanContext) -> bool:
        """Last chance to ask the user before the run starts."""
        return True

    def notes(self, ctx: PlanContext) -> list[str]:
        """Log lines to print when the run starts."""
        return []

    def shutdown(self) -> None:
        """The window is closing: stop anything the panel still has running."""

    # ---- settings --------------------------------------------------------------------

    def save(self) -> None:
        s = self.settings
        s.setValue(self.enabled_key, self.enabled)
        for key, box in self._spins.items():
            s.setValue(key, box.value())
        for key, check in self._checks.items():
            s.setValue(key, check.isChecked())
        for key, combo in self._combos.items():
            s.setValue(key, combo.currentData())
        for key, line in self._edits.items():
            s.setValue(key, line.text().strip())
        for key, button in self._colors.items():
            s.setValue(key, button.hex())
        self.save_extra()

    def restore(self) -> None:
        s = self.settings
        self._enabled = s.value(self.enabled_key, self.enabled_default, type=bool)
        # choices first: a choice may reset the range or value of a spin box that follows it
        for key, combo in self._combos.items():
            combo.setCurrentIndex(max(0, combo.findData(s.value(key, combo.currentData()))))
        for key, check in self._checks.items():
            check.setChecked(s.value(key, check.isChecked(), type=bool))
        for key, line in self._edits.items():
            line.setText(s.value(key, line.text(), type=str))
        for key, button in self._colors.items():
            button.setColor(s.value(key, button.hex(), type=str))
        for key, box in self._spins.items():
            kind = float if isinstance(box, QDoubleSpinBox) else int
            box.setValue(s.value(key, box.value(), type=kind))
        self.restore_extra()

    def save_extra(self) -> None:
        pass

    def restore_extra(self) -> None:
        pass
