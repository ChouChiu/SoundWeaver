"""Speaker geometry and meter indices consumed by both monitor canvases."""

from __future__ import annotations

from dataclasses import dataclass

from soundweaver.core.codecs.mpegh import CicpLayout
from soundweaver.core.speakers import SpeakerLayout


@dataclass(frozen=True, slots=True)
class MonitorSpeaker:
    name: str
    channel: int
    position: tuple[float, float, float]
    layer: str = "middle"


def room_speakers(layout: SpeakerLayout) -> list[MonitorSpeaker]:
    return [MonitorSpeaker(name, layout.index[name], pos) for name, pos in layout.positions.items()]


def spherical_speakers(layout: CicpLayout) -> list[MonitorSpeaker]:
    return [
        MonitorSpeaker(speaker.name, channel, speaker.cartesian(), speaker.layer)
        for channel, speaker in enumerate(layout.speakers)
        if not speaker.is_lfe
    ]
