"""The output panels in the order they are shown and run.

Stems come first: other outputs may need stems the split has to write first.
Adding an output means adding a feature package and one entry here.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from dolbytool.features.gradient.panel import GradientPanel
from dolbytool.features.lyrics.panel import LyricsPanel
from dolbytool.features.player.panel import PlayerPanel
from dolbytool.features.soundfield.panel import SoundfieldPanel
from dolbytool.features.stems.panel import StemsPanel
from dolbytool.features.waveform.panel import WaveformPanel
from dolbytool.ui.feature import FeaturePanel

PANELS: tuple[type[FeaturePanel], ...] = (
    StemsPanel,
    GradientPanel,
    WaveformPanel,
    LyricsPanel,
    PlayerPanel,
    SoundfieldPanel,
)


def output_panels(settings: QSettings) -> list[FeaturePanel]:
    return [panel(settings) for panel in PANELS]
