"""
Load Engine — Subprocess-based Locust test runner.

Root cause of previous hang: Locust uses gevent which monkey-patches ssl/socket at
import time. Calling gevent.sleep() inside an asyncio coroutine blocks the entire
asyncio event loop (gevent and asyncio are incompatible in the same thread).

Fix: Run Locust as a subprocess via asyncio.create_subprocess_exec().
  - Fully isolates gevent from our asyncio event loop
  - Locust writes CSV stats files we parse for metrics
  - asyncio.wait_for() gives us a clean timeout/cancel mechanism

Locust subprocess command:
  python -m locust --headless --host <url> --users <n> --spawn-rate <r>
                   --run-time <duration>s --csv <prefix> -f <script>

CSV output (parsed for metrics):
  {prefix}_stats.csv     → per-endpoint p50/p90/p95/p99, RPS, errors
  {prefix}_failures.csv  → error details per endpoint
"""
from __future__ import annotations

import asyncio
import csv
import os
import sys
import time
from pathlib import Path
from typing import Optional

import structlog

from layer3_performance.models.perf_models import (
    DiscoveredEndpoint,
    EndpointMetrics,
    PerfTestRequest,
    TestRunResult,
    TestType,
)

logger = structlog.get_logger()

# Locust CSV column names (from _stats.csv)
_COL_TYPE           = "Type"
_COL_NAME           = "Name"
_COL_REQUEST_COUNT  = "Request Count"
_COL_FAILURE_COUNT  = "Failure Count"
_COL_MEDIAN_RT      = "Median Response Time"
_COL_AVG_RT         = "Average Response Time"
_COL_MIN_RT         = "Min Response Time"
_COL_MAX_RT         = "Max Response Time"
_COL_RPS            = "Requests/s"
_COL_FAILURES_S     = "Failures/s"
_COL_P50            = "50%"
_COL_P90            = "90%"
_COL_P95            = "95%"
_COL_P99            = "99%"


class LoadEngine:
    """
    Subprocess-based Locust runner.
    Each test type (load/stress/soak) spawns an isolated locust process,
    collects CSV output, and parses it into TestRunResult.
    """

    def __init__(self, base_url: str, auth_headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.auth_headers = auth_headers or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_all(
        self,
        request: PerfTestRequest,
        script_path: str,
        endpoints: list[DiscoveredEndpoint],
    ) -> list[TestRunResult]:
        """Run all requested test types sequentially and return results."""
        results = []
        for test_type in request.test_types:
            logger.info("load_engine.starting_test", test_type=test_type.value)
            try:
                result = await self._run_test(test_type, request, script_path)
                results.append(result)
                logger.info(
                    "load_engine.test_complete",
                    test_type=test_type.value,
                    rps=round(result.overall_rps, 2),
                    error_rate=f"{result.overall_error_rate:.1%}",
                    total_requests=result.total_requests,
                )
            except Exception as e:
                logger.error("load_engine.test_failed", test_type=test_type.value, error=str(e))

        return results

    # ------------------------------------------------------------------
    # Core Subprocess Runner
    # ------------------------------------------------------------------

    async def _run_test(
        self,
        test_type: TestType,
        request: PerfTestRequest,
        script_path: str,
    ) -> TestRunResult:
        """Spawn a Locust subprocess and wait for it to finish."""

        # Resolve test profile
        if test_type == TestType.LOAD:
            users       = request.load_users
            spawn_rate  = request.load_spawn_rate
            duration    = request.load_duration_seconds
        elif test_type == TestType.STRESS:
            users       = request.stress_max_users
            spawn_rate  = request.stress_spawn_rate
            duration    = request.stress_duration_seconds
        else:  # SOAK
            users       = request.soak_users
            spawn_rate  = max(1.0, request.soak_users / 30.0)
            duration    = request.soak_duration_seconds

        # CSV output prefix — one file set per test type
        output_dir = os.path.dirname(script_path) or "output"
        csv_prefix = os.path.join(output_dir, f"locust_{test_type.value}")

        cmd = [
            sys.executable, "-m", "locust",
            "--headless",
            "--host", self.base_url,
            "--users", str(users),
            "--spawn-rate", str(spawn_rate),
            "--run-time", f"{duration}s",
            "--csv", csv_prefix,
            "--csv-full-history",
            "-f", script_path,
            "--loglevel", "WARNING",   # suppress per-request noise
        ]

        logger.info(
            "load_engine.subprocess_start",
            test_type=test_type.value,
            users=users,
            spawn_rate=spawn_rate,
            duration=duration,
        )

        start_time = time.time()

        # Subprocess timeout = test duration + 60s grace period
        timeout = duration + 60

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Stream stderr in real time so the user sees progress
            async def _log_stderr():
                async for line in process.stderr:
                    text = line.decode("utf-8", errors="replace").strip()
                    if text:
                        logger.info("locust", msg=text[:200])

            stderr_task = asyncio.create_task(_log_stderr())

            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("load_engine.timeout_killing_process", test_type=test_type.value)
                process.kill()
                await process.wait()
            finally:
                stderr_task.cancel()

        except FileNotFoundError:
            raise RuntimeError(
                "Locust not found. Install it: pip install locust"
            )

        elapsed = time.time() - start_time

        # Parse CSV output
        return self._parse_csv_results(
            csv_prefix=csv_prefix,
            test_type=test_type,
            peak_users=users,
            spawn_rate=spawn_rate,
            duration=int(elapsed),
        )

    # ------------------------------------------------------------------
    # CSV Parsing
    # ------------------------------------------------------------------

    def _parse_csv_results(
        self,
        csv_prefix: str,
        test_type: TestType,
        peak_users: int,
        spawn_rate: float,
        duration: int,
    ) -> TestRunResult:
        """Parse Locust _stats.csv and _failures.csv into TestRunResult."""

        stats_file    = f"{csv_prefix}_stats.csv"
        failures_file = f"{csv_prefix}_failures.csv"

        endpoint_metrics: list[EndpointMetrics] = []
        total_requests = 0
        total_errors   = 0
        overall_rps    = 0.0
        peak_rps       = 0.0

        # Parse failures for status-code breakdown
        failure_map: dict[str, dict[str, int]] = {}  # key → {status_code: count}
        if os.path.exists(failures_file):
            failure_map = self._parse_failures_csv(failures_file)

        if not os.path.exists(stats_file):
            logger.warning("load_engine.no_csv_output", file=stats_file)
            return TestRunResult(
                test_type=test_type,
                duration_seconds=duration,
                peak_users=peak_users,
                spawn_rate=spawn_rate,
            )

        with open(stats_file, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                name = row.get(_COL_NAME, "").strip()
                req_type = row.get(_COL_TYPE, "GET").strip()

                # Skip the "Aggregated" summary row — we build our own
                if name.lower() in ("aggregated", ""):
                    agg_rps = self._safe_float(row.get(_COL_RPS, "0"))
                    overall_rps = agg_rps
                    peak_rps    = agg_rps
                    continue

                req_count  = self._safe_int(row.get(_COL_REQUEST_COUNT, "0"))
                fail_count = self._safe_int(row.get(_COL_FAILURE_COUNT, "0"))

                if req_count == 0:
                    continue

                p50  = self._safe_float(row.get(_COL_P50, row.get(_COL_MEDIAN_RT, "0")))
                p90  = self._safe_float(row.get(_COL_P90, "0"))
                p95  = self._safe_float(row.get(_COL_P95, "0"))
                p99  = self._safe_float(row.get(_COL_P99, "0"))
                rps  = self._safe_float(row.get(_COL_RPS, "0"))
                min_rt = self._safe_float(row.get(_COL_MIN_RT, "0"))
                max_rt = self._safe_float(row.get(_COL_MAX_RT, "0"))
                avg_rt = self._safe_float(row.get(_COL_AVG_RT, "0"))

                err_rate   = fail_count / req_count if req_count > 0 else 0.0
                status_map = failure_map.get(f"{req_type}:{name}", {})

                total_requests += req_count
                total_errors   += fail_count
                peak_rps = max(peak_rps, rps)

                endpoint_metrics.append(EndpointMetrics(
                    endpoint=name,
                    method=req_type,
                    p50_ms=p50,
                    p90_ms=p90,
                    p95_ms=p95,
                    p99_ms=p99,
                    min_ms=min_rt,
                    max_ms=max_rt,
                    mean_ms=avg_rt,
                    total_requests=req_count,
                    error_count=fail_count,
                    error_rate=err_rate,
                    requests_per_second=rps,
                    status_codes=status_map,
                ))

        overall_error_rate = (total_errors / total_requests) if total_requests > 0 else 0.0

        return TestRunResult(
            test_type=test_type,
            duration_seconds=duration,
            peak_users=peak_users,
            spawn_rate=spawn_rate,
            total_requests=total_requests,
            overall_error_rate=overall_error_rate,
            overall_rps=overall_rps or sum(m.requests_per_second for m in endpoint_metrics),
            peak_rps=peak_rps,
            endpoint_metrics=endpoint_metrics,
        )

    def _parse_failures_csv(self, failures_file: str) -> dict[str, dict[str, int]]:
        """Parse _failures.csv → {method:name: {error_code: count}}."""
        failure_map: dict[str, dict[str, int]] = {}
        try:
            with open(failures_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    method = row.get("Method", "GET").strip()
                    name   = row.get("Name", "").strip()
                    error  = row.get("Error", "").strip()
                    count  = self._safe_int(row.get("Occurrences", "1"))
                    key    = f"{method}:{name}"
                    # Extract HTTP status code from error string if present
                    code = error.split(" ")[0] if error else "error"
                    if key not in failure_map:
                        failure_map[key] = {}
                    failure_map[key][code] = failure_map[key].get(code, 0) + count
        except Exception as e:
            logger.warning("load_engine.failures_parse_error", error=str(e))
        return failure_map

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_float(val: str) -> float:
        try:
            return float(val) if val and val.strip() not in ("", "N/A") else 0.0
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _safe_int(val: str) -> int:
        try:
            return int(val) if val and val.strip() not in ("", "N/A") else 0
        except (ValueError, TypeError):
            return 0
