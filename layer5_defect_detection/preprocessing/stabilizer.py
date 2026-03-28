"""
Stabilizer — minimal noise reduction for Playwright PNG screenshots.

Playwright screenshots are lossless PNG, so this step is lightweight.
Primary purpose: convert RGBA → RGB and apply a very mild median filter
to suppress any sub-pixel rendering artifacts before Pillow annotation.
"""
from __future__ import annotations

from PIL import Image


class Stabilizer:
    def stabilize(self, image_path: str) -> Image.Image:
        """Load and normalize a screenshot for downstream processing."""
        img = Image.open(image_path)
        # Playwright may produce RGBA (transparency layer); convert to RGB
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img
