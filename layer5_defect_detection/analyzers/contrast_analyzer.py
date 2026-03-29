"""
Contrast Analyzer — WCAG AA contrast ratio checks.

Uses CSS color values already extracted by LayoutAnalyzer via getComputedStyle().
No pixel sampling needed — all data comes from the DOM analysis pass.
"""
from __future__ import annotations

import re
from typing import Optional
from uuid import uuid4

from layer5_defect_detection.models.defect_models import (
    DefectCategory,
    DefectFinding,
    DefectSeverity,
    ElementInfo,
)


# WCAG 2.1 thresholds
_WCAG_AA_NORMAL = 4.5   # Normal text (< 18pt / 14pt bold)
_WCAG_AA_LARGE = 3.0    # Large text (>= 18pt or >= 14pt bold)
_LARGE_TEXT_PT = 18.0

# Tags where contrast issues are actionable
_TEXT_TAGS = {"p", "span", "h1", "h2", "h3", "h4", "h5", "h6",
              "a", "button", "label", "li", "td", "th"}


def _relative_luminance(r: int, g: int, b: int) -> float:
    """WCAG 2.1 relative luminance formula."""
    def ch(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _contrast_ratio(
    rgb1: tuple[int, int, int], rgb2: tuple[int, int, int]
) -> float:
    l1 = _relative_luminance(*rgb1)
    l2 = _relative_luminance(*rgb2)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


_CSS_RGB_RE  = re.compile(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)")
_CSS_RGBA_RE = re.compile(r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)")


def _parse_css_color(css: str) -> Optional[tuple[int, int, int]]:
    """Parse 'rgb(r, g, b)' or 'rgba(r, g, b, a)' → (r, g, b), or None if transparent/unparseable."""
    css = (css or "").strip()
    m = _CSS_RGBA_RE.match(css)
    if m:
        alpha = float(m.group(4))
        if alpha < 0.05:
            # Fully or near-fully transparent — no usable background color
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = _CSS_RGB_RE.match(css)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


class ContrastAnalyzer:
    """Check WCAG AA contrast for elements extracted by LayoutAnalyzer."""

    def check_elements(
        self,
        elements: list[ElementInfo],
        phase: str,
    ) -> list[DefectFinding]:
        findings = []
        for el in elements:
            finding = self._check_element(el, phase)
            if finding:
                findings.append(finding)
        return findings

    def _check_element(self, el: ElementInfo, phase: str) -> Optional[DefectFinding]:
        if el.tag not in _TEXT_TAGS:
            return None

        fg = _parse_css_color(el.color)
        bg = _parse_css_color(el.background_color)
        if not fg or not bg:
            return None

        ratio = _contrast_ratio(fg, bg)
        threshold = _WCAG_AA_LARGE if el.font_size >= _LARGE_TEXT_PT else _WCAG_AA_NORMAL

        if ratio >= threshold:
            return None

        return DefectFinding(
            defect_id=str(uuid4()),
            severity=DefectSeverity.INFO,
            category=DefectCategory.CONTRAST,
            title=f"Low contrast {ratio:.2f}:1 (need {threshold:.1f}:1)",
            description=(
                f"Element '{el.text[:50] or el.selector}' has contrast ratio "
                f"{ratio:.2f}:1, below WCAG AA threshold {threshold:.1f}:1. "
                f"fg={el.color} bg={el.background_color} font={el.font_size:.0f}pt"
            ),
            element_selector=el.selector,
            element_bbox=el.bbox,
            contrast_ratio=ratio,
            snapshot_phase=phase,
            annotation_color="blue",
        )
