"""Fluent Design (PySide6-Fluent-Widgets) glue: theme, icons and Qt-compatible shims.

The theme follows the desktop: dark or light from the platform's colour
scheme, the accent from the palette's highlight (KDE's accent colour).  The
panels keep using Qt-style calls; the shims here cover where the Fluent
widgets' API differs from Qt's.
"""

from __future__ import annotations

from collections.abc import Callable
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
    qconfig,
    setFontFamilies,
    setTheme,
    setThemeColor,
)
from qfluentwidgets import ComboBox as _ComboBox
from qfluentwidgets.components.dialog_box.mask_dialog_base import MaskDialogBase

from soundweaver.core.i18n import t

_NEGATIVE_LIGHT = QColor("#c42b1c")
_NEGATIVE_DARK = QColor("#ff99a4")
_POSITIVE_LIGHT = QColor("#0f7b0f")
_POSITIVE_DARK = QColor("#6ccb5f")
_WARNING_LIGHT = QColor("#9d5d00")
_WARNING_DARK = QColor("#fce100")
_SECONDARY_LIGHT = QColor(96, 96, 96)
_SECONDARY_DARK = QColor(206, 206, 206)
_FOCUS_STROKE_LIGHT = "#242424"
_FOCUS_STROKE_DARK = "#ffffff"

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
    "media-skip-backward": FluentIcon.CARE_LEFT_SOLID,
    "media-skip-forward": FluentIcon.CARE_RIGHT_SOLID,
    "view-fullscreen": FluentIcon.FULL_SCREEN,
    "view-restore": FluentIcon.MINIMIZE,
    "headphones": FluentIcon.HEADPHONE,
    "globe": FluentIcon.GLOBE,
    # the outputs
    "view-media-equalizer": FluentIcon.MIX_VOLUMES,
    "color-gradient": FluentIcon.PALETTE,
    "view-media-visualization": FluentIcon.MEDIA,
    "view-media-lyrics": FluentIcon.FONT_SIZE,
    "audio-speakers-symbolic": FluentIcon.SPEAKERS,
    "video-x-generic": FluentIcon.VIDEO,
}


def positive() -> QColor:
    """Fluent 2 system-success color for active meters/statuses."""
    return _POSITIVE_DARK if isDarkTheme() else _POSITIVE_LIGHT


def warning() -> QColor:
    """Fluent 2 system-caution color."""
    return _WARNING_DARK if isDarkTheme() else _WARNING_LIGHT


def surface_card() -> QColor:
    """Fluent 2 CardBackgroundFillDefault color."""
    return QColor(255, 255, 255, 13) if isDarkTheme() else QColor(255, 255, 255, 178)


def surface_subtle() -> QColor:
    """Fluent 2 SubtleFillSecondary color."""
    return QColor(255, 255, 255, 10) if isDarkTheme() else QColor(0, 0, 0, 9)


def stroke_card() -> QColor:
    """Fluent 2 CardStrokeColorDefault hairline border."""
    return QColor(255, 255, 255, 18) if isDarkTheme() else QColor(0, 0, 0, 15)


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
    return _SECONDARY_DARK if isDarkTheme() else _SECONDARY_LIGHT


def focus_stroke_hex(dark: bool | None = None) -> str:
    """Fluent 2 keyboard focus indicator color hex."""
    is_dark = isDarkTheme() if dark is None else dark
    return _FOCUS_STROKE_DARK if is_dark else _FOCUS_STROKE_LIGHT


def focus_stroke() -> QColor:
    """Fluent 2 keyboard focus indicator color."""
    return QColor(focus_stroke_hex())


def on_theme_change(slot: Callable[[], None]) -> None:
    """Call `slot` after the light/dark theme flips, for colours taken from the theme once."""
    qconfig.themeChangedFinished.connect(slot)


def _system_dark_from(palette: QPalette, app: QGuiApplication) -> bool:
    scheme = app.styleHints().colorScheme()
    if scheme != Qt.ColorScheme.Unknown:
        return scheme == Qt.ColorScheme.Dark
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


THEME_AUTO = "auto"
THEME_LIGHT = "light"
THEME_DARK = "dark"


def get_theme_mode() -> str:
    return QSettings().value("theme_mode", THEME_AUTO, type=str)


# the desktop's own palette, taken before a forced theme replaces it
_system_palette: QPalette | None = None


def _forced_palette(dark: bool) -> QPalette:
    """The window colours Fluent draws its own light or dark theme on.

    The Fluent widgets colour themselves, but the window behind them takes the
    application palette: forcing dark on a light desktop would leave white text
    on a light window.
    """
    palette = QPalette(_system_palette)
    window, text = (
        (QColor(32, 32, 32), QColor(255, 255, 255))
        if dark
        else (QColor(243, 243, 243), QColor(0, 0, 0))
    )
    for role, color in (
        (QPalette.ColorRole.Window, window),
        (QPalette.ColorRole.Base, window.lighter(115) if dark else QColor(255, 255, 255)),
        (QPalette.ColorRole.WindowText, text),
        (QPalette.ColorRole.Text, text),
        (QPalette.ColorRole.ButtonText, text),
    ):
        palette.setColor(role, color)
    return palette


ACCENT_AUTO = "auto"
ACCENT_PRESETS: tuple[tuple[str, str], ...] = (
    ("blue", "#0078d4"),
    ("indigo", "#4f6bed"),
    ("purple", "#8764b8"),
    ("pink", "#e3008c"),
    ("red", "#d13438"),
    ("orange", "#ca5010"),
    ("green", "#107c10"),
    ("teal", "#038387"),
)


def get_accent_color() -> str:
    return str(QSettings().value("accent_color", ACCENT_AUTO))


def set_accent_color(accent: str) -> None:
    QSettings().setValue("accent_color", accent)
    app = QApplication.instance()
    if app:
        _sync_theme(app)


def _sync_theme(app: QApplication) -> None:
    global _system_palette
    if _system_palette is None:
        _system_palette = QPalette(app.palette())
    mode = get_theme_mode()
    system_dark = _system_dark_from(_system_palette, app)
    dark = mode == THEME_DARK or (mode == THEME_AUTO and system_dark)
    # the desktop's palette where it already matches, a stand-in where it is overridden
    app.setPalette(_system_palette if dark == system_dark else _forced_palette(dark))
    setTheme(Theme.DARK if dark else Theme.LIGHT, lazy=False)
    accent = get_accent_color()
    preset_hex = dict(ACCENT_PRESETS).get(accent)
    accent_color = QColor(preset_hex) if preset_hex else _system_palette.color(QPalette.ColorRole.Highlight)
    setThemeColor(accent_color, lazy=False)


def set_theme_mode(mode: str) -> None:
    """Set the theme mode ('auto', 'light', 'dark') and apply immediately."""
    QSettings().setValue("theme_mode", mode)
    app = QApplication.instance()
    if app:
        _sync_theme(app)


def apply_theme(app: QApplication) -> None:
    """Follow the desktop's dark/light scheme, accent colour and UI font, now and on change."""
    setFontFamilies([app.font().family(), "Segoe UI", "Microsoft YaHei UI", "Noto Sans CJK SC"])
    _sync_theme(app)
    app.styleHints().colorSchemeChanged.connect(lambda _s: _resync_desktop(app))


def _resync_desktop(app: QApplication) -> None:
    """The desktop switched schemes: take its new palette before following it."""
    global _system_palette
    _system_palette = None
    app.setPalette(QPalette())
    _sync_theme(app)


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


def ask(
    parent: QWidget, title: str, text: str, details: str = "", accept: str | None = None
) -> bool:
    return bool(
        MessageDialog(parent, title, text, details, accept, cancel=t("action.cancel")).exec()
    )


def tell(parent: QWidget, title: str, text: str, details: str = "") -> None:
    MessageDialog(parent, title, text, details, t("action.confirm"), cancel=None).exec()
