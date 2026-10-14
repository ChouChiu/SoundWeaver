"""Fluent Design (PySide6-Fluent-Widgets) glue: theme, icons and Qt-compatible shims.

The theme follows the desktop: dark or light from the platform's colour
scheme, the accent from the palette's highlight (KDE's accent colour).  The
panels keep using Qt-style calls; the shims here cover where the Fluent
widgets' API differs from Qt's.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPropertyAnimation, QSettings, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    MessageBoxBase,
    PlainTextEdit,
    StrongBodyLabel,
    SubtitleLabel,
    Theme,
    isDarkTheme,
    setFontFamilies,
    setTheme,
    setThemeColor,
)
from qfluentwidgets import ComboBox as _ComboBox
from qfluentwidgets.components.dialog_box.mask_dialog_base import MaskDialogBase

# Fluent's critical colours, readable on their own theme
_NEGATIVE_LIGHT = QColor("#c42b1c")
_NEGATIVE_DARK = QColor("#ff99a4")
# secondary text on a light and on a dark theme
_SECONDARY_LIGHT = QColor(96, 96, 96)
_SECONDARY_DARK = QColor(206, 206, 206)

# freedesktop icon names used across the app, mapped to Fluent's own
_ICONS: dict[str, FluentIcon] = {
    "home": FluentIcon.HOME,
    "document-open": FluentIcon.MUSIC,
    "document-open-folder": FluentIcon.FOLDER,
    "folder-open": FluentIcon.FOLDER,
    "folder-add": FluentIcon.FOLDER_ADD,
    "list-add": FluentIcon.ADD,
    "list-remove": FluentIcon.REMOVE,
    "edit-clear": FluentIcon.CANCEL,
    "edit-clear-all": FluentIcon.BROOM,
    "document-import": FluentIcon.DOWNLOAD,
    "insert-image": FluentIcon.PHOTO,
    "text-xml": FluentIcon.FONT,
    "view-refresh": FluentIcon.SYNC,
    "application-exit": FluentIcon.POWER_BUTTON,
    "help-about": FluentIcon.INFO,
    "preferences-system": FluentIcon.SETTING,
    "media-playback-start": FluentIcon.PLAY,
    "media-playback-pause": FluentIcon.PAUSE,
    "process-stop": FluentIcon.CLOSE,
    "window-close": FluentIcon.CLOSE,
    "go-up": FluentIcon.UP,
    "go-down": FluentIcon.DOWN,
    "audio-x-generic": FluentIcon.MUSIC,
    "view-media-playlist": FluentIcon.ALBUM,
    "view-list-details": FluentIcon.CHECKBOX,
    "text-x-log": FluentIcon.DOCUMENT,
    "appointment-soon": FluentIcon.HISTORY,
    "dialog-ok-apply": FluentIcon.ACCEPT,
    "dialog-error": FluentIcon.CANCEL_MEDIUM,
    "media-skip-forward": FluentIcon.SKIP_FORWARD,
    # the outputs
    "view-media-equalizer": FluentIcon.MIX_VOLUMES,
    "color-gradient": FluentIcon.PALETTE,
    "view-media-visualization": FluentIcon.MEDIA,
    "view-media-lyrics": FluentIcon.FONT_SIZE,
    "audio-speakers-symbolic": FluentIcon.SPEAKERS,
    "video-x-generic": FluentIcon.VIDEO,
}


def icon(name: str) -> FluentIcon | QIcon:
    """The Fluent icon standing for a freedesktop icon name, else the desktop's own."""
    return _ICONS.get(name) or QIcon.fromTheme(name)


def qicon(name: str) -> QIcon:
    """`icon()` as a plain QIcon, for item views and labels, in the current theme's colour."""
    found = _ICONS.get(name)
    return found.icon() if found is not None else QIcon.fromTheme(name)


def negative() -> QColor:
    return _NEGATIVE_DARK if isDarkTheme() else _NEGATIVE_LIGHT


def secondary(label: Any, problem: bool = False) -> None:
    """Colour a Fluent label as secondary text, or as a problem to fix."""
    if problem:
        label.setTextColor(_NEGATIVE_LIGHT, _NEGATIVE_DARK)
    else:
        label.setTextColor(_SECONDARY_LIGHT, _SECONDARY_DARK)


def dim() -> QColor:
    """Secondary text colour."""
    return QColor(255, 255, 255, 140) if isDarkTheme() else QColor(0, 0, 0, 120)


def _system_dark(app: QGuiApplication) -> bool:
    scheme = app.styleHints().colorScheme()
    if scheme != Qt.ColorScheme.Unknown:
        return scheme == Qt.ColorScheme.Dark
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


THEME_AUTO = "auto"
THEME_LIGHT = "light"
THEME_DARK = "dark"


def get_theme_mode() -> str:
    return QSettings().value("theme_mode", THEME_AUTO, type=str)


def _sync_theme(app: QApplication) -> None:
    mode = get_theme_mode()
    if mode == THEME_LIGHT:
        theme = Theme.LIGHT
    elif mode == THEME_DARK:
        theme = Theme.DARK
    else:
        theme = Theme.DARK if _system_dark(app) else Theme.LIGHT
    setTheme(theme, lazy=False)
    setThemeColor(app.palette().color(QPalette.ColorRole.Highlight), lazy=False)


def set_theme_mode(mode: str) -> None:
    """Set the theme mode ('auto', 'light', 'dark') and apply immediately."""
    QSettings().setValue("theme_mode", mode)
    app = QApplication.instance()
    if app:
        _sync_theme(app)


def apply_theme(app: QApplication) -> None:
    """Follow the desktop's dark/light scheme, accent colour and UI font, now and on change."""
    setFontFamilies([app.font().family(), "Noto Sans CJK SC", "Microsoft YaHei UI"])
    _sync_theme(app)
    app.styleHints().colorSchemeChanged.connect(lambda _s: _sync_theme(app))


class ComboBox(_ComboBox):
    """Fluent combo box taking Qt's `addItem(text, data)` and `setItemData(i, v, role)`."""

    def addItem(self, text: str, userData: Any = None, icon: Any = None) -> None:
        super().addItem(text, icon, userData)

    def setItemData(self, index: int, value: Any, role: int = Qt.ItemDataRole.UserRole) -> None:
        # Fluent menus show no per-item tooltips; only the data is kept
        if role == Qt.ItemDataRole.UserRole:
            super().setItemData(index, value)


_fade_in = MaskDialogBase.showEvent


def _fade_in_unshadowed(self: MaskDialogBase, event) -> None:
    """Fade in as usual, with the card's shadow put back only once the fade is over."""
    self.widget.setGraphicsEffect(None)
    _fade_in(self, event)
    # the fade just started is the dialog's newest animation; it drops the opacity
    # effect when it finishes, and the shadow comes back right after
    animations = self.findChildren(QPropertyAnimation)
    if animations:
        animations[-1].finished.connect(self.setShadowEffect)
    else:
        self.setShadowEffect()


# The mask dialogs fade in with an opacity effect on the whole dialog while their card
# carries a drop shadow effect; on a real Wayland/GL desktop the nested effects leave
# the card's contents unpainted (an empty dialog).  Keep the fade but drop the shadow
# while it runs, for every mask dialog, the colour dialog included.  Closing needs
# nothing: the library already removes the shadow before fading out.
MaskDialogBase.showEvent = _fade_in_unshadowed


from dolbytool.core.i18n import t


class MessageDialog(MessageBoxBase):
    """A Fluent message dialog, optionally with a read-only details box below the text."""

    def __init__(
        self,
        parent: QWidget,
        title: str,
        text: str,
        details: str = "",
        accept: str | None = None,
        cancel: str | None = None,
    ) -> None:
        super().__init__(parent.window())
        self.viewLayout.addWidget(SubtitleLabel(title, self.widget))
        body = BodyLabel(text, self.widget)
        body.setWordWrap(True)
        self.viewLayout.addWidget(body)
        if details:
            box = PlainTextEdit(self.widget)
            box.setPlainText(details)
            box.setReadOnly(True)
            box.setMinimumHeight(120)
            box.setMaximumHeight(220)
            self.viewLayout.addWidget(StrongBodyLabel(t("action.details"), self.widget))
            self.viewLayout.addWidget(box)
        self.widget.setMinimumWidth(420)
        self.yesButton.setText(accept if accept is not None else t("action.confirm"))
        if cancel is None:
            self.cancelButton.hide()
        else:
            self.cancelButton.setText(cancel)


def ask(parent: QWidget, title: str, text: str, details: str = "", accept: str | None = None) -> bool:
    return bool(MessageDialog(parent, title, text, details, accept, cancel=t("action.cancel")).exec())


def tell(parent: QWidget, title: str, text: str, details: str = "") -> None:
    MessageDialog(parent, title, text, details, t("action.confirm"), cancel=None).exec()

