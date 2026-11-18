"""Offscreen FluentUI 2 MainWindow, MonitorPage, StemsPanel, Inspector, and Planner tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from PySide6.QtWidgets import QApplication
from qfluentwidgets import FluentWindow

from soundweaver.core.audio import AudioInfo
from soundweaver.core.project import Project
from soundweaver.core.speakers import BY_NAME
from soundweaver.features.batch.planner import build_batch_plan, find_target_clash
from soundweaver.features.source.inspector import StreamInspectorDialog
from soundweaver.features.stems.panel import StemsPanel
from soundweaver.ui import fluent
from soundweaver.ui.main_window import MainWindow


class UiAndPlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        fluent.apply_theme(cls.app)

    def test_fluent_window_and_monitor_modes(self) -> None:
        win = MainWindow()
        try:
            self.assertIsInstance(win, FluentWindow)
            self.assertGreaterEqual(len(win.panels), 5)
            # Switch routes, toggle full-window monitor & Escape cleanly
            win._go("monitor")
            self.assertEqual(win._current_route(), "monitor")
            win.monitor_page.full_btn.setChecked(True)
            self.assertTrue(win.navigationInterface.isHidden())
            win._handle_escape()
            self.assertFalse(win.navigationInterface.isHidden())

            # Toggle Spherical 360RA WalkMix mode and elevation layer filters
            sph_idx = win.monitor_page.mode_box.findData("spherical")
            win.monitor_page.mode_box.setCurrentIndex(sph_idx)
            self.assertFalse(win.monitor_page.btn_upper.isHidden())
            win.monitor_page.btn_upper.setChecked(False)
            self.assertFalse(win.monitor_page.btn_upper.isChecked())
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_batch_planner_and_inspector_dialog(self) -> None:
        win = MainWindow()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                audio_p = Path(tmp) / "01. Demo Track.m4a"
                audio_p.write_bytes(b"")
                out_dir = Path(tmp) / "out"
                info = AudioInfo(
                    channels=10,
                    duration=12.5,
                    atmos=True,
                    codec="Audio Vivid (AVS3)",
                    sample_rate=48000,
                    bed_channels=("L", "R", "C", "LFE", "Ls", "Rs", "Ltf", "Rtf", "Ltr", "Rtr"),
                    is_av3a=True,
                    hoa_order=0,
                )
                proj = Project(audio=audio_p, info=info, output_dir=out_dir, atmos_layout=BY_NAME["5.1.4"])
                stems_panel = next(p for p in win.panels if isinstance(p, StemsPanel))
                stems_panel.set_enabled(True)
                stems_panel.binaural.setChecked(True)
                stems_panel.av3a_encode.setChecked(True)

                plan = build_batch_plan(win.panels, [proj], None, is_album=False)
                self.assertEqual(plan.tracks, 1)
                self.assertGreaterEqual(len(plan.steps), 1)
                self.assertIsNone(find_target_clash({audio_p}, plan.steps))

                dlg = StreamInspectorDialog(audio_p, info, win)
                self.assertIn("Audio Vivid", dlg._summary_text)
                dlg.close()
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()
