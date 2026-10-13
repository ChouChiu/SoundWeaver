from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    from dolbytool.core.logs import setup_logging

    setup_logging()
    from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from dolbytool.core.paths import sweep_scratch
    from dolbytool.ui.main_window import MainWindow

    sweep_scratch()

    app = QApplication(sys.argv)
    app.setOrganizationName("dolbytool")
    app.setApplicationName("dolbytool")
    app.setApplicationDisplayName("杜比视频素材工具")
    app.setWindowIcon(QIcon.fromTheme("video-x-generic"))

    from dolbytool.ui import fluent
    from dolbytool.ui.logs import InterruptHandler, route_qt_messages

    route_qt_messages()
    fluent.apply_theme(app)

    # The interface is Chinese; make Qt's own dialogs and context menus match.
    QLocale.setDefault(QLocale(QLocale.Language.Chinese, QLocale.Country.China))
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
