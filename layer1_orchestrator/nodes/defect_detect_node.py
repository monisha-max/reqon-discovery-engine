"""
Defect Detection Node — LangGraph node that runs Layer 5 after perf testing.

Triggered by: should_run_defect_detection() conditional edge in orchestrator.py
Input state: defect_config (with snapshot_artifacts populated by perf_test_node)
Output state: defect_result (DefectDetectionResult.model_dump())
"""
from __future__ import annotations

import structlog

from layer5_defect_detection.defect_orchestrator import run_defect_detection

logger = structlog.get_logger()


async def defect_detect_node(state: dict) -> dict:
    """
    LangGraph node: analyze pre-captured screenshots for layout defects.

    Expects state["defect_config"]["snapshot_artifacts"] to be populated
    by perf_test_node before this node runs.
    """
    defect_config: dict = state.get("defect_config") or {}
    snapshot_artifacts: list[dict] = defect_config.get("snapshot_artifacts", [])

    if not snapshot_artifacts:
        logger.warning("defect_detect_node.no_artifacts",
                       reason="snapshot_artifacts missing from defect_config")
        return {"defect_result": None, "phase": "defect_complete"}

    request_data: dict = state.get("request") or {}
    target_url: str = request_data.get("target_url", "")
    storage_state_path: str | None = state.get("storage_state_path")

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
