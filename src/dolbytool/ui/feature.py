"""The contract every output panel follows, so the main window never needs to know which is which."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import QDoubleSpinBox, QSpinBox, QWidget

from dolbytool.core.project import PlanContext, Project
from dolbytool.core.task import Step
from dolbytool.ui import widgets


class FeaturePanel(QWidget):
    """The settings page of one output, which turns the current project into steps.

    Subclasses set the class attributes, build their controls in `__init__`
    and override the hooks they need.  Whether the output is on is the main
    window's checkbox, read here as `enabled`.  Spin boxes created through
    `spin()` are saved and restored automatically.
    """

    changed = Signal()
    # a setting of this panel changed which tracks every project has
    tracks_changed = Signal()
    log = Signal(str)

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
        self._enabled = self.enabled_default

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> None:
        if on != self._enabled:
            self._enabled = on
            self.changed.emit()

    def spin(
        self, key: str, low: int, high: int, value: int, suffix: str = "", step: int = 1
    ) -> QSpinBox:
        box = widgets.spin(low, high, value, suffix, step)
        self.remember(key, box)
        return box

    def remember(self, key: str, box: QSpinBox | QDoubleSpinBox) -> None:
        self._spins[key] = box
        box.valueChanged.connect(self.changed)

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
        self.settings.setValue(self.enabled_key, self.enabled)
        for key, box in self._spins.items():
            self.settings.setValue(key, box.value())
        self.save_extra()

    def restore(self) -> None:
        self._enabled = self.settings.value(self.enabled_key, self.enabled_default, type=bool)
        for key, box in self._spins.items():
            kind = float if isinstance(box, QDoubleSpinBox) else int
            box.setValue(self.settings.value(key, box.value(), type=kind))
        self.restore_extra()

    def save_extra(self) -> None:
        pass

    def restore_extra(self) -> None:
        pass
