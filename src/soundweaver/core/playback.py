"""Real-time spatial audio playback engine with stereo fold-down, M/S matrix, and 20 Hz metering."""

from __future__ import annotations

import logging
import math
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QIODevice, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

from soundweaver.core.atmos import ObjectTrace, PreparedADM, decode_atmos, is_adm_bwf
from soundweaver.core.audio import AudioInfo, load_audio, probe_audio
from soundweaver.core.cancel import CancellationToken
from soundweaver.core.codecs import av3a, mpegh
from soundweaver.core.paths import work_root
from soundweaver.core.pcm import DEFAULT_BIT_DEPTH, AudioData, read_audio
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


class _PCMBufferDevice(QIODevice):
    """Pull-mode QIODevice backed by PlaybackEngine."""

    def __init__(self, engine: PlaybackEngine) -> None:
        super().__init__(engine)
        self._engine = engine

    def isSequential(self) -> bool:
        return True

    def atEnd(self) -> bool:
        return False

    def readData(self, maxlen: int) -> bytes:
        frames = maxlen // 8
        if frames <= 0:
            return b""
        return self._engine._pull_stereo(frames).tobytes()

    def writeData(self, data: bytes) -> int:
        return 0

    def bytesAvailable(self) -> int:
        return 32768 + super().bytesAvailable()


class PlaybackEngine(QObject):
    """Streaming spatial audio player supporting Atmos, ADM, AV3A/HOA, 360RA/CICP, and Binaural."""

    track_loaded = Signal(object, object)  # (Path, AudioInfo)
    load_failed = Signal(str)
    state_changed = Signal(bool)
    position_changed = Signal(float)
    meters_updated = Signal(list, list)  # (channel_dbs, object_snapshots)
    track_finished = Signal()
    _ready_to_play = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._layout: SpeakerLayout = DEFAULT_ATMOS
        self._cicp: int = 13
        self._binaural: bool = False
        self._active_channels: tuple[str, ...] = DEFAULT_ATMOS.channels
        self._fold = fold_matrix(self._active_channels)

        self._audio: AudioData | None = None
        self._binaural_audio: AudioData | None = None
        self._adm: PreparedADM | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._trace: ObjectTrace | None = None
        self._vvm1_header: av3a.VvmTimelineHeader | None = None
        self._vvm1_frames: np.ndarray | None = None  # shape (n_frames, total_ch, 5)
        self._info: AudioInfo | None = None
        self._path: Path | None = None
        self._sample_rate = 48000
        self._total_frames = 0
        self._cursor = 0
        self._playing = False
        self._volume = 1.0
        self._muted_ch: set[int] = set()
        self._soloed_ch: set[int] = set()
        self._muted_obj: set[int] = set()
        self._soloed_obj: set[int] = set()
        self._last_ch_dbs: list[float] = []
        self._last_objs: list[ObjectSnapshot] = []
        self._decode_token: CancellationToken | None = None

        self._sink: QAudioSink | None = None
        self._io = _PCMBufferDevice(self)
        self._io.open(QIODevice.OpenModeFlag.ReadOnly)

        self._meter_timer = QTimer(self)
        self._meter_timer.setInterval(50)  # 20 Hz
        self._meter_timer.timeout.connect(self._emit_telemetry)
        self._ready_to_play.connect(self._start_sink, Qt.ConnectionType.QueuedConnection)

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
        return self._info

    @property
    def is_playing(self) -> bool:
        return self._playing

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
        if self._path is not None and self._info is not None and self._info.atmos and self._adm is None and not self._info.is_av3a:
            self.load(self._path, start_at=self.position, auto_play=self._playing)

    def set_cicp(self, cicp: int) -> None:
        with self._lock:
            self._cicp = cicp
            if self._info is not None and self._info.is_mpegh:
                self._active_channels = mpegh.cicp_layout(cicp).channel_names
                self._fold = fold_matrix(self._active_channels)
            self._muted_ch.clear()
            self._soloed_ch.clear()
        if self._path is not None and self._info is not None and self._info.is_mpegh:
            self.load(self._path, start_at=self.position, auto_play=self._playing)

    def set_binaural(self, enabled: bool) -> None:
        if self._binaural == enabled:
            return
        self._binaural = enabled
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

    def load(self, path: Path, start_at: float = 0.0, auto_play: bool = True) -> None:
        self.stop()
        if self._decode_token is not None:
            self._decode_token.cancel()
        token = CancellationToken()
        self._decode_token = token
        self._path = path

        def worker() -> None:
            try:
                info = probe_audio(path)
                token.raise_if_cancelled()
                adm: PreparedADM | None = None
                temp_dir: tempfile.TemporaryDirectory[str] | None = None
                audio: AudioData | None = None
                bin_audio: AudioData | None = None
                trace: ObjectTrace | None = None
                vvm1_header: av3a.VvmTimelineHeader | None = None
                vvm1_frames: np.ndarray | None = None
                active_channels = self._layout.channels

                if is_adm_bwf(path):
                    adm = PreparedADM(path)
                    if self._binaural and av3a.find_binaural_renderer():
                        temp_dir = tempfile.TemporaryDirectory(dir=work_root())
                        bin_wav = Path(temp_dir.name) / "binaural.wav"
                        av3a.render_binaural_file(path, bin_wav, token=token)
                        bin_audio = read_audio(bin_wav, token)
                elif info.is_av3a:
                    temp_dir = tempfile.TemporaryDirectory(dir=work_root())
                    tmp = Path(temp_dir.name)
                    tl_path = tmp / "timeline.vvm1"
                    pcm_path = tmp / "source.f32"
                    raw_bin = tmp / "stream.f32"
                    stream = av3a.stream_av3a(path, "source", tl_path, pcm_path)
                    chunks = []
                    try:
                        while True:
                            token.raise_if_cancelled()
                            blk = stream.read_frames(8192)
                            if blk.shape[0] == 0:
                                break
                            chunks.append(blk)
                    finally:
                        stream.close()
                    stacked = np.vstack(chunks) if chunks else np.zeros((0, stream.channels), dtype=np.float32)
                    stacked.tofile(raw_bin)
                    if stacked.shape[0] > 0:
                        mm = np.memmap(raw_bin, dtype=np.float32, mode="r", shape=stacked.shape)
                        audio = AudioData(mm.T, stream.sample_rate, DEFAULT_BIT_DEPTH, raw_bin)
                    else:
                        audio = AudioData(np.zeros((stream.channels, 0), dtype=np.float32), stream.sample_rate)
                    vvm1_header = av3a.read_vvm1_header(tl_path)
                    if vvm1_header is not None:
                        tot = vvm1_header.bed_channels + vvm1_header.object_count
                        raw_tl = tl_path.read_bytes()[24:]
                        nf = len(raw_tl) // (tot * 5 * 4) if tot > 0 else 0
                        if nf > 0:
                            vvm1_frames = np.frombuffer(raw_tl[: nf * tot * 5 * 4], dtype="<f4").reshape(nf, tot, 5).copy()
                    active_channels = info.bed_channels or tuple(f"Ch {i + 1}" for i in range(audio.channels))
                    if self._binaural and av3a.find_binaural_renderer() and (info.hoa_order > 0 or audio.channels >= 4):
                        bin_audio = av3a.render_binaural_audio(
                            audio,
                            speakers=None if info.hoa_order > 0 else active_channels[: len(info.bed_channels) or audio.channels],
                            hoa_order=info.hoa_order,
                            token=token,
                        )
                elif info.is_mpegh:
                    audio = mpegh.decode_mpegh(path, token, cicp=self._cicp)
                    active_channels = mpegh.cicp_layout(self._cicp).channel_names[: audio.channels]
                    if self._binaural and av3a.find_binaural_renderer() and audio.channels >= 4:
                        bin_audio = av3a.render_binaural_audio(audio, speakers=active_channels, token=token)
                elif info.atmos:
                    temp_dir = tempfile.TemporaryDirectory(dir=work_root())
                    adm_path = Path(temp_dir.name) / "decoded_adm.wav"
                    decoded = decode_atmos(
                        path, token, lambda _f: None, layout=self._layout, speakers=False, adm=adm_path
                    )
                    trace = decoded.trace
                    if adm_path.is_file():
                        adm = PreparedADM(adm_path)
                        if self._binaural and av3a.find_binaural_renderer():
                            bin_wav = Path(temp_dir.name) / "binaural.wav"
                            av3a.render_binaural_file(adm_path, bin_wav, token=token)
                            bin_audio = read_audio(bin_wav, token)
                    else:
                        decoded = decode_atmos(path, token, lambda _f: None, layout=self._layout, speakers=True)
                        audio = decoded.speakers
                else:
                    audio = load_audio(path, token)
                    if info.speakers is not None:
                        active_channels = info.speakers.channels
                    else:
                        active_channels = tuple(f"Ch {i + 1}" for i in range(audio.channels))

                token.raise_if_cancelled()
                with self._lock:
                    self._info = info
                    if self._adm is not None:
                        self._adm.close()
                    self._adm = adm
                    if self._temp_dir is not None:
                        self._temp_dir.cleanup()
                    self._temp_dir = temp_dir
                    self._trace = trace
                    self._vvm1_header = vvm1_header
                    self._vvm1_frames = vvm1_frames
                    if self._audio is not None:
                        self._audio.cleanup()
                    self._audio = audio
                    if self._binaural_audio is not None:
                        self._binaural_audio.cleanup()
                    self._binaural_audio = bin_audio
                    self._active_channels = active_channels
                    self._fold = fold_matrix(active_channels)
                    self._sample_rate = adm.sample_rate if adm else (audio.sample_rate if audio else 48000)
                    self._total_frames = adm.frame_count if adm else (audio.frames if audio else 0)
                    self._cursor = min(self._total_frames, max(0, int(start_at * self._sample_rate)))
                    self._muted_ch.clear()
                    self._soloed_ch.clear()
                    self._muted_obj.clear()
                    self._soloed_obj.clear()
                self.track_loaded.emit(path, info)
                if auto_play:
                    self._ready_to_play.emit()
            except Exception as exc:
                if not token.is_cancelled():
                    logger.warning("Playback load failed: %s", exc)
                    self.load_failed.emit(str(exc))

        threading.Thread(target=worker, daemon=True).start()

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
        self._sink = QAudioSink(device, fmt, self)
        self._sink.start(self._io)
        self._meter_timer.start()
        self.state_changed.emit(True)

    def play(self) -> None:
        if self._total_frames == 0:
            return
        if self._sink is None:
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
        self.state_changed.emit(False)

    def toggle_play(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()

    def stop(self) -> None:
        self._playing = False
        self._meter_timer.stop()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
            self._sink = None
        self._cursor = 0
        self.state_changed.emit(False)
        self.position_changed.emit(0.0)

    def seek(self, seconds: float) -> None:
        with self._lock:
            self._cursor = min(self._total_frames, max(0, int(seconds * self._sample_rate)))
        self.position_changed.emit(self.position)

    def _pull_stereo(self, frames: int) -> np.ndarray:
        with self._lock:
            if not self._playing or self._cursor >= self._total_frames:
                if self._playing and self._cursor >= self._total_frames and self._total_frames > 0:
                    self._playing = False
                    self.track_finished.emit()
                return np.zeros((frames, 2), dtype=np.float32)

            avail = min(frames, self._total_frames - self._cursor)
            start = self._cursor
            self._cursor += avail

            if self._adm is not None:
                raw = self._adm.read_frames(start, avail)
                t_mid = (start + avail * 0.5) / self._sample_rate
                spk_buf = np.zeros((avail, len(self._layout.channels)), dtype=np.float32)
                objs: list[ObjectSnapshot] = []
                obj_idx = 0
                for tr in self._adm.tracks:
                    if tr.is_object:
                        active_ms = (
                            obj_idx not in self._muted_obj
                            and (not self._soloed_obj or obj_idx in self._soloed_obj)
                        )
                        peak = float(np.max(np.abs(raw[:, tr.channel_index]))) if avail > 0 else 0.0
                        db = max(SILENCE_DB, 20.0 * math.log10(peak)) if peak > 1e-5 else SILENCE_DB
                        blk = tr.blocks[0] if tr.blocks else None
                        for b in tr.blocks:
                            if b.start <= t_mid:
                                blk = b
                            else:
                                break
                        ox, oy, oz = (blk.x, blk.y, blk.z) if blk else (0.5, 0.5, 0.0)
                        objs.append(ObjectSnapshot(obj_idx, ox, oy, oz, db if active_ms else SILENCE_DB, active_ms))
                        if active_ms:
                            g = self._adm._track_gains_at(tr, t_mid, self._layout)
                            if np.any(g):
                                spk_buf += raw[:, tr.channel_index : tr.channel_index + 1] * g[np.newaxis, :]
                        obj_idx += 1
                    elif not self._soloed_obj:
                        g = self._adm._track_gains_at(tr, t_mid, self._layout)
                        if np.any(g):
                            spk_buf += raw[:, tr.channel_index : tr.channel_index + 1] * g[np.newaxis, :]
                self._last_objs = objs
                buf = spk_buf
            elif self._audio is not None:
                buf = np.ascontiguousarray(self._audio.samples[:, start : start + avail].T)
                if self._vvm1_frames is not None and self._vvm1_header is not None:
                    self._last_objs = self._eval_vvm1(start)
                elif self._trace is not None:
                    t_mid = (start + avail * 0.5) / self._sample_rate
                    self._last_objs = self._eval_trace(t_mid)
                else:
                    self._last_objs = []
            else:
                return np.zeros((frames, 2), dtype=np.float32)

            # Channel M/S & metering reflect bed/speaker channels
            bed_limit = (
                self._vvm1_header.bed_channels
                if self._vvm1_header is not None and self._vvm1_header.bed_channels > 0
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

            if self._binaural_audio is not None and not self._muted_ch and not self._soloed_ch and not self._muted_obj and not self._soloed_obj:
                b_avail = min(avail, max(0, self._binaural_audio.frames - start))
                if b_avail > 0:
                    stereo = np.ascontiguousarray(self._binaural_audio.samples[:, start : start + b_avail].T)
                    if b_avail < avail:
                        stereo = np.vstack([stereo, np.zeros((avail - b_avail, 2), dtype=np.float32)])
                else:
                    stereo = np.zeros((avail, 2), dtype=np.float32)
            elif n_ch == 1:
                stereo = np.repeat(masked, 2, axis=1)
            elif n_ch == 2:
                stereo = masked
            elif n_ch == self._fold.shape[0]:
                stereo = masked @ self._fold
            else:
                stereo = masked[:, :2]

            stereo = np.clip(stereo * self._volume, -1.0, 1.0).astype(np.float32, copy=False)
            if avail < frames:
                pad = np.zeros((frames - avail, 2), dtype=np.float32)
                return np.vstack([stereo, pad])
            return stereo

    def _eval_vvm1(self, start_frame: int) -> list[ObjectSnapshot]:
        hdr = self._vvm1_header
        frames_arr = self._vvm1_frames
        if hdr is None or frames_arr is None or hdr.object_count <= 0:
            return []
        idx = min(frames_arr.shape[0] - 1, max(0, start_frame // hdr.frame_samples))
        row = frames_arr[idx]
        objs: list[ObjectSnapshot] = []
        for oi in range(hdr.object_count):
            ch_idx = hdr.bed_channels + oi
            if ch_idx >= row.shape[0]:
                break
            active_ms = oi not in self._muted_obj and (not self._soloed_obj or oi in self._soloed_obj)
            vx, vy, vz, peak, _ = row[ch_idx]
            ox = max(0.0, min(1.0, float((vx + 1.0) * 0.5)))
            oy = max(0.0, min(1.0, float((1.0 - vy) * 0.5)))
            oz = max(0.0, min(1.0, float(vz)))
            db = max(SILENCE_DB, 20.0 * math.log10(float(peak))) if peak > 1e-5 else SILENCE_DB
            shown = active_ms and db > -80.0
            objs.append(ObjectSnapshot(oi, ox, oy, oz, db if shown else SILENCE_DB, shown))
        return objs

    def _eval_trace(self, t_mid: float) -> list[ObjectSnapshot]:
        trace = self._trace
        if trace is None:
            return []
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
        self.position_changed.emit(self.position)
        self.meters_updated.emit(list(self._last_ch_dbs), list(self._last_objs))
