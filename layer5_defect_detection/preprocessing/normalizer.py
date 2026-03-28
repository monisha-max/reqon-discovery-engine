"""
Normalizer — ensures screenshots are at the expected viewport resolution.

Since Playwright captures at the configured viewport (1920×1080 by default),
this is mostly a safety measure. If a screenshot is at a different size
(e.g., mobile emulation), it's scaled to the standard width so that
Pillow annotation coordinates (from DOM bounding boxes) remain accurate.
"""
from __future__ import annotations

from PIL import Image


TARGET_WIDTH = 1920


class Normalizer:
    def normalize(self, img: Image.Image) -> tuple[Image.Image, float]:
        """
        Scale image to TARGET_WIDTH if needed.

        Returns (normalized_image, scale_factor).
        scale_factor=1.0 means no change was needed.
        """
        w, h = img.size
        if w == TARGET_WIDTH:
            return img, 1.0

        scale = TARGET_WIDTH / w
        new_h = int(h * scale)
        resized = img.resize((TARGET_WIDTH, new_h), Image.LANCZOS)
        return resized, scale
