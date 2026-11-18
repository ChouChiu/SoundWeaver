"""Direct wheel input with Fluent scroll bars."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractScrollArea, QFrame
from qfluentwidgets import ListWidget, ScrollArea
from qfluentwidgets.components.widgets.scroll_bar import SmoothScrollDelegate


class _PixelWheel(QObject):
    def __init__(self, area: QAbstractScrollArea) -> None:
        super().__init__(area)
        area.viewport().installEventFilter(self)

    def eventFilter(self, viewport: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.Wheel:
            return False

        delta = event.pixelDelta()
        if delta.isNull():
            return False  # Qt handles wheel steps and fractional angle deltas.

        area = self.parent()
        moved = False
        for bar, distance in (
            (area.horizontalScrollBar(), delta.x()),
            (area.verticalScrollBar(), delta.y()),
        ):
            if distance:
                before = bar.value()
                bar.setValue(before - distance)
                moved |= bar.value() != before

        # Let a surrounding page scroll when a nested list reaches its boundary.
        event.setAccepted(moved)
        return moved


def use_direct_wheel(area: QAbstractScrollArea, *, pixel_scroll: bool = True) -> None:
    """Keep Fluent's scrollbar overlays and delegate wheel input to Qt or the touchpad."""
    delegate = area.findChild(SmoothScrollDelegate, options=Qt.FindChildOption.FindDirectChildrenOnly)
    area.viewport().removeEventFilter(delegate)
    delegate.vScrollBar.setScrollAnimation(0)
    delegate.hScrollBar.setScrollAnimation(0)
    if pixel_scroll:
        _PixelWheel(area)


class DirectScrollArea(ScrollArea):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        use_direct_wheel(self)


class DirectListWidget(ListWidget):
    def __init__(self, parent=None, *, transparent: bool = False) -> None:
        super().__init__(parent)
        use_direct_wheel(self)
        if transparent:
            self.setFrameShape(QFrame.Shape.NoFrame)
            self.setStyleSheet("ListWidget { background: transparent; border: none; }")
