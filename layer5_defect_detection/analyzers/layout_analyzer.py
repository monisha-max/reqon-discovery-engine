"""
Layout Geometry Analyzer — DOM-based bounding box extraction and defect detection.

All analysis is done via page.evaluate() — no pixel processing, no OpenCV.
Detects: overlaps, text truncation, viewport overflow, layout drift vs baseline.
"""
from __future__ import annotations

from typing import Optional
from uuid import uuid4

import structlog

from layer5_defect_detection.models.defect_models import (
    BoundingBox,
    DefectCategory,
    DefectFinding,
    DefectSeverity,
    ElementInfo,
)
from layer5_defect_detection.preprocessing.region_masker import is_in_masked_region

logger = structlog.get_logger()

# JavaScript injected via page.evaluate() to extract all interactive elements
_LAYOUT_EXTRACTION_JS = """
() => {
    const CTA_PATTERNS = [
        /\\bsubmit\\b/i, /\\bbuy\\b/i, /\\bcheckout\\b/i,
        /\\bsign[\\s-]?up\\b/i, /\\blogin\\b/i, /\\blog[\\s-]?in\\b/i,
        /\\badd\\s+to\\s+cart\\b/i, /\\bget\\s+started\\b/i,
        /\\bpurchase\\b/i, /\\bregister\\b/i, /\\bbook\\s+now\\b/i,
        /\\bdownload\\b/i, /\\bsubscribe\\b/i, /\\bpay\\b/i,
        /\\bplace\\s+order\\b/i,
    ];

    const isCTA = (el) => {
        const text = (el.textContent || '').trim();
        const aria = el.getAttribute('aria-label') || '';
        const val  = el.getAttribute('value') || '';
        const combined = text + ' ' + aria + ' ' + val;
        return CTA_PATTERNS.some(p => p.test(combined));
    };

    const getSelector = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const parts = [];
        let cur = el;
        while (cur && cur !== document.body && parts.length < 5) {
            let seg = cur.tagName.toLowerCase();
            if (cur.id) { seg = '#' + CSS.escape(cur.id); parts.unshift(seg); break; }
            if (cur.className) {
                const cls = cur.className.toString().trim().split(/\\s+/)[0];
                if (cls) seg += '.' + CSS.escape(cls);
            }
            const parent = cur.parentElement;
            if (parent) {
                const siblings = [...parent.children].filter(c => c.tagName === cur.tagName);
                if (siblings.length > 1) {
                    seg += ':nth-of-type(' + (siblings.indexOf(cur) + 1) + ')';
                }
            }
            parts.unshift(seg);
            cur = cur.parentElement;
        }
        return parts.join(' > ');
    };

    const SELECTORS = [
        'button', 'a[href]', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="menuitem"]',
        'h1', 'h2', 'h3',
        'nav', 'form',
        '[class*="btn"]', '[class*="button"]', '[class*="cta"]',
    ];

    const seen = new WeakSet();
    const elements = [];
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    for (const sel of SELECTORS) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); } catch(e) { continue; }

        nodes.forEach(el => {
            if (seen.has(el)) return;
            seen.add(el);

            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);

            if (rect.width === 0 || rect.height === 0) return;
            if (style.display === 'none' || style.visibility === 'hidden') return;
            if (parseFloat(style.opacity) < 0.05) return;

            // Document-absolute coordinates (for full-page screenshots)
            const absX = rect.left + window.scrollX;
            const absY = rect.top  + window.scrollY;

            // Text truncation: scrollable content exceeds visible box
            const isTextEl = /^(h[1-6]|p|span|label|td|th|li|a)$/i.test(el.tagName);
            const isTruncated = isTextEl && (
                el.scrollWidth  > el.clientWidth + 2 ||
                el.scrollHeight > el.clientHeight + 2
            );

            // Off-screen: element center is well outside viewport
            const centerX = rect.left + rect.width  / 2;
            const centerY = rect.top  + rect.height / 2;
            const isOffScreen = centerX < -10 || centerX > vw + 10 ||
                                 centerY < -10 || centerY > vh * 3;

            const overflow = (style.overflow || '') +
                             (style.overflowX || '') +
                             (style.overflowY || '');
            const hasOverflowClipping = overflow.includes('hidden') ||
                                        overflow.includes('clip');

            elements.push({
                selector: getSelector(el),
                tag: el.tagName.toLowerCase(),
                text: (el.textContent || '').trim().substring(0, 120),
                role: el.getAttribute('role') || '',
                bbox: { x: absX, y: absY, width: rect.width, height: rect.height },
                is_cta: isCTA(el),
                is_truncated: isTruncated,
                is_off_screen: isOffScreen,
                has_overflow_clipping: hasOverflowClipping,
                color: style.color || '',
                background_color: style.backgroundColor || '',
                font_size: parseFloat(style.fontSize) || 16,
            });
        });
    }

    return {
        elements,
        viewport: { width: vw, height: vh,
                    scroll_height: document.documentElement.scrollHeight }
    };
}
"""

# Minimum intersection area (px²) to avoid counting 1-px border touches
_MIN_OVERLAP_AREA = 6.0

# Maximum drift (px) before raising a LOW finding
_DRIFT_THRESHOLD_PX = 5.0

# Only scan elements within this many viewport heights of the top
_MAX_Y_FACTOR = 3


class LayoutAnalyzer:
    """Runs DOM-based geometry analysis on a Playwright page."""

    def __init__(self) -> None:
        self._last_elements: list[ElementInfo] = []

    @property
    def last_elements(self) -> list[ElementInfo]:
        return self._last_elements

    async def analyze(
        self,
        page: object,
        masked_regions: list[BoundingBox],
        phase: str,
        baseline_elements: Optional[list[ElementInfo]] = None,
    ) -> list[DefectFinding]:
        """
        Extract DOM layout data and return all defect findings for this snapshot.

        Args:
            page: Playwright Page (already navigated)
            masked_regions: Bounding boxes of dynamic regions to exclude
            phase: "baseline" | "peak" | "post"
            baseline_elements: ElementInfo list from baseline phase for drift detection

        Returns:
            List of DefectFinding objects
        """
        raw = await page.evaluate(_LAYOUT_EXTRACTION_JS)
        viewport = raw.get("viewport", {})
        vh = viewport.get("height", 1080)

        elements: list[ElementInfo] = []
        for item in raw.get("elements", []):
            try:
                el = ElementInfo(**item)
                # Skip elements in dynamic masked regions
                if is_in_masked_region(el.bbox, masked_regions):
                    el.is_dynamic = True
                    continue
                # Skip elements far below the fold
                if el.bbox.y > vh * _MAX_Y_FACTOR:
                    continue
                elements.append(el)
            except Exception:
                continue

        self._last_elements = elements

        findings: list[DefectFinding] = []

        # 1. Overlap detection
        findings.extend(_detect_overlaps(elements, phase))

        # 2. Text truncation in headings
        findings.extend(_detect_truncation(elements, phase))

        # 3. Off-screen / viewport overflow
        findings.extend(_detect_overflow(elements, phase))

        # 4. Layout drift vs baseline (LOW severity)
        if baseline_elements:
            findings.extend(_detect_drift(elements, baseline_elements, phase))

        logger.info(
            "layout_analyzer.done",
            phase=phase,
            elements=len(elements),
            findings=len(findings),
        )
        return findings


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _detect_overlaps(elements: list[ElementInfo], phase: str) -> list[DefectFinding]:
    findings = []
    n = len(elements)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = elements[i], elements[j]
            area = a.bbox.intersection_area(b.bbox)
            if area < _MIN_OVERLAP_AREA:
                continue
            # Skip if they share a parent-child relationship (legitimate nesting)
            if a.selector.startswith(b.selector) or b.selector.startswith(a.selector):
                continue
            finding = _make_overlap_finding(a, b, area, phase)
            findings.append(finding)
    return findings


def _make_overlap_finding(
    a: ElementInfo, b: ElementInfo, area: float, phase: str
) -> DefectFinding:
    is_cta = a.is_cta or b.is_cta
    is_form = a.tag in ("input", "select", "textarea") or b.tag in ("input", "select", "textarea")

    if is_cta:
        severity = DefectSeverity.CRITICAL
        color = "red"
        category = DefectCategory.OVERLAP
    elif is_form:
        severity = DefectSeverity.HIGH
        color = "orange"
        category = DefectCategory.FORM_COLLISION
    else:
        severity = DefectSeverity.MEDIUM
        color = "yellow"
        category = DefectCategory.OVERLAP

    cta_note = " (CTA button)" if is_cta else ""
    return DefectFinding(
        defect_id=str(uuid4()),
        severity=severity,
        category=category,
        title=f"Element overlap{cta_note}: {a.tag} ↔ {b.tag}",
        description=(
            f"'{a.text[:40] or a.selector}' overlaps "
            f"'{b.text[:40] or b.selector}' by {area:.0f}px²"
        ),
        element_selector=a.selector,
        element_bbox=a.bbox,
        conflicting_selector=b.selector,
        conflicting_bbox=b.bbox,
        overlap_area_px=area,
        snapshot_phase=phase,
        annotation_color=color,
    )


def _detect_truncation(elements: list[ElementInfo], phase: str) -> list[DefectFinding]:
    findings = []
    for el in elements:
        if el.tag in ("h1", "h2", "h3") and el.is_truncated:
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.HIGH,
                category=DefectCategory.TEXT_TRUNCATION,
                title=f"{el.tag.upper()} text truncated",
                description=(
                    f"Heading text is clipped: '{el.text[:60]}' "
                    f"at ({el.bbox.x:.0f},{el.bbox.y:.0f}) "
                    f"{el.bbox.width:.0f}×{el.bbox.height:.0f}px"
                ),
                element_selector=el.selector,
                element_bbox=el.bbox,
                snapshot_phase=phase,
                annotation_color="orange",
            ))
    return findings


def _detect_overflow(elements: list[ElementInfo], phase: str) -> list[DefectFinding]:
    findings = []
    for el in elements:
        if el.is_off_screen:
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.MEDIUM,
                category=DefectCategory.OVERFLOW,
                title=f"Element off-screen: {el.tag}",
                description=(
                    f"'{el.text[:50] or el.selector}' is outside the viewport "
                    f"at ({el.bbox.x:.0f},{el.bbox.y:.0f})"
                ),
                element_selector=el.selector,
                element_bbox=el.bbox,
                snapshot_phase=phase,
                annotation_color="yellow",
            ))
    return findings


def _detect_drift(
    elements: list[ElementInfo],
    baseline_elements: list[ElementInfo],
    phase: str,
) -> list[DefectFinding]:
    """Compare element positions against baseline; flag shifts > threshold."""
    # Build lookup: selector → ElementInfo for baseline
    baseline_map: dict[str, ElementInfo] = {el.selector: el for el in baseline_elements}
    findings = []

    for el in elements:
        base = baseline_map.get(el.selector)
        if base is None:
            continue
        drift = el.bbox.distance_to(base.bbox)
        if drift > _DRIFT_THRESHOLD_PX:
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.LOW,
                category=DefectCategory.LAYOUT_DRIFT,
                title=f"Layout drift {drift:.1f}px: {el.tag}",
                description=(
                    f"'{el.text[:50] or el.selector}' shifted {drift:.1f}px from baseline. "
                    f"Baseline: ({base.bbox.x:.0f},{base.bbox.y:.0f}) → "
                    f"Now: ({el.bbox.x:.0f},{el.bbox.y:.0f})"
                ),
                element_selector=el.selector,
                element_bbox=el.bbox,
                drift_px=drift,
                snapshot_phase=phase,
                annotation_color="green",
            ))
    return findings
