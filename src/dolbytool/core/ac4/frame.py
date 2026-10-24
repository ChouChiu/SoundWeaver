"""Decoding one raw AC-4 frame of the first presentation into PCM per channel."""

from __future__ import annotations

import logging
from collections import deque

import numpy as np

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError
from dolbytool.core.atmos.qmf import BANDS, DELAY, Analysis, Synthesis

from . import acpl, ajoc, aspx
from .asf import Noise
from .elements import Element, Reader, Tools, audio_data
from .imdct import Imdct
from .toc import Toc, read_toc

logger = logging.getLogger(__name__)

# the order channels come out in: WAVE's (and ffmpeg's) for the layouts AC-4 codes
CHANNEL_ORDER = ("L", "R", "C", "LFE", "Lb", "Rb", "Ls", "Rs", "Lw", "Rw", "Tfl", "Tfr")
# Table 188 (TS 103 190-1 clause 5.6), per frame_length: the PCM delay d_pcm in samples
# and the control data delay d_ctrl in frames that line the QMF tools' data up with the signal
ALIGNMENT = {
    1920: (288, 1), 2048: (352, 1), 1536: (96, 1), 960: (960, 2),
    1024: (1056, 2), 768: (672, 2), 512: (1312, 4), 384: (864, 4),
}  # fmt: skip


IMS_VERSION = 2
STEREO = 1


def coded_channel_mode(presentation_version: int, channel_mode: int) -> int:
    """The channel mode the audio data is coded in.

    A presentation_version 2 presentation is Immersive Stereo, which TS 103 190-2
    V1.3.1 does not define.  In the streams at hand its substream names an
    immersive channel_mode (7.1 in Apple Music's) while every frame's audio data
    is a channel_pair_element that fills audio_size to the byte, so the audio is
    read as the stereo it is and the channel_mode is taken for what it was made from.
    """
    return STEREO if presentation_version == IMS_VERSION else channel_mode


class FrameDecoder:
    """Keeps the per-channel transform state from one frame to the next."""

    def __init__(self) -> None:
        self.transforms: dict[str, Imdct] = {}
        self.channel_mode: int | None = None
        self.tools: Tools | None = None
        self.analysis: dict[str, Analysis] = {}
        self.synthesis: dict[str, Synthesis] = {}
        # samples the output lags the coded timeline by: 0, or the QMF tools' delay
        self.delay = 0
        # an A-JOC substream's state, and what its last frame said
        self.ajoc_state = ajoc.AjocState()
        self.reconstructor: ajoc.Reconstructor | None = None
        self.payload: ajoc.AjocPayload | None = None
        # frame alignment: the PCM held back by d_pcm, and the last frames' QMF tool data
        self.held: dict[str, np.ndarray] = {}
        self.controls: deque[tuple[Element, ajoc.AjocPayload | None]] = deque()
        # for a decoder started on a later I-frame: A-SPX's noise and sine table positions
        # (aspx.Cursor) as decoding from the start would have them on its first frame
        self.resume: dict[str, aspx.Cursor] | None = None

    def toc(self, frame: bytes) -> Toc:
        return read_toc(frame)

    def element(self, frame: bytes, toc: Toc | None = None) -> Element:
        """The audio of the first presentation's channel coded substream, before the transform."""
        toc = toc or read_toc(frame)
        if not toc.presentations:
            raise BitstreamError("the frame has no presentation")
        presentation = toc.presentations[0]
        group = toc.groups[presentation.groups[0]]
        info = group.substreams[0]
        if not group.channel_coded and info.kind != "ajoc":
            raise BitstreamError("object coded substreams are only supported A-JOC coded")
        start = toc.data_start + toc.payload_base + sum(toc.substream_sizes[: info.index])
        end = start + toc.substream_sizes[info.index] if toc.substream_sizes else len(frame)
        bits = BitReader(frame, start * 8, min(end, len(frame)) * 8)
        size = bits.read(15)
        if bits.flag():
            size += bits.variable_bits(7) << 15
        audio_start = bits.pos
        if self.tools is None or self.tools.frame_len != toc.frame_length:
            self.tools = Tools(toc.frame_length, toc.fs_index == 1)
        reader = Reader(
            bits, toc.frame_length, info.iframe or toc.iframe_global, Noise(toc.sequence_counter),
            self.tools,
        )  # fmt: skip
        if info.kind == "ajoc":
            self.ajoc_state.group_timing = None
            if group.oamd_index is not None and toc.substream_sizes:
                at = (
                    toc.data_start + toc.payload_base + sum(toc.substream_sizes[: group.oamd_index])
                )
                stop = at + toc.substream_sizes[group.oamd_index]
                ajoc.oamd_substream(BitReader(frame, at * 8, stop * 8), self.ajoc_state)
            element, self.payload = ajoc.audio_data_ajoc(
                reader, info, presentation.alternative, self.ajoc_state
            )
        else:
            mode = coded_channel_mode(presentation.version, info.channel_mode)
            element = audio_data(reader, mode, info.add_ch_base)
            self.payload = None
        if bits.pos - audio_start > size * 8:
            raise BitstreamError(
                f"audio data overran its {size} bytes by {bits.pos - audio_start - size * 8} bits"
            )
        self.channel_mode = info.channel_mode
        return element

    def decode(self, frame: bytes) -> dict[str, np.ndarray]:
        """PCM per channel; for A-JOC per object ("o0", "o1", ...) and "LFE", with the
        objects' metadata in `payload`.

        Through the QMF tools the frame alignment of clause 5.6 applies: the
        signal is held back by d_pcm and each frame's tool data (A-JOC's and its
        object metadata among them) by d_ctrl frames, so `payload` is then the
        one of d_ctrl frames ago, which the returned samples go with.
        """
        toc = read_toc(frame)
        element = self.element(frame, toc)
        out = {}
        for name, track in element.channels.items():
            transform = self.transforms.get(name)
            if transform is None:
                transform = self.transforms[name] = Imdct(toc.frame_length)
            out[name] = transform(track.spectrum, track.windows)
        if element.qmf or self.delay or self.payload is not None:
            pcm_delay, ctrl_delay = ALIGNMENT[toc.frame_length]
            for name, x in out.items():
                y = np.concatenate([self.held.get(name, np.zeros(pcm_delay)), x])
                out[name], self.held[name] = y[: len(x)], y[len(x) :]
            self.controls.append((element, self.payload))
            # until d_ctrl frames have gone by, the first frame's data stands in
            if len(self.controls) > ctrl_delay:
                element, self.payload = self.controls.popleft()
                if self.resume is not None:
                    for name, cursor in self.resume.items():
                        cursor.restore(self.tools.channel(name))
                    self.resume = None
            else:
                element, self.payload = self.controls[0]
            out = self._qmf(element, out, toc.frame_length)
            self.delay += pcm_delay
        return out

    def _qmf(
        self, element: Element, pcm: dict[str, np.ndarray], frame_len: int
    ) -> dict[str, np.ndarray]:
        """Companding, A-SPX and A-CPL in the QMF domain (clause 5.7), every channel delayed alike.

        Once a stream has used them, every later frame goes through the banks too,
        so the output keeps one delay: the banks' and A-SPX's.
        """
        tools = self.tools
        q = {}
        for name, x in pcm.items():
            bank = self.analysis.setdefault(name, Analysis(1))
            q[name] = bank(x[None, :])[0].T  # [subband, slot]
        k = 2 if frame_len >= 1536 else 1
        if element.companding is not None:
            comp = element.companding
            spans, mats = [], []
            for name in comp.channels:
                f = element.aspx.get(name)
                ch = tools.aspx.get(name)
                sb1 = ch.bands.sbx if ch is not None and ch.bands is not None else BANDS
                sb0 = (
                    element.acpl[0].config.qmf_band
                    if element.codec_mode == 2 and element.acpl
                    else 0
                )
                ts0, ts1 = (
                    (f.sig_borders[0] * k, f.sig_borders[-1] * k) if f else (0, q[name].shape[1])
                )
                spans.append((sb0, sb1, ts0, min(ts1, q[name].shape[1])))
                mats.append(q[name] / 32768.0)
            aspx.compand(mats, spans, comp.on, comp.avg, comp.sync)
            for name, m in zip(comp.channels, mats):
                q[name] = m * 32768.0
        # A-SPX, jointly for the two channels of a balanced pair; the rest only delayed
        done = set()
        names = list(element.aspx)
        for i, name in enumerate(names):
            if name in done:
                continue
            f = element.aspx[name]
            group = [name]
            if i + 1 < len(names) and element.aspx[names[i + 1]].second:
                group.append(names[i + 1])
            pairs = [(tools.channel(n), element.aspx[n]) for n in group]
            for n, y in zip(group, aspx.decode_channels(pairs, [q[n] for n in group])):
                q[n] = y
            done.update(group)
        for name, m in list(q.items()):
            if name not in done:
                q[name] = aspx.skip_channel(tools.channel(name), m)
        for c in element.acpl:
            module = tools.module(c.module, c.decorrelator)
            residual = q.get(c.residual) if c.residual else None
            z0, z1 = acpl.couple(module, c.data, c.config, q[c.source], residual)
            q[c.outputs[0]], q[c.outputs[1]] = z0 * c.scales[0], z1 * c.scales[1]
        for name, gain in element.gains.items():
            q[name] = q[name] * gain
        if self.payload is not None:
            q = self._objects(q)
        out = {}
        for name, m in q.items():
            bank = self.synthesis.setdefault(name, Synthesis(1))
            out[name] = bank(m.T[None])[0]
        self.delay = DELAY + aspx.HFGEN_ATS * k * BANDS
        return out

    def _objects(self, q: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        """The A-JOC objects (clause 5.7) from the downmix, the LFE passed by."""
        p = self.payload
        r = self.reconstructor
        if r is None or (r.n_dmx, r.n_umx) != (len(p.inputs), p.upmix):
            r = self.reconstructor = ajoc.Reconstructor(len(p.inputs), p.upmix)
        z = r(p.frame, np.stack([q[name] for name in p.inputs]))
        out = {f"o{i}": z[i] for i in range(p.upmix)}
        if p.lfe is not None:
            out["LFE"] = q[p.lfe]
        return out
