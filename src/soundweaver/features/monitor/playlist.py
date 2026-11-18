"""Fluent playlist rows, with independent selection and playback markers."""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QFont, QIcon, QPainter, QPalette
from PySide6.QtWidgets import QListWidgetItem, QStyle, QStyleOptionViewItem, QWidget
from qfluentwidgets import getFont
from qfluentwidgets.components.widgets.list_view import ListItemDelegate

from soundweaver.core.i18n import t
from soundweaver.features.monitor.playlist_metadata import TrackMetadata, read_track_metadata
from soundweaver.ui import fluent, widgets
from soundweaver.ui.background import Each
from soundweaver.ui.scrolling import DirectListWidget

_ARTIST = Qt.ItemDataRole.UserRole + 1
_DURATION = Qt.ItemDataRole.UserRole + 2
_ARTWORK = Qt.ItemDataRole.UserRole + 3
_ACTIVE = Qt.ItemDataRole.UserRole + 4


class _TrackDelegate(ListItemDelegate):
    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(0, 56)

    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().initStyleOption(option, index)
        option.text = ""
        option.state &= ~QStyle.StateFlag.State_Selected

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, QStyleOptionViewItem(option), index)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(option.rect)
        row = option.rect.adjusted(12, 0, -8, 0)
        tile = QRect(row.left(), row.center().y() - 18, 36, 36)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fluent.surface_subtle())
        painter.drawRoundedRect(tile, 4, 4)
        artwork = index.data(_ARTWORK)
        if artwork is not None:
            artwork.paint(painter, tile)
        else:
            fluent.qicon("audio-x-generic").paint(painter, tile.adjusted(8, 8, -8, -8))
        if index.data(_ACTIVE):
            badge = QRect(tile.right() - 14, tile.bottom() - 14, 16, 16)
            painter.setBrush(option.palette.color(QPalette.ColorRole.Base))
            painter.drawEllipse(badge)
            fluent.qicon("media-playback-start").paint(painter, badge.adjusted(3, 3, -3, -3))

        x = tile.right() + 9
        width = row.right() - x + 1
        small_font = getFont(12)
        painter.setFont(small_font)
        duration = index.data(_DURATION) or ""
        if duration:
            duration_width = painter.fontMetrics().horizontalAdvance(duration)
            painter.setPen(fluent.dim())
            painter.drawText(
                QRect(x, row.y(), width, row.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                duration,
            )
            width -= duration_width + 8

        title = index.data(Qt.ItemDataRole.DisplayRole)
        artist = index.data(_ARTIST)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title_rect = (
            QRect(x, row.center().y() - 19, width, 20)
            if artist
            else QRect(x, row.y(), width, row.height())
        )
        painter.setFont(
            getFont(
                14, weight=QFont.Weight.DemiBold if index.data(_ACTIVE) else QFont.Weight.Medium
            )
        )
        painter.setPen(option.palette.color(QPalette.ColorRole.Text))
        painter.drawText(
            title_rect,
            align,
            painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, width),
        )
        if artist:
            painter.setFont(small_font)
            painter.setPen(fluent.dim())
            artist_rect = QRect(x, row.center().y() + 1, width, 18)
            painter.drawText(
                artist_rect,
                align,
                painter.fontMetrics().elidedText(artist, Qt.TextElideMode.ElideRight, width),
            )
        painter.restore()


class PlaylistView(DirectListWidget):
    remove_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent, transparent=True)
        self.setAccessibleName(t("monitor.playlist"))
        self.setUniformItemSizes(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(_TrackDelegate(self))
        self._metadata = Each(self)
        self._items: dict[Path, QListWidgetItem] = {}
        self._active: QListWidgetItem | None = None

    @property
    def active_row(self) -> int:
        return self.row(self._active) if self._active is not None else -1

    def append_track(self, path: Path) -> None:
        if path in self._items:
            return
        item = QListWidgetItem(path.stem)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(str(path))
        self._items[path] = item
        self.addItem(item)
        self._metadata.run(
            lambda: read_track_metadata(path), lambda result: self._update_item(path, item, result)
        )

    def _update_item(
        self, path: Path, item: QListWidgetItem, result: TrackMetadata | Exception
    ) -> None:
        if isinstance(result, Exception) or self._items.get(path) is not item:
            return
        item.setText(result.title)
        item.setData(_ARTIST, result.artist)
        if math.isfinite(result.duration) and result.duration > 0:
            item.setData(_DURATION, widgets.format_time(result.duration))
        if result.artwork is not None:
            item.setData(_ARTWORK, QIcon(str(result.artwork)))
        self._describe_item(item)

    def _describe_item(self, item: QListWidgetItem) -> None:
        title = item.text()
        artist = item.data(_ARTIST)
        if artist:
            title = f"{title} · {artist}"
        description = t("monitor.current_track", name=title) if item is self._active else title
        duration = item.data(_DURATION)
        item.setData(Qt.ItemDataRole.AccessibleTextRole, description)
        item.setData(Qt.ItemDataRole.AccessibleDescriptionRole, duration or "")
        item.setToolTip(f"{title}\n{item.data(Qt.ItemDataRole.UserRole)}")

    def activate(self, row: int) -> Path:
        previous, self._active = self._active, self.item(row)
        if previous is not None:
            previous.setData(_ACTIVE, False)
            self._describe_item(previous)
        self._active.setData(_ACTIVE, True)
        self._describe_item(self._active)
        self.setCurrentRow(row)
        self.scrollToItem(self._active)
        return self._active.data(Qt.ItemDataRole.UserRole)

    def remove_current(self) -> None:
        row = self.currentRow()
        if row < 0:
            return
        item = self.item(row)
        if item is self._active:
            self._active = None
        self._items.pop(item.data(Qt.ItemDataRole.UserRole))
        self.takeItem(row)

    def clear(self) -> None:
        self._metadata.drop()
        self._items.clear()
        self._active = None
        super().clear()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Delete and self.currentRow() >= 0:
            self.remove_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)
