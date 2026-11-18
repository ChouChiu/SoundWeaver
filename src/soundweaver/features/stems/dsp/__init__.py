"""Signal processing for the split: crossovers, channel matrices and spectral centre separation."""

from .crossover import difference, lowpass
from .matrix import (
    copy_channels_into,
    downmix_instrumental_into,
    downmix_into,
    fit_group,
    height_into,
    lfe_lag,
    mid_into,
    side_into,
)
from .panning import (
    cancel_centre_from_front,
    separate_centre,
)

__all__ = [
    "cancel_centre_from_front",
    "copy_channels_into",
    "difference",
    "downmix_instrumental_into",
    "downmix_into",
    "fit_group",
    "height_into",
    "lfe_lag",
    "lowpass",
    "mid_into",
    "separate_centre",
    "side_into",
]
