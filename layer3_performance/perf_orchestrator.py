"""
Performance Orchestrator — coordinates the full 4-step performance testing flow.

Step 1: EndpointDiscoverer  → finds all testable endpoints
Step 2: PayloadGenerator    → populates realistic request bodies
Step 3: ScriptGenerator     → AI writes Locust HttpUser script
Step 4: LoadEngine          → runs load / stress / soak tests
Step 5: ResultsAnalyzer     → aggregates metrics, flags bottlenecks, AI narrative

Called by the LangGraph perf_test_node after the crawl is complete.
"""
from __future__ import annotations

import time
from typing import Optional

import structlog

from layer3_performance.analyzers.results_analyzer import ResultsAnalyzer
from layer3_performance.discovery.endpoint_discoverer import EndpointDiscoverer
from layer3_performance.discovery.payload_generator import PayloadGenerator
from layer3_performance.engines.load_engine import LoadEngine
from layer3_performance.engines.script_generator import ScriptGenerator
from layer3_performance.models.perf_models import (
    PerformanceTestResult,
    PerfTestRequest,
    TestType,
)

logger = structlog.get_logger()


async def run_performance_tests(
    request: PerfTestRequest,
    crawled_pages: list[dict],
    output_dir: str = "output",
) -> PerformanceTestResult:
    """
    Main entry point for Layer 3.

    Args:
        request:       PerfTestRequest config (target URL, test types, user counts, etc.)
        crawled_pages: PageData dicts from Layer 2 — used for endpoint discovery
        output_dir:    Where to save Locust script and perf_result.json

    Returns:
        PerformanceTestResult with all metrics, bottlenecks, and AI analysis
    """
    start_time = time.time()
    logger.info(
        "perf_orchestrator.starting",
        target=request.target_url,
        test_types=[t.value for t in request.test_types],
    )

    # ------------------------------------------------------------------
    # Step 1: Endpoint Discovery
    # ------------------------------------------------------------------
    discoverer = EndpointDiscoverer(base_url=request.target_url)

    if request.openapi_spec_path:
        logger.info("perf_orchestrator.discovery_mode", mode="openapi_spec")
        endpoints = await discoverer.discover_from_spec(request.openapi_spec_path)

        # Fall back to page-based if spec yields nothing
        if not endpoints:
            logger.warning("perf_orchestrator.spec_empty_fallback_to_pages")
            endpoints = await discoverer.discover_from_pages(crawled_pages)
    else:
        logger.info("perf_orchestrator.discovery_mode", mode="crawled_pages")
        endpoints = await discoverer.discover_from_pages(crawled_pages)

    if not endpoints:
        # Last resort: just use the target URL itself as a GET endpoint
        from layer3_performance.models.perf_models import DiscoveredEndpoint, EndpointSource
        endpoints = [DiscoveredEndpoint(
            url=request.target_url,
            method="GET",
            path_template="/",
            source=EndpointSource.CRAWL,
            priority=0.5,
        )]
        logger.warning("perf_orchestrator.no_endpoints_found_using_root")

    logger.info("perf_orchestrator.endpoints_discovered", count=len(endpoints))

    # ------------------------------------------------------------------
    # Step 2: Payload Generation
    # ------------------------------------------------------------------
    logger.info("perf_orchestrator.generating_payloads")
    payload_gen = PayloadGenerator()
    endpoints = await payload_gen.generate_for_endpoints(endpoints)

    # ------------------------------------------------------------------
    # Step 3: Script Generation
    # ------------------------------------------------------------------
    logger.info("perf_orchestrator.generating_script")
    script_gen = ScriptGenerator(output_dir=output_dir)
    script_path = await script_gen.generate(
        endpoints=endpoints,
        base_url=request.target_url,
        auth_headers=request.auth_headers or None,
    )

    # ------------------------------------------------------------------
    # Step 4: Test Execution
    # ------------------------------------------------------------------
    engine = LoadEngine(
        base_url=request.target_url,
        auth_headers=request.auth_headers,
    )
    test_runs = await engine.run_all(request, script_path, endpoints)

    # ------------------------------------------------------------------
    # Step 5: Analysis
    # ------------------------------------------------------------------
    logger.info("perf_orchestrator.analyzing_results")
    analyzer = ResultsAnalyzer()
    result = await analyzer.analyze(
        test_runs=test_runs,
        endpoints=endpoints,
        request=request,
        script_path=script_path,
        start_time=start_time,
    )

    logger.info(
        "perf_orchestrator.complete",
        endpoints_tested=result.endpoints_tested,
        bottlenecks=len(result.bottlenecks),
        duration_s=result.total_duration_seconds,
    )

    return result
