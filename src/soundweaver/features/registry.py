"""The output panels in the order they are shown and run.

Stems come first: other outputs may need stems the split has to write first.
Adding an output means adding a feature package and one entry here.
"""

from __future__ import annotations

from PySide6.QtCore import QSettings

from soundweaver.features.gradient.panel import GradientPanel
from soundweaver.features.lyrics.panel import LyricsPanel
from soundweaver.features.player.panel import PlayerPanel
from soundweaver.features.soundfield.panel import SoundfieldPanel
from soundweaver.features.stems.panel import StemsPanel
from soundweaver.features.waveform.panel import WaveformPanel
from soundweaver.ui.feature import FeaturePanel

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
