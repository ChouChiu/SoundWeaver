"""Settings page: manage language, appearance, runtime environment, and storage."""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    FluentIcon,
    HyperlinkCard,
    InfoBadge,
    InfoBar,
    PrimaryPushButton,
    PushButton,
    PushSettingCard,
    ScrollArea,
    SettingCard,
    SettingCardGroup,
    TitleLabel,
)

from dolbytool.core.i18n import (
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
    get_language,
    t,
)
from dolbytool.core.paths import WORK_ROOT, sweep_scratch
from dolbytool.features.waveform.encoders import ENCODERS, encoder_available
from dolbytool.ui import fluent
from dolbytool.ui.fluent import (
    THEME_AUTO,
    THEME_DARK,
    THEME_LIGHT,
    ComboBox,
    get_theme_mode,
    set_theme_mode,
)


class SettingsPage(QWidget):
    """Modern Fluent settings page."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()

        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(36, 20, 36, 24)
        container_layout.setSpacing(20)

        # Title
        self.title_label = TitleLabel(t("settings.title"), container)
        container_layout.addWidget(self.title_label)

        # 1. General group
        self._build_general_group(container_layout, container)

        # 2. Environment & Acceleration group
        self._build_environment_group(container_layout, container)

        # 3. Storage & Cache group
        self._build_storage_group(container_layout, container)

        # 4. About group
        self._build_about_group(container_layout, container)

        container_layout.addSpacing(40)
        scroll.setWidget(container)
        layout.addWidget(scroll)

    def _build_general_group(self, layout: QVBoxLayout, container: QWidget) -> None:
        self.group_general = SettingCardGroup(t("settings.group_general"), container)

        # Language card
        self.card_lang = SettingCard(
            FluentIcon.LANGUAGE,
            t("settings.language"),
            t("settings.language_desc"),
            self.group_general,
        )
        self.combo_lang = ComboBox(self.card_lang)
        for code, name in LANGUAGE_NAMES.items():
            self.combo_lang.addItem(name, userData=code)

        cur_lang = get_language()
        for idx in range(self.combo_lang.count()):
            if self.combo_lang.itemData(idx) == cur_lang:
                self.combo_lang.setCurrentIndex(idx)
                break

        self.combo_lang.currentIndexChanged.connect(self._on_language_changed)
        self.card_lang.hBoxLayout.addWidget(self.combo_lang, 0, Qt.AlignRight)
        self.card_lang.hBoxLayout.addSpacing(16)
        self.group_general.addSettingCard(self.card_lang)

        # Theme card
        self.card_theme = SettingCard(
            FluentIcon.BRUSH,
            t("settings.theme"),
            t("settings.theme_desc"),
            self.group_general,
        )
        self.combo_theme = ComboBox(self.card_theme)
        self.combo_theme.addItem(t("settings.theme_auto"), userData=THEME_AUTO)
        self.combo_theme.addItem(t("settings.theme_light"), userData=THEME_LIGHT)
        self.combo_theme.addItem(t("settings.theme_dark"), userData=THEME_DARK)

        cur_mode = get_theme_mode()
        for idx in range(self.combo_theme.count()):
            if self.combo_theme.itemData(idx) == cur_mode:
                self.combo_theme.setCurrentIndex(idx)
                break

        self.combo_theme.currentIndexChanged.connect(self._on_theme_changed)
        self.card_theme.hBoxLayout.addWidget(self.combo_theme, 0, Qt.AlignRight)
        self.card_theme.hBoxLayout.addSpacing(16)
        self.group_general.addSettingCard(self.card_theme)

        layout.addWidget(self.group_general)

    def _build_environment_group(self, layout: QVBoxLayout, container: QWidget) -> None:
        self.group_env = SettingCardGroup(t("settings.group_environment"), container)

        # FFmpeg status card
        ffmpeg_bin = shutil.which("ffmpeg")
        ffprobe_bin = shutil.which("ffprobe")
        ffmpeg_ok = bool(ffmpeg_bin and ffprobe_bin)
        ffmpeg_desc = t("settings.ffmpeg_available", path=ffmpeg_bin) if ffmpeg_ok else t("settings.ffmpeg_missing")

        self.card_ffmpeg = SettingCard(
            FluentIcon.MEDIA,
            t("settings.ffmpeg_status"),
            ffmpeg_desc,
            self.group_env,
        )
        badge_ffmpeg = (
            InfoBadge.success(t("settings.status_ready"), parent=self.card_ffmpeg)
            if ffmpeg_ok
            else InfoBadge.error(t("settings.status_missing"), parent=self.card_ffmpeg)
        )
        badge_ffmpeg.setAlignment(Qt.AlignCenter)
        badge_ffmpeg.setFixedSize(54, 22)
        self.card_ffmpeg.hBoxLayout.addWidget(badge_ffmpeg, 0, Qt.AlignRight)
        self.card_ffmpeg.hBoxLayout.addSpacing(16)
        self.group_env.addSettingCard(self.card_ffmpeg)

        # NVENC Hardware encoder status card
        has_nvenc = encoder_available("h264_nvenc") or encoder_available("hevc_nvenc")
        enc_desc = t("settings.encoders_nvenc_available") if has_nvenc else t("settings.encoders_software_only")

        self.card_encoder = SettingCard(
            FluentIcon.SPEED_HIGH,
            t("settings.encoders_status"),
            enc_desc,
            self.group_env,
        )
        badge_enc = (
            InfoBadge.success(t("settings.status_ready"), parent=self.card_encoder)
            if has_nvenc
            else InfoBadge.warning(t("settings.status_software"), parent=self.card_encoder)
        )
        badge_enc.setAlignment(Qt.AlignCenter)
        badge_enc.setFixedSize(68 if not has_nvenc else 54, 22)
        self.card_encoder.hBoxLayout.addWidget(badge_enc, 0, Qt.AlignRight)
        self.card_encoder.hBoxLayout.addSpacing(16)
        self.group_env.addSettingCard(self.card_encoder)

        layout.addWidget(self.group_env)

    def _build_storage_group(self, layout: QVBoxLayout, container: QWidget) -> None:
        self.group_storage = SettingCardGroup(t("settings.group_storage"), container)

        # Scratch directory card with Open and Clear buttons
        scratch_dir = WORK_ROOT
        self.card_scratch = SettingCard(
            FluentIcon.FOLDER,
            t("settings.scratch_cache"),
            str(scratch_dir),
            self.group_storage,
        )
        btn_open = PushButton(t("settings.open_dir"), self.card_scratch)
        btn_open.clicked.connect(lambda: self._open_path(scratch_dir))
        btn_clear = PushButton(t("settings.clear_cache"), self.card_scratch)
        btn_clear.clicked.connect(self._clear_cache)

        self.card_scratch.hBoxLayout.addWidget(btn_open, 0, Qt.AlignRight)
        self.card_scratch.hBoxLayout.addSpacing(8)
        self.card_scratch.hBoxLayout.addWidget(btn_clear, 0, Qt.AlignRight)
        self.card_scratch.hBoxLayout.addSpacing(16)
        self.group_storage.addSettingCard(self.card_scratch)

        # Config file path
        settings = QSettings("SoundWeaver", "SoundWeaver")
        conf_file = Path(settings.fileName())
        self.card_config = SettingCard(
            FluentIcon.DOCUMENT,
            t("settings.config_file"),
            str(conf_file),
            self.group_storage,
        )
        btn_open_conf = PushButton(t("settings.open_dir"), self.card_config)
        btn_open_conf.clicked.connect(lambda: self._open_path(conf_file.parent))
        self.card_config.hBoxLayout.addWidget(btn_open_conf, 0, Qt.AlignRight)
        self.card_config.hBoxLayout.addSpacing(16)
        self.group_storage.addSettingCard(self.card_config)

        layout.addWidget(self.group_storage)

    def _build_about_group(self, layout: QVBoxLayout, container: QWidget) -> None:
        self.group_about = SettingCardGroup(t("settings.group_about"), container)

        self.card_about = PushSettingCard(
            t("settings.open_about_dialog"),
            FluentIcon.INFO,
            "SoundWeaver",
            t("settings.app_version") + " 0.1.0 (AGPL-3.0-or-later)",
            self.group_about,
        )
        self.card_about.clicked.connect(self._show_about)
        self.group_about.addSettingCard(self.card_about)

        layout.addWidget(self.group_about)

    def _on_language_changed(self, index: int) -> None:
        code = self.combo_lang.itemData(index)
        if not code or code == get_language():
            return
        win = self.window()
        if hasattr(win, "_set_language"):
            win._set_language(code)

    def _on_theme_changed(self, index: int) -> None:
        mode = self.combo_theme.itemData(index)
        if mode:
            set_theme_mode(mode)

    def _open_path(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _clear_cache(self) -> None:
        sweep_scratch()
        # Clean up process scratch folders
        scratch_dir = WORK_ROOT / "scratch"
        if scratch_dir.is_dir():
            for child in scratch_dir.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except Exception:
                    pass
        InfoBar.success(
            t("settings.clear_cache"),
            t("settings.cache_cleared"),
            duration=3000,
            parent=self.window(),
        )

    def _show_about(self) -> None:
        win = self.window()
        if hasattr(win, "_about"):
            win._about()
        else:
            fluent.tell(self, t("app.about_title"), t("app.description"))
