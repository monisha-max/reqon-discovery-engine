"""
Performance Test Node — LangGraph node that runs Layer 3 after crawling.

Reads crawled PageData from state["pages"] (already collected by Layer 2),
builds a PerfTestRequest from state["request"], calls run_performance_tests(),
and writes the result back into state["perf_result"].

Also publishes performance events to the Redis stream "perf_events".

When defect_config is present in state, this node also coordinates the
three-phase visual capture (baseline / peak / post) in parallel with Locust
for Layer 5 defect detection. Screenshots are saved; artifact metadata is
stored in defect_config["snapshot_artifacts"] for defect_detect_node.
"""
from __future__ import annotations

import asyncio
import os

import structlog

from layer3_performance.models.perf_models import PerfTestRequest, TestType
from layer3_performance.perf_orchestrator import run_performance_tests
from shared.state.redis_state import RedisStateManager

logger = structlog.get_logger()


async def perf_test_node(state: dict) -> dict:
    """LangGraph node: run performance tests on the crawled target."""
    request_data = state.get("request", {})
    pages = state.get("pages", [])

    target_url = (
        request_data.get("target_url")
        if isinstance(request_data, dict)
        else getattr(request_data, "target_url", "")
    )

    if not target_url:
        logger.error("perf_test_node.no_target_url")
        return {"perf_result": None, "phase": "perf_complete"}

    # Build PerfTestRequest from orchestrator state
    perf_config = state.get("perf_config", {})

    # Parse test types
    raw_types = perf_config.get("test_types", ["load"])
    test_types = []
    for t in raw_types:
        try:
            test_types.append(TestType(t))
        except ValueError:
            logger.warning("perf_test_node.invalid_test_type", type=t)
    if not test_types:
        test_types = [TestType.LOAD]

    # Auth headers — pass through from auth layer if available
    auth_headers = {}
    auth_session = state.get("auth_session", {})
    if isinstance(auth_session, dict):
        token = auth_session.get("token") or auth_session.get("access_token")
        if token:
            auth_headers["Authorization"] = f"Bearer {token}"

    load_users = perf_config.get("load_users", 50)
    load_spawn_rate = perf_config.get("load_spawn_rate", 5.0)
    load_duration = perf_config.get("load_duration_seconds", 300)

    perf_request = PerfTestRequest(
        target_url=target_url,
        openapi_spec_path=perf_config.get("openapi_spec_path"),
        test_types=test_types,
        load_users=load_users,
        load_spawn_rate=load_spawn_rate,
        load_duration_seconds=load_duration,
        stress_max_users=perf_config.get("stress_max_users", 300),
        stress_spawn_rate=perf_config.get("stress_spawn_rate", 10.0),
        stress_duration_seconds=perf_config.get("stress_duration_seconds", 600),
        soak_users=perf_config.get("soak_users", 25),
        soak_duration_seconds=perf_config.get("soak_duration_seconds", 1800),
        auth_headers=auth_headers,
        storage_state_path=state.get("storage_state_path"),
    )

    logger.info(
        "perf_test_node.starting",
        target=target_url,
        test_types=[t.value for t in test_types],
        pages_available=len(pages),
    )

    # -----------------------------------------------------------------------
    # Layer 5 integration: three-phase visual capture
    # -----------------------------------------------------------------------
    defect_config: dict = dict(state.get("defect_config") or {})
    run_defect = bool(defect_config.get("enabled"))

    if run_defect:
        result_dict, updated_defect_config = await _run_with_visual_capture(
            perf_request=perf_request,
            pages=pages,
            target_url=target_url,
            state=state,
            defect_config=defect_config,
        )
    else:
        result_dict = await _run_perf_only(perf_request, pages, target_url)
        updated_defect_config = defect_config

    if result_dict is None:
        errors = list(state.get("errors") or [])
        errors.append("Performance testing failed")
        return {"perf_result": None, "errors": errors,
                "defect_config": updated_defect_config, "phase": "perf_complete"}

    # Persist to Redis
    try:
        redis = RedisStateManager()
        await redis.connect()
        await redis.set("last_perf_result", result_dict, ttl=86400)
        await redis.push_to_stream("perf_events", {
            "event": "perf_complete",
            "target_url": target_url,
            "test_types": [t.value for t in test_types],
        })
        await redis.disconnect()
    except Exception as e:
        logger.warning("perf_test_node.redis_failed", error=str(e))

    return {
        "perf_result": result_dict,
        "defect_config": updated_defect_config,
        "phase": "perf_complete",
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _run_perf_only(
    perf_request: PerfTestRequest, pages: list, target_url: str
) -> dict | None:
    """Run performance tests without visual capture."""
    try:
        result = await run_performance_tests(request=perf_request, crawled_pages=pages)
        logger.info(
            "perf_test_node.complete",
            endpoints_tested=result.endpoints_tested,
            bottlenecks=len(result.bottlenecks),
            duration_s=result.total_duration_seconds,
        )
        return result.model_dump()
    except Exception as e:
        logger.error("perf_test_node.failed", error=str(e))
        return None


async def _run_with_visual_capture(
    perf_request: PerfTestRequest,
    pages: list,
    target_url: str,
    state: dict,
    defect_config: dict,
) -> tuple[dict | None, dict]:
    """
    Coordinate three-phase Playwright capture around the Locust subprocess.

    Returns (perf_result_dict, updated_defect_config).
    updated_defect_config["snapshot_artifacts"] contains per-page/phase metadata.
    """
    from layer5_defect_detection.capture.screenshot_capture import ScreenshotCapture
    from layer5_defect_detection.priority.page_priority_filter import (
        make_snapshot_artifact,
        resolve_priority_pages,
    )

    output_dir = os.path.join("output", "defect_reports")
    viewport = defect_config.get("viewport", {"width": 1920, "height": 1080})
    max_pages = defect_config.get("max_pages", 10)
    storage_state_path = state.get("storage_state_path")

    priority_pages = resolve_priority_pages(pages, target_url, max_pages)

    logger.info("perf_test_node.visual_capture_start",
                priority_pages=len(priority_pages))

    capture = ScreenshotCapture(target_url, storage_state_path, output_dir, viewport)
    snapshot_artifacts: list[dict] = []

    await capture.start()
    try:
        # Phase 1: Baseline — BEFORE Locust starts (parallel across pages)
        baseline_results = await asyncio.gather(
            *[capture.capture_and_release("baseline", url=p["url"]) for p in priority_pages],
            return_exceptions=True,
        )
        for p, res in zip(priority_pages, baseline_results):
            if isinstance(res, Exception):
                logger.warning("perf_test_node.baseline_capture_failed",
                               url=p["url"], error=str(res))
            else:
                snapshot_artifacts.append(make_snapshot_artifact("baseline", p, res[0]))
        logger.info("perf_test_node.baseline_captured", count=len(priority_pages))

        # Phase 2: Locust + peak capture — concurrent.
        # Peak fires after users are fully ramped (ramp_time * 1.2 or half duration).
        ramp_time = perf_request.load_users / max(perf_request.load_spawn_rate, 0.1)
        peak_delay = max(ramp_time * 1.2, perf_request.load_duration_seconds / 2)

        async def capture_peak() -> list[dict]:
            await asyncio.sleep(peak_delay)
            results = await asyncio.gather(
                *[capture.capture_and_release("peak", url=p["url"]) for p in priority_pages],
                return_exceptions=True,
            )
            artifacts = []
            for p, res in zip(priority_pages, results):
                if isinstance(res, Exception):
                    logger.warning("perf_test_node.peak_capture_failed",
                                   url=p["url"], error=str(res))
                else:
                    artifacts.append(make_snapshot_artifact("peak", p, res[0]))
            return artifacts

        locust_task = asyncio.create_task(
            run_performance_tests(request=perf_request, crawled_pages=pages)
        )
        peak_task = asyncio.create_task(capture_peak())

        locust_result, peak_artifacts = await asyncio.gather(
            locust_task, peak_task, return_exceptions=True
        )

        if isinstance(locust_result, Exception):
            logger.error("perf_test_node.locust_failed", error=str(locust_result))
            result_dict = None
        else:
            logger.info(
                "perf_test_node.complete",
                endpoints_tested=locust_result.endpoints_tested,
                bottlenecks=len(locust_result.bottlenecks),
            )
            result_dict = locust_result.model_dump()

        if isinstance(peak_artifacts, list):
            snapshot_artifacts.extend(peak_artifacts)
            logger.info("perf_test_node.peak_captured", count=len(peak_artifacts))
        else:
            logger.warning("perf_test_node.peak_capture_task_failed",
                           error=str(peak_artifacts))

        # Phase 3: Post-test — AFTER Locust exits (parallel across pages)
        post_results = await asyncio.gather(
            *[capture.capture_and_release("post", url=p["url"]) for p in priority_pages],
            return_exceptions=True,
        )
        for p, res in zip(priority_pages, post_results):
            if isinstance(res, Exception):
                logger.warning("perf_test_node.post_capture_failed",
                               url=p["url"], error=str(res))
            else:
                snapshot_artifacts.append(make_snapshot_artifact("post", p, res[0]))
        logger.info("perf_test_node.post_captured", count=len(priority_pages))

    finally:
        await capture.stop()

    try:
        from layer5_defect_detection.evidence.evidence_builder import EvidenceBuilder
        builder = EvidenceBuilder(output_dir)
        builder.write_priority_pages_manifest(priority_pages, output_dir)
    except Exception as exc:
        logger.warning("perf_test_node.manifest_failed", error=str(exc))

    defect_config["snapshot_artifacts"] = snapshot_artifacts
    return result_dict, defect_config
