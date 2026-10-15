"""Settings page: language, appearance, the tools found, storage, shortcuts and version."""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    InfoBadge,
    InfoBar,
    PushButton,
    PushSettingCard,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
    TitleLabel,
)

from dolbytool.core.i18n import LANGUAGE_NAMES, get_language, t
from dolbytool.core.paths import WORK_ROOT, cache_dir, sweep_scratch
from dolbytool.features.waveform.encoders import encoder_available
from dolbytool.ui import widgets
from dolbytool.ui.fluent import (
    THEME_AUTO,
    THEME_DARK,
    THEME_LIGHT,
    ComboBox,
    get_theme_mode,
    set_theme_mode,
)

logger = logging.getLogger(__name__)

# caches rebuilt on demand: decoded Atmos traces and motion artwork frames
_CLEARABLE = ("cache", "motion")


def _app_version() -> str:
    try:
        return version("soundweaver")
    except PackageNotFoundError:
        return "?"


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class SettingsPage(QWidget):
    """The app's own settings, apart from what each output page sets."""

    language_picked = Signal(str)
    about_requested = Signal()

    def __init__(self, shortcuts: Sequence[QAction], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        container = QWidget()
        column = QVBoxLayout(container)
        column.setContentsMargins(*widgets.PAGE_MARGINS)
        column.setSpacing(widgets.SECTION_SPACING * 2)
        column.addWidget(TitleLabel(t("settings.title"), container))
        column.addWidget(self._general_group(container))
        column.addWidget(self._environment_group(container))
        column.addWidget(self._storage_group(container))
        column.addWidget(self._shortcuts_group(container, shortcuts))
        column.addWidget(self._about_group(container))
        column.addStretch(1)
        scroll.setWidget(container)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)

    @staticmethod
    def _trailing(card: SettingCard, *items: QWidget) -> None:
        """Put `items` at the right end of a setting card."""
        for index, item in enumerate(items):
            if index:
                card.hBoxLayout.addSpacing(8)
            card.hBoxLayout.addWidget(item, 0, Qt.AlignmentFlag.AlignRight)
        card.hBoxLayout.addSpacing(16)

    @staticmethod
    def _combo(card: SettingCard, items: Sequence[tuple[str, str]], current: str) -> ComboBox:
        combo = ComboBox(card)
        for label, data in items:
            combo.addItem(label, data)
        combo.setCurrentIndex(max(0, combo.findData(current)))
        return combo

    def _general_group(self, parent: QWidget) -> SettingCardGroup:
        group = SettingCardGroup(t("settings.group_general"), parent)
        language = SettingCard(
            FluentIcon.LANGUAGE, t("settings.language"), t("settings.language_desc"), group
        )
        self.combo_lang = self._combo(
            language, [(name, code) for code, name in LANGUAGE_NAMES.items()], get_language()
        )
        self.combo_lang.currentIndexChanged.connect(
            lambda i: self.language_picked.emit(self.combo_lang.itemData(i))
        )
        self._trailing(language, self.combo_lang)
        group.addSettingCard(language)

        theme = SettingCard(FluentIcon.BRUSH, t("settings.theme"), t("settings.theme_desc"), group)
        self.combo_theme = self._combo(
            theme,
            [
                (t("settings.theme_auto"), THEME_AUTO),
                (t("settings.theme_light"), THEME_LIGHT),
                (t("settings.theme_dark"), THEME_DARK),
            ],
            get_theme_mode(),
        )
        self.combo_theme.currentIndexChanged.connect(
            lambda i: set_theme_mode(self.combo_theme.itemData(i))
        )
        self._trailing(theme, self.combo_theme)
        group.addSettingCard(theme)
        return group

    def _environment_group(self, parent: QWidget) -> SettingCardGroup:
        group = SettingCardGroup(t("settings.group_environment"), parent)
        ffmpeg_bin, ffprobe_bin = shutil.which("ffmpeg"), shutil.which("ffprobe")
        ffmpeg_ok = bool(ffmpeg_bin and ffprobe_bin)
        card = SettingCard(
            FluentIcon.MEDIA,
            t("settings.ffmpeg_status"),
            t("settings.ffmpeg_available", path=ffmpeg_bin)
            if ffmpeg_ok
            else t("settings.ffmpeg_missing"),
            group,
        )
        badge = (
            InfoBadge.success(t("settings.status_ready"), parent=card)
            if ffmpeg_ok
            else InfoBadge.error(t("settings.status_missing"), parent=card)
        )
        self._trailing(card, badge)
        group.addSettingCard(card)

        nvenc = ffmpeg_ok and (encoder_available("h264_nvenc") or encoder_available("hevc_nvenc"))
        card = SettingCard(
            FluentIcon.SPEED_HIGH,
            t("settings.encoders_status"),
            t("settings.encoders_nvenc_available")
            if nvenc
            else t("settings.encoders_software_only"),
            group,
        )
        badge = (
            InfoBadge.success(t("settings.status_ready"), parent=card)
            if nvenc
            else InfoBadge.warning(t("settings.status_software"), parent=card)
        )
        self._trailing(card, badge)
        group.addSettingCard(card)
        return group

    def _storage_group(self, parent: QWidget) -> SettingCardGroup:
        group = SettingCardGroup(t("settings.group_storage"), parent)
        scratch = SettingCard(FluentIcon.FOLDER, t("settings.scratch_cache"), str(WORK_ROOT), group)
        scratch.setToolTip(t("settings.scratch_cache_desc"))
        open_scratch = PushButton(t("settings.open_dir"), scratch)
        open_scratch.clicked.connect(lambda: self._open_path(WORK_ROOT))
        self.clear_button = PushButton(t("settings.clear_cache"), scratch)
        self.clear_button.setToolTip(t("settings.clear_cache_tip"))
        self.clear_button.clicked.connect(self._clear_cache)
        self._trailing(scratch, open_scratch, self.clear_button)
        group.addSettingCard(scratch)

        conf_file = Path(QSettings().fileName())
        config = SettingCard(FluentIcon.DOCUMENT, t("settings.config_file"), str(conf_file), group)
        open_config = PushButton(t("settings.open_dir"), config)
        open_config.clicked.connect(lambda: self._open_path(conf_file.parent))
        self._trailing(config, open_config)
        group.addSettingCard(config)
        return group

    def _shortcuts_group(self, parent: QWidget, actions: Sequence[QAction]) -> SettingCardGroup:
        group = SettingCardGroup(t("settings.group_shortcuts"), parent)
        for action in actions:
            card = SettingCard(action.icon(), action.text().replace("&", ""), None, group)
            keys = BodyLabel(action.shortcut().toString(QKeySequence.SequenceFormat.NativeText))
            self._trailing(card, keys)
            group.addSettingCard(card)
        return group

    def _about_group(self, parent: QWidget) -> SettingCardGroup:
        group = SettingCardGroup(t("settings.group_about"), parent)
        about = PushSettingCard(
            t("settings.open_about_dialog"),
            FluentIcon.INFO,
            "SoundWeaver",
            f"{t('settings.app_version')} {_app_version()} (AGPL-3.0-or-later)",
            group,
        )
        about.clicked.connect(self.about_requested)
        group.addSettingCard(about)
        return group

    def set_running(self, running: bool) -> None:
        """A run reads the caches; they are not cleared under it."""
        self.clear_button.setEnabled(not running)

    @staticmethod
    def _open_path(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _clear_cache(self) -> None:
        """Empty the rebuildable caches and the scratch of dead processes, never this one's."""
        sweep_scratch()
        freed = 0
        for name in _CLEARABLE:
            root = cache_dir(name)
            for entry in root.iterdir():
                try:
                    size = _size(entry) if entry.is_dir() else entry.stat().st_size
                    shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
                except OSError as error:
                    logger.warning("cannot remove %s: %s", entry, error)
                    continue
                freed += size
        InfoBar.success(
            t("settings.clear_cache"),
            t("settings.cache_cleared", size=f"{freed / 2**20:.1f} MB"),
            duration=3000,
            parent=self.window(),
        )
