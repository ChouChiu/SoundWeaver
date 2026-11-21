"""Offscreen FluentUI 2 MainWindow, MonitorPage, StemsPanel, Inspector, and Planner tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

import numpy as np
from PySide6.QtCore import QObject, QPoint, QPointF, QSettings, Qt, Signal
from PySide6.QtGui import QWheelEvent
from PySide6.QtMultimedia import QAudio
from PySide6.QtTest import QSignalSpy, QTest
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import FluentWindow

from soundweaver.core.audio import AudioInfo
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs.mpegh import cicp_layout
from soundweaver.core.paths import atomic_output
from soundweaver.core.pcm import AudioData
from soundweaver.core.planning import dir_problem
from soundweaver.core.playback import ObjectSnapshot, PlaybackEngine
from soundweaver.core.playback_buffer import BLOCK, PlaybackBuffer
from soundweaver.core.playback_source import PlaybackSource
from soundweaver.core.project import PlanContext, Project
from soundweaver.core.speakers import BY_NAME
from soundweaver.core.task import Step
from soundweaver.features.batch.planner import build_batch_plan, find_target_clash
from soundweaver.features.monitor.playlist_metadata import TrackMetadata
from soundweaver.features.monitor.speakers import room_speakers, spherical_speakers
from soundweaver.features.source.inspector import StreamInspectorDialog
from soundweaver.features.stems.panel import StemsPanel
from soundweaver.features.tasks.panel import TasksPage
from soundweaver.ui import fluent, widgets
from soundweaver.ui.main_window import MainWindow
from soundweaver.ui.scrolling import DirectScrollArea


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

    def test_output_selection_and_navigation_by_keyboard(self) -> None:
        win = MainWindow()
        try:
            win._go("inputs")
            win.mode.setCurrentIndex(0)
            win.resize(760, 680)
            win.show()
            QTest.qWait(250)
            outputs = win.outputs
            page = win.stackedWidget.currentWidget()
            self.assertEqual(page.horizontalScrollBar().maximum(), 0)
            output_card = outputs.parentWidget()
            output_top = output_card.mapTo(page.widget(), output_card.rect().topLeft()).y()
            source_bottom = win.source.mapTo(page.widget(), win.source.rect().bottomLeft()).y()
            self.assertIn(output_top - source_bottom, range(12, 33))

            outputs.setCurrentRow(0)
            QTest.keyClick(outputs, Qt.Key.Key_Down)
            self.assertEqual(win._current_route(), "inputs")
            QTest.keyClick(outputs, Qt.Key.Key_Return)
            self.assertEqual(win._current_route(), "output1")

            switch = win.headers[1].check.indicator
            before = win.panels[1].enabled
            QTest.keyClick(switch, Qt.Key.Key_Space)
            self.assertEqual(win.panels[1].enabled, not before)
            nav = win.navigationInterface.widget("tasks")
            self.assertTrue(nav.accessibleName())
            QTest.keyClick(nav, Qt.Key.Key_Space)
            self.assertEqual(win._current_route(), "tasks")
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_segmented_input_mode_uses_arrow_keys_and_exposes_selection(self) -> None:
        win = MainWindow()
        try:
            win._go("inputs")
            win.mode.setCurrentIndex(0)
            win.show()
            win.activateWindow()
            QTest.qWait(320)
            single = win.mode_bar.widget("single")
            batch = win.mode_bar.widget("batch")
            single.setFocus()
            self.assertTrue(single.isChecked())
            QTest.keyClick(single, Qt.Key.Key_Right)
            self.assertEqual(win.mode.currentIndex(), 1)
            self.assertTrue(batch.isChecked())
            self.assertTrue(batch.hasFocus())
            QTest.keyClick(batch, Qt.Key.Key_Space)
            self.assertTrue(batch.isChecked())
            QTest.keyClick(batch, Qt.Key.Key_Left)
            self.assertEqual(win.mode.currentIndex(), 0)
            self.assertTrue(single.isChecked())
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_export_fields_keep_order_when_reflowing(self) -> None:
        first, second = widgets.spin(1, 100, 24), widgets.spin(1, 100, 60)
        grid = widgets.SettingsGrid([("Width", first, "Frame rate", second)])
        try:
            grid.show()
            grid.resize(800, 200)
            self.app.processEvents()
            self.assertEqual(first.y(), second.y())
            grid.resize(max(200, grid.minimumSizeHint().width()), 200)
            self.app.processEvents()
            self.assertLess(first.y(), second.y())
            self.assertEqual(first.value(), 24)
            self.assertEqual(second.value(), 60)
            self.assertEqual(first.accessibleName(), "Width")
        finally:
            grid.close()
            grid.deleteLater()
            self.app.processEvents()

    def test_wheel_responds_immediately_to_pixels_and_detents(self) -> None:
        area = DirectScrollArea()
        try:
            content = QWidget()
            content.setFixedSize(600, 1000)
            area.setWidget(content)
            area.resize(320, 240)
            area.show()
            self.app.processEvents()
            vertical = area.verticalScrollBar()
            horizontal = area.horizontalScrollBar()

            def wheel(*, x=0, y=0, angle=0):
                event = QWheelEvent(
                    QPointF(10, 10), QPointF(10, 10), QPoint(x, y), QPoint(0, angle),
                    Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                    Qt.ScrollPhase.ScrollUpdate, False,
                )
                self.app.sendEvent(area.viewport(), event)
                return event.isAccepted()

            self.assertTrue(wheel(y=-23))
            self.assertEqual(vertical.value(), 23)
            self.assertTrue(wheel(y=9, angle=40))
            self.assertEqual(vertical.value(), 14)
            self.assertTrue(wheel(x=-17))
            self.assertEqual(horizontal.value(), 17)
            self.assertTrue(wheel(angle=-120))
            after_detent = vertical.value()
            self.assertGreater(after_detent, 14)
            QTest.qWait(170)
            self.assertEqual(vertical.value(), after_detent)
            vertical.setValue(vertical.maximum())
            self.assertFalse(wheel(y=-20))
        finally:
            area.close()
            area.deleteLater()
            self.app.processEvents()

    def test_rapid_navigation_preserves_scroll_and_slides_into_place(self) -> None:
        win = MainWindow()
        try:
            win.show()
            win.resize(920, 680)
            win._go("output0")
            self.app.processEvents()
            stems = win.stackedWidget.currentWidget()
            bar = stems.verticalScrollBar()
            self.assertGreater(bar.maximum(), 0)
            bar.setValue(min(80, bar.maximum() // 2))
            previous = bar.value()

            win.navigationInterface.widget("settings").click()
            win.navigationInterface.widget("output0").click()
            self.assertEqual(win._current_route(), "output0")
            self.assertEqual(bar.value(), previous)
            win._go("monitor")
            view = win.stackedWidget.view
            monitor = view.currentWidget()
            animation = view.aniInfos[view.currentIndex()].ani
            self.assertEqual(animation.duration(), 300)
            self.assertEqual(monitor.y(), 76)
            animation.setCurrentTime(140)
            self.assertGreater(monitor.y(), 0)
            self.assertLess(monitor.y(), 76)

            win._go("tasks")
            tasks = view.currentWidget()
            animation = view.aniInfos[view.currentIndex()].ani
            self.assertEqual(tasks.y(), 76)
            animation.setCurrentTime(animation.duration())
            self.assertEqual(win._current_route(), "tasks")
            self.assertEqual(tasks.y(), 0)
            win._go("monitor")
            self.assertEqual(monitor.y(), 76)
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_tasks_wait_for_thread_completion_and_can_run_again(self) -> None:
        page = TasksPage()
        try:
            completed = QSignalSpy(page.finished)
            progress = QSignalSpy(page.progress)
            page.start([Step("test", lambda token, report: report(70, "working"))])
            for _ in range(100):
                if completed.count():
                    break
                QTest.qWait(10)
            self.assertEqual(completed.at(0), ["done"])
            self.assertFalse(page.running)
            self.assertEqual(progress.at(progress.count() - 1)[0], 100)

            started = Event()

            def cancellable(token, report):
                started.set()
                token.wait(2)
                token.raise_if_cancelled()

            page.start([Step("cancel", cancellable)])
            self.assertTrue(started.wait(1))
            page.cancel()
            for _ in range(100):
                if completed.count() == 2:
                    break
                QTest.qWait(10)
            self.assertEqual(completed.at(1), ["cancelled"])
            self.assertFalse(page.running)
        finally:
            page.shutdown()
            page.deleteLater()
            self.app.processEvents()

    def test_start_flushes_source_edit_before_running(self) -> None:
        win = MainWindow()
        try:
            win.mode.setCurrentIndex(0)
            with tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "edited.wav"
                win.source.audio_edit.setText(str(source))
                with patch.object(fluent, "tell"), patch.object(win.tasks_page, "start") as start:
                    win._start()
                    start.assert_not_called()
                self.assertEqual(win.source._probed, source)
                self.assertIsNotNone(win.source.project().audio_error)
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_monitor_object_row_selects_by_id_and_keeps_mute_independent(self) -> None:
        win = MainWindow()
        try:
            page = win.monitor_page
            self.assertEqual(page.style_box.currentData(), "person")
            self.assertGreaterEqual(page.style_box.findData("logic"), 0)
            self.assertEqual(page._meter_mode, "channels")
            page._set_meter_mode("objects")
            first = ObjectSnapshot(3, 0.25, 0.5, 0.2, -12.0, True)
            silent = ObjectSnapshot(7, 0.7, 0.25, 0.8, -96.0, False)
            page._on_meters([], [silent, first])
            self.assertEqual([row.index for row in page._meter_rows], [3, 7])
            row = page._meter_rows[1]
            QTest.mouseClick(row, Qt.MouseButton.LeftButton)
            self.assertEqual(page.canvas_3d._selected_object, 7)
            self.assertTrue(row._object_button.isChecked())
            row.btn_m.click()
            self.assertIn(7, win.playback_engine.object_ms()[0])
            self.assertEqual(page.canvas_3d._selected_object, 7)

            page._on_meters([], [first, silent])
            self.assertIs(page._meter_rows[1], row)
            self.assertTrue(row._object_button.isChecked())
            page._set_meter_mode("channels")
            page._set_meter_mode("objects")
            button = page._meter_rows[1]._object_button
            self.assertTrue(button.isChecked())
            QTest.keyClick(button, Qt.Key.Key_Space)
            self.assertIsNone(page.canvas_3d._selected_object)

            page._meter_rows[0]._object_button.click()
            page._on_track_cleared()
            self.assertIsNone(page.canvas_3d._selected_object)
            self.assertFalse(page._meter_rows)
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_player_cover_and_seek_can_be_used_from_the_keyboard(self) -> None:
        win = MainWindow()
        try:
            win.show()
            cover, seek = win.player_bar.cover_lbl, win.player_bar.seek_slider
            self.assertTrue(cover.accessibleName())
            self.assertTrue(seek.accessibleName())
            QTest.keyClick(cover, Qt.Key.Key_Space)
            self.assertEqual(win._current_route(), "monitor")
            seek.setEnabled(True)
            seek.setFocus()
            with (
                patch.object(PlaybackEngine, "duration", 100.0),
                patch.object(win.playback_engine, "seek") as seek_audio,
            ):
                QTest.keyClick(seek, Qt.Key.Key_End)
                seek_audio.assert_called_once_with(100.0)
            self.app.processEvents()
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_playlist_delete_keeps_playing_track_separate_from_selected_row(self) -> None:
        win = MainWindow()
        try:
            page = win.monitor_page
            playlist = page.playlist_list
            songs = [Path(name) for name in ("one.wav", "two.wav", "three.wav")]
            with (
                patch.object(playlist._metadata, "run", side_effect=lambda call, done: done(call())),
                patch(
                    "soundweaver.features.monitor.playlist.read_track_metadata",
                    return_value=TrackMetadata("Song", "Artist", 60),
                ),
                patch.object(win.playback_engine, "load") as load,
            ):
                for song in songs:
                    playlist.append_track(song)
                page.play_index(1)
                self.assertEqual(playlist.item(1).text(), "Song")
                playlist.setCurrentRow(0)
                QTest.keyClick(playlist, Qt.Key.Key_Delete)
                self.assertEqual(playlist.active_row, 0)
                load.assert_called_once_with(songs[1], auto_play=True)
                page.play_next()
                load.assert_called_with(songs[2], auto_play=True)
        finally:
            win.close_without_asking()
            win.deleteLater()
            self.app.processEvents()

    def test_unloaded_segment_is_buffering_instead_of_end_of_track(self) -> None:
        waiting, release, closed = Event(), Event(), Event()
        token = CancellationToken()
        resources = ExitStack()
        resources.callback(closed.set)
        pcm = np.zeros((BLOCK, 2), dtype=np.float32)
        chunks = iter((pcm, pcm[:0]))

        def read(_count):
            block = next(chunks)
            if not len(block):
                waiting.set()
                release.wait(2)
            return block

        progress = PlaybackBuffer(16000, 2, False, read, token, resources)
        engine = PlaybackEngine()
        try:
            progress.start(0)
            self.assertTrue(waiting.wait(2))
            engine._source = PlaybackSource(
                AudioInfo(2, 1), ("L", "R"), ExitStack(), buffer=progress,
            )
            engine._total_frames = 16000
            engine._playing = True
            self.assertEqual(len(engine._io.readData((BLOCK + 100) * 8)), BLOCK * 8)
            self.assertFalse(engine._io.atEnd())
            self.assertEqual(engine._io.readData(8), b"")
            self.assertEqual(engine._cursor, BLOCK)
            release.set()
            self.assertTrue(closed.wait(2))
            self.assertTrue(engine._io.atEnd())
        finally:
            release.set()
            engine.shutdown()
            progress.close()

    def test_playback_finishes_after_sink_callback_returns(self) -> None:
        class Sink(QObject):
            stateChanged = Signal(object)

            def __init__(self, _device, _fmt, parent):
                super().__init__(parent)
                self.in_callback = False

            def start(self, io):
                self.in_callback = True
                self.pcm = io.readData(32)
                self.stateChanged.emit(QAudio.State.IdleState)
                self.in_callback = False

            def stop(self):
                pass

            def state(self):
                return QAudio.State.IdleState

        engine = PlaybackEngine()
        engine._source = PlaybackSource(
            AudioInfo(2, 2 / 48000), ("L", "R"), ExitStack(),
            audio=AudioData(np.zeros((2, 2), dtype=np.float32), 48000),
        )
        engine._total_frames = 2
        events = []
        engine.track_finished.connect(lambda: events.append(engine._sink.in_callback))
        try:
            with (
                patch("soundweaver.core.playback.QAudioSink", Sink),
                patch("soundweaver.core.playback.QMediaDevices.defaultAudioOutput"),
            ):
                engine.play()
                self.assertEqual(events, [])
                self.assertEqual(len(engine._sink.pcm), 16)
                self.assertTrue(engine._io.atEnd())
                self.app.processEvents()
                self.assertEqual(events, [False])
                self.assertFalse(engine.is_playing)
        finally:
            engine.shutdown()

    def test_stopped_playback_releases_a_late_prepared_source(self) -> None:
        engine = PlaybackEngine()
        started, release, cleaned = Event(), Event(), Event()
        loaded: list[Path] = []
        engine.track_loaded.connect(lambda path, _info: loaded.append(path))

        def prepare(*_args) -> PlaybackSource:
            started.set()
            release.wait(2)
            resources = ExitStack()
            resources.callback(cleaned.set)
            return PlaybackSource(AudioInfo(2, 1.0), ("L", "R"), resources)

        try:
            with patch("soundweaver.core.playback.prepare_playback", prepare):
                engine.load(Path("old.wav"), auto_play=False)
                self.assertTrue(started.wait(2))
                engine.stop()
                release.set()
                self.assertTrue(cleaned.wait(2))
                self.app.processEvents()
                self.assertEqual(loaded, [])
        finally:
            release.set()
            engine.shutdown()

    def test_compact_adm_export_option_in_single_and_batch_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat)
            panel = StemsPanel(settings)
            audio = root / "track.m4a"
            audio.touch()
            project = Project(
                audio=audio, output_dir=root, info=AudioInfo(6, 3.0, atmos=True)
            )
            ctx = PlanContext(project)
            panel.refresh(project)
            self.assertFalse(panel.remove_bed_placeholders.isChecked())
            self.assertFalse(panel.remove_bed_placeholders.isEnabled())
            panel.adm.setChecked(True)
            self.assertTrue(panel.remove_bed_placeholders.isEnabled())
            self.assertEqual(panel.plan(ctx)[0].targets, (root / "track_adm.wav",))

            panel.remove_bed_placeholders.setChecked(True)
            target = root / "track_adm_compact.wav"
            batch = build_batch_plan([panel], [project], None, is_album=False)
            for step in (panel.plan(ctx)[0], batch.steps[0]):
                self.assertEqual(step.targets, (target,))
                with patch("soundweaver.features.stems.service.decode_atmos") as decode:
                    decode.return_value.speakers = None
                    step.run(CancellationToken(), lambda *_: None)
                    self.assertTrue(decode.call_args.kwargs["remove_bed_placeholders"])
                    self.assertEqual(decode.call_args.kwargs["adm"], target)

            panel.save()
            restored = StemsPanel(settings)
            restored.restore()
            self.assertTrue(restored.remove_bed_placeholders.isChecked())

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


class MonitorSpeakerTests(unittest.TestCase):
    def test_meter_indices_follow_pcm_order_in_both_projections(self) -> None:
        surround = BY_NAME["5.1"]
        room = {speaker.name: speaker.channel for speaker in room_speakers(surround)}
        self.assertEqual(room, {name: surround.index[name] for name in surround.positions})
        self.assertEqual(room["Ls"], 4)  # LFE occupies index 3 without a room marker

        layout = cicp_layout(13)
        dome = {speaker.name: speaker.channel for speaker in spherical_speakers(layout)}
        self.assertEqual(dome, {
            speaker.name: i for i, speaker in enumerate(layout.speakers) if not speaker.is_lfe
        })


class OutputPathTests(unittest.TestCase):
    def test_existing_file_ancestor_blocks_directory_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "file"
            file.touch()
            self.assertIsNotNone(dir_problem(file / "child" / "output"))
            self.assertIsNone(dir_problem(Path(tmp) / "new" / "output"))

    def test_atomic_output_keeps_independent_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "output.wav"
            target.write_bytes(b"original")
            neighbor = target.with_name(".output.partial.wav")
            neighbor.write_bytes(b"source")
            with atomic_output(target) as first:
                self.assertEqual(first.suffix, target.suffix)
                first.write_bytes(b"complete")
                with self.assertRaises(ValueError), atomic_output(target) as second:
                    self.assertNotEqual(first, second)
                    second.write_bytes(b"aborted")
                    raise ValueError
                self.assertFalse(second.exists())
                self.assertEqual(first.read_bytes(), b"complete")
                self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(target.read_bytes(), b"complete")
            self.assertEqual(neighbor.read_bytes(), b"source")
