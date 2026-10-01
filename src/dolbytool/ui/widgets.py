"""Small reusable widgets and layout helpers shared by the panels."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


def hex_name(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb)


def swatch_icon(color: QColor, size: int = 16) -> QIcon:
    pix = QPixmap(size, size)
    pix.fill(color)
    return QIcon(pix)


class ColorButton(QPushButton):
    """A button showing its colour; clicking opens the colour dialog."""

    colorChanged = Signal(QColor)

    def __init__(self, color: str, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = title
        self._color = QColor(color)
        self.clicked.connect(self._pick)
        self._sync()

    def color(self) -> QColor:
        return QColor(self._color)

    def hex(self) -> str:
        return hex_name(self._color)

    def setColor(self, color: QColor | str) -> None:
        color = QColor(color)
        if color.isValid() and color != self._color:
            self._color = color
            self._sync()
            self.colorChanged.emit(QColor(color))

    def _sync(self) -> None:
        self.setIcon(swatch_icon(self._color))
        self.setText(hex_name(self._color))

    def _pick(self) -> None:
        color = QColorDialog.getColor(self._color, self, self._title)
        if color.isValid():
            self.setColor(color)


def spin(low: int, high: int, value: int, suffix: str = "", step: int = 1) -> QSpinBox:
    box = QSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setSingleStep(step)
    box.setAccelerated(True)
    return box


def browse_row(
    placeholder: str, slot: Callable[[], None], tooltip: str = "浏览…"
) -> tuple[QLineEdit, QToolButton, QHBoxLayout]:
    edit = QLineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(True)
    button = QToolButton()
    button.setIcon(QIcon.fromTheme("document-open-folder"))
    button.setToolTip(tooltip)
    button.clicked.connect(slot)
    row = QHBoxLayout()
    row.setSpacing(4)
    row.addWidget(edit, 1)
    row.addWidget(button)
    return edit, button, row


def hbox(*items: QWidget | str | int, stretch: bool = True) -> QHBoxLayout:
    """A row of widgets; strings become labels, ints become spacing."""
    row = QHBoxLayout()
    for item in items:
        if isinstance(item, str):
            row.addWidget(QLabel(item))
        elif isinstance(item, int):
            row.addSpacing(item)
        else:
            row.addWidget(item)
    if stretch:
        row.addStretch(1)
    return row


def size_row(width: QSpinBox, height: QSpinBox) -> QHBoxLayout:
    return hbox(width, "×", height)


def preview_label(tooltip: str = "", text: str = "", side: int = 120) -> QLabel:
    label = QLabel(text)
    label.setFixedSize(side, side)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setFrameShape(QLabel.Shape.StyledPanel)
    label.setWordWrap(True)
    if tooltip:
        label.setToolTip(tooltip)
    return label


def hint_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setEnabled(False)
    return label


def section(title: str, content: QLayout | QWidget) -> QGroupBox:
    box = QGroupBox(title)
    if isinstance(content, QWidget):
        layout = QVBoxLayout()
        layout.addWidget(content)
        content = layout
    box.setLayout(content)
    return box


def form(rows: Iterable[tuple[str, QWidget | QLayout]]) -> QFormLayout:
    layout = QFormLayout()
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
    for label, field in rows:
        layout.addRow(label, field)
    return layout


Cell = QWidget | QLayout
Row = tuple[str, Cell, str | None, Cell | None]


def two_column_grid(rows: Sequence[Row]) -> QGridLayout:
    """Label/field pairs in two columns; a row with nothing on the right takes the full width."""
    grid = QGridLayout()
    grid.setHorizontalSpacing(12)
    right_align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    for r, (left_label, left, right_label, right) in enumerate(rows):
        grid.addWidget(QLabel(left_label), r, 0, right_align)
        if isinstance(left, QLayout):
            grid.addLayout(left, r, 1, 1, 1 if right is not None else 3)
        else:
            grid.addWidget(left, r, 1, Qt.AlignmentFlag.AlignLeft)
        if right is not None:
            grid.addWidget(QLabel(right_label or ""), r, 2, right_align)
            if isinstance(right, QLayout):
                grid.addLayout(right, r, 3)
            else:
                grid.addWidget(right, r, 3, Qt.AlignmentFlag.AlignLeft)
    grid.setColumnStretch(1, 1)
    grid.setColumnStretch(3, 1)
    return grid


@dataclass(frozen=True, slots=True)
class TrackItem:
    key: str
    text: str
    tooltip: str = ""
    # drawn greyed, e.g. a stem not split yet
    pending: bool = False


class TrackList(QWidget):
    """A checkable list of tracks with select-all / none, remembering what was ticked by key."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setMinimumHeight(110)
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(lambda _item: self.changed.emit())
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        for text, state in (("全选", True), ("全不选", False)):
            button = QPushButton(text)
            button.setFlat(True)
            button.clicked.connect(lambda _=False, on=state: self.check_all(on))
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.count_label = hint_label()
        buttons.addWidget(self.count_label)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list, 0, 0)
        layout.addLayout(buttons, 1, 0)
        self.changed.connect(self._count)

    def is_empty(self) -> bool:
        return self.list.count() == 0

    def checked(self) -> set[str]:
        return {
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        }

    def set_tracks(self, tracks: Sequence[TrackItem], checked: set[str]) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        dim = self.palette().placeholderText()
        for track in tracks:
            item = QListWidgetItem(track.text)
            item.setData(Qt.ItemDataRole.UserRole, track.key)
            item.setToolTip(track.tooltip)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            on = track.key in checked
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            if track.pending:
                item.setForeground(dim)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._count()

    def check_all(self, on: bool) -> None:
        self.list.blockSignals(True)
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)
        self.list.blockSignals(False)
        self.changed.emit()

    def _count(self) -> None:
        total = self.list.count()
        self.count_label.setText(f"已选 {len(self.checked())}/{total}" if total else "")

    def sizeHint(self) -> QSize:
        return QSize(320, 180)
