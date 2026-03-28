"""
Findings Mapper — deduplication and normalization of raw defect findings.

Single source of truth for severity → annotation color mapping.
Removes duplicate findings for the same element/category within a phase.
"""
from __future__ import annotations

from layer5_defect_detection.models.defect_models import DefectFinding, DefectSeverity

SEVERITY_COLOR: dict[DefectSeverity, str] = {
    DefectSeverity.CRITICAL: "red",
    DefectSeverity.HIGH: "orange",
    DefectSeverity.MEDIUM: "yellow",
    DefectSeverity.LOW: "green",
    DefectSeverity.INFO: "blue",
}

_SEVERITY_ORDER = list(DefectSeverity)


class FindingsMapper:
    """Normalizes, deduplicates, and sorts a list of DefectFinding objects."""

    def process(self, raw_findings: list[DefectFinding]) -> list[DefectFinding]:
        """
        1. Assign annotation_color from severity
        2. Deduplicate: same (category, element_selector, phase) → keep highest severity
        3. Sort: Critical first, Info last
        """
        # Dedup: keep highest-severity finding per (category, selector, phase)
        best: dict[tuple[str, str, str], DefectFinding] = {}
        for f in raw_findings:
            key = (f.category.value, f.element_selector, f.snapshot_phase)
            existing = best.get(key)
            if existing is None:
                best[key] = f
            else:
                # Keep whichever has lower enum index (higher severity)
                if _SEVERITY_ORDER.index(f.severity) < _SEVERITY_ORDER.index(existing.severity):
                    best[key] = f

        result = list(best.values())

        # Assign colors
        for f in result:
            f.annotation_color = SEVERITY_COLOR.get(f.severity, "red")

        # Sort by severity
        result.sort(key=lambda f: _SEVERITY_ORDER.index(f.severity))
        return result
