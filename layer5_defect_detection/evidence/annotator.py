"""
Annotator — draws colored bounding boxes on screenshots using Pillow.

This is the only file in Layer 5 that uses Pillow for image drawing.
All geometry data comes from DOM analysis (not pixel inspection).
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

from layer5_defect_detection.models.defect_models import DefectFinding

# RGBA colors per annotation_color name
_COLOR_RGBA: dict[str, tuple[int, int, int, int]] = {
    "red":    (220, 38,  38,  200),   # Critical
    "orange": (234, 88,  12,  180),   # High
    "yellow": (202, 138,  4,  160),   # Medium
    "green":  (22,  163, 74,  140),   # Low
    "blue":   (37,   99, 235, 140),   # Info
}

_FILL_ALPHA = 50    # semi-transparent fill
_BORDER_WIDTH = 3
_LABEL_PADDING = 4


class Annotator:
    """Annotates a screenshot PNG with colored bounding boxes for each finding."""

    def annotate(
        self,
        screenshot_path: str,
        findings: list[DefectFinding],
        output_path: str,
    ) -> str:
        """
        Draw findings onto screenshot and save to output_path.

        Returns output_path.
        """
        img = Image.open(screenshot_path).convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)

        font = _load_font(size=11)

        for finding in findings:
            bbox = finding.element_bbox
            rgba = _COLOR_RGBA.get(finding.annotation_color, _COLOR_RGBA["red"])
            r, g, b, _ = rgba

            x0, y0 = int(bbox.x), int(bbox.y)
            x1, y1 = int(bbox.x + bbox.width), int(bbox.y + bbox.height)

            # Clamp to image bounds
            iw, ih = img.size
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(iw, x1), min(ih, y1)
            if x1 <= x0 or y1 <= y0:
                continue

            # Semi-transparent fill
            draw.rectangle([x0, y0, x1, y1], fill=(r, g, b, _FILL_ALPHA))
            # Solid border
            draw.rectangle([x0, y0, x1, y1], outline=(r, g, b, 220), width=_BORDER_WIDTH)

            # Label: "SEVERITY: category"
            label = f"{finding.severity.value.upper()}: {finding.category.value.replace('_', ' ')}"
            lx = x0 + _LABEL_PADDING
            ly = max(0, y0 - 16) if y0 > 16 else y0 + _LABEL_PADDING

            # Label background
            draw.rectangle(
                [lx - 1, ly - 1, lx + len(label) * 6 + 2, ly + 13],
                fill=(r, g, b, 200),
            )
            draw.text((lx, ly), label, fill=(255, 255, 255, 255), font=font)

        composite = Image.alpha_composite(img, overlay).convert("RGB")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        composite.save(output_path, "PNG")
        return output_path


def _load_font(size: int = 11) -> ImageFont.ImageFont:
    """Try to load a system font; fall back to Pillow's built-in."""
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ]:
        try:
            return ImageFont.truetype(font_path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()
