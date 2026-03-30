"""
Defect Orchestrator — main entry point for Layer 5.

Orchestrates the full vision pipeline on pre-captured snapshot artifacts:
  1. Open each priority page with a fresh Playwright session
  2. Preprocess (stabilize, normalize, mask dynamic regions)
  3. Run layout geometry analysis + contrast analysis
  4. Map findings to severity
  5. Annotate screenshots with bounding boxes
  6. Compare baseline vs peak/post → regression score
  7. Write JSON + HTML reports

Called from defect_detect_node after Locust has completed.
The three-phase capture (baseline/peak/post) is done inside perf_test_node
and passed as snapshot_artifacts in defect_config.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

from layer5_defect_detection.analyzers.a11y_analyzer import A11yAnalyzer
from layer5_defect_detection.analyzers.contrast_analyzer import ContrastAnalyzer
from layer5_defect_detection.analyzers.dom_behavioral_analyzer import DOMBehavioralAnalyzer
from layer5_defect_detection.analyzers.functional_analyzer import FunctionalAnalyzer
from layer5_defect_detection.analyzers.layout_analyzer import LayoutAnalyzer

# Extra viewports to analyse in addition to the configured desktop viewport.
# Each entry: (phase_label, viewport_dict)
_EXTRA_VIEWPORTS: list[tuple[str, dict]] = [
    ("mobile",  {"width": 375,  "height": 812}),
    ("tablet",  {"width": 768,  "height": 1024}),
]
from layer5_defect_detection.capture.screenshot_capture import ScreenshotCapture
from layer5_defect_detection.evidence.annotator import Annotator
from layer5_defect_detection.evidence.evidence_builder import EvidenceBuilder
from layer5_defect_detection.mapper.findings_mapper import FindingsMapper
from layer5_defect_detection.models.defect_models import (
    ComparisonResult,
    DefectDetectionResult,
    DefectFinding,
    DefectSeverity,
    ElementInfo,
    PageDefectSummary,
    RegressionDefect,
    SnapshotReport,
)
from layer5_defect_detection.preprocessing.normalizer import Normalizer
from layer5_defect_detection.preprocessing.region_masker import get_masked_regions
from layer5_defect_detection.preprocessing.stabilizer import Stabilizer

logger = structlog.get_logger()

_SEVERITY_WEIGHTS = {
    DefectSeverity.CRITICAL: 40,
    DefectSeverity.HIGH:     20,
    DefectSeverity.MEDIUM:   10,
    DefectSeverity.LOW:       2,
    DefectSeverity.INFO:      1,
}


async def run_defect_detection(
    target_url: str,
    snapshot_artifacts: list[dict],
    defect_config: dict,
    storage_state_path: Optional[str] = None,
    output_dir: str = "output/defect_reports",
) -> DefectDetectionResult:
    """
    Main entry point for Layer 5 defect detection.

    Args:
        target_url: The root URL of the tested application
        snapshot_artifacts: List of {phase, url, page_type, page_slug,
                             screenshot_path, priority_reason} dicts
                             produced by perf_test_node
        defect_config: Config dict from state (viewport, max_pages, etc.)
        storage_state_path: Optional path to Playwright auth storage state
        output_dir: Base directory for all defect report output

    Returns:
        DefectDetectionResult with per-page summaries and report paths
    """
    start_time = time.time()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(output_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)

    viewport = defect_config.get("viewport", {"width": 1920, "height": 1080})

    stabilizer = Stabilizer()
    normalizer = Normalizer()
    layout_analyzer = LayoutAnalyzer()
    contrast_analyzer = ContrastAnalyzer()
    functional_analyzer = FunctionalAnalyzer()
    dom_behavioral_analyzer = DOMBehavioralAnalyzer()
    a11y_analyzer = A11yAnalyzer()
    mapper = FindingsMapper()
    annotator = Annotator()
    builder = EvidenceBuilder(run_dir)

    # Group artifacts by (url, page_slug) → list of phase artifacts
    page_groups: dict[str, list[dict]] = {}
    for artifact in snapshot_artifacts:
        key = artifact.get("page_slug") or artifact.get("url", "unknown")
        page_groups.setdefault(key, []).append(artifact)

    pages_analyzed: list[PageDefectSummary] = []

    capture = ScreenshotCapture(target_url, storage_state_path, run_dir, viewport)
    await capture.start()

    try:
        for slug, artifacts in page_groups.items():
            page_url = artifacts[0].get("url", target_url)
            page_type = artifacts[0].get("page_type", "unknown")
            priority_reason = artifacts[0].get("priority_reason", "")
            page_dir = os.path.join(run_dir, slug)
            os.makedirs(page_dir, exist_ok=True)

            snapshots: list[SnapshotReport] = []
            baseline_elements: Optional[list[ElementInfo]] = None

            # Sort phases: baseline → peak → post
            phase_order = {"baseline": 0, "peak": 1, "post": 2}
            artifacts_sorted = sorted(
                artifacts, key=lambda a: phase_order.get(a.get("phase", ""), 99)
            )

            for artifact in artifacts_sorted:
                phase = artifact.get("phase", "unknown")
                screenshot_path = artifact.get("screenshot_path", "")

                if not screenshot_path or not os.path.exists(screenshot_path):
                    logger.warning("defect_orchestrator.missing_screenshot",
                                   phase=phase, url=page_url)
                    continue

                # Preprocessing
                img = stabilizer.stabilize(screenshot_path)
                img, scale = normalizer.normalize(img)
                if scale != 1.0:
                    # Save normalized version for annotation
                    norm_path = screenshot_path.replace(".png", "_norm.png")
                    img.save(norm_path, "PNG")
                    screenshot_path = norm_path

                # Open a fresh Playwright page for DOM analysis
                # monitor_events=True captures console errors + failed requests
                _, page = await capture.capture(phase, url=page_url, monitor_events=True)
                try:
                    masked_regions = await get_masked_regions(page)
                    raw_findings = await layout_analyzer.analyze(
                        page, masked_regions, phase, baseline_elements
                    )
                    raw_findings.extend(
                        contrast_analyzer.check_elements(layout_analyzer.last_elements, phase)
                    )
                    raw_findings.extend(
                        await functional_analyzer.check_broken_links(page, phase)
                    )
                    raw_findings.extend(
                        await dom_behavioral_analyzer.analyze(page, phase)
                    )
                    raw_findings.extend(
                        await a11y_analyzer.analyze(page, phase)
                    )
                    # Console errors / network failures / response telemetry
                    # — all captured pre-navigation via event listeners
                    console_errors = getattr(page, "_reqon_console_errors", [])
                    failed_requests = getattr(page, "_reqon_failed_requests", [])
                    responses = getattr(page, "_reqon_responses", [])
                    raw_findings.extend(
                        functional_analyzer.check_events(console_errors, failed_requests, phase)
                    )
                    raw_findings.extend(
                        functional_analyzer.check_network_telemetry(responses, phase)
                    )
                finally:
                    await page.close()

                findings = mapper.process(raw_findings)

                if phase == "baseline":
                    baseline_elements = layout_analyzer.last_elements[:]

                # Annotate screenshot
                annotated_path = os.path.join(page_dir, f"annotated_{phase}.png")
                if findings:
                    annotator.annotate(screenshot_path, findings, annotated_path)
                else:
                    # No findings — copy clean screenshot as annotated
                    import shutil
                    shutil.copy2(screenshot_path, annotated_path)

                # Move raw screenshot to page dir with standard name
                dst_raw = os.path.join(page_dir, f"{phase}.png")
                if screenshot_path != dst_raw:
                    try:
                        import shutil as _sh
                        _sh.copy2(screenshot_path, dst_raw)
                    except Exception:
                        dst_raw = screenshot_path

                snapshots.append(SnapshotReport(
                    phase=phase,
                    url=page_url,
                    page_type=page_type,
                    page_slug=slug,
                    screenshot_path=dst_raw,
                    annotated_screenshot_path=annotated_path,
                    viewport_width=viewport["width"],
                    viewport_height=viewport["height"],
                    findings=findings,
                    total_elements_analyzed=len(layout_analyzer.last_elements),
                    dynamic_regions_masked=sum(
                        1 for e in layout_analyzer.last_elements if e.is_dynamic
                    ),
                    captured_at=datetime.now(timezone.utc).isoformat(),
                ))

            # Run extra viewport analysis (mobile / tablet) on the baseline URL
            for vp_phase, vp_size in _EXTRA_VIEWPORTS:
                try:
                    vp_snapshot = await _analyze_extra_viewport(
                        page_url=page_url,
                        page_type=page_type,
                        slug=slug,
                        phase_label=vp_phase,
                        viewport=vp_size,
                        storage_state_path=storage_state_path,
                        run_dir=run_dir,
                        layout_analyzer=layout_analyzer,
                        contrast_analyzer=contrast_analyzer,
                        mapper=mapper,
                        annotator=annotator,
                    )
                    if vp_snapshot:
                        snapshots.append(vp_snapshot)
                except Exception as exc:
                    logger.warning(
                        "defect_orchestrator.extra_viewport_failed",
                        phase=vp_phase, url=page_url, error=str(exc),
                    )

            comparison = _compare_snapshots(snapshots)
            summary = _build_page_summary(page_url, page_type, slug, priority_reason,
                                          snapshots, comparison)
            pages_analyzed.append(summary)

    finally:
        await capture.stop()

    result = _build_result(target_url, run_id, pages_analyzed, start_time)
    json_path = builder.build_json_report(result, run_id)
    html_path = builder.build_html_report(result, run_id)
    result.report_path = html_path

    logger.info(
        "defect_orchestrator.complete",
        run_id=run_id,
        pages=len(pages_analyzed),
        total_defects=result.total_defects,
        max_regression_score=result.max_regression_score,
        json=json_path,
        html=html_path,
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _analyze_extra_viewport(
    page_url: str,
    page_type: str,
    slug: str,
    phase_label: str,
    viewport: dict,
    storage_state_path: Optional[str],
    run_dir: str,
    layout_analyzer: "LayoutAnalyzer",
    contrast_analyzer: "ContrastAnalyzer",
    mapper: "FindingsMapper",
    annotator: "Annotator",
) -> Optional[SnapshotReport]:
    """
    Run a single layout+contrast analysis pass at an alternative viewport
    (e.g. mobile 375px, tablet 768px).

    Creates its own short-lived ScreenshotCapture session so it doesn't
    interfere with the main desktop capture context.
    """
    vp_dir = os.path.join(run_dir, slug, phase_label)
    os.makedirs(vp_dir, exist_ok=True)

    cap = ScreenshotCapture(page_url, storage_state_path, vp_dir, viewport)
    await cap.start()
    try:
        screenshot_path, page = await cap.capture(phase_label, url=page_url)
        try:
            masked_regions = await get_masked_regions(page)
            raw_findings = await layout_analyzer.analyze(
                page, masked_regions, phase_label, baseline_elements=None
            )
            raw_findings.extend(
                contrast_analyzer.check_elements(layout_analyzer.last_elements, phase_label)
            )
        finally:
            await page.close()
    finally:
        await cap.stop()

    findings = mapper.process(raw_findings)

    annotated_path = os.path.join(vp_dir, f"annotated_{phase_label}.png")
    if findings:
        annotator.annotate(screenshot_path, findings, annotated_path)
    else:
        import shutil
        shutil.copy2(screenshot_path, annotated_path)

    logger.info(
        "defect_orchestrator.extra_viewport_done",
        phase=phase_label,
        viewport=viewport,
        url=page_url,
        findings=len(findings),
    )

    return SnapshotReport(
        phase=phase_label,
        url=page_url,
        page_type=page_type,
        page_slug=slug,
        screenshot_path=screenshot_path,
        annotated_screenshot_path=annotated_path,
        viewport_width=viewport["width"],
        viewport_height=viewport["height"],
        findings=findings,
        total_elements_analyzed=len(layout_analyzer.last_elements),
        dynamic_regions_masked=sum(
            1 for e in layout_analyzer.last_elements if e.is_dynamic
        ),
        captured_at=datetime.now(timezone.utc).isoformat(),
    )


def _compare_snapshots(snapshots: list[SnapshotReport]) -> ComparisonResult:
    baseline = next((s for s in snapshots if s.phase == "baseline"), None)
    if not baseline:
        return ComparisonResult()

    baseline_fps = {_fingerprint(f) for f in baseline.findings}
    regression_defects: list[RegressionDefect] = []
    # Deduplicate: same defect introduced in peak AND post → report once only
    seen_regression_fps: set[str] = set()

    for snapshot in snapshots:
        if snapshot.phase == "baseline":
            continue
        for finding in snapshot.findings:
            fp = _fingerprint(finding)
            if fp not in baseline_fps and fp not in seen_regression_fps:
                seen_regression_fps.add(fp)
                regression_defects.append(RegressionDefect(
                    defect=finding,
                    introduced_at_phase=snapshot.phase,
                    baseline_clear=True,
                ))

    score = min(100.0, sum(
        _SEVERITY_WEIGHTS.get(rd.defect.severity, 1) for rd in regression_defects
    ))

    peak_snap = next((s for s in snapshots if s.phase == "peak"), None)
    post_snap = next((s for s in snapshots if s.phase == "post"), None)

    return ComparisonResult(
        baseline_finding_count=len(baseline.findings),
        peak_finding_count=len(peak_snap.findings) if peak_snap else 0,
        post_finding_count=len(post_snap.findings) if post_snap else 0,
        regression_defects=regression_defects,
        regression_score=score,
    )


def _fingerprint(f: DefectFinding) -> str:
    """
    Stable fingerprint for a finding that survives DOM reordering between phases.

    Uses category + bbox bucketed to 20px grid + first 30 chars of text/selector.
    Bucketing absorbs minor layout drift without treating the same element as new.
    """
    bbox = f.element_bbox
    if bbox:
        bx = round(bbox.x / 20) * 20
        by = round(bbox.y / 20) * 20
        pos = f"{bx},{by}"
    else:
        pos = "nopos"
    # Use selector as tiebreaker but strip volatile nth-of-type suffixes
    sel = re.sub(r":nth-of-type\(\d+\)", "", f.element_selector or "")
    return f"{f.category.value}::{pos}::{sel[:40]}"


def _build_page_summary(
    url: str,
    page_type: str,
    slug: str,
    priority_reason: str,
    snapshots: list[SnapshotReport],
    comparison: ComparisonResult,
) -> PageDefectSummary:
    all_findings: list[DefectFinding] = [f for s in snapshots for f in s.findings]
    counts = _count_by_severity(all_findings)
    return PageDefectSummary(
        url=url,
        page_type=page_type,
        page_slug=slug,
        priority_reason=priority_reason,
        snapshots=snapshots,
        comparison=comparison,
        **counts,
    )


def _build_result(
    target_url: str,
    run_id: str,
    pages: list[PageDefectSummary],
    start_time: float,
) -> DefectDetectionResult:
    all_findings: list[DefectFinding] = [
        f for p in pages for s in p.snapshots for f in s.findings
    ]
    counts = _count_by_severity(all_findings)
    max_reg = max((p.comparison.regression_score for p in pages if p.comparison), default=0.0)
    return DefectDetectionResult(
        target_url=target_url,
        run_id=run_id,
        pages_analyzed=pages,
        total_priority_pages=len(pages),
        total_defects=len(all_findings),
        max_regression_score=max_reg,
        timestamp=datetime.now(timezone.utc).isoformat(),
        duration_seconds=round(time.time() - start_time, 2),
        **counts,
    )


def _count_by_severity(findings: list[DefectFinding]) -> dict:
    return {
        "critical_count": sum(1 for f in findings if f.severity == DefectSeverity.CRITICAL),
        "high_count":     sum(1 for f in findings if f.severity == DefectSeverity.HIGH),
        "medium_count":   sum(1 for f in findings if f.severity == DefectSeverity.MEDIUM),
        "low_count":      sum(1 for f in findings if f.severity == DefectSeverity.LOW),
        "info_count":     sum(1 for f in findings if f.severity == DefectSeverity.INFO),
    }
