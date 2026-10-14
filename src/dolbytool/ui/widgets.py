"""Small reusable widgets and layout helpers shared by the panels."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ColorDialog,
    CompactSpinBox,
    FluentIcon,
    HyperlinkButton,
    LineEdit,
    ListWidget,
    PushButton,
    SimpleCardWidget,
    StrongBodyLabel,
    ToolButton,
    TreeWidget,
)

from dolbytool.core.i18n import t
from dolbytool.ui import fluent


def hex_name(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb)


def swatch_icon(color: QColor, size: int = 16) -> QIcon:
    """A rounded colour chip with a faint outline, visible on light and dark alike."""
    scale = 2
    pix = QPixmap(size * scale, size * scale)
    pix.setDevicePixelRatio(scale)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0.5, 0.5, size - 1, size - 1, size / 4, size / 4)
    painter.fillPath(path, color)
    painter.setPen(QColor(128, 128, 128, 110))
    painter.drawPath(path)
    painter.end()
    return QIcon(pix)


class ColorButton(PushButton):
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
        dialog = ColorDialog(self._color, self._title, self.window())
        dialog.yesButton.setText(t("action.confirm"))
        dialog.cancelButton.setText(t("action.cancel"))
        dialog.colorChanged.connect(self.setColor)
        dialog.exec()


def spin(low: int, high: int, value: int, suffix: str = "", step: int = 1) -> CompactSpinBox:
    box = CompactSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setSingleStep(step)
    box.setAccelerated(True)
    return box


def browse_row(
    placeholder: str, slot: Callable[[], None], tooltip: str = ""
) -> tuple[LineEdit, ToolButton, QHBoxLayout]:
    edit = LineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(True)
    button = ToolButton(FluentIcon.FOLDER)
    button.setToolTip(tooltip or t("action.browse"))
    button.clicked.connect(slot)
    row = QHBoxLayout()
    row.setSpacing(6)
    row.addWidget(edit, 1)
    row.addWidget(button)
    return edit, button, row


def hbox(*items: QWidget | str | int, stretch: bool = True, spacing: int = 16) -> QHBoxLayout:
    """A row of widgets; strings become labels, ints become spacing."""
    row = QHBoxLayout()
    # Fluent check boxes and combo boxes carry no margin of their own
    row.setSpacing(spacing)
    for item in items:
        if isinstance(item, str):
            row.addWidget(BodyLabel(item))
        elif isinstance(item, int):
            row.addSpacing(item)
        else:
            row.addWidget(item)
    if stretch:
        row.addStretch(1)
    return row


def size_row(width: CompactSpinBox, height: CompactSpinBox) -> QHBoxLayout:
    return hbox(width, "×", height, spacing=6)


class HintTree(TreeWidget):
    """A tree that says what belongs in it while it is empty."""

    def __init__(self, empty_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.empty_text = empty_text

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.topLevelItemCount():
            return
        painter = QPainter(self.viewport())
        painter.setPen(fluent.dim())
        painter.drawText(
            self.viewport().rect().adjusted(16, 16, -16, -16),
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
            self.empty_text,
        )
        painter.end()


# a framed box for a picture, readable as a box even while empty
PREVIEW_STYLE = (
    "border: 1px solid rgba(128, 128, 128, 0.35); border-radius: 6px;"
    "background: rgba(128, 128, 128, 0.08);"
)


def preview_label(tooltip: str = "", text: str = "", side: int = 120) -> QLabel:
    label = CaptionLabel(text)
    label.setFixedSize(side, side)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet(PREVIEW_STYLE)
    label.setWordWrap(True)
    if tooltip:
        label.setToolTip(tooltip)
    return label


def hint_label(text: str = "", wrap: bool = True) -> CaptionLabel:
    """Secondary text; `wrap=False` for a short note in a row, which wrapping would chop up."""
    label = CaptionLabel(text)
    label.setWordWrap(wrap)
    fluent.secondary(label)
    return label


class Card(SimpleCardWidget):
    """A Fluent card with an optional title, holding one body layout or widget."""

    def __init__(self, title: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(16, 12, 16, 14)
        self._layout.setSpacing(10)
        if title:
            self._layout.addWidget(StrongBodyLabel(title))

    def set_body(self, content: QLayout | QWidget, stretch: int = 1) -> None:
        if isinstance(content, QWidget):
            self._layout.addWidget(content, stretch)
        else:
            self._layout.addLayout(content, stretch)


def section(title: str, content: QLayout | QWidget) -> Card:
    card = Card(title)
    card.set_body(content)
    return card


def form(rows: Iterable[tuple[str, QWidget | QLayout]]) -> QFormLayout:
    layout = QFormLayout()
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
    layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    layout.setHorizontalSpacing(12)
    layout.setVerticalSpacing(8)
    for label, field in rows:
        layout.addRow(BodyLabel(label), field)
    return layout


Cell = QWidget | QLayout
Row = tuple[str, Cell, str | None, Cell | None]


def two_column_grid(rows: Sequence[Row]) -> QGridLayout:
    """Label/field pairs in two columns kept side by side, the slack after them.

    A row with nothing on the right takes the full width; a text field there
    stretches across it, other fields keep their own size.
    """
    grid = QGridLayout()
    grid.setHorizontalSpacing(12)
    grid.setVerticalSpacing(8)
    right_align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
    for r, (left_label, left, right_label, right) in enumerate(rows):
        grid.addWidget(BodyLabel(left_label), r, 0, right_align)
        if right is None and isinstance(left, (QLayout, QLineEdit)):
            if isinstance(left, QLayout):
                grid.addLayout(left, r, 1, 1, 4)
            else:
                grid.addWidget(left, r, 1, 1, 4)
        elif isinstance(left, QLayout):
            grid.addLayout(left, r, 1)
        else:
            grid.addWidget(left, r, 1, Qt.AlignmentFlag.AlignLeft)
        if right is not None:
            label = BodyLabel(right_label or "")
            # a gap between the two pairs, which would otherwise touch
            label.setContentsMargins(36, 0, 0, 0)
            grid.addWidget(label, r, 2, right_align)
            if isinstance(right, QLayout):
                grid.addLayout(right, r, 3)
            else:
                grid.addWidget(right, r, 3, Qt.AlignmentFlag.AlignLeft)
    grid.setColumnStretch(4, 1)
    return grid


# rows a track list grows to before it scrolls
_MAX_ROWS = 8


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
        self.list = ListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(lambda _item: self._remember())
        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        for text, state in ((t("action.select_all"), True), (t("action.deselect_all"), False)):
            button = HyperlinkButton()
            button.setText(text)
            button.clicked.connect(lambda _=False, on=state: self.check_all(on))
            buttons.addWidget(button)
        buttons.addStretch(1)
        self.count_label = hint_label(wrap=False)
        buttons.addWidget(self.count_label)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list, 0, 0)
        layout.setVerticalSpacing(4)
        layout.addLayout(buttons, 1, 0)
        self.changed.connect(self._count)
        # ticked keys, including those of layouts not shown right now
        self._memory: set[str] = set()

    def is_empty(self) -> bool:
        return self.list.count() == 0

    def checked(self) -> set[str]:
        return {
            self.list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.list.count())
            if self.list.item(i).checkState() == Qt.CheckState.Checked
        }

    def seed(self, keys: set[str]) -> None:
        """Start from remembered keys, before any layout has been shown."""
        self._memory = set(keys)

    def keys(self) -> set[str]:
        """Every ticked key, also those the current layout does not show."""
        return set(self._memory)

    def set_tracks(
        self, tracks: Sequence[TrackItem], checked: set[str], remember: bool = True
    ) -> None:
        """Show `tracks` ticked by `checked`; `remember=False` shows a default without keeping it."""
        if remember:
            shown = {track.key for track in tracks}
            self._memory = (self._memory - shown) | (checked & shown)
        self.list.blockSignals(True)
        self.list.clear()
        dim = fluent.dim()
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
        self._fit()

    def _fit(self) -> None:
        """As tall as the tracks, so a layout's stems show without scrolling, up to a point."""
        row = self.list.sizeHintForRow(0) if self.list.count() else 38
        rows = min(max(self.list.count(), 3), _MAX_ROWS)
        self.list.setFixedHeight(rows * row + 2 * self.list.frameWidth() + 10)

    def check_all(self, on: bool) -> None:
        self.list.blockSignals(True)
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(state)
        self.list.blockSignals(False)
        self._remember()

    def _remember(self) -> None:
        shown = {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())}
        self._memory = (self._memory - shown) | self.checked()
        self.changed.emit()

    def _count(self) -> None:
        total = self.list.count()
        self.count_label.setText(
            t("widgets.selected_count", selected=len(self.checked()), total=total) if total else ""
        )

