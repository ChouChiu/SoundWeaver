"""Channel elements of a channel coded substream (TS 103 190-1 clauses 4.2.5, 4.2.6 and 5.3.4).

Each element reads its tracks, runs them through the stereo and multichannel
tool and hands back the channels by name, each with its spectrum and the
window lengths the IMDCT cuts it into.  A-SPX and A-CPL data are read by
`aspx` and `acpl` and attached to the element for the QMF stage.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError

from . import acpl, aspx, smp
from .asf import Layout, Noise, read_layout, read_lfe_layout, read_master, read_spectrum

SIMPLE, ASPX, ASPX_ACPL_1, ASPX_ACPL_2, ASPX_ACPL_3 = range(5)
ASF, SSF = 0, 1


@dataclass(slots=True)
class Track:
    spectrum: np.ndarray
    layout: Layout

    @property
    def windows(self) -> list[int]:
        return self.layout.window_lengths


@dataclass(slots=True)
class Companding:
    """companding_control (table 49) over the channels it names, in order."""

    channels: tuple[str, ...]
    sync: bool
    on: list[bool]
    avg: bool


@dataclass(slots=True)
class Coupling:
    """One A-CPL module: `source` (with `residual`) coupled into `outputs`, then scaled."""

    module: str
    decorrelator: int
    data: acpl.AcplData
    config: acpl.AcplConfig
    source: str
    residual: str | None
    outputs: tuple[str, str]
    scales: tuple[float, float]


@dataclass(slots=True)
class Element:
    """The decoded channels of one element, and what the QMF tools need of it."""

    channels: dict[str, Track]
    codec_mode: int = SIMPLE
    companding: Companding | None = None
    # A-SPX data per channel name, read this frame
    aspx: dict[str, aspx.AspxFrame] = field(default_factory=dict)
    # A-CPL modules to run after A-SPX, in order, then plain gains of uncoupled channels
    acpl: list[Coupling] = field(default_factory=list)
    gains: dict[str, float] = field(default_factory=dict)

    @property
    def qmf(self) -> bool:
        return self.codec_mode != SIMPLE


class Tools:
    """What the QMF tools keep from frame to frame: configs sent with I-frames, channel state."""

    def __init__(self, frame_len: int, base_48k: bool) -> None:
        self.frame_len, self.base_48k = frame_len, base_48k
        self.aspx_config: aspx.AspxConfig | None = None
        self.aspx: dict[str, aspx.AspxChannel] = {}
        self.acpl_config: acpl.AcplConfig | None = None
        self.modules: dict[str, acpl.Module] = {}

    def module(self, name: str, decorrelator: int) -> acpl.Module:
        module = self.modules.get(name)
        if module is None:
            module = self.modules[name] = acpl.Module(decorrelator)
        return module

    def channel(self, name: str) -> aspx.AspxChannel:
        ch = self.aspx.get(name)
        if ch is None:
            ch = self.aspx[name] = aspx.AspxChannel(self.frame_len)
        return ch


class Reader:
    """The bits of one substream and the frame's decoding context."""

    def __init__(
        self,
        bits: BitReader,
        frame_len: int,
        iframe: bool,
        noise: Noise,
        tools: Tools | None = None,
    ) -> None:
        self.b, self.frame_len, self.iframe, self.noise = bits, frame_len, iframe, noise
        self.tools = tools if tools is not None else Tools(frame_len, True)
        # the most recent sf_info, which a chparam_info read on its own refers to
        self.last_layout: Layout | None = None

    def aspx_config(self) -> None:
        if self.iframe:
            self.tools.aspx_config = aspx.read_config(self.b)
        elif self.tools.aspx_config is None:
            raise BitstreamError("A-SPX coded frame before the I-frame that configures it")

    def configs(self, mode: int, acpl_2ch: bool = False) -> None:
        """The aspx_config and acpl_config of an I-frame (the sticky copies otherwise)."""
        if mode == SIMPLE:
            return
        self.aspx_config()
        if mode in (ASPX_ACPL_1, ASPX_ACPL_2, ASPX_ACPL_3):
            if self.iframe:
                if mode == ASPX_ACPL_3:
                    self.tools.acpl_config = acpl.read_config_2ch(self.b)
                else:
                    partial = acpl.PARTIAL if mode == ASPX_ACPL_1 else acpl.FULL
                    self.tools.acpl_config = acpl.read_config_1ch(self.b, partial)
            elif self.tools.acpl_config is None:
                raise BitstreamError("A-CPL coded frame before the I-frame that configures it")

    def coupling(
        self, element: Element, module: str, decorrelator: int, source: str, residual: str | None,
        outputs: tuple[str, str], scales: tuple[float, float] = (1.0, 1.0),
    ) -> None:  # fmt: skip
        """acpl_data_1ch, for a module run after A-SPX."""
        data = acpl.read_data_1ch(self.b, self.tools.acpl_config)
        element.acpl.append(
            Coupling(
                module,
                decorrelator,
                data,
                self.tools.acpl_config,
                source,
                residual,
                outputs,
                scales,
            )
        )

    def companding(self, names: tuple[str, ...]) -> Companding:
        b = self.b
        sync = b.flag() if len(names) > 1 else False
        on = [b.flag() for _ in range(1 if sync else len(names))]
        avg = b.flag() if not all(on) else False
        return Companding(names, sync, on, avg)

    def aspx_1ch(self, element: Element, name: str) -> None:
        ch = self.tools.channel(name)
        element.aspx[name] = aspx.read_data_1ch(
            self.b, ch, self.tools.aspx_config, self.iframe, self.tools.base_48k
        )

    def aspx_2ch(self, element: Element, first: str, second: str) -> None:
        a, c = self.tools.channel(first), self.tools.channel(second)
        f0, f1 = aspx.read_data_2ch(
            self.b, a, c, self.tools.aspx_config, self.iframe, self.tools.base_48k
        )
        element.aspx[first], element.aspx[second] = f0, f1

    def layout(self, **kwargs: bool) -> Layout:
        self.last_layout = read_layout(self.b, self.frame_len, **kwargs)
        return self.last_layout

    def spectrum(self, layout: Layout, side: bool = False) -> Track:
        return Track(read_spectrum(self.b, layout, self.noise, side), layout)

    def frontend(self) -> None:
        if self.b.read(1) != ASF:
            raise BitstreamError("the speech spectral frontend is not supported")


def mono_data(r: Reader, lfe: bool) -> Track:
    if lfe:
        layout = read_lfe_layout(r.b, r.frame_len)
    else:
        r.frontend()
        layout = r.layout()
    return r.spectrum(layout)


def _pair(r: Reader, stereo: bool) -> list[Track]:
    """two_channel_data, or stereo_data when `stereo` (which names each track's frontend)."""
    if r.b.flag():  # b_enable_mdct_stereo_proc
        layout = r.layout()
        param = smp.read_chparam(r.b, layout)
        t0, t1 = r.spectrum(layout), r.spectrum(layout)
        x0, x1 = smp.matrix(param, layout).apply(t0.spectrum, t1.spectrum)
        return [Track(x0, layout), Track(x1, layout)]
    if stereo:
        r.frontend()
    l0 = r.layout()
    if stereo:
        r.frontend()
    l1 = r.layout()
    return [r.spectrum(l0), r.spectrum(l1)]


def two_channel_data(r: Reader) -> list[Track]:
    return _pair(r, False)


def stereo_data(r: Reader) -> list[Track]:
    return _pair(r, True)


def three_channel_data(r: Reader) -> list[Track]:
    layout = r.layout()
    selector = r.b.read(4)
    params = [smp.read_chparam(r.b, layout) for _ in range(2)]
    tracks = [r.spectrum(layout).spectrum for _ in range(3)]
    out = smp.three(selector, *(smp.matrix(p, layout) for p in params), tracks)
    return [Track(x, layout) for x in out]


def four_channel_data(r: Reader) -> list[Track]:
    layout = r.layout()
    params = [smp.read_chparam(r.b, layout) for _ in range(4)]
    tracks = [r.spectrum(layout).spectrum for _ in range(4)]
    out = smp.four([smp.matrix(p, layout) for p in params], tracks)
    return [Track(x, layout) for x in out]


def five_channel_data(r: Reader) -> list[Track]:
    layout = r.layout()
    selector = r.b.read(4)
    params = [smp.read_chparam(r.b, layout) for _ in range(5)]
    tracks = [r.spectrum(layout).spectrum for _ in range(5)]
    out = smp.five(selector, [smp.matrix(p, layout) for p in params], tracks)
    return [Track(x, layout) for x in out]


def _unsupported(what: str, mode: int) -> BitstreamError:
    return BitstreamError(f"{what} codec mode {mode} is not supported yet")


SQRT2 = 2**0.5


def single_channel_element(r: Reader) -> Element:
    mode = r.b.read(1)
    r.configs(mode)
    element = Element({}, mode)
    if mode == ASPX:
        element.companding = r.companding(("C",))
    element.channels["C"] = mono_data(r, False)
    if mode == ASPX:
        r.aspx_1ch(element, "C")
    return element


def channel_pair_element(r: Reader) -> Element:
    mode = r.b.read(2)
    r.configs(mode)
    element = Element({}, mode)
    if mode in (SIMPLE, ASPX):
        if mode == ASPX:
            element.companding = r.companding(("L", "R"))
        element.channels.update(zip("LR", stereo_data(r), strict=True))
        if mode == ASPX:
            r.aspx_2ch(element, "L", "R")
        return element
    element.companding = r.companding(("L",))
    if mode == ASPX_ACPL_1:
        if r.b.flag():  # b_enable_mdct_stereo_proc
            layout = r.layout(dual_maxsfb=True)
            param = smp.read_chparam(r.b, layout)
            mid, side = r.spectrum(layout), r.spectrum(layout, side=True)
            x0, x1 = smp.matrix(param, layout).apply(mid.spectrum, side.spectrum)
            element.channels["L"], element.channels["R"] = Track(x0, layout), Track(x1, layout)
        else:
            r.frontend()
            l0 = r.layout()
            r.frontend()
            l1 = r.layout(side_limited=True)
            element.channels["L"], element.channels["R"] = r.spectrum(l0), r.spectrum(l1)
        r.aspx_1ch(element, "L")
        r.coupling(element, "pair", 0, "L", "R", ("L", "R"))
    else:
        r.frontend()
        layout = r.layout()
        element.channels["L"] = r.spectrum(layout)
        element.channels["R"] = Track(np.zeros(r.frame_len), layout)
        r.aspx_1ch(element, "L")
        r.coupling(element, "pair", 0, "L", None, ("L", "R"))
    return element


def three_zero_element(r: Reader) -> Element:
    mode = r.b.read(1)
    if mode == ASPX:
        r.aspx_config()
    element = Element({}, mode)
    if mode == ASPX:
        element.companding = r.companding(("L", "R", "C"))
    if r.b.read(1):  # 3_0_coding_config
        element.channels.update(zip("LRC", three_channel_data(r), strict=True))
    else:
        element.channels.update(zip("LR", stereo_data(r), strict=True))
        element.channels["C"] = mono_data(r, False)
    if mode == ASPX:
        r.aspx_2ch(element, "L", "R")
        r.aspx_1ch(element, "C")
    return element


def _residuals(
    r: Reader, element: Element, pairs: tuple[tuple[str, str], ...], last: Layout
) -> None:
    """max_sfb_master, two chparam_info and two sf_data: residual tracks matrixed with their pair."""
    layout = read_master(r.b, last)
    params = [smp.read_chparam(r.b, layout) for _ in range(2)]
    tracks = [r.spectrum(layout) for _ in range(2)]
    for (front, back), param, track in zip(pairs, params, tracks, strict=True):
        x0, x1 = smp.matrix(param, layout).apply(element.channels[front].spectrum, track.spectrum)
        element.channels[front] = Track(x0, element.channels[front].layout)
        element.channels[back] = Track(x1, layout)


def _core_five(r: Reader, element: Element, names: dict[str, str]) -> None:
    """The coding_config switch of 5.X and 7.X SIMPLE/ASPX: tables 180 and 182."""
    config = r.b.read(2)
    a, b, c, d, e = (names[k] for k in "ABCDE")
    out = element.channels
    if config == 0:
        swap = r.b.read(1)  # 2ch_mode
        first, second = two_channel_data(r), two_channel_data(r)
        pairs = ((a, d), (b, e)) if swap else ((a, b), (d, e))
        for pair, tracks in zip(pairs, (first, second), strict=True):
            out.update(zip(pair, tracks, strict=True))
    elif config == 1:
        out.update(zip((a, b, c), three_channel_data(r), strict=True))
        out.update(zip((d, e), two_channel_data(r), strict=True))
    elif config == 2:
        out.update(zip((a, b, d, e), four_channel_data(r), strict=True))
    else:
        out.update(zip((a, b, c, d, e), five_channel_data(r), strict=True))
    return config


def five_x_element(r: Reader, lfe: bool) -> Element:
    mode = r.b.read(3)
    if mode >= ASPX_ACPL_3:
        raise _unsupported("5.X", mode)
    r.configs(mode)
    element = Element({}, mode)
    if lfe:
        element.channels["LFE"] = mono_data(r, True)
    if mode in (SIMPLE, ASPX):
        if mode == ASPX:
            element.companding = r.companding(("L", "R", "C", "Ls", "Rs"))
        names = dict(zip("ABCDE", ("L", "R", "C", "Ls", "Rs")))
        config = _core_five(r, element, names)
        if config in (0, 2):
            element.channels["C"] = mono_data(r, False)
        if mode == ASPX:
            r.aspx_2ch(element, "L", "R")
            r.aspx_2ch(element, "Ls", "Rs")
            r.aspx_1ch(element, "C")
        return element
    # ASPX_ACPL_1 and _2: L and R (and C) coded, the surrounds coupled (table 181)
    element.companding = r.companding(("L", "R", "C"))
    if r.b.read(1):
        element.channels.update(zip("LRC", three_channel_data(r), strict=True))
        has_centre = True
    else:
        element.channels.update(zip("LR", two_channel_data(r), strict=True))
        has_centre = False
    if mode == ASPX_ACPL_1:
        _residuals(r, element, (("L", "Ls"), ("R", "Rs")), r.last_layout)
    if not has_centre:
        element.channels["C"] = mono_data(r, False)
    r.aspx_2ch(element, "L", "R")
    r.aspx_1ch(element, "C")
    residual = mode == ASPX_ACPL_1
    r.coupling(element, "left", 0, "L", "Ls" if residual else None, ("L", "Ls"), (1.0, SQRT2))
    r.coupling(element, "right", 1, "R", "Rs" if residual else None, ("R", "Rs"), (1.0, SQRT2))
    return element


# Table 183: the channels the preliminary F and G pair with D and E become, per channel_mode
_SEVEN_X = {
    5: ("back", ("Ls", "Lb", "Rs", "Rb")), 6: ("back", ("Ls", "Lb", "Rs", "Rb")),
    7: ("wide", ("L", "Lw", "R", "Rw")), 8: ("wide", ("L", "Lw", "R", "Rw")),
    9: ("top", ("L", "Tfl", "R", "Tfr")), 10: ("top", ("L", "Tfl", "R", "Tfr")),
}  # fmt: skip
# Table 202: A-CPL inputs x0, x1, x3, x4, x6, x7 per channel_mode group and add_ch_base
_SEVEN_X_ACPL = {
    ("back", 0): ("Ls", "Rs", "Lb", "Rb", "L", "R"), ("back", 1): ("Ls", "Rs", "Lb", "Rb", "L", "R"),
    ("wide", 0): ("L", "R", "Lw", "Rw", "Ls", "Rs"), ("wide", 1): ("Ls", "Rs", "Lw", "Rw", "L", "R"),
    ("top", 0): ("L", "R", "Tfl", "Tfr", "Ls", "Rs"), ("top", 1): ("Ls", "Rs", "Tfl", "Tfr", "L", "R"),
}  # fmt: skip


def seven_x_element(r: Reader, channel_mode: int, add_ch_base: int = 0) -> Element:
    """7_X_channel_element (table 33), tables 182 to 185 and 202.

    Which channels each aspx_data block of the element extends is not spelt out;
    they are taken in the order the core carries them: front pair, surround
    pair, centre, then the additional pair.
    """
    mode = r.b.read(2)
    r.configs(mode)
    element = Element({}, mode)
    kind, _ = _SEVEN_X[channel_mode]
    extra = {"back": ("Lb", "Rb"), "wide": ("Lw", "Rw"), "top": ("Tfl", "Tfr")}[kind]
    if channel_mode in (6, 8, 10):
        element.channels["LFE"] = mono_data(r, True)
    acpl_mode = mode in (ASPX_ACPL_1, ASPX_ACPL_2)
    if acpl_mode:
        x0, x1, x3, x4, x6, x7 = _SEVEN_X_ACPL[(kind, add_ch_base)]
        element.companding = r.companding((x0, x1, "C", x6, x7))
    config = _core_five(r, element, {"A": "L", "B": "R", "C": "C", "D": "Ls", "E": "Rs"})
    if mode in (SIMPLE, ASPX):
        params = reference = None
        if r.b.flag():  # b_use_sap_add_ch: its chparam_info refers to the sf_info read last
            reference = r.last_layout
            params = [smp.read_chparam(r.b, reference) for _ in range(2)]
        f, g = two_channel_data(r)
        frame_len = f.spectrum.shape[0]
        if params is None:
            m0 = m1 = smp.Matrix.identity(frame_len)
        else:
            m0, m1 = (smp.matrix(p, reference) for p in params)
        # table 183: 3/4/0 pairs the surrounds with F and G, 5/2/0 and 3/2/2 the fronts
        left, right = ("Ls", "Rs") if kind == "back" else ("L", "R")
        out = element.channels
        a0, a1 = m0.apply(out[left].spectrum, f.spectrum)
        b0, b1 = m1.apply(out[right].spectrum, g.spectrum)
        out[left], out[extra[0]] = Track(a0, out[left].layout), Track(a1, f.layout)
        out[right], out[extra[1]] = Track(b0, out[right].layout), Track(b1, g.layout)
    elif mode == ASPX_ACPL_1:
        _residuals(r, element, ((x0, x3), (x1, x4)), r.last_layout)
    if config in (0, 2):
        element.channels["C"] = mono_data(r, False)
    if mode != SIMPLE:
        r.aspx_2ch(element, "L", "R")
        r.aspx_2ch(element, "Ls", "Rs")
        r.aspx_1ch(element, "C")
    if mode == ASPX:
        r.aspx_2ch(element, *extra)
    if acpl_mode:
        residual = mode == ASPX_ACPL_1
        front = SQRT2 if add_ch_base == 1 or kind == "back" else 1.0
        r.coupling(element, "left", 0, x0, x3 if residual else None, (x0, x3), (front, SQRT2))
        r.coupling(element, "right", 1, x1, x4 if residual else None, (x1, x4), (front, SQRT2))
        if add_ch_base == 0:
            element.gains = {x6: SQRT2, x7: SQRT2}
    return element


def audio_data(r: Reader, channel_mode: int, add_ch_base: int = 0) -> Element:
    if channel_mode == 0:
        return single_channel_element(r)
    if channel_mode == 1:
        return channel_pair_element(r)
    if channel_mode == 2:
        return three_zero_element(r)
    if channel_mode in (3, 4):
        return five_x_element(r, channel_mode == 4)
    if channel_mode in _SEVEN_X:
        return seven_x_element(r, channel_mode, add_ch_base)
    raise BitstreamError(f"channel_mode {channel_mode} is not supported yet")
