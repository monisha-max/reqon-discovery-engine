"""
Results Analyzer — Metric aggregation, bottleneck detection, AI narrative.

After all test runs complete, this module:
  1. Detects bottlenecks per endpoint (slow p99, high error rate, throughput collapse)
  2. Computes degradation factors (how much worse did endpoints get under stress?)
  3. Uses GPT-4o-mini to write a human-readable analysis + recommendations
  4. Returns a finalized PerformanceTestResult
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import structlog

from layer3_performance.models.perf_models import (
    DiscoveredEndpoint,
    EndpointMetrics,
    PerformanceTestResult,
    PerfTestRequest,
    TestRunResult,
    TestType,
)

logger = structlog.get_logger()

# Bottleneck thresholds
P99_SLOW_MS = 2000.0       # p99 > 2s is a bottleneck
ERROR_RATE_HIGH = 0.05     # > 5% errors is a bottleneck
DEGRADATION_FACTOR = 2.5   # stress p99 / load p99 > 2.5x = degradation
RPS_DROP_FACTOR = 0.5      # throughput drops > 50% under stress = bottleneck


class ResultsAnalyzer:
    """Analyzes performance test results to surface bottlenecks and insights."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze(
        self,
        test_runs: list[TestRunResult],
        endpoints: list[DiscoveredEndpoint],
        request: PerfTestRequest,
        script_path: str,
        start_time: float,
    ) -> PerformanceTestResult:
        """
        Full analysis pipeline.
        Returns a complete PerformanceTestResult ready for output.
        """
        import time

        # 1. Flag bottlenecks per endpoint per run
        for run in test_runs:
            for metric in run.endpoint_metrics:
                self._flag_bottleneck(metric)

        # 2. Compute degradation across test types
        if len(test_runs) >= 2:
            self._compute_degradation(test_runs)

        # 3. Collect all bottleneck strings
        bottlenecks = self._collect_bottlenecks(test_runs)

        # 4. AI narrative
        ai_analysis, recommendations = await self._ai_analyze(test_runs, bottlenecks, request)

        total_duration = time.time() - start_time

        return PerformanceTestResult(
            target_url=request.target_url,
            endpoints_discovered=len(endpoints),
            endpoints_tested=self._count_tested(test_runs),
            test_runs=test_runs,
            bottlenecks=bottlenecks,
            ai_analysis=ai_analysis,
            recommendations=recommendations,
            generated_script_path=script_path,
            total_duration_seconds=round(total_duration, 1),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ------------------------------------------------------------------
    # Bottleneck Detection
    # ------------------------------------------------------------------

    def _flag_bottleneck(self, metric: EndpointMetrics):
        """Mark metric as bottleneck if it violates thresholds."""
        reasons = []

        if metric.p99_ms > P99_SLOW_MS:
            reasons.append(f"p99={metric.p99_ms:.0f}ms exceeds {P99_SLOW_MS:.0f}ms threshold")

        if metric.error_rate > ERROR_RATE_HIGH:
            reasons.append(f"error_rate={metric.error_rate:.1%} exceeds {ERROR_RATE_HIGH:.0%} threshold")

        if metric.requests_per_second < 0.5 and metric.total_requests > 10:
            reasons.append(f"throughput critically low ({metric.requests_per_second:.2f} RPS)")

        if reasons:
            metric.is_bottleneck = True
            metric.bottleneck_reason = "; ".join(reasons)

    def _compute_degradation(self, test_runs: list[TestRunResult]):
        """
        Compare load vs stress p99 per endpoint.
        Flag endpoints that degrade significantly under stress.
        """
        load_run = next((r for r in test_runs if r.test_type == TestType.LOAD), None)
        stress_run = next((r for r in test_runs if r.test_type == TestType.STRESS), None)

        if not load_run or not stress_run:
            return

        load_map: dict[str, EndpointMetrics] = {
            f"{m.method}:{m.endpoint}": m for m in load_run.endpoint_metrics
        }

        for stress_metric in stress_run.endpoint_metrics:
            key = f"{stress_metric.method}:{stress_metric.endpoint}"
            load_metric = load_map.get(key)
            if not load_metric or load_metric.p99_ms == 0:
                continue

            factor = stress_metric.p99_ms / load_metric.p99_ms
            stress_metric.degradation_factor = round(factor, 2)

            if factor >= DEGRADATION_FACTOR:
                stress_metric.is_bottleneck = True
                reason = (
                    f"degrades {factor:.1f}x under stress "
                    f"(load p99={load_metric.p99_ms:.0f}ms → stress p99={stress_metric.p99_ms:.0f}ms)"
                )
                if stress_metric.bottleneck_reason:
                    stress_metric.bottleneck_reason += f"; {reason}"
                else:
                    stress_metric.bottleneck_reason = reason

            # RPS collapse detection
            if load_metric.requests_per_second > 0:
                rps_factor = stress_metric.requests_per_second / load_metric.requests_per_second
                if rps_factor < RPS_DROP_FACTOR and load_metric.requests_per_second > 1:
                    stress_metric.is_bottleneck = True
                    rps_reason = (
                        f"throughput collapsed under stress "
                        f"({load_metric.requests_per_second:.1f} → {stress_metric.requests_per_second:.1f} RPS)"
                    )
                    if stress_metric.bottleneck_reason:
                        stress_metric.bottleneck_reason += f"; {rps_reason}"
                    else:
                        stress_metric.bottleneck_reason = rps_reason

    def _collect_bottlenecks(self, test_runs: list[TestRunResult]) -> list[str]:
        """Build a deduplicated list of human-readable bottleneck strings."""
        seen: set[str] = set()
        bottlenecks: list[str] = []

        for run in test_runs:
            for metric in run.endpoint_metrics:
                if metric.is_bottleneck and metric.bottleneck_reason:
                    entry = f"[{run.test_type.value.upper()}] {metric.method} {metric.endpoint}: {metric.bottleneck_reason}"
                    if entry not in seen:
                        seen.add(entry)
                        bottlenecks.append(entry)

        return bottlenecks

    def _count_tested(self, test_runs: list[TestRunResult]) -> int:
        if not test_runs:
            return 0
        endpoints = {f"{m.method}:{m.endpoint}" for run in test_runs for m in run.endpoint_metrics}
        return len(endpoints)

    # ------------------------------------------------------------------
    # AI Narrative Analysis
    # ------------------------------------------------------------------

    async def _ai_analyze(
        self,
        test_runs: list[TestRunResult],
        bottlenecks: list[str],
        request: PerfTestRequest,
    ) -> tuple[str, list[str]]:
        """Use GPT-4o-mini to write a narrative analysis and recommendations."""
        from config.settings import settings
        if not settings.OPENAI_API_KEY:
            return self._rule_based_analysis(test_runs, bottlenecks), []

        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

            # Build a compact summary for the prompt
            summary = self._build_summary_for_llm(test_runs)

            prompt = f"""You are a performance engineering expert analyzing load test results for a web application.

Target: {request.target_url}
Test types run: {[r.test_type.value for r in test_runs]}

Test Summary:
{json.dumps(summary, indent=2)}

Detected Bottlenecks:
{chr(10).join(bottlenecks) if bottlenecks else "No bottlenecks detected."}

Provide:
1. A concise executive summary (2-3 sentences) of the system's performance profile
2. Root cause analysis of each bottleneck (if any)
3. Scalability assessment: how many concurrent users can this system reliably handle?
4. Exactly 3 actionable recommendations to improve performance

Respond ONLY with JSON (no markdown):
{{
  "analysis": "Your executive summary + root cause analysis here...",
  "recommendations": [
    "Recommendation 1",
    "Recommendation 2",
    "Recommendation 3"
  ]
}}"""

            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=700,
            )

            content = response.choices[0].message.content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]

            result = json.loads(content)
            analysis = result.get("analysis", "")
            recommendations = result.get("recommendations", [])

            logger.info("results_analyzer.ai_analysis_complete")
            return analysis, recommendations

        except Exception as e:
            logger.warning("results_analyzer.ai_failed", error=str(e))
            return self._rule_based_analysis(test_runs, bottlenecks), []

    def _build_summary_for_llm(self, test_runs: list[TestRunResult]) -> list[dict]:
        summary = []
        for run in test_runs:
            bottleneck_eps = [
                {
                    "endpoint": f"{m.method} {m.endpoint}",
                    "p99_ms": m.p99_ms,
                    "error_rate": f"{m.error_rate:.1%}",
                    "rps": round(m.requests_per_second, 2),
                    "degradation": m.degradation_factor,
                }
                for m in run.endpoint_metrics if m.is_bottleneck
            ]
            summary.append({
                "test_type": run.test_type.value,
                "peak_users": run.peak_users,
                "duration_seconds": run.duration_seconds,
                "total_requests": run.total_requests,
                "overall_error_rate": f"{run.overall_error_rate:.1%}",
                "overall_rps": round(run.overall_rps, 2),
                "bottleneck_endpoints": bottleneck_eps,
            })
        return summary

    def _rule_based_analysis(self, test_runs: list[TestRunResult], bottlenecks: list[str]) -> str:
        """Fallback analysis when LLM is unavailable."""
        if not test_runs:
            return "No test runs completed."

        lines = []
        for run in test_runs:
            lines.append(
                f"{run.test_type.value.title()} test: {run.total_requests} requests, "
                f"{run.overall_error_rate:.1%} error rate, "
                f"{run.overall_rps:.1f} RPS @ {run.peak_users} users."
            )

        if bottlenecks:
            lines.append(f"Detected {len(bottlenecks)} bottleneck(s).")
        else:
            lines.append("No bottlenecks detected within test thresholds.")

        return " ".join(lines)
