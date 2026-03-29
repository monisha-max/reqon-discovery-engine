"""
Defect Detection Node — LangGraph node that runs Layer 5 after perf testing.

Triggered by: should_run_defect_detection() conditional edge in orchestrator.py
Input state: defect_config (with snapshot_artifacts populated by perf_test_node)
Output state: defect_result (DefectDetectionResult.model_dump())

Standalone mode: if snapshot_artifacts are absent (perf tests didn't run),
the node captures its own baseline screenshots from crawled pages so defect
detection can run independently of the performance pipeline.
"""
from __future__ import annotations

import os

import structlog

from layer5_defect_detection.defect_orchestrator import run_defect_detection

logger = structlog.get_logger()


async def _capture_standalone_artifacts(
    target_url: str,
    pages: list[dict],
    storage_state_path: str | None,
    defect_config: dict,
) -> list[dict]:
    """
    Capture baseline-only screenshots from crawled pages so defect detection
    can run without perf_test_node having populated snapshot_artifacts.
    """
    from layer5_defect_detection.capture.screenshot_capture import ScreenshotCapture
    from layer5_defect_detection.priority.page_priority_filter import (
        get_priority_pages,
        probe_priority_paths,
    )

    output_dir = os.path.join("output", "defect_reports", "standalone")
    viewport = defect_config.get("viewport", {"width": 1920, "height": 1080})
    max_pages = defect_config.get("max_pages", 10)

    priority_pages = get_priority_pages(pages, max_pages=max_pages)
    if not priority_pages:
        priority_pages = probe_priority_paths(target_url)
    if not priority_pages:
        priority_pages = [{
            "url": target_url,
            "page_type": "unknown",
            "page_type_confidence": 0.0,
            "performance": {},
            "_priority_tier": 4,
            "_priority_reason": "Standalone fallback — target root URL",
            "_page_slug": "root",
        }]

    logger.info(
        "defect_detect_node.standalone_capture",
        priority_pages=len(priority_pages),
    )

    capture = ScreenshotCapture(target_url, storage_state_path, output_dir, viewport)
    artifacts: list[dict] = []

    await capture.start()
    try:
        for p in priority_pages:
            try:
                path, _ = await capture.capture_and_release("baseline", url=p["url"])
                artifacts.append({
                    "phase": "baseline",
                    "url": p.get("url", target_url),
                    "page_type": p.get("page_type", "unknown"),
                    "page_slug": p.get("_page_slug", "unknown"),
                    "priority_tier": p.get("_priority_tier"),
                    "priority_reason": p.get("_priority_reason", "standalone"),
                    "screenshot_path": path,
                })
            except Exception as exc:
                logger.warning(
                    "defect_detect_node.standalone_capture_failed",
                    url=p.get("url"), error=str(exc),
                )
    finally:
        await capture.stop()

    return artifacts


async def defect_detect_node(state: dict) -> dict:
    """
    LangGraph node: analyze pre-captured screenshots for layout defects.

    Preferred: state["defect_config"]["snapshot_artifacts"] populated by perf_test_node.
    Fallback: captures baseline screenshots from crawled pages (standalone mode).
    """
    defect_config: dict = state.get("defect_config") or {}
    snapshot_artifacts: list[dict] = defect_config.get("snapshot_artifacts", [])

    request_data: dict = state.get("request") or {}
    target_url: str = request_data.get("target_url", "")
    storage_state_path: str | None = state.get("storage_state_path")

    if not snapshot_artifacts:
        pages: list[dict] = state.get("pages") or []
        if not pages and not target_url:
            logger.warning(
                "defect_detect_node.no_artifacts_no_pages",
                reason="No snapshot_artifacts and no crawled pages — cannot run defect detection",
            )
            return {"defect_result": None, "phase": "defect_complete"}

        logger.info(
            "defect_detect_node.standalone_mode",
            reason="No snapshot_artifacts from perf — capturing baseline from crawled pages",
            crawled_pages=len(pages),
        )
        snapshot_artifacts = await _capture_standalone_artifacts(
            target_url=target_url,
            pages=pages,
            storage_state_path=storage_state_path,
            defect_config=defect_config,
        )
        defect_config = dict(defect_config)
        defect_config["snapshot_artifacts"] = snapshot_artifacts

    if not snapshot_artifacts:
        logger.warning("defect_detect_node.no_artifacts",
                       reason="Standalone capture produced no artifacts")
        return {"defect_result": None, "phase": "defect_complete"}

    logger.info(
        "defect_detect_node.start",
        target_url=target_url,
        artifacts=len(snapshot_artifacts),
    )

    try:
        result = await run_defect_detection(
            target_url=target_url,
            snapshot_artifacts=snapshot_artifacts,
            defect_config=defect_config,
            storage_state_path=storage_state_path,
        )

        logger.info(
            "defect_detect_node.complete",
            total_defects=result.total_defects,
            critical=result.critical_count,
            high=result.high_count,
            regression_score=result.max_regression_score,
            report=result.report_path,
        )

        return {
            "defect_result": result.model_dump(),
            "phase": "defect_complete",
        }

    except Exception as exc:
        logger.error("defect_detect_node.failed", error=str(exc), exc_info=True)
        errors: list[str] = list(state.get("errors") or [])
        errors.append(f"Defect detection failed: {exc}")
        return {
            "defect_result": None,
            "errors": errors,
            "phase": "defect_complete",
        }
