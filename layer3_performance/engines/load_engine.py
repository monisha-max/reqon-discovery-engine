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
    SoakDegradation,
    SoakTrendPoint,
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

        # Optional warmup pass before any timed test
        if request.warmup_requests > 0:
            await self._run_warmup(request, endpoints)

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

    async def _run_warmup(
        self,
        request: PerfTestRequest,
        endpoints: list[DiscoveredEndpoint],
    ) -> None:
        """
        Send warmup_requests sequential GET/HEAD requests to each endpoint
        to populate server caches before the timed test begins.

        Uses httpx so we don't spin up a full Locust process.
        """
        import httpx
        n = request.warmup_requests
        logger.info("load_engine.warmup_start", requests_per_endpoint=n, endpoints=len(endpoints))

        headers = dict(self.auth_headers)
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=10.0,
            follow_redirects=True,
        ) as client:
            for ep in endpoints:
                path = ep.path_template
                # Replace {param} placeholders with a safe literal value
                path = path.replace("{id}", "1").replace("{uuid}", "00000000-0000-0000-0000-000000000001")
                # Only warm up GET endpoints (avoid side effects on POST/PUT)
                if ep.method not in ("GET", "HEAD"):
                    continue
                for _ in range(n):
                    try:
                        await client.get(path)
                    except Exception:
                        pass  # Warmup failures are non-fatal

        logger.info("load_engine.warmup_complete")

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
        result = self._parse_csv_results(
            csv_prefix=csv_prefix,
            test_type=test_type,
            peak_users=users,
            spawn_rate=spawn_rate,
            duration=int(elapsed),
        )

        # For soak tests: enrich with time-series degradation analysis
        if test_type == TestType.SOAK:
            history_file = f"{csv_prefix}_stats_history.csv"
            if os.path.exists(history_file):
                result.soak_trend, result.soak_degradations = (
                    self._analyze_soak_history(history_file)
                )

        return result

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
    # Soak Degradation Analysis
    # ------------------------------------------------------------------

    def _analyze_soak_history(
        self, history_file: str
    ) -> tuple[list[SoakTrendPoint], list[SoakDegradation]]:
        """
        Parse Locust _stats_history.csv and detect time-series degradation.

        Returns (trend_points, degradation_list).
        Degradation is detected when the linear slope of p95 exceeds
        _SOAK_P95_SLOPE_THRESHOLD_MS_PER_MIN (default: 50ms/min).
        """
        _SOAK_P95_SLOPE_THRESHOLD = 50.0   # ms per minute = degrading

        # Build per-endpoint time series: {method:name → [(ts, p95, p99, rps, err_rate)]}
        series: dict[str, list[tuple[float, float, float, float, float]]] = {}

        try:
            with open(history_file, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = row.get("Name", "").strip()
                    if not name or name.lower() == "aggregated":
                        continue
                    method = row.get("Type", "GET").strip()

                    # History CSV uses Unix timestamp or elapsed seconds
                    ts_raw = row.get("Timestamp", "0")
                    try:
                        ts = float(ts_raw)
                    except ValueError:
                        continue

                    p95  = self._safe_float(row.get("95%", "0"))
                    p99  = self._safe_float(row.get("99%", "0"))
                    rps  = self._safe_float(row.get("Requests/s", "0"))

                    req_count = self._safe_int(row.get("Request Count", "0"))
                    fail_count = self._safe_int(row.get("Failure Count", "0"))
                    err_rate = fail_count / req_count if req_count > 0 else 0.0

                    key = f"{method}:{name}"
                    series.setdefault(key, []).append((ts, p95, p99, rps, err_rate))

        except Exception as exc:
            logger.warning("load_engine.history_parse_failed", error=str(exc))
            return [], []

        # Build aggregate trend (all endpoints combined, by timestamp)
        ts_map: dict[float, list[float]] = {}
        for pts in series.values():
            for ts, p95, p99, rps, _ in pts:
                ts_map.setdefault(ts, []).append(p95)

        trend_points = [
            SoakTrendPoint(timestamp=ts, p95_ms=round(sum(vals)/len(vals), 1))
            for ts, vals in sorted(ts_map.items())
        ]

        # Per-endpoint degradation via linear regression
        degradations: list[SoakDegradation] = []
        for key, pts in series.items():
            if len(pts) < 4:
                continue
            method, _, name = key.partition(":")
            pts.sort(key=lambda x: x[0])
            ts0 = pts[0][0]
            # x = minutes elapsed, y = p95
            xs = [(p[0] - ts0) / 60.0 for p in pts]
            ys = [p[1] for p in pts]

            slope = self._linear_slope(xs, ys)   # ms/minute

            start_p95 = ys[0]
            end_p95   = ys[-1]
            is_degrading = slope > _SOAK_P95_SLOPE_THRESHOLD

            if is_degrading or slope > 10.0:  # also record moderate slopes for context
                summary = (
                    f"p95 increased at {slope:.1f}ms/min over the soak test "
                    f"({start_p95:.0f}ms → {end_p95:.0f}ms). "
                    + ("Indicates memory leak or connection pool exhaustion." if is_degrading else "Minor upward trend.")
                )
                degradations.append(SoakDegradation(
                    endpoint=name,
                    method=method,
                    p95_slope_ms_per_min=round(slope, 2),
                    p99_slope_ms_per_min=round(
                        self._linear_slope(xs, [p[2] for p in pts]), 2
                    ),
                    start_p95_ms=round(start_p95, 1),
                    end_p95_ms=round(end_p95, 1),
                    is_degrading=is_degrading,
                    degradation_summary=summary,
                ))

        logger.info(
            "load_engine.soak_analysis_done",
            trend_points=len(trend_points),
            degrading=sum(1 for d in degradations if d.is_degrading),
        )
        return trend_points, degradations

    @staticmethod
    def _linear_slope(xs: list[float], ys: list[float]) -> float:
        """Compute slope of the best-fit line (least squares) for lists xs, ys."""
        n = len(xs)
        if n < 2:
            return 0.0
        sum_x  = sum(xs)
        sum_y  = sum(ys)
        sum_xy = sum(x * y for x, y in zip(xs, ys))
        sum_x2 = sum(x * x for x in xs)
        denom  = n * sum_x2 - sum_x ** 2
        if denom == 0:
            return 0.0
        return (n * sum_xy - sum_x * sum_y) / denom

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
