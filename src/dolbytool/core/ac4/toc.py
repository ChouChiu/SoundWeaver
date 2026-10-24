"""AC-4 sync frames and the table of contents (TS 103 190-1 clause 4.2.3, TS 103 190-2 clause 6.2.1).

Only what decoding needs is kept: the frame layout, the presentations with
their substream groups, and where each substream's bytes lie.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass, field

from dolbytool.core.atmos.bitstream import BitReader, BitstreamError

logger = logging.getLogger(__name__)

SYNC = 0xAC40
SYNC_CRC = 0xAC41

# Table 83: frame_len_base at 48 kHz per frame_rate_index
FRAME_LENGTHS = (1920, 1920, 2048, 1536, 1536, 960, 960, 1024, 768, 768, 512, 384, 384, 2048)

# obj_type
BED, DYN, ISF = 0, 1, 2
_ISF_COUNTS = (4, 8, 10, 14, 15, 30, 0, 0)
# The speakers of A-JOC coded beds: bed_chan_assign_code (table 63), the channel
# assignment flags in channel order (tables 64 and 65) and nonstd_bed_channel_assignment
# (table 66, 3 reserved); an A-JOC bed has no LFE, which is a b_lfe object instead
_AJOC_BEDS = (
    ("L", "R"), ("L", "R", "C"), ("L", "R", "C", "Ls", "Rs"),
    ("L", "R", "C", "Ls", "Rs", "Tsl", "Tsr"), ("L", "R", "C", "Ls", "Rs", "Tfl", "Tfr", "Tbl", "Tbr"),
    ("L", "R", "C", "Ls", "Rs", "Lb", "Rb"), ("L", "R", "C", "Ls", "Rs", "Lb", "Rb", "Tsl", "Tsr"),
    ("L", "R", "C", "Ls", "Rs", "Lb", "Rb", "Tfl", "Tfr", "Tbl", "Tbr"),
)  # fmt: skip
_NONSTD_LABELS = (
    "L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb",
    "Tfl", "Tfr", "Tsl", "Tsr", "Tbl", "Tbr", "Lw", "Rw", "LFE2",
)  # fmt: skip
_STD_GROUPS = (
    ("L", "R"), ("C",), ("LFE",), ("Ls", "Rs"), ("Lb", "Rb"),
    ("Tfl", "Tfr"), ("Tsl", "Tsr"), ("Tbl", "Tbr"), ("Lw", "Rw"), ("LFE2",),
)  # fmt: skip


@dataclass(slots=True)
class SubstreamInfo:
    """What ac4_substream_info_* says about one substream."""

    kind: str  # "chan", "ajoc", "obj"
    index: int = 0
    channel_mode: int = 0
    b_lfe: bool = False
    # A-JOC
    static_dmx: bool = True
    dmx_signals: int = 0
    upmix_signals: int = 0
    dmx_objects: list[tuple[int, bool]] = field(default_factory=list)
    upmix_objects: list[tuple[int, bool]] = field(default_factory=list)
    # the speaker of each bed object, in order; isf_config of ISF ones
    dmx_beds: tuple[str, ...] = ()
    upmix_beds: tuple[str, ...] = ()
    isf_config: int | None = None
    oamd_common: object | None = None  # the Trim of its oamd_common_data
    # ac4_substream_info_obj
    objects: list[tuple[int, bool]] = field(default_factory=list)
    iframe: bool = False
    add_ch_base: int = 0


@dataclass(slots=True)
class SubstreamGroup:
    substreams_present: bool = False
    channel_coded: bool = False
    oamd_index: int | None = None
    substreams: list[SubstreamInfo] = field(default_factory=list)


@dataclass(slots=True)
class Presentation:
    version: int = 0
    config: int = -1  # -1: single substream group
    groups: list[int] = field(default_factory=list)  # substream group indices
    presentation_substream: int | None = None
    enabled: bool = True
    alternative: bool = False


@dataclass(slots=True)
class Toc:
    bitstream_version: int
    sequence_counter: int
    fs_index: int
    frame_rate_index: int
    iframe_global: bool
    presentations: list[Presentation]
    groups: list[SubstreamGroup]
    substream_sizes: list[int]
    payload_base: int
    # byte offset of the first substream in the raw frame
    data_start: int = 0

    @property
    def frame_length(self) -> int:
        return FRAME_LENGTHS[self.frame_rate_index]


def oamd_common_data(b: BitReader):
    """Clause 6.2.8.1: the trim it carries, if any."""
    from .oamd import read_common  # oamd builds on this module's object types

    return read_common(b)


def read_toc(frame: bytes) -> Toc:
    return _TocReader(BitReader(frame)).read()


def sync_frames(stream: bytes) -> Iterator[bytes]:
    """The raw_ac4_frame of every ac4_syncframe (TS 103 190-1 Annex G.3) in `stream`."""
    pos, size = 0, len(stream)
    while pos + 4 <= size:
        sync = stream[pos] << 8 | stream[pos + 1]
        if sync not in (SYNC, SYNC_CRC):
            found = stream.find(b"\xac\x40", pos + 1)
            other = stream.find(b"\xac\x41", pos + 1)
            found = min(f for f in (found, other, size) if f != -1)
            if found >= size or not any(stream[pos:found]):
                if any(stream[pos:]):
                    logger.warning("AC-4: %d bytes after byte %d hold no frame", size - pos, pos)
                break
            logger.warning("AC-4: sync lost at byte %d, found again at %d", pos, found)
            pos = found
            continue
        length = stream[pos + 2] << 8 | stream[pos + 3]
        head = 4
        if length == 0xFFFF:
            length = int.from_bytes(stream[pos + 4 : pos + 7], "big")
            head = 7
        end = pos + head + length
        if end > size:
            break
        yield stream[pos + head : end]
        pos = end + (2 if sync == SYNC_CRC else 0)


class _TocReader:
    def __init__(self, bits: BitReader) -> None:
        self.bits = bits
        self.version = 0
        self.fs_index = 1
        self.frame_rate_index = 0
        self.groups_wanted = 0
        self.factor = 1

    def read(self) -> Toc:
        b = self.bits
        version = b.read(2)
        if version == 3:
            version += b.variable_bits(2)
        self.version = version
        sequence_counter = b.read(10)
        if b.flag() and b.read(3) > 0:  # b_wait_frames, wait_frames
            b.read(2)  # br_code
        self.fs_index = b.read(1)
        self.frame_rate_index = b.read(4)
        if self.frame_rate_index >= len(FRAME_LENGTHS):
            raise BitstreamError(f"reserved frame_rate_index {self.frame_rate_index}")
        iframe_global = b.flag()
        if b.flag():  # b_single_presentation
            n_presentations = 1
        elif b.flag():  # b_more_presentations
            n_presentations = b.variable_bits(2) + 2
        else:
            n_presentations = 0
        payload_base = 0
        if b.flag():
            payload_base = b.read(5) + 1
            if payload_base == 0x20:
                payload_base += b.variable_bits(3)
        if version <= 1:
            raise BitstreamError(
                f"bitstream_version {version} (TS 103 190-1 only) is not supported"
            )
        if b.flag():  # b_program_id
            b.read(16)
            if b.flag():
                b.skip(128)
        presentations = [self._presentation_v1() for _ in range(n_presentations)]
        groups = [self._substream_group() for _ in range(self.groups_wanted)]
        sizes = self._substream_index_table()
        # byte_align
        b.pos = (b.pos + 7) & ~7
        return Toc(
            version,
            sequence_counter,
            self.fs_index,
            self.frame_rate_index,
            iframe_global,
            presentations,
            groups,
            sizes,
            payload_base,
            b.pos // 8,
        )

    def _frame_rate_factor(self) -> int:
        b, index = self.bits, self.frame_rate_index
        if index in (2, 3, 4):
            if b.flag():
                return 4 if b.flag() else 2
            return 1
        if index in (0, 1, 7, 8, 9):
            return 2 if b.flag() else 1
        return 1

    def _emdf_info(self) -> None:
        b = self.bits
        if b.read(2) == 3:
            b.variable_bits(2)
        if b.read(3) == 7:
            b.variable_bits(3)
        if b.flag():  # b_emdf_payloads_substream_info
            self._substream_index()
        self._emdf_reserved()

    def _emdf_reserved(self) -> None:
        b = self.bits
        primary, secondary = b.read(2), b.read(2)
        skip = 0
        if primary:
            skip += 1 << (2 * (primary - 1))
        if secondary:
            skip += 1 << (2 * (secondary - 1))
        b.skip(8 * skip)

    def _substream_index(self) -> int:
        b = self.bits
        index = b.read(2)
        if index == 3:
            index += b.variable_bits(2)
        return index

    def _presentation_v1(self) -> Presentation:
        b = self.bits
        p = Presentation()
        single = b.flag()
        config = -1
        if not single:
            config = b.read(3)
            if config == 7:
                config += b.variable_bits(2)
        p.config = config
        if self.version != 1:
            while b.flag():
                p.version += 1
        if not single and config == 6:
            add_emdf = True
        else:
            if self.version != 1:
                b.read(3)  # md_compat
            if b.flag():
                b.variable_bits(2)  # presentation_id
            factor = self.factor = self._frame_rate_factor()
            self._frame_rate_fractions(factor)
            self._emdf_info()
            if b.flag():  # b_presentation_filter
                p.enabled = b.flag()
            if single:
                p.groups.append(self._sgi_specifier())
            else:
                b.flag()  # b_multi_pid
                count = {0: 2, 1: 2, 2: 2, 3: 3, 4: 3}.get(config)
                if config == 5:
                    count = b.read(2) + 2
                    if count == 5:
                        count += b.variable_bits(2)
                if count is None:
                    self._config_ext_info()
                else:
                    p.groups.extend(self._sgi_specifier() for _ in range(count))
            b.flag()  # b_pre_virtualized
            add_emdf = b.flag()
            p.alternative = b.flag()
            b.flag()  # b_pres_ndot
            p.presentation_substream = self._substream_index()
        if add_emdf:
            n = b.read(2)
            if n == 0:
                n = b.variable_bits(2) + 4
            for _ in range(n):
                self._emdf_info()
        return p

    def _frame_rate_fractions(self, factor: int) -> None:
        b, index = self.bits, self.frame_rate_index
        if index in (5, 6, 7, 8, 9) and factor == 1:
            b.flag()
        if index in (10, 11, 12) and b.flag():
            b.flag()

    def _config_ext_info(self) -> None:
        b = self.bits
        skip = b.read(5)
        if b.flag():
            skip += b.variable_bits(2) << 5
        b.skip(8 * skip)

    def _sgi_specifier(self) -> int:
        b = self.bits
        index = b.read(3)
        if index == 7:
            index += b.variable_bits(2)
        self.groups_wanted = max(self.groups_wanted, index + 1)
        return index

    def _substream_group(self) -> SubstreamGroup:
        b = self.bits
        g = SubstreamGroup()
        g.substreams_present = b.flag()
        hsf = b.flag()
        if b.flag():
            n = 1
        else:
            n = b.read(2) + 2
            if n == 5:
                n += b.variable_bits(2)
        g.channel_coded = b.flag()
        if g.channel_coded:
            for _ in range(n):
                if self.version == 1:
                    b.flag()  # sus_ver
                g.substreams.append(self._info_chan(g.substreams_present))
                if hsf:
                    self._hsf_info(g.substreams_present)
        else:
            if b.flag():  # b_oamd_substream
                b.flag()  # b_oamd_ndot
                if g.substreams_present:
                    g.oamd_index = self._substream_index()
            for _ in range(n):
                if b.flag():
                    g.substreams.append(self._info_ajoc(g.substreams_present))
                else:
                    g.substreams.append(self._info_obj(g.substreams_present))
                if hsf:
                    self._hsf_info(g.substreams_present)
        if b.flag():  # b_content_type
            self._content_type()
        return g

    def _hsf_info(self, present: bool) -> None:
        if present:
            self._substream_index()

    def _content_type(self) -> None:
        b = self.bits
        b.read(3)
        if b.flag():  # b_language_indicator
            if b.flag():  # b_serialized_language_tag
                b.read(1)
                b.read(16)
            else:
                b.skip(8 * b.read(6))

    def _channel_mode(self) -> int:
        """Table 88 prefix code: 0, 10, 1100..1110, 1111000..1111100, ..."""
        b = self.bits
        if not b.flag():
            return 0
        if not b.flag():
            return 1
        code = 0b11 << 2 | b.read(2)
        if code < 0b1111:
            return code - 0b1100 + 2  # 2..4
        code = code << 3 | b.read(3)
        if code < 0b1111101:
            return code - 0b1111000 + 5  # 5..9 (the 0b111110x codes)
        if code == 0b1111101:
            return 10
        code = code << 1 | b.read(1)
        if code == 0b11111100:
            return 11
        if code == 0b11111101:
            return 12
        code = code << 1 | b.read(1)
        if code == 0b111111100:
            return 13
        if code == 0b111111101:
            return 14
        if code == 0b111111110:
            return 15
        return 16 + b.variable_bits(2)

    def _info_tail(self, info: SubstreamInfo, present: bool, factor: int = 1) -> None:
        b = self.bits
        if self.fs_index == 1 and b.flag():
            b.flag()  # sf_multiplier
        if b.flag():  # b_bitrate_info
            self._bitrate_indicator()
        ndot = [b.flag() for _ in range(factor)]  # b_audio_ndot
        info.iframe = ndot[0]
        if present:
            info.index = self._substream_index()

    def _info_chan(self, present: bool) -> SubstreamInfo:
        b = self.bits
        info = SubstreamInfo("chan")
        mode = self._channel_mode()
        info.channel_mode = mode
        if mode in (11, 12, 13, 14):
            b.read(4)  # b_4_back_channels_present, b_centre_present, top_channels_present
        if self.fs_index == 1 and b.flag():
            b.flag()
        if b.flag():
            self._bitrate_indicator()
        if mode in (7, 8, 9, 10):
            info.add_ch_base = b.read(1)
        ndot = [b.flag() for _ in range(self.factor)]  # b_audio_ndot
        info.iframe = ndot[0]
        if present:
            info.index = self._substream_index()
        return info

    def _info_ajoc(self, present: bool) -> SubstreamInfo:
        b = self.bits
        info = SubstreamInfo("ajoc")
        info.b_lfe = b.flag()
        info.static_dmx = b.flag()
        if info.static_dmx:
            info.dmx_signals = 5
        else:
            info.dmx_signals = b.read(4) + 1
            info.dmx_objects, info.dmx_beds, _ = self._bed_dyn_obj_assignment(info.dmx_signals)
        if b.flag():  # b_oamd_common_data_present
            info.oamd_common = oamd_common_data(b)
        n = b.read(4) + 1
        if n == 16:
            n += b.variable_bits(3)
        info.upmix_signals = n
        info.upmix_objects, info.upmix_beds, info.isf_config = self._bed_dyn_obj_assignment(n)
        self._info_tail(info, present, self.factor)
        return info

    def _info_obj(self, present: bool) -> SubstreamInfo:
        b = self.bits
        info = SubstreamInfo("obj")
        objs: list[tuple[int, bool]] = []
        count = (0, 1, 2, 3, 5, 7, 0, 0)[b.read(3)]
        if b.flag():  # b_dynamic_objects
            lfe = b.flag()
            objs = [(BED, True) if lfe and i == 0 else (DYN, False) for i in range(count)]
        elif b.flag():  # b_bed_objects
            if b.flag():  # b_bed_start
                if b.flag():  # b_ch_assign_code
                    n = (2, 3, 6, 8, 10, 8, 10, 12)[b.read(3)]
                    objs = [(BED, i == 3) for i in range(n)]
                elif b.flag():  # b_nonstd_bed_channel_assignment_flags_present
                    flags = b.read(17)
                    objs = [(BED, i in (3, 16)) for i in range(17) if flags >> (16 - i) & 1]
                else:
                    flags = b.read(10)
                    for i, width in enumerate((2, 1, 1, 2, 2, 2, 2, 2, 2, 1)):
                        if flags >> (9 - i) & 1:
                            objs += [(BED, i in (2, 9))] * width
        elif b.flag():  # b_isf
            if b.flag():  # b_isf_start
                objs = [(ISF, False)] * _ISF_COUNTS[b.read(3)]
        else:
            b.skip(8 * b.read(4))
        info.objects = objs
        self._info_tail(info, present, self.factor)
        return info

    def _bed_dyn_obj_assignment(
        self, n_signals: int
    ) -> tuple[list[tuple[int, bool]], tuple[str, ...], int | None]:
        """(obj_type, b_lfe) of the signals, the beds' speakers and the isf_config.

        Any signal not named as a bed or ISF object is dynamic.
        """
        b = self.bits
        beds: list[str] = []
        isf = None
        if not b.flag():  # b_dyn_objects_only
            if b.flag():  # b_isf
                isf = b.read(3)
            elif b.flag():  # b_ch_assign_code
                beds = list(_AJOC_BEDS[b.read(3)])
            elif b.flag():  # b_channel_assignment_flags_present
                if b.flag():
                    flags = b.read(17)
                    beds = [
                        label for i, label in enumerate(_NONSTD_LABELS)
                        if flags >> (16 - i) & 1 and i not in (3, 16)
                    ]  # fmt: skip
                else:
                    flags = b.read(10)
                    for i, group in enumerate(_STD_GROUPS):
                        if flags >> (9 - i) & 1 and i not in (2, 9):
                            beds += group
            else:
                n_bed = b.read((n_signals - 1).bit_length()) + 1 if n_signals > 1 else 1
                for _ in range(n_bed):
                    code = b.read(4)
                    if code != 3:
                        beds.append(_NONSTD_LABELS[code])
        objs = [(ISF, False)] * _ISF_COUNTS[isf] if isf is not None else [(BED, False)] * len(beds)
        objs += [(DYN, False)] * (n_signals - len(objs))
        return objs, tuple(beds), isf

    def _substream_index_table(self) -> list[int]:
        b = self.bits
        n = b.read(2)
        if n == 0:
            n = b.variable_bits(2) + 4
        present = b.flag() if n == 1 else True
        sizes = []
        if present:
            for _ in range(n):
                more = b.flag()
                size = b.read(10)
                if more:
                    size += b.variable_bits(2) << 10
                sizes.append(size)
        return sizes

    def _bitrate_indicator(self) -> None:
        b = self.bits
        if b.read(3) & 1:  # 3/5 bits: a set low bit asks for two more
            b.read(2)
