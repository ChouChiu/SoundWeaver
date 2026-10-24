"""Decoding one raw AC-4 frame of the first presentation into PCM per channel."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import replace

import numpy as np

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError
from dolbytool.core.atmos.oamd import Frame, Trim
from dolbytool.core.atmos.qmf import BANDS, DELAY, Analysis, Synthesis
from dolbytool.core.atmos.render import Renderer
from dolbytool.core.speakers import LAYOUTS, SpeakerLayout

from . import acpl, ajoc, aspx, oamd
from .asf import Noise
from .elements import Element, Reader, Tools, audio_data
from .imdct import Imdct
from .toc import DYN, SubstreamGroup, Toc, read_toc

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
# the A-SPX positions (FrameDecoder.resume) of the extra objects' substream go by this
EXTRA = "extra:"
# objs_to_channel_mode (clause 6.2.3.3) for the counts supported: the channels that
# hold the object essences, in their order
_OBJS_CHANNELS = {1: ("C",), 2: ("L", "R")}


def _check_size(bits: BitReader, audio_start: int, size: int) -> None:
    if bits.pos - audio_start > size * 8:
        raise BitstreamError(
            f"audio data overran its {size} bytes by {bits.pos - audio_start - size * 8} bits"
        )


def _joined(meta: Frame, extra: Frame) -> Frame:
    """An A-JOC frame's metadata with the extra objects after its own, as dynamic objects."""
    n = meta.object_count
    program = replace(
        meta.program, dynamic_objects=meta.program.dynamic_objects + extra.object_count
    )
    exempt = meta.trim.exempt | {n + i for i in extra.trim.exempt}
    return Frame(
        n + extra.object_count, program, meta.updates + extra.updates,
        replace(meta.trim, exempt=frozenset(exempt)),
    )  # fmt: skip


def _layout_of(channels: dict[str, np.ndarray]) -> SpeakerLayout:
    """The speaker layout of a channel coded presentation's channels, its LFE aside."""
    names = set(channels) - {"LFE"}
    for layout in LAYOUTS:
        if set(layout.channels) - {"LFE"} == names:
            return layout
    raise BitstreamError(f"no speaker layout to render objects to for {'/'.join(channels)}")


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
        # (with the extra objects' metadata of a channel coded presentation)
        self.held: dict[str, np.ndarray] = {}
        self.controls: deque[tuple[Element, ajoc.AjocPayload | None, Frame | None]] = deque()
        # for a decoder started on a later I-frame: A-SPX's noise and sine table positions
        # (aspx.Cursor) as decoding from the start would have them on its first frame;
        # those of the extra objects' substream are named with EXTRA before them
        self.resume: dict[str, aspx.Cursor] | None = None
        # the presentation's second substream group, object coded but not A-JOC (the
        # dialogue of a presentation that sets its own dialogue level): a decoder of its own,
        # the element it read from this frame, and its metadata as the output goes with it
        self.extra: FrameDecoder | None = None
        self.extra_element: Element | None = None
        self.extra_meta: Frame | None = None
        self.renderer: Renderer | None = None
        # as the extra decoder: its objects' OAMD state, and what this frame's said
        self.is_extra = False
        self.objects: oamd.Objects | None = None
        self.timing = oamd.Timing(())
        self.meta: Frame | None = None
        self.essences: tuple[str, ...] = ()

    def toc(self, frame: bytes) -> Toc:
        return read_toc(frame)

    def element(self, frame: bytes, toc: Toc | None = None) -> Element:
        """The audio of the first presentation's first substream, before the transform.

        The objects of a second substream group are read alongside by `extra`:
        an A-JOC payload takes them on as further objects, and a channel
        coded presentation has them rendered into its channels by `decode`.
        """
        toc = toc or read_toc(frame)
        if not toc.presentations:
            raise BitstreamError("the frame has no presentation")
        presentation = toc.presentations[0]
        group = toc.groups[presentation.groups[0]]
        info = group.substreams[0]
        if not group.channel_coded and info.kind != "ajoc":
            raise BitstreamError("object coded substreams are only supported A-JOC coded")
        bits, size, reader = self._reader(frame, toc, info)
        audio_start = bits.pos
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
        _check_size(bits, audio_start, size)
        self.channel_mode = info.channel_mode
        extra = presentation.groups[1:]
        if extra:
            if len(extra) > 1:
                raise BitstreamError("a presentation of more than two substream groups")
            if self.extra is None:
                self.extra = FrameDecoder()
                self.extra.is_extra = True
            self.extra_element = self.extra._objects_element(frame, toc, toc.groups[extra[0]])
            if self.payload is not None:
                self.payload.metadata = _joined(self.payload.metadata, self.extra.meta)
        elif self.extra is not None:
            raise BitstreamError("the presentation's extra objects went away")
        return element

    def _reader(self, frame: bytes, toc: Toc, info) -> tuple[BitReader, int, Reader]:
        """The bits of substream `info` past its audio_size, that size, and a Reader on them."""
        if not toc.substream_sizes and self.is_extra:
            raise BitstreamError("an object substream without a substream_index_table")
        start = toc.data_start + toc.payload_base + sum(toc.substream_sizes[: info.index])
        end = start + toc.substream_sizes[info.index] if toc.substream_sizes else len(frame)
        bits = BitReader(frame, start * 8, min(end, len(frame)) * 8)
        size = bits.read(15)
        if bits.flag():
            size += bits.variable_bits(7) << 15
        if self.tools is None or self.tools.frame_len != toc.frame_length:
            self.tools = Tools(toc.frame_length, toc.fs_index == 1)
        reader = Reader(
            bits, toc.frame_length, info.iframe or toc.iframe_global, Noise(toc.sequence_counter),
            self.tools,
        )  # fmt: skip
        return bits, size, reader

    def _objects_element(self, frame: bytes, toc: Toc, group: SubstreamGroup) -> Element:
        """audio_data_objs (clause 6.2.3.2) of an object substream, and its group's
        oamd_substream (clause 6.2.2.4) into `meta`.

        Its objects are coded as the channels of objs_to_channel_mode; the
        order of the object essences is that of the channels in the element.
        """
        info = group.substreams[0]
        if len(group.substreams) > 1 or info.kind != "obj":
            raise BitstreamError(
                "a second substream group is only supported as one object substream"
            )
        if len(info.objects) not in _OBJS_CHANNELS or any(
            kind != DYN or lfe for kind, lfe in info.objects
        ):
            raise BitstreamError(f"{len(info.objects)} object essences are not supported yet")
        if group.oamd_index is None:
            raise BitstreamError("object essences without an oamd_substream")
        bits, size, reader = self._reader(frame, toc, info)
        audio_start = bits.pos
        element = audio_data(reader, len(info.objects) - 1)  # mono or stereo
        _check_size(bits, audio_start, size)
        iframe = info.iframe or toc.iframe_global
        if self.objects is None or iframe:
            types, program = oamd.program_for(info.objects, (), None, False)
            trim = self.objects.trim if self.objects is not None else Trim()
            self.objects = oamd.Objects(types, program, trim=trim)
        at = toc.data_start + toc.payload_base + sum(toc.substream_sizes[: group.oamd_index])
        b = BitReader(frame, at * 8, (at + toc.substream_sizes[group.oamd_index]) * 8)
        if b.flag():  # b_oamd_common_data_present
            trim = oamd.read_common(b)
            if trim is not None:
                self.objects.trim = trim
        if b.flag():  # b_oamd_timing_present
            self.timing = oamd.read_timing(b)
        if toc.presentations[0].alternative:
            raise BitstreamError("alternative object properties are not supported yet")
        # oamd_dyndata_multi: every object of the group is coded here, none A-JOC coded
        self.meta = oamd.read_dyndata(b, self.objects, self.timing, iframe, False)
        self.essences = _OBJS_CHANNELS[len(info.objects)]
        return element

    def decode(self, frame: bytes) -> dict[str, np.ndarray]:
        """PCM per channel; for A-JOC per object ("o0", "o1", ...) and "LFE", with the
        objects' metadata in `payload`.

        Through the QMF tools the frame alignment of clause 5.6 applies: the
        signal is held back by d_pcm and each frame's tool data (A-JOC's and its
        object metadata among them) by d_ctrl frames, so `payload` is then the
        one of d_ctrl frames ago, which the returned samples go with.  A
        presentation with extra objects goes through the banks throughout, as
        their decoder does, so the two come out with one delay.
        """
        toc = read_toc(frame)
        element = self.element(frame, toc)
        out = self._pcm(toc, element)
        if self.extra is not None:
            objects = self.extra._pcm(toc, self.extra_element)
            if self.extra.delay != self.delay:
                raise BitstreamError("the extra objects came out with a delay of their own")
            essences = [objects[name] for name in self.extra.essences]
            if self.payload is not None:
                first = self.payload.upmix
                out.update({f"o{first + i}": x for i, x in enumerate(essences)})
            else:
                self._mix(out, essences, toc.frame_length)
        return out

    def _mix(self, out: dict[str, np.ndarray], essences: list[np.ndarray], frame_len: int) -> None:
        """The extra objects rendered into the presentation's own channels."""
        layout = _layout_of(out)
        if self.renderer is None:
            self.renderer = Renderer(len(essences), layout)
        gains = self.renderer.gains(self.extra_meta, frame_len)[..., :-1]
        mixed = np.einsum("ots,ot->st", gains, np.stack(essences))
        for name, x in zip(layout.channels, mixed, strict=True):
            if name in out:
                out[name] = out[name] + x

    def _pcm(self, toc: Toc, element: Element) -> dict[str, np.ndarray]:
        out = {}
        for name, track in element.channels.items():
            transform = self.transforms.get(name)
            if transform is None:
                transform = self.transforms[name] = Imdct(toc.frame_length)
            out[name] = transform(track.spectrum, track.windows)
        banks = self.is_extra or self.extra is not None
        if element.qmf or self.delay or self.payload is not None or banks:
            pcm_delay, ctrl_delay = ALIGNMENT[toc.frame_length]
            for name, x in out.items():
                y = np.concatenate([self.held.get(name, np.zeros(pcm_delay)), x])
                out[name], self.held[name] = y[: len(x)], y[len(x) :]
            meta = self.extra.meta if self.extra is not None else None
            self.controls.append((element, self.payload, meta))
            # until d_ctrl frames have gone by, the first frame's data stands in
            if len(self.controls) > ctrl_delay:
                element, self.payload, self.extra_meta = self.controls.popleft()
                if self.resume is not None:
                    for name, cursor in self.resume.items():
                        if not name.startswith(EXTRA):
                            cursor.restore(self.tools.channel(name))
                    if self.extra is not None:
                        self.extra.resume = {
                            name.removeprefix(EXTRA): cursor
                            for name, cursor in self.resume.items()
                            if name.startswith(EXTRA)
                        }
                    self.resume = None
            else:
                element, self.payload, self.extra_meta = self.controls[0]
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
