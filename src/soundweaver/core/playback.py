"""Real-time spatial audio playback engine with stereo fold-down, M/S matrix, and 20 Hz metering."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QIODevice, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from soundweaver.core.audio import AudioInfo
from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.codecs import mpegh
from soundweaver.core.playback_buffer import START_SECONDS
from soundweaver.core.playback_mix import (
    SILENCE_DB,
    ObjectSnapshot,
    channel_mask,
    fold_matrix,
    mix_adm_block,
    peaks_dbfs,
    stereo_mix,
    vivid_snapshots,
)
from soundweaver.core.playback_source import PlaybackOptions, PlaybackSource, prepare_playback
from soundweaver.core.speakers import DEFAULT_ATMOS, SpeakerLayout

__all__ = ["SILENCE_DB", "ObjectSnapshot", "PlaybackEngine", "fold_matrix"]

logger = logging.getLogger(__name__)

@dataclass(frozen=True, slots=True)
class _LoadRequest:
    path: Path
    start_at: float
    auto_play: bool
    options: PlaybackOptions
    token: CancellationToken


class _PCMBufferDevice(QIODevice):
    """Pull-mode QIODevice backed by PlaybackEngine."""

    def __init__(self, engine: PlaybackEngine) -> None:
        super().__init__(engine)
        self._engine = engine

    def isSequential(self) -> bool:
        return True

    def atEnd(self) -> bool:
        return self._engine.at_end

    def readData(self, maxlen: int) -> bytes:
        frames = maxlen // 8
        if frames <= 0:
            return b""
        return self._engine._pull_stereo(frames).tobytes()

    def writeData(self, data: bytes) -> int:
        return 0

    def bytesAvailable(self) -> int:
        return self._engine._playable_frames() * 8 + super().bytesAvailable()


class PlaybackEngine(QObject):
    """Streaming spatial audio player supporting Atmos, ADM, AV3A/HOA, and 360RA/CICP."""

    track_loaded = Signal(object, object)  # (Path, AudioInfo)
    load_failed = Signal(str)
    state_changed = Signal(bool)
    position_changed = Signal(float)
    meters_updated = Signal(list, list)  # (channel_dbs, object_snapshots)
    track_finished = Signal()
    track_cleared = Signal()
    buffering_changed = Signal(bool)
    _source_ready = Signal(object)
    _source_failed = Signal(object, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._layout: SpeakerLayout = DEFAULT_ATMOS
        self._cicp: int = 13
        self._active_channels: tuple[str, ...] = DEFAULT_ATMOS.channels
        self._fold = fold_matrix(self._active_channels)

        self._source: PlaybackSource | None = None
        self._pending_source: PlaybackSource | None = None
        self._path: Path | None = None
        self._sample_rate = 48000
        self._total_frames = 0
        self._cursor = 0
        self._playing = False
        self._buffering = False
        self._waiting_for_pcm = False
        self._volume = 1.0
        self._muted_ch: set[int] = set()
        self._soloed_ch: set[int] = set()
        self._muted_obj: set[int] = set()
        self._soloed_obj: set[int] = set()
        self._last_ch_dbs: list[float] = []
        self._last_objs: list[ObjectSnapshot] = []
        self._request: _LoadRequest | None = None

        self._sink: QAudioSink | None = None
        self._io = _PCMBufferDevice(self)
        self._io.open(QIODevice.OpenModeFlag.ReadOnly)

        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(50)  # 20 Hz
        self._meter_timer.timeout.connect(self._emit_telemetry)
        self._source_ready.connect(self._accept_source, Qt.ConnectionType.QueuedConnection)
        self._source_failed.connect(self._reject_source, Qt.ConnectionType.QueuedConnection)

    @property
    def layout(self) -> SpeakerLayout:
        return self._layout

    @property
    def cicp(self) -> int:
        return self._cicp

    @property
    def active_channels(self) -> tuple[str, ...]:
        return self._active_channels

    @property
    def info(self) -> AudioInfo | None:
        return self._source.info if self._source else None

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def is_loading(self) -> bool:
        return self._request is not None

    @property
    def readable_frames(self) -> int:
        with self._lock:
            if self._source is not None and self._source.progress is not None:
                frames, _, _ = self._source.progress.state()
                return max(0, frames - self._cursor)
            return self._total_frames - self._cursor

    @property
    def at_end(self) -> bool:
        with self._lock:
            if self._source is not None and self._source.progress is not None:
                frames, eof, _ = self._source.progress.state()
                return eof and self._cursor >= frames
            return self._cursor >= self._total_frames

    def _playable_frames(self) -> int:
        with self._lock:
            source = self._source
            if source is None or source.progress is None:
                return self._total_frames - self._cursor
            frames, eof, _ = source.progress.state()
            available = max(0, frames - self._cursor)
            if self._waiting_for_pcm and not eof and available < self._sample_rate * START_SECONDS:
                return 0
            return available

    def _set_buffering(self, buffering: bool) -> None:
        if buffering != self._buffering:
            self._buffering = buffering
            self.buffering_changed.emit(buffering)

    @property
    def duration(self) -> float:
        return self._total_frames / self._sample_rate if self._sample_rate else 0.0

    @property
    def position(self) -> float:
        return self._cursor / self._sample_rate if self._sample_rate else 0.0

    @property
    def volume(self) -> float:
        return self._volume

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(2.0, value))

    def set_layout(self, layout: SpeakerLayout) -> None:
        with self._lock:
            self._layout = layout
            self._active_channels = layout.channels
            self._fold = fold_matrix(self._active_channels)
            self._muted_ch.clear()
            self._soloed_ch.clear()
        if self._reload_pending():
            return
        source = self._source
        if (
            self._path is not None and source is not None
            and source.info.atmos and source.adm is None and not source.info.is_av3a
        ):
            self.load(self._path, start_at=self.position, auto_play=self._playing)

    def set_cicp(self, cicp: int) -> None:
        with self._lock:
            self._cicp = cicp
            if self.info is not None and self.info.is_mpegh:
                self._active_channels = mpegh.cicp_layout(cicp).channel_names
                self._fold = fold_matrix(self._active_channels)
            self._muted_ch.clear()
            self._soloed_ch.clear()
        if self._reload_pending():
            return
        if self._path is not None and self.info is not None and self.info.is_mpegh:
            self.load(self._path, start_at=self.position, auto_play=self._playing)

    def _toggle_set(self, target: set[int], idx: int) -> None:
        with self._lock:
            target.symmetric_difference_update((idx,))

    def toggle_channel_mute(self, idx: int) -> None:
        self._toggle_set(self._muted_ch, idx)

    def toggle_channel_solo(self, idx: int) -> None:
        self._toggle_set(self._soloed_ch, idx)

    def toggle_object_mute(self, idx: int) -> None:
        self._toggle_set(self._muted_obj, idx)

    def toggle_object_solo(self, idx: int) -> None:
        self._toggle_set(self._soloed_obj, idx)

    def clear_ms(self) -> None:
        with self._lock:
            self._muted_ch.clear()
            self._soloed_ch.clear()
            self._muted_obj.clear()
            self._soloed_obj.clear()

    def channel_ms(self) -> tuple[set[int], set[int]]:
        with self._lock:
            return set(self._muted_ch), set(self._soloed_ch)

    def object_ms(self) -> tuple[set[int], set[int]]:
        with self._lock:
            return set(self._muted_obj), set(self._soloed_obj)

    def _reload_pending(self) -> bool:
        request = self._request
        if request is None:
            return False
        self.load(request.path, request.start_at, request.auto_play)
        return True

    def _close_async(self, source: PlaybackSource | None) -> None:
        """Retire a source on its own thread; its close may wait on subprocesses."""
        if source is not None:
            threading.Thread(target=source.close, daemon=True).start()

    def load(self, path: Path, start_at: float = 0.0, auto_play: bool = True) -> None:
        self.stop()
        with self._lock:
            stale, self._source = self._source, None
            self._total_frames = 0
        # closing a source can wait on decoder subprocesses; never on the UI thread
        self._close_async(stale)
        self._set_buffering(True)
        token = CancellationToken()
        self._path = path
        options = PlaybackOptions(self._layout, self._cicp)
        request = _LoadRequest(path, start_at, auto_play, options, token)
        self._request = request

        def worker() -> None:
            try:
                source = prepare_playback(request.path, request.options, request.token, request.start_at)
            except ProcessingCancelled:
                return
            except Exception as error:
                with self._lock:
                    if not token.cancelled:
                        logger.exception("Playback load failed for %s", path)
                        self._source_failed.emit(request, str(error) or type(error).__name__)
                return

            with self._lock:
                if not token.cancelled:
                    self._pending_source = source
                    self._source_ready.emit(request)
                    return
            source.close()

        threading.Thread(target=worker, daemon=True).start()

    @Slot(object, str)
    def _reject_source(self, request: _LoadRequest, message: str) -> None:
        if request is self._request:
            self._request = None
            self._set_buffering(False)
            self.load_failed.emit(message)

    @Slot(object)
    def _accept_source(self, request: _LoadRequest) -> None:
        if request is not self._request:
            return
        with self._lock:
            source, self._pending_source = self._pending_source, None
            if source is None:
                return
            previous, self._source = self._source, source
            self._request = None
            self._active_channels = self._layout.channels if source.adm else source.channels
            self._fold = fold_matrix(self._active_channels)
            if source.adm is not None:
                self._sample_rate, self._total_frames = source.adm.sample_rate, source.adm.frame_count
            elif source.buffer is not None:
                self._sample_rate = source.buffer.sample_rate
                frames, eof, _ = source.buffer.state()
                estimate = round(source.info.duration * self._sample_rate)
                self._total_frames = frames if eof else max(estimate, frames)
            elif source.audio is not None:
                self._sample_rate, self._total_frames = source.audio.sample_rate, source.audio.frames
            else:
                self._sample_rate, self._total_frames = 48000, 0
            self._cursor = min(self._total_frames, max(0, int(request.start_at * self._sample_rate)))
            self._waiting_for_pcm = False
            self.clear_ms()
            self._last_ch_dbs, self._last_objs = [], []
        self._close_async(previous)
        self._set_buffering(False)
        self.track_loaded.emit(request.path, source.info)
        self.position_changed.emit(self.position)
        if request.auto_play and source is self._source and self._request is None:
            self._start_sink()

    def _cancel_load(self) -> None:
        with self._lock:
            if self._request is not None:
                self._request.token.cancel()
                self._request = None
            pending, self._pending_source = self._pending_source, None
        self._close_async(pending)

    def unload(self) -> None:
        self.stop()
        with self._lock:
            source, self._source = self._source, None
            self._path = None
            self._total_frames = 0
            self._last_ch_dbs, self._last_objs = [], []
            self.clear_ms()
        self._close_async(source)
        self.track_cleared.emit()

    def shutdown(self) -> None:
        self.unload()

    @Slot()
    def _start_sink(self) -> None:
        fmt = QAudioFormat()
        fmt.setSampleRate(self._sample_rate)
        fmt.setChannelCount(2)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
        device = QMediaDevices.defaultAudioOutput()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._playing = True
        sink = self._sink = QAudioSink(device, fmt, self)
        sink.stateChanged.connect(lambda state: self._queue_sink_state(sink, state))
        sink.start(self._io)
        self._meter_timer.start()
        self.state_changed.emit(True)

    def _queue_sink_state(self, sink: QAudioSink, state: QAudio.State) -> None:
        # Let the Qt Multimedia callback return before stopping or replacing the sink.
        if state == QAudio.State.IdleState:
            QTimer.singleShot(0, self, lambda: self._finish_if_drained(sink))

    def _finish_if_drained(self, sink: QAudioSink) -> None:
        if (
            sink is not self._sink
            or sink.state().value != QAudio.State.IdleState.value
            or not self._playing
            or not self.at_end
        ):
            return
        self._playing = False
        self._meter_timer.stop()
        self.position_changed.emit(self.position)
        self.state_changed.emit(False)
        self.track_finished.emit()

    def play(self) -> None:
        if self._total_frames == 0 or self.is_loading:
            return
        sink = self._sink
        at_end = self.at_end
        if sink is None or (at_end and sink.state().value == QAudio.State.IdleState.value):
            if at_end:
                self.seek(0)
            self._start_sink()
        elif not self._playing:
            self._sink.resume()
            self._playing = True
            self._meter_timer.start()
            self.state_changed.emit(True)

    def pause(self) -> None:
        if self._sink is not None and self._playing:
            self._sink.suspend()
        self._playing = False
        self._meter_timer.stop()
        self._set_buffering(False)
        self.state_changed.emit(False)

    def toggle_play(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._cancel_load()
        self._set_buffering(False)
        with self._lock:
            self._playing = False
            self._cursor = 0
            self._waiting_for_pcm = False
            if self._source is not None:
                self._source.request(0)
        self._meter_timer.stop()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
        self.state_changed.emit(False)
        self.position_changed.emit(0.0)

    def seek(self, seconds: float) -> None:
        with self._lock:
            self._cursor = min(self._total_frames, max(0, int(seconds * self._sample_rate)))
            self._waiting_for_pcm = False
            if self._source is not None and self._source.progress is not None:
                self._source.request(self._cursor)
        sink = self._sink
        if sink is not None:
            was_playing = self._playing
            self._sink = None
            sink.reset()
            sink.deleteLater()
            if was_playing:
                self._start_sink()
                self._set_buffering(not self.at_end and not self.readable_frames)
        self.position_changed.emit(self.position)

    def _pull_stereo(self, frames: int) -> np.ndarray:
        with self._lock:
            if not self._playing:
                return np.empty((0, 2), dtype=np.float32)

            avail = min(frames, self._playable_frames())
            if avail == 0:
                self._waiting_for_pcm = not self.at_end
                return np.empty((0, 2), dtype=np.float32)
            self._waiting_for_pcm = False
            start = self._cursor
            self._cursor += avail

            source = self._source
            if source is None:
                return np.empty((0, 2), dtype=np.float32)
            block, streamed_stereo, objects = self._read_block(source, start, avail)
            self._last_objs = objects
            mask = channel_mask(block.shape[1], self._muted_ch, self._soloed_ch)
            masked = block * mask
            self._last_ch_dbs = peaks_dbfs(masked)

            if self._muted_ch or self._soloed_ch or self._muted_obj or self._soloed_obj:
                streamed_stereo = None
            return stereo_mix(
                masked, mask, self._fold, source.mpegh_matrix, streamed_stereo, self._volume,
            )

    def _read_block(
        self, source: PlaybackSource, start: int, frames: int,
    ) -> tuple[np.ndarray, np.ndarray | None, list[ObjectSnapshot]]:
        if source.adm is not None:
            if source.adm_progress is not None:
                source.adm_progress.request(start + frames)
            block, objects = mix_adm_block(
                source.adm, self._layout, start, frames, self._muted_obj, self._soloed_obj,
            )
            return block, None, objects

        if source.buffer is not None:
            block = source.buffer.read(start, frames)
            channels = source.buffer.channels
            stereo = block[:, channels:] if source.buffer.stereo else None
            objects = []
            reader = source.vvm1
            if reader is not None:
                header = reader.header
                row = reader.row(start // header.frame_samples)
                if row is not None:
                    objects = vivid_snapshots(header, row, self._muted_obj, self._soloed_obj)
                if header.bed_channels > 0:
                    channels = header.bed_channels
            return block[:, : min(channels, source.buffer.channels)], stereo, objects

        # A source without samples exposes zero playable frames; stay silent if not.
        if source.audio is None:
            return np.zeros((frames, 0), dtype=np.float32), None, []
        return source.audio.samples[:, start : start + frames].T, None, []

    def _emit_telemetry(self) -> None:
        source = self._source
        if source is not None and source.progress is not None:
            frames, eof, error = source.progress.state()
            if error is not None:
                self.unload()
                self.load_failed.emit(error)
                return
            with self._lock:
                if eof:
                    self._total_frames = frames
                    self._cursor = min(self._cursor, frames)
                else:
                    self._total_frames = max(self._total_frames, frames)
            sink = self._sink
            if sink is not None and sink.state().value == QAudio.State.IdleState.value:
                if self.at_end:
                    self._finish_if_drained(sink)
                    return
                playable = self._playable_frames()
                self._set_buffering(not playable)
                if playable:
                    self._io.readyRead.emit()
            else:
                self._set_buffering(False)
        self.position_changed.emit(self.position)
        self.meters_updated.emit(list(self._last_ch_dbs), list(self._last_objs))
