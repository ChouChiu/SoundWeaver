from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    from dolbytool.core.logs import setup_logging

    setup_logging()
    from PySide6.QtCore import QLibraryInfo, QLocale, QSettings, QTranslator
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from dolbytool.core.i18n import (
        LANG_EN,
        LANG_ZH_CN,
        LANG_ZH_TW,
        set_language,
        t,
    )
    from dolbytool.core.paths import sweep_scratch
    from dolbytool.ui.main_window import MainWindow

    sweep_scratch()

    app = QApplication(sys.argv)
    app.setOrganizationName("SoundWeaver")
    app.setApplicationName("SoundWeaver")

    # Initialize localization
    settings = QSettings()
    lang = settings.value("language", None)
    if not lang:
        loc = QLocale.system().name().lower()
        if "tw" in loc or "hk" in loc or "mo" in loc:
            lang = LANG_ZH_TW
        elif "zh" in loc:
            lang = LANG_ZH_CN
        else:
            lang = LANG_EN
    set_language(lang)

    app.setApplicationDisplayName(t("app.title"))
    app.setWindowIcon(QIcon.fromTheme("video-x-generic"))

    from dolbytool.ui import fluent
    from dolbytool.ui.logs import InterruptHandler, route_qt_messages

    route_qt_messages()
    fluent.apply_theme(app)

    if lang == LANG_ZH_TW:
        QLocale.setDefault(QLocale(QLocale.Language.Chinese, QLocale.Country.Taiwan))
    elif lang == LANG_ZH_CN:
        QLocale.setDefault(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
    else:
        QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))

    translations = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    translator = QTranslator(app)
    if translator.load(QLocale(), "qtbase", "_", translations):
        app.installTranslator(translator)

    from dolbytool.features.source.service import kind_of

    window = MainWindow()
    args = [Path(a).expanduser() for a in sys.argv[1:]]
    audio = [a for a in args if kind_of(a) == "audio"]
    if any(a.is_dir() for a in args) or len(audio) > 1:
        # a folder or several tracks: a batch (an album, for a folder)
        window.add_batch(args)
    else:
        if args:
            window.set_audio(args[0])
        if len(args) > 1:
            window.set_cover(args[1])
    window.show()
    interrupt = InterruptHandler(app, window.close_without_asking)  # noqa: F841 - lives with the loop
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
