"""
A11y Analyzer — WCAG accessibility checks via axe-core.

Injects axe-core from a local file (avoids CSP / network restrictions on
target sites) and runs WCAG 2.1 A/AA + best-practice rules against the live
Playwright page. Each failing DOM node becomes one DefectFinding so the report
shows a distinct selector, bounding box, and annotation per element.

axe-core impact → DefectSeverity:
  critical → CRITICAL
  serious  → HIGH
  moderate → MEDIUM
  minor    → LOW

Rule ID → DefectCategory mapping covers the 20 most common rules; all others
fall back to DefectCategory.ACCESSIBILITY.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

import structlog

from layer5_defect_detection.models.defect_models import (
    BoundingBox,
    DefectCategory,
    DefectFinding,
    DefectSeverity,
)

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = structlog.get_logger()

# Path to the bundled axe-core JS (fetched once at setup time)
_AXE_JS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "assets", "axe.min.js")
)

# Sentinel bbox for findings where axe provides no geometry
_ZERO_BBOX = BoundingBox(x=0, y=0, width=0, height=0)

_IMPACT_SEVERITY: dict[str, DefectSeverity] = {
    "critical": DefectSeverity.CRITICAL,
    "serious":  DefectSeverity.HIGH,
    "moderate": DefectSeverity.MEDIUM,
    "minor":    DefectSeverity.LOW,
}

# Specific axe rule IDs that map to existing DefectCategory values.
# All other rules fall back to DefectCategory.ACCESSIBILITY.
_RULE_CATEGORY: dict[str, DefectCategory] = {
    # Contrast
    "color-contrast":              DefectCategory.CONTRAST,
    "color-contrast-enhanced":     DefectCategory.CONTRAST,
    # ARIA
    "aria-allowed-attr":           DefectCategory.ARIA_VIOLATION,
    "aria-hidden-body":            DefectCategory.ARIA_VIOLATION,
    "aria-hidden-focus":           DefectCategory.ARIA_VIOLATION,
    "aria-input-field-name":       DefectCategory.ARIA_VIOLATION,
    "aria-prohibited-attr":        DefectCategory.ARIA_VIOLATION,
    "aria-required-attr":          DefectCategory.ARIA_VIOLATION,
    "aria-required-children":      DefectCategory.ARIA_VIOLATION,
    "aria-required-parent":        DefectCategory.ARIA_VIOLATION,
    "aria-roles":                  DefectCategory.ARIA_VIOLATION,
    "aria-valid-attr":             DefectCategory.ARIA_VIOLATION,
    "aria-valid-attr-value":       DefectCategory.ARIA_VIOLATION,
    # Interactive labels
    "button-name":                 DefectCategory.EMPTY_INTERACTIVE,
    "link-name":                   DefectCategory.EMPTY_INTERACTIVE,
    # Images
    "image-alt":                   DefectCategory.MISSING_ALT_TEXT,
    "input-image-alt":             DefectCategory.MISSING_ALT_TEXT,
    "role-img-alt":                DefectCategory.MISSING_ALT_TEXT,
    # Forms
    "label":                       DefectCategory.FORM_INTEGRITY,
    "label-content-name-mismatch": DefectCategory.FORM_INTEGRITY,
    "select-name":                 DefectCategory.FORM_INTEGRITY,
    # DOM structure
    "heading-order":               DefectCategory.DOM_STRUCTURAL,
    "duplicate-id-active":         DefectCategory.DOM_STRUCTURAL,
    "duplicate-id-aria":           DefectCategory.DOM_STRUCTURAL,
}


class A11yAnalyzer:
    """
    Runs axe-core WCAG 2.1 checks on a live Playwright page.

    Returns one DefectFinding per failing DOM node (not per rule) so each
    finding has a real selector and bounding box for screenshot annotation.
    """

    async def analyze(self, page: "Page", phase: str) -> list[DefectFinding]:
        """
        Inject axe-core and run WCAG 2.1 A/AA + best-practice rules.

        Returns an empty list (not an exception) if axe cannot be injected
        or the page blocks script execution — defect detection continues
        with the other analyzers regardless.
        """
        if not os.path.exists(_AXE_JS_PATH):
            logger.warning("a11y_analyzer.axe_not_found", path=_AXE_JS_PATH)
            return []

        try:
            await page.add_script_tag(path=_AXE_JS_PATH)
        except Exception as exc:
            logger.warning("a11y_analyzer.inject_failed", error=str(exc))
            return []

        try:
            axe_results: dict = await page.evaluate("""
                async () => {
                    return await axe.run(document, {
                        runOnly: {
                            type: 'tag',
                            values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'best-practice']
                        }
                    });
                }
            """)
        except Exception as exc:
            logger.warning("a11y_analyzer.run_failed", error=str(exc))
            return []

        violations: list[dict] = axe_results.get("violations", [])
        findings: list[DefectFinding] = []
        for violation in violations:
            findings.extend(self._violation_to_findings(violation, phase))

        logger.info(
            "a11y_analyzer.complete",
            phase=phase,
            violations=len(violations),
            findings=len(findings),
        )
        return findings

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _violation_to_findings(
        self, violation: dict, phase: str
    ) -> list[DefectFinding]:
        """Convert one axe violation (one rule, N nodes) into N DefectFindings."""
        rule_id   = violation.get("id", "unknown")
        impact    = violation.get("impact") or "minor"
        help_text = violation.get("help", rule_id)
        help_url  = violation.get("helpUrl", "")
        tags      = violation.get("tags", [])

        # Extract WCAG criteria tags (e.g. 'wcag143', 'wcag21aa')
        wcag_tags = [
            t for t in tags
            if t.startswith("wcag") and t not in ("wcag2a", "wcag2aa", "wcag21a", "wcag21aa")
        ]
        wcag_label = ", ".join(wcag_tags) if wcag_tags else "WCAG"

        severity = _IMPACT_SEVERITY.get(impact, DefectSeverity.LOW)
        category = _RULE_CATEGORY.get(rule_id, DefectCategory.ACCESSIBILITY)

        findings = []
        for node in violation.get("nodes", []):
            selector = _best_selector(node)
            bbox     = _node_bbox(node)
            summary  = _node_summary(node)

            findings.append(DefectFinding(
                severity=severity,
                category=category,
                title=f"[axe/{rule_id}] {help_text}",
                description=(
                    f"{wcag_label} — {summary} "
                    f"Selector: {selector}. "
                    f"See: {help_url}"
                ),
                element_selector=selector,
                element_bbox=bbox,
                snapshot_phase=phase,
                annotation_color="blue",  # overwritten by FindingsMapper.process()
            ))
        return findings


# ---------------------------------------------------------------------------
# Node-level helpers (module-level so they are easily unit-testable)
# ---------------------------------------------------------------------------

def _best_selector(node: dict) -> str:
    """
    Extract the most actionable CSS selector from an axe node.
    axe provides `target` (list of selectors, may be nested for iframes).
    Falls back to the outer HTML snippet if target is absent.
    """
    target = node.get("target", [])
    if target:
        leaf = target[-1]
        # iframe-nested selectors appear as sub-lists
        return (leaf if isinstance(leaf, str) else str(leaf))[:200]
    return node.get("html", "unknown")[:200]


def _node_bbox(node: dict) -> BoundingBox:
    """
    axe >= 4.4 includes `boundingRect` {top, left, width, height}.
    Returns _ZERO_BBOX sentinel when geometry is unavailable — matches
    the pattern used by dom_behavioral_analyzer and functional_analyzer.
    """
    rect = node.get("boundingRect") or {}
    if rect.get("width") or rect.get("height"):
        return BoundingBox(
            x=float(rect.get("left", 0)),
            y=float(rect.get("top", 0)),
            width=float(rect.get("width", 0)),
            height=float(rect.get("height", 0)),
        )
    return _ZERO_BBOX


def _node_summary(node: dict) -> str:
    """
    Collect failure messages from axe node check groups (any / all / none).
    Capped at 3 messages to keep descriptions readable.
    """
    messages = []
    for group in ("any", "all", "none"):
        for check in node.get(group, []):
            msg = check.get("message", "")
            if msg:
                messages.append(msg)
    return " | ".join(messages[:3])
