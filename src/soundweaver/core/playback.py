"""Real-time spatial audio playback engine with stereo fold-down, M/S matrix, and 20 Hz metering."""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QIODevice, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QMediaDevices

from soundweaver.core.atmos import ObjectTrace
from soundweaver.core.audio import AudioInfo
from soundweaver.core.cancel import CancellationToken, ProcessingCancelled
from soundweaver.core.codecs import mpegh
from soundweaver.core.codecs.av3a import VvmTimelineHeader, read_vvm1_frame
from soundweaver.core.playback_mix import cap_stream_peak, monitor_gain
from soundweaver.core.playback_source import PlaybackOptions, PlaybackSource, prepare_playback
from soundweaver.core.speakers import DEFAULT_ATMOS, SpeakerLayout

logger = logging.getLogger(__name__)

SILENCE_DB = -96.0


def fold_matrix(channel_names: tuple[str, ...] | list[str]) -> np.ndarray:
    """Stereo fold-down matrix of shape `(len(channel_names), 2)`."""
    n = len(channel_names)
    mat = np.zeros((n, 2), dtype=np.float32)
    for i, name in enumerate(channel_names):
        if name in ("LFE", "LFE2"):
            continue
        if name == "ACN0":
            mat[i, 0] = 0.7071
            mat[i, 1] = 0.7071
            continue
        if name == "ACN1":  # Y axis (+left / -right) -> stereo difference
            mat[i, 0] = 0.7071
            mat[i, 1] = -0.7071
            continue
        if name.startswith("ACN"):
            continue
        low = name.lower()
        if name in ("C", "Cb", "Cv", "Cvr", "Cs", "Ts", "Tfc", "Tsc", "Tbc", "Bfc"):
            mat[i, 0] = 0.7071
            mat[i, 1] = 0.7071
        elif low.startswith(("l", "tl", "tf", "ts", "tb", "bf")) and "r" not in low:
            mat[i, 0] = 1.0 if name in ("L", "Lfc", "Lc", "Lv") else 0.7071
        elif low.startswith(("r", "tr", "bf")) or "r" in low:
            mat[i, 1] = 1.0 if name in ("R", "Rfc", "Rc", "Rv") else 0.7071
        else:
            mat[i, 0] = 0.5
            mat[i, 1] = 0.5
    return mat


def _peaks_dbfs(block: np.ndarray) -> list[float]:
    """Per-column peak dBFS for shape `(frames, channels)`."""
    if block.shape[0] == 0:
        return [SILENCE_DB] * block.shape[1]
    peaks = np.max(np.abs(block), axis=0)
    out: list[float] = []
    for p in peaks:
        val = float(p)
        out.append(max(SILENCE_DB, 20.0 * math.log10(val)) if val > 1e-5 else SILENCE_DB)
    return out


@dataclass(frozen=True, slots=True)
class ObjectSnapshot:
    index: int
    x: float
    y: float
    z: float
    level_db: float
    active: bool


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
        return self._engine.readable_frames * 8 + super().bytesAvailable()


class PlaybackEngine(QObject):
    """Streaming spatial audio player supporting Atmos, ADM, AV3A/HOA, 360RA/CICP, and Binaural."""

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
        self._binaural: bool = False
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
    def binaural(self) -> bool:
        return self._binaural

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

    def set_binaural(self, enabled: bool) -> None:
        if self._binaural == enabled:
            return
        self._binaural = enabled
        if self._reload_pending():
            return
        if self._path is not None:
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

    def load(self, path: Path, start_at: float = 0.0, auto_play: bool = True) -> None:
        self.stop()
        with self._lock:
            if self._source is not None and self._source.progress is not None:
                self._source.close()
                self._source = None
                self._total_frames = 0
        self._set_buffering(True)
        token = CancellationToken()
        self._path = path
        options = PlaybackOptions(self._layout, self._cicp, self._binaural)
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
            assert source is not None
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
            self.clear_ms()
            self._last_ch_dbs, self._last_objs = [], []
        if previous is not None:
            previous.close()
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
        if pending is not None:
            pending.close()

    def unload(self) -> None:
        self.stop()
        with self._lock:
            source, self._source = self._source, None
            self._path = None
            self._total_frames = 0
            self._last_ch_dbs, self._last_objs = [], []
            self.clear_ms()
        if source is not None:
            source.close()
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
            if self._source is not None and self._source.buffer is not None:
                self._source.buffer.request(self._cursor)
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
            if not self._playing or self.readable_frames == 0:
                return np.empty((0, 2), dtype=np.float32)

            avail = min(frames, self.readable_frames)
            start = self._cursor
            self._cursor += avail

            source = self._source
            assert source is not None
            streamed_stereo = None
            if source.buffer is not None:
                block = source.buffer.read(start, avail)
                buf = block[:, : source.buffer.channels]
                if source.buffer.stereo:
                    streamed_stereo = block[:, source.buffer.channels :]
                self._last_objs = []
                if source.vvm1_header is not None and source.vvm1_path is not None:
                    frame = start // source.vvm1_header.frame_samples
                    row = read_vvm1_frame(source.vvm1_path, source.vvm1_header, frame)
                    if row is not None:
                        self._last_objs = self._eval_vvm1_row(source.vvm1_header, row)
            elif source.adm is not None:
                raw = source.adm.read_frames(start, avail)
                t_mid = (start + avail * 0.5) / self._sample_rate
                spk_buf = np.zeros((avail, len(self._layout.channels)), dtype=np.float32)
                objs: list[ObjectSnapshot] = []
                obj_idx = 0
                for tr in source.adm.tracks:
                    if tr.is_object:
                        active_ms = (
                            obj_idx not in self._muted_obj
                            and (not self._soloed_obj or obj_idx in self._soloed_obj)
                        )
                        peak = float(np.max(np.abs(raw[:, tr.channel_index])))
                        db = max(SILENCE_DB, 20.0 * math.log10(peak)) if peak > 1e-5 else SILENCE_DB
                        blk = source.adm.block_at(tr, t_mid)
                        ox, oy, oz = (blk.x, blk.y, blk.z) if blk else (0.5, 0.5, 0.0)
                        objs.append(ObjectSnapshot(obj_idx, ox, oy, oz, db if active_ms else SILENCE_DB, active_ms))
                        if active_ms:
                            g = source.adm._track_gains_at(tr, t_mid, self._layout)
                            if np.any(g):
                                spk_buf += raw[:, tr.channel_index : tr.channel_index + 1] * g[np.newaxis, :]
                        obj_idx += 1
                    elif not self._soloed_obj:
                        g = source.adm._track_gains_at(tr, t_mid, self._layout)
                        if np.any(g):
                            spk_buf += raw[:, tr.channel_index : tr.channel_index + 1] * g[np.newaxis, :]
                self._last_objs = objs
                buf = spk_buf
            elif source.audio is not None:
                buf = np.ascontiguousarray(source.audio.samples[:, start : start + avail].T)
                if source.vvm1_frames is not None and source.vvm1_header is not None:
                    self._last_objs = self._eval_vvm1(start, source.vvm1_header, source.vvm1_frames)
                elif source.trace is not None:
                    t_mid = (start + avail * 0.5) / self._sample_rate
                    self._last_objs = self._eval_trace(t_mid, source.trace)
                else:
                    self._last_objs = []
            else:
                return np.zeros((frames, 2), dtype=np.float32)

            # Channel M/S & metering reflect bed/speaker channels
            bed_limit = (
                source.vvm1_header.bed_channels
                if source.vvm1_header is not None and source.vvm1_header.bed_channels > 0
                else buf.shape[1]
            )
            meter_buf = buf[:, :bed_limit]
            n_ch = meter_buf.shape[1]
            mask = np.ones(n_ch, dtype=np.float32)
            for idx in range(n_ch):
                if idx in self._muted_ch or (self._soloed_ch and idx not in self._soloed_ch):
                    mask[idx] = 0.0
            masked = meter_buf * mask[np.newaxis, :]
            self._last_ch_dbs = _peaks_dbfs(masked)

            gain = self._volume
            alternate = source.stereo_audio
            mpegh_mix = source.mpegh_monitor
            unchanged = not (self._muted_ch or self._soloed_ch or self._muted_obj or self._soloed_obj)
            if streamed_stereo is not None and unchanged:
                return cap_stream_peak(streamed_stereo, gain)
            if alternate is not None and unchanged:
                stereo = np.zeros((avail, 2), dtype=np.float32)
                samples = alternate.samples[:, start : start + avail].T
                stereo[: len(samples)] = samples
                if mpegh_mix is not None:
                    gain = monitor_gain(gain, mpegh_mix.stereo_peak)
            elif mpegh_mix is not None:
                stereo = masked @ mpegh_mix.matrix
                if source.buffer is not None:
                    if n_ch <= 2:
                        return cap_stream_peak(stereo, gain)
                    bound = float((np.abs(mpegh_mix.matrix) * mask[:, None]).sum(axis=0).max())
                else:
                    bound = mpegh_mix.fold_peak
                gain = monitor_gain(gain, bound)
            elif n_ch == 1:
                stereo = np.repeat(masked, 2, axis=1)
            elif n_ch == 2:
                stereo = masked
            elif n_ch == self._fold.shape[0]:
                stereo = masked @ self._fold
            else:
                stereo = masked[:, :2]

            return np.clip(stereo * gain, -1.0, 1.0).astype(np.float32, copy=False)

    def _eval_vvm1(
        self, start_frame: int, hdr: VvmTimelineHeader, frames_arr: np.ndarray
    ) -> list[ObjectSnapshot]:
        idx = min(frames_arr.shape[0] - 1, max(0, start_frame // hdr.frame_samples))
        return self._eval_vvm1_row(hdr, frames_arr[idx])

    def _eval_vvm1_row(self, hdr: VvmTimelineHeader, row: np.ndarray) -> list[ObjectSnapshot]:
        objs: list[ObjectSnapshot] = []
        for oi in range(hdr.object_count):
            ch_idx = hdr.bed_channels + oi
            active_ms = oi not in self._muted_obj and (not self._soloed_obj or oi in self._soloed_obj)
            vx, vy, vz, peak, _ = row[ch_idx]
            ox = max(0.0, min(1.0, float((vx + 1.0) * 0.5)))
            oy = max(0.0, min(1.0, float((1.0 - vy) * 0.5)))
            oz = max(0.0, min(1.0, float(vz)))
            db = max(SILENCE_DB, 20.0 * math.log10(float(peak))) if peak > 1e-5 else SILENCE_DB
            shown = active_ms and db > -80.0
            objs.append(ObjectSnapshot(oi, ox, oy, oz, db if shown else SILENCE_DB, shown))
        return objs

    def _eval_trace(self, t_mid: float, trace: ObjectTrace) -> list[ObjectSnapshot]:
        objs: list[ObjectSnapshot] = []
        step_idx = int((t_mid - trace.level_start) / trace.level_step) if trace.level_step > 0 else 0
        for oi, path in enumerate(trace.objects):
            active_ms = oi not in self._muted_obj and (not self._soloed_obj or oi in self._soloed_obj)
            ox, oy, oz, active = 0.5, 0.5, 0.0, True
            prev_x, prev_y, prev_z = 0.5, 0.5, 0.0
            for k_t, k_ramp, kx, ky, kz, ka in path.keys:
                if t_mid < k_t:
                    break
                if k_ramp > 0.0 and t_mid < k_t + k_ramp:
                    f = (t_mid - k_t) / k_ramp
                    ox = prev_x + (kx - prev_x) * f
                    oy = prev_y + (ky - prev_y) * f
                    oz = prev_z + (kz - prev_z) * f
                    active = bool(ka)
                    break
                ox, oy, oz, active = kx, ky, kz, bool(ka)
                prev_x, prev_y, prev_z = kx, ky, kz
            db = float(path.levels[step_idx]) if 0 <= step_idx < len(path.levels) else SILENCE_DB
            shown = active and active_ms and db > -80.0
            objs.append(ObjectSnapshot(oi, ox, oy, oz, db if shown else SILENCE_DB, shown))
        return objs

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
                self._set_buffering(not self.readable_frames)
                if self.readable_frames:
                    self._io.readyRead.emit()
            else:
                self._set_buffering(False)
        self.position_changed.emit(self.position)
        self.meters_updated.emit(list(self._last_ch_dbs), list(self._last_objs))
