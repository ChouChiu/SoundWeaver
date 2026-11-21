"""Small reusable widgets and layout helpers shared by the panels."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QButtonGroup,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QStackedLayout,
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
    PushButton,
    SegmentedWidget,
    SimpleCardWidget,
    StrongBodyLabel,
    SwitchButton,
    ToolButton,
    TreeWidget,
    setCustomStyleSheet,
)

from soundweaver.core.i18n import t
from soundweaver.ui import fluent
from soundweaver.ui.scrolling import DirectListWidget, use_direct_wheel

# One spacing scale for every page, so the pages line up with each other.
# Page edge (left, top, right, bottom) inside stackedWidget's frame.
PAGE_MARGINS = (16, 16, 16, 16)
# between the cards of a page, and between a title and what it heads
SECTION_SPACING = 16
# inside a card: its edge, and the gap between its rows
CARD_MARGINS = (16, 12, 16, 12)
ROW_SPACING = 8
# between a label and its field, between controls in one row, and before a
# second label/field pair in the same row
FIELD_SPACING = 12
INLINE_SPACING = 8
PAIR_GAP = 24


def focus_ring(control: QWidget) -> None:
    """A visible keyboard focus border on Fluent controls whose paint leaves none.

    Qt draws the focus rect only for some styles, and the custom Fluent paint
    replaces it; a stylesheet border keeps the indicator visible on every theme.
    """
    # QSS matches Qt class names, which the Fluent subclasses inherit from
    if isinstance(control, PushButton):
        kind = "QPushButton"
    elif isinstance(control, LineEdit):
        kind = "QLineEdit"
    elif control.inherits("QCheckBox"):
        kind = "QCheckBox"
    elif control.inherits("QComboBox"):
        kind = "QComboBox"
    elif control.inherits("QAbstractSpinBox"):
        kind = "QAbstractSpinBox"
    else:
        return
    rule = f"{kind}:focus {{ border: 1px solid %s; }}"
    setCustomStyleSheet(
        control,
        rule % fluent.focus_stroke_hex(False),
        rule % fluent.focus_stroke_hex(True),
    )


def name_control(control: QWidget, text: str) -> None:
    """Name a control whose purpose is otherwise conveyed only by an icon or its position."""
    control.setAccessibleName(text)
    control.setToolTip(text)
    control.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    if isinstance(control, ToolButton):
        setCustomStyleSheet(
            control,
            f"QToolButton:focus {{ border: 2px solid {fluent.focus_stroke_hex(False)}; }}",
            f"QToolButton:focus {{ border: 2px solid {fluent.focus_stroke_hex(True)}; }}",
        )


class _FocusOutline(QWidget):
    """Draw focus around Fluent controls whose own paint omits it."""

    def __init__(self, control: QWidget) -> None:
        super().__init__(control)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setGeometry(control.rect())
        self.hide()
        control.installEventFilter(self)
        fluent.on_theme_change(self.update)

    def eventFilter(self, control: QWidget, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.FocusIn:
            self.show()
            self.raise_()
        elif kind == QEvent.Type.FocusOut:
            self.hide()
        elif kind == QEvent.Type.Resize:
            self.setGeometry(control.rect())
        return False

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(fluent.focus_stroke(), 2))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 4, 4)


class _KeyboardActivation(QObject):
    def __init__(self, control: QWidget, activate: Callable[[], None]) -> None:
        super().__init__(control)
        self._activate = activate
        control.installEventFilter(self)

    def eventFilter(self, control: QWidget, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() in (
            Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter
        ):
            if not event.isAutoRepeat():
                self._activate()
            return True
        return False


def keyboard_activate(control: QWidget, text: str, activate: Callable[[], None]) -> None:
    """Give custom-painted click targets a tab stop and standard activation keys."""
    name_control(control, text)
    _FocusOutline(control)
    _KeyboardActivation(control, activate)


def name_switch(switch: SwitchButton, text: str) -> None:
    """Expose the checkable child as the switch's keyboard stop and accessible control."""
    switch.setAccessibleName(text)
    switch.indicator.setAccessibleName(text)
    switch.indicator.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    _FocusOutline(switch.indicator)


class SegmentedChoice(SegmentedWidget):
    """Expose the selected segment as a checked button and support arrow navigation."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._group = QButtonGroup(self)
        self.currentItemChanged.connect(lambda key: self.widget(key).setChecked(True))

    def add_choice(
        self, key: str, text: str, select: Callable[[], None], icon: FluentIcon | QIcon | None = None,
    ) -> None:
        button = self.addItem(key, text, lambda _checked: select(), icon)
        self._group.addButton(button)
        button.setCheckable(True)
        keyboard_activate(button, text, button.click)
        button.installEventFilter(self)

    def eventFilter(self, control: QWidget, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            buttons = self._group.buttons()
            step = -1 if event.key() == Qt.Key.Key_Left else 1
            button = buttons[(buttons.index(control) + step) % len(buttons)]
            button.click()
            button.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return super().eventFilter(control, event)


class CurrentPageStack(QStackedLayout):
    """Use the visible form's dimensions, including when switching to a smaller form."""

    def sizeHint(self) -> QSize:
        return self.currentWidget().sizeHint()

    def minimumSize(self) -> QSize:
        return self.currentWidget().minimumSizeHint()

    def hasHeightForWidth(self) -> bool:
        return self.currentWidget().hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:
        return self.currentWidget().heightForWidth(width)


class AdaptiveColumns(QWidget):
    """Place two content groups on a grid and stack them when the viewport narrows."""

    def __init__(
        self, primary: QWidget, secondary: QWidget, *, breakpoint: int, spacing: int = SECTION_SPACING
    ) -> None:
        super().__init__()
        self._primary = primary
        self._secondary = secondary
        self._breakpoint = breakpoint
        self._wide = False
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(spacing)
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._grid.addWidget(primary, 0, 0, Qt.AlignmentFlag.AlignTop)
        self._grid.addWidget(secondary, 1, 0, Qt.AlignmentFlag.AlignTop)
        self._grid.setColumnStretch(0, 1)
        self._grid.setRowStretch(2, 1)

    def minimumSizeHint(self) -> QSize:
        sizes = self._primary.minimumSizeHint(), self._secondary.minimumSizeHint()
        return QSize(max(size.width() for size in sizes), self._grid.minimumSize().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        minimum = sum(
            widget.minimumSizeHint().width() for widget in (self._primary, self._secondary)
        ) + self._grid.spacing()
        wide = self.width() >= max(self._breakpoint, minimum)
        if wide != self._wide:
            self._wide = wide
            self._grid.removeWidget(self._secondary)
            self._grid.addWidget(
                self._secondary, 0 if wide else 1, int(wide), Qt.AlignmentFlag.AlignTop
            )
            self._grid.setColumnStretch(0, 2 if wide else 1)
            self._grid.setColumnStretch(1, int(wide))


def format_time(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def hex_name(color: QColor) -> str:
    return color.name(QColor.NameFormat.HexRgb)


def swatch_icon(color: QColor, size: int = 16) -> QIcon:
    """A rounded colour chip with a faint outline, visible on light and dark alike."""
    # drawn at the screen's pixel density, so it stays sharp at 1.5x, 2x or 3x
    scale = max(1.0, QGuiApplication.instance().devicePixelRatio())
    pix = QPixmap(round(size * scale), round(size * scale))
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
        self.setAccessibleName(title)
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


def fixed_width(box: QAbstractSpinBox) -> None:
    """Keep a spin box at its own size hint: a stretched spin reads as a text field."""
    policy = box.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Fixed)
    box.setSizePolicy(policy)


def spin(low: int, high: int, value: int, suffix: str = "", step: int = 1) -> CompactSpinBox:
    box = CompactSpinBox()
    box.setRange(low, high)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setSingleStep(step)
    box.setAccelerated(True)
    fixed_width(box)
    return box


def browse_row(
    placeholder: str, slot: Callable[[], None], tooltip: str = ""
) -> tuple[LineEdit, ToolButton, QHBoxLayout]:
    edit = LineEdit()
    edit.setPlaceholderText(placeholder)
    edit.setClearButtonEnabled(True)
    button = ToolButton(FluentIcon.FOLDER)
    name_control(button, tooltip or t("action.browse"))
    button.clicked.connect(slot)
    row = QHBoxLayout()
    row.setSpacing(INLINE_SPACING)
    row.addWidget(edit, 1)
    row.addWidget(button)
    return edit, button, row


def hbox(
    *items: QWidget | str | int, stretch: bool = True, spacing: int = FIELD_SPACING
) -> QHBoxLayout:
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
    return hbox(width, "×", height, spacing=INLINE_SPACING)


class HintTree(TreeWidget):
    """A tree that says what belongs in it while it is empty."""

    def __init__(self, empty_text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.empty_text = empty_text
        use_direct_wheel(self)

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
        self._layout.setContentsMargins(*CARD_MARGINS)
        self._layout.setSpacing(ROW_SPACING)
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


def field_label(text: str, field: QWidget | QLayout | None) -> BodyLabel:
    """A label naming `field`, tied to it so screen readers and mnemonics find the field."""
    label = BodyLabel(text)
    if isinstance(field, QWidget):
        label.setBuddy(field)
        if not field.accessibleName():
            field.setAccessibleName(text.rstrip("：:"))
    return label


def form(rows: Iterable[tuple[str, QWidget | QLayout]]) -> QFormLayout:
    layout = QFormLayout()
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
    layout.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    layout.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    layout.setHorizontalSpacing(FIELD_SPACING)
    layout.setVerticalSpacing(ROW_SPACING)
    for label, field in rows:
        layout.addRow(field_label(label, field), field)
    return layout


Cell = QWidget | QLayout
Row = tuple[str, Cell, str | None, Cell | None]


class SettingsGrid(QWidget):
    """Place related settings side by side where the available width permits.

    Every grid under the same scroll page shares one set of column widths once
    shown, so stacked cards read as one continuous form: labels start at the same
    x, fields of the left column start at the same x, and the second pair sits at
    the same x in every card.  The cards also flip to two columns together.
    """

    def __init__(self, rows: Sequence[Row]) -> None:
        super().__init__()
        self._rows = [
            (self._field(left_label, left), self._field(right_label, right) if right else None)
            for left_label, left, right_label, right in rows
        ]
        self._wide = True
        # column widths shared across the page's grids: (labels, left fields, right labels)
        self._shared: tuple[int, int, int] | None = None
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(FIELD_SPACING)
        self._grid.setVerticalSpacing(ROW_SPACING)
        self._grid.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self._place_fields()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._sync_group()

    def _field(self, label: str | None, control: Cell) -> tuple[BodyLabel, QWidget]:
        if isinstance(control, QLayout):
            field = QWidget(self)
            field.setLayout(control)
            # the style would pad a fresh wrapper by ~9 px a side, nudging composite
            # fields off the column every direct-widget field sits on
            control.setContentsMargins(0, 0, 0, 0)
        else:
            field = control
        return field_label(label or "", control), field

    @staticmethod
    def _width(field: tuple[QWidget, QWidget]) -> tuple[int, int]:
        label, control = field
        return label.sizeHint().width(), max(control.minimumWidth(), control.minimumSizeHint().width())

    def _content_widths(self) -> tuple[int, int, int]:
        """The three column widths this grid's own content needs."""
        labels, left, right = 0, 0, 0
        for first, second in self._rows:
            left = max(left, self._width(first)[1])
            labels = max(labels, self._width(first)[0])
            if second is not None:
                right_label, _right_field = self._width(second)
                labels = max(labels, right_label)
                right = max(right, right_label)
        return labels, left, right

    def _two_column_width(self) -> int:
        pairs = [(first, second) for first, second in self._rows if second is not None]
        if not pairs:
            return 0
        labels, left_field, right_labels = self._shared or self._content_widths()
        right_fields = max(self._width(second)[1] for _, second in pairs)
        return labels + left_field + right_labels + right_fields + PAIR_GAP + FIELD_SPACING * 3

    def _sync_group(self) -> None:
        """Share one column layout with every other grid on the same page."""
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        group = [*parent.findChildren(SettingsGrid)] if parent is not None else [self]
        if self not in group:
            group.append(self)
        shared = (
            max(g._content_widths()[0] for g in group),
            max(g._content_widths()[1] for g in group),
            max(g._content_widths()[2] for g in group),
        )
        for grid in group:
            if grid._shared != shared:
                grid._shared = shared
                grid._wide = grid.width() >= grid._two_column_width()
                grid._place_fields()
                grid.updateGeometry()

    def minimumSizeHint(self) -> QSize:
        fields = [self._width(field) for row in self._rows for field in row if field is not None]
        labels, controls = zip(*fields, strict=True)
        width = max(labels) + FIELD_SPACING + max(controls)
        return QSize(max(width, self._grid.minimumSize().width()), self._grid.minimumSize().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        wide = self.width() >= self._two_column_width()
        if self._wide != wide:
            self._wide = wide
            self._place_fields()
            self.updateGeometry()

    def _place_fields(self) -> None:
        grid = self._grid
        while grid.count():
            grid.takeAt(0)
        for column in range(6):
            grid.setColumnMinimumWidth(column, 0)
            grid.setColumnStretch(column, 0)
        labels, left_fields, right_labels = self._shared or self._content_widths()
        grid.setColumnMinimumWidth(0, labels)
        if self._wide:
            grid.setColumnMinimumWidth(1, left_fields)
            grid.setColumnMinimumWidth(2, PAIR_GAP - FIELD_SPACING)
            grid.setColumnMinimumWidth(3, right_labels)
            grid.setColumnStretch(5, 1)
        else:
            grid.setColumnStretch(1, 1)

        row = 0
        for first, second in self._rows:
            self._place_field(first, row, 0, span=5 if self._wide and second is None else 1)
            if second is not None:
                if self._wide:
                    self._place_field(second, row, 3)
                else:
                    row += 1
                    self._place_field(second, row, 0)
            row += 1

    def _place_field(
        self, field: tuple[BodyLabel, QWidget], row: int, column: int, span: int = 1,
    ) -> None:
        label, control = field
        self._grid.addWidget(label, row, column, Qt.AlignmentFlag.AlignVCenter)
        alignment = Qt.AlignmentFlag.AlignLeft
        if isinstance(control, QLineEdit) or control.layout() is not None:
            alignment = Qt.AlignmentFlag(0)
        self._grid.addWidget(control, row, column + 1, 1, span, alignment)


# rows a track list grows to before it scrolls
_MAX_ROWS = 8
_PENDING = Qt.ItemDataRole.UserRole + 1


def select_all_row(check_all: Callable[[bool], None], count: QWidget) -> QHBoxLayout:
    """Select all / select none links, and a count of what is ticked at the far end."""
    row = QHBoxLayout()
    row.setSpacing(4)
    for text, state in ((t("action.select_all"), True), (t("action.deselect_all"), False)):
        button = HyperlinkButton()
        button.setText(text)
        button.clicked.connect(lambda _=False, on=state: check_all(on))
        row.addWidget(button)
    row.addStretch(1)
    row.addWidget(count)
    return row


class Debouncer(QObject):
    """Runs `slot` once a burst of `poke()` calls has been quiet for `ms` milliseconds.

    For work that follows typing or a stream of file events, where each event
    alone would redo it.
    """

    def __init__(self, slot: Callable[[], None], ms: int = 400, parent: QObject | None = None):
        super().__init__(parent)
        self._slot = slot
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(ms)
        self._timer.timeout.connect(slot)

    def poke(self) -> None:
        self._timer.start()

    def flush(self) -> None:
        if self._timer.isActive():
            self.now()

    def now(self) -> None:
        """Run at once, dropping the pending call."""
        self._timer.stop()
        self._slot()


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
        self.list = DirectListWidget()
        # Single selection keeps a visible keyboard focus indicator, and Space
        # toggles the current row's check state (Qt's default for checkable items).
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.itemChanged.connect(lambda _item: self._remember())
        self.count_label = hint_label(wrap=False)
        buttons = select_all_row(self.check_all, self.count_label)
        layout = QGridLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.list, 0, 0)
        layout.setVerticalSpacing(4)
        layout.addLayout(buttons, 1, 0)
        self.changed.connect(self._count)
        # ticked keys, including those of layouts not shown right now
        self._memory: set[str] = set()
        fluent.on_theme_change(self._recolor)

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
        for track in tracks:
            item = QListWidgetItem(track.text)
            item.setData(Qt.ItemDataRole.UserRole, track.key)
            item.setToolTip(track.tooltip)
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            on = track.key in checked
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            item.setData(_PENDING, track.pending)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._recolor()
        self._count()
        self._fit()

    def _recolor(self) -> None:
        """Grey the pending tracks in the current theme's secondary colour."""
        dim = fluent.dim()
        self.list.blockSignals(True)
        for i in range(self.list.count()):
            item = self.list.item(i)
            if item.data(_PENDING):
                item.setForeground(dim)
        self.list.blockSignals(False)

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
