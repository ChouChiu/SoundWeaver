"""Capture offscreen snapshots of every page, for reviewing layout changes.

Usage: `uv run python scripts/ui_snapshots.py <output-dir> [width height]`.
Runs the real main window on the offscreen platform and saves one full-window
PNG per navigation route; view them side by side to check spacing and alignment.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui_snapshots")
    size = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (1180, 860)

    from soundweaver.core.i18n import set_language
    from soundweaver.core.logs import setup_logging

    setup_logging()
    set_language("zh_cn")

    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    from soundweaver.ui import fluent
    from soundweaver.ui.main_window import MainWindow

    fluent.apply_theme(app)
    win = MainWindow()
    win.resize(*size)
    win.show()
    app.processEvents()
    out.mkdir(parents=True, exist_ok=True)
    for key, page in win._routes.items():
        win._go(key)
        for _ in range(6):
            app.processEvents()
        win.grab().save(str(out / f"{key}.png"))
        page.grab().save(str(out / f"{key}-page.png"))
    win.close()
    print(f"saved {len(list(out.glob('*.png')))} snapshots to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
