"""Cover colour extraction: k-means dominant colour plus a near-black of the same hue."""

from __future__ import annotations

import colorsys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

# Reference pair from the original command: #343b3d over #040509
_TOP_VALUE_RANGE = (0.18, 0.55)
_BOTTOM_VALUE = 0.036
_BOTTOM_SAT_SCALE = 1.4


@dataclass(frozen=True, slots=True)
class Swatch:
    rgb: tuple[int, int, int]
    share: float

    @property
    def hex(self) -> str:
        return to_hex(self.rgb)


def to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _kmeans(
    pixels: np.ndarray, k: int, iterations: int = 20, seed: int = 0
) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    # k-means++ seeding keeps small accent clusters from being swallowed
    centres = [pixels[rng.integers(len(pixels))]]
    for _ in range(1, k):
        dist = np.min(((pixels[:, None, :] - np.array(centres)[None]) ** 2).sum(-1), axis=1)
        # k never exceeds the distinct colours, so some pixel is always still unclaimed
        centres.append(pixels[rng.choice(len(pixels), p=dist / dist.sum())])
    centres = np.array(centres, dtype=np.float64)
    labels = np.zeros(len(pixels), dtype=np.int64)
    for _ in range(iterations):
        labels = ((pixels[:, None, :] - centres[None]) ** 2).sum(-1).argmin(1)
        new = np.array(
            [pixels[labels == i].mean(0) if np.any(labels == i) else centres[i] for i in range(k)]
        )
        if np.allclose(new, centres, atol=0.5):
            break
        centres = new
    counts = np.bincount(labels, minlength=k)
    return centres, counts


def palette(image_path: Path, k: int = 6) -> list[Swatch]:
    """Cover colours sorted by how much of the image they cover."""
    with Image.open(image_path) as img:
        small = img.convert("RGBA")
        small.thumbnail((160, 160))
        rgba = np.asarray(small, dtype=np.float64).reshape(-1, 4)
    # Transparent areas are not part of the artwork; keep them out of the vote.
    opaque = rgba[rgba[:, 3] >= 128]
    pixels = (opaque if len(opaque) else rgba)[:, :3]
    k = max(1, min(k, len(np.unique(pixels, axis=0))))
    centres, counts = _kmeans(pixels, k)
    order = np.argsort(counts)[::-1]
    return [
        Swatch(tuple(round(c) for c in centres[i]), counts[i] / counts.sum())
        for i in order
        if counts[i]
    ]


def _hsv(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    return colorsys.rgb_to_hsv(*(c / 255 for c in rgb))


def _rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    return tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h, min(s, 1.0), min(v, 1.0)))


def dominant(swatches: list[Swatch]) -> Swatch:
    """Pick by coverage, nudged toward colours that are neither blown out nor pure black."""

    def score(sw: Swatch) -> float:
        _, s, v = _hsv(sw.rgb)
        usable = 0.35 if v < 0.08 or (v > 0.95 and s < 0.08) else 1.0
        return sw.share * usable * (0.6 + 0.4 * s)

    return max(swatches, key=score)


def gradient_pair(main: tuple[int, int, int]) -> tuple[str, str]:
    """Top = dominant clamped to a background-friendly brightness, bottom = same hue near black."""
    h, s, v = _hsv(main)
    low, high = _TOP_VALUE_RANGE
    top = _rgb(h, s, min(max(v, low), high))
    bottom = _rgb(h, s * _BOTTOM_SAT_SCALE, _BOTTOM_VALUE)
    return to_hex(top), to_hex(bottom)
