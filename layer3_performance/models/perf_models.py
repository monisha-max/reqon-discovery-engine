"""
Performance Testing Models — Pydantic v2 data structures for Layer 3.

Covers the full lifecycle:
  DiscoveredEndpoint  → what to test
  EndpointMetrics     → per-endpoint results
  TestRunResult       → one test run (load / stress / soak)
  PerformanceTestResult → final aggregated output
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TestType(str, Enum):
    LOAD   = "load"    # Normal expected traffic — baseline metrics
    STRESS = "stress"  # Ramp beyond capacity — find the breaking point
    SOAK   = "soak"    # Sustained moderate load — detect memory leaks / degradation


class EndpointSource(str, Enum):
    OPENAPI   = "openapi"    # Parsed from Swagger / OpenAPI spec
    CRAWL     = "crawl"      # Discovered from Layer 2 crawled pages (URL pattern)
    HTML_FORM = "html_form"  # Extracted from <form action="..."> elements
    JS_FETCH  = "js_fetch"   # Inferred from JS fetch/XHR calls in page source
    AI_INFER  = "ai_infer"   # Suggested by GPT-4o-mini from page content


# ---------------------------------------------------------------------------
# Endpoint Discovery
# ---------------------------------------------------------------------------

class PathParameter(BaseModel):
    name: str
    location: str = "path"   # "path", "query", "header", "body"
    required: bool = True
    schema_type: str = "string"
    example: Optional[str] = None


class DiscoveredEndpoint(BaseModel):
    """A single HTTP endpoint to be performance-tested."""
    url: str                          # Full URL with sample path params filled in
    method: str                       # GET, POST, PUT, DELETE, PATCH
    path_template: str                # e.g. /api/users/{id}
    source: EndpointSource = EndpointSource.CRAWL
    parameters: list[PathParameter] = Field(default_factory=list)
    request_schema: Optional[dict] = None    # JSON schema from OpenAPI spec
    sample_payload: Optional[dict] = None    # AI-generated realistic payload
    sample_headers: dict[str, str] = Field(default_factory=dict)
    auth_required: bool = False
    priority: float = 0.5             # 0.0–1.0, drives task weighting in Locust
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-Endpoint Metrics (one test run)
# ---------------------------------------------------------------------------

class EndpointMetrics(BaseModel):
    """Aggregated performance metrics for a single endpoint in one test run."""
    endpoint: str            # path template, e.g. /api/products
    method: str

    # Response time percentiles (milliseconds)
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    mean_ms: float = 0.0

    # Reliability
    total_requests: int = 0
    error_count: int = 0
    error_rate: float = 0.0          # 0.0–1.0

    # Throughput
    requests_per_second: float = 0.0

    # Status code breakdown  {"200": 95, "500": 5}
    status_codes: dict[str, int] = Field(default_factory=dict)

    # Degradation across test phases (populated by analyzer)
    degradation_factor: Optional[float] = None  # p99_stress / p99_load ratio

    # Bottleneck flags (set by ResultsAnalyzer)
    is_bottleneck: bool = False
    bottleneck_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# One Complete Test Run
# ---------------------------------------------------------------------------

class TestRunResult(BaseModel):
    """Results of a single test type (load, stress, or soak)."""
    test_type: TestType
    duration_seconds: int
    peak_users: int
    spawn_rate: float               # users/second ramp-up rate

    # Aggregate across all endpoints
    total_requests: int = 0
    overall_error_rate: float = 0.0
    overall_rps: float = 0.0
    peak_rps: float = 0.0

    # Per-endpoint breakdown
    endpoint_metrics: list[EndpointMetrics] = Field(default_factory=list)

    # Raw Locust stats snapshot (for debugging / export)
    raw_stats: Optional[dict] = None


# ---------------------------------------------------------------------------
# Final Performance Test Result
# ---------------------------------------------------------------------------

class PerformanceTestResult(BaseModel):
    """Top-level result returned by the performance testing layer."""
    target_url: str
    endpoints_discovered: int
    endpoints_tested: int

    # One entry per test type executed
    test_runs: list[TestRunResult] = Field(default_factory=list)

    # Bottleneck summary (endpoint + reason strings)
    bottlenecks: list[str] = Field(default_factory=list)

    # AI-generated narrative: bottleneck analysis + recommendations
    ai_analysis: str = ""
    recommendations: list[str] = Field(default_factory=list)

    # Generated Locust script (saved to disk)
    generated_script_path: str = ""

    # Timing
    total_duration_seconds: float = 0.0
    timestamp: str = ""


# ---------------------------------------------------------------------------
# Performance Test Request (input config)
# ---------------------------------------------------------------------------

class PerfTestRequest(BaseModel):
    """Input configuration for a performance test run."""
    target_url: str
    openapi_spec_path: Optional[str] = None   # path or URL to swagger.json / openapi.yaml
    test_types: list[TestType] = Field(default_factory=lambda: [TestType.LOAD])

    # Load test profile
    load_users: int = 50
    load_spawn_rate: float = 5.0
    load_duration_seconds: int = 300          # 5 minutes

    # Stress test profile
    stress_max_users: int = 300
    stress_spawn_rate: float = 10.0
    stress_duration_seconds: int = 600        # 10 minutes

    # Soak test profile
    soak_users: int = 25
    soak_duration_seconds: int = 1800         # 30 minutes

    # Auth (reuse from orchestrator)
    auth_headers: dict[str, str] = Field(default_factory=dict)
    storage_state_path: Optional[str] = None
