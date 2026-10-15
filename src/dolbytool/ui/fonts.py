"""The font fallback list shared by the lyric and the player video, and its family picker."""

from __future__ import annotations

import shutil
import subprocess
from functools import cache

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCompleter,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    EditableComboBox,
    ListWidget,
    PushButton,
    ToolButton,
    setCustomStyleSheet,
)
from qfluentwidgets.components.widgets.combo_box import ComboBoxMenu
from qfluentwidgets.components.widgets.menu import MenuAnimationType

from dolbytool.core.i18n import t
from dolbytool.ui import fluent, widgets

# what the completer matches on: every name of the family, not just the one shown
_SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1


@cache
def font_names() -> dict[str, frozenset[str]]:
    """Installed families, by the name to show and hand the browser, with every name they go by.

    Qt lists one name per family, the localised one under a Chinese locale (苹方-简, not
    PingFang SC); fontconfig knows them all.  The shown name is the first Latin one, which
    the browser resolves the same way and which reads the same in every locale.
    """
    qt = set(QFontDatabase.families())
    aliases: dict[str, set[str]] = {family: {family} for family in qt}
    if shutil.which("fc-list"):
        try:
            out = subprocess.run(
                ["fc-list", "--format", "%{family}\\n"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            names = [name.strip() for name in line.split(",") if name.strip()]
            for family in (name for name in names if name in qt):
                aliases[family].update(names)
    shown: dict[str, set[str]] = {}
    for family, names in aliases.items():
        # fontconfig puts the base family before style variants (PingFang SC before PingFang SC Light)
        latin = [name for name in names if name.isascii()]
        name = family if family.isascii() else min(latin, key=len, default=family)
        shown.setdefault(name, set()).update(names)
    return {
        name: frozenset(names)
        for name, names in sorted(shown.items(), key=lambda kv: kv[0].casefold())
    }


class _FontMenu(ComboBoxMenu):
    """The drop-down, built in linear time.

    RoundMenu re-measures the whole list and rescans every action for an icon on each
    item it adds: with a thousand families the menu took a second or more to open.
    Fonts carry no icons, and exec() measures the list once before showing it.
    """

    def _hasItemIcon(self) -> bool:
        return False

    def addAction(self, action) -> None:
        QListWidget.addItem(self.view, self._createActionItem(action))

    # PySide resolves exec() on a second-level subclass to QMenu's own, which has no aniType
    def exec(self, pos, ani=True, aniType=MenuAnimationType.DROP_DOWN):
        return ComboBoxMenu.exec(self, pos, ani, aniType)


class _FontPicker(EditableComboBox):
    """Enter adds the family instead of putting whatever was typed into the drop-down."""

    submitted = Signal()

    def _createComboMenu(self) -> ComboBoxMenu:
        return _FontMenu(self)

    def _onReturnPressed(self) -> None:
        # with the completion menu open, Enter picks from it; that key press reaches here first
        menu = self._completerMenu
        if not (menu and menu.isVisible()):
            self.submitted.emit()


class FontList(QWidget):
    """Ordered font fallback list: pick a family, add it, drag or move to reorder."""

    def __init__(self) -> None:
        super().__init__()
        self.list = ListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setMaximumHeight(132)
        self.list.setMinimumHeight(96)
        # an empty Fluent list draws nothing at all; outline it so it reads as a box
        setCustomStyleSheet(
            self.list,
            "ListWidget { border: 1px solid rgba(0, 0, 0, 0.12); border-radius: 6px;"
            " background: rgba(0, 0, 0, 0.02); }",
            "ListWidget { border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 6px;"
            " background: rgba(255, 255, 255, 0.03); }",
        )
        self.list.setToolTip(t("fonts.fallback_tip"))
        # type to filter: there can be hundreds of families
        names = font_names()
        # any name of a family finds it, in any case: 苹方, pingfang, PingFang SC
        self._lookup = {
            alias.casefold(): name for name, aliases in names.items() for alias in aliases
        }
        # a shown name always means itself, whichever family also lists it as an alias
        self._lookup.update({name.casefold(): name for name in names})
        self.pick = _FontPicker()
        self.pick.setPlaceholderText(t("fonts.placeholder"))
        self.pick.addItems(list(names))
        self.pick.setMaxVisibleItems(12)
        model = QStandardItemModel(self.pick)
        for name, aliases in names.items():
            item = QStandardItem(name)
            item.setData(" / ".join(sorted(aliases)), _SEARCH_ROLE)
            model.appendRow(item)
        completer = QCompleter(model, self.pick)
        completer.setCompletionRole(_SEARCH_ROLE)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setMaxVisibleItems(12)
        self.pick.setCompleter(completer)
        self.pick.setText(self._resolve(QFont().defaultFamily()) or "")
        self.pick.submitted.connect(self._add)
        add = PushButton(fluent.icon("list-add"), t("action.add"))
        add.clicked.connect(self._add)
        buttons = QVBoxLayout()
        buttons.setSpacing(8)
        pick_row = QHBoxLayout()
        pick_row.addWidget(self.pick, 1)
        pick_row.addWidget(add)
        buttons.addLayout(pick_row)
        moves = QHBoxLayout()
        for icon, tip, step in (
            ("go-up", t("action.move_up"), -1),
            ("go-down", t("action.move_down"), 1),
        ):
            button = ToolButton(fluent.icon(icon))
            button.setToolTip(tip)
            button.clicked.connect(lambda _=False, step=step: self._move(step))
            moves.addWidget(button)
        remove = PushButton(fluent.icon("list-remove"), t("action.remove"))
        remove.clicked.connect(self._remove)
        moves.addWidget(remove)
        moves.addStretch(1)
        buttons.addLayout(moves)
        self.empty = widgets.hint_label(t("fonts.fallback_hint"))
        buttons.addWidget(self.empty)
        buttons.addStretch(1)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons, 1)

    def families(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_families(self, families: list[str]) -> None:
        self.list.clear()
        for family in families:
            item = QListWidgetItem(family)
            item.setFont(QFont(family))
            self.list.addItem(item)

    def _resolve(self, typed: str) -> str | None:
        return self._lookup.get(typed.strip().casefold())

    def _add(self) -> None:
        family = self._resolve(self.pick.currentText())
        if family is None:
            return
        self.pick.setText(family)
        if family not in self.families():
            self.set_families([*self.families(), family])
        self.list.setCurrentRow(self.families().index(family))

    def _move(self, step: int) -> None:
        row = self.list.currentRow()
        target = row + step
        if row < 0 or not 0 <= target < self.list.count():
            return
        self.list.insertItem(target, self.list.takeItem(row))
        self.list.setCurrentRow(target)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
