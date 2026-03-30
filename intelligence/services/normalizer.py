from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

from intelligence.models.contracts import (
    Dimension,
    DiscoveryScanBundle,
    Element,
    Issue,
    Page,
    PerformanceSnapshot,
    ScanRequest,
    Severity,
)
from intelligence.services.identity import build_application_key


_PERF_BOTTLENECK_PATTERN = re.compile(
    r"\[(?P<test_type>[A-Z]+)\]\s+(?P<method>[A-Z]+)\s+(?P<path>\S+):\s*(?P<detail>.+)"
)


def build_discovery_bundle(
    *,
    final_state: dict,
    target_url: str,
    scan_id: str,
    scanned_at: datetime | None = None,
) -> DiscoveryScanBundle:
    return DiscoveryScanBundle(
        target_url=target_url,
        scan_id=scan_id,
        scanned_at=scanned_at or datetime.now(timezone.utc),
        pages=list(final_state.get("pages") or []),
        perf_result=final_state.get("perf_result"),
        defect_result=final_state.get("defect_result"),
        scan_metadata={
            "coverage_score": final_state.get("coverage_score", 0.0),
            "page_type_distribution": final_state.get("page_type_distribution", {}),
            "iterations": final_state.get("iteration", 0),
            "errors": list(final_state.get("errors") or []),
        },
    )


def normalize_discovery_bundle(bundle: DiscoveryScanBundle) -> ScanRequest:
    parsed = urlparse(bundle.target_url)
    application_name = parsed.hostname or bundle.target_url
    application_key = build_application_key(bundle.target_url)

    page_index: dict[str, dict] = {}
    page_order: list[str] = []

    for raw_page in bundle.pages:
        url = str(raw_page.get("url") or bundle.target_url)
        page_index[url] = {
            "url": url,
            "title": raw_page.get("title"),
            "page_type": _page_type_value(raw_page.get("page_type")),
            "performance_snapshot": _build_page_performance_snapshot(raw_page),
            "issues_by_selector": defaultdict(list),
            "evidence": {
                "page_type": _page_type_value(raw_page.get("page_type")),
                "screenshot_path": raw_page.get("screenshot_path"),
            },
        }
        page_order.append(url)
        _map_accessibility_issues(page_index[url], raw_page)
        _map_console_errors(page_index[url], raw_page)
        _map_failed_requests(page_index[url], raw_page)

    _map_perf_result(page_index, page_order, bundle)
    _map_defect_result(page_index, page_order, bundle)

    if not page_index:
        page_index[bundle.target_url] = {
            "url": bundle.target_url,
            "title": application_name,
            "page_type": "unknown",
            "performance_snapshot": None,
            "issues_by_selector": defaultdict(list),
            "evidence": {"page_type": "unknown"},
        }
        page_order.append(bundle.target_url)

    pages = [
        Page(
            url=page_index[url]["url"],
            title=page_index[url]["title"],
            page_type=page_index[url]["page_type"],
            performance_snapshot=page_index[url]["performance_snapshot"],
            evidence=page_index[url]["evidence"],
            elements=[
                Element(selector=selector, issues=issues)
                for selector, issues in page_index[url]["issues_by_selector"].items()
                if issues
            ],
        )
        for url in page_order
    ]

    return ScanRequest(
        scan_id=bundle.scan_id,
        application_name=application_name,
        application_key=application_key,
        scanned_at=bundle.scanned_at,
        pages=pages,
        metadata=bundle.scan_metadata,
    )


def _ensure_page(page_index: dict[str, dict], page_order: list[str], url: str, title: str | None = None) -> dict:
    if url not in page_index:
        page_index[url] = {
            "url": url,
            "title": title,
            "page_type": "unknown",
            "performance_snapshot": None,
            "issues_by_selector": defaultdict(list),
            "evidence": {"page_type": "unknown"},
        }
        page_order.append(url)
    elif title and not page_index[url]["title"]:
        page_index[url]["title"] = title
    return page_index[url]


def _page_type_value(page_type) -> str:
    if page_type is None:
        return "unknown"
    return getattr(page_type, "value", str(page_type))


def _map_accessibility_issues(page_record: dict, raw_page: dict) -> None:
    accessibility = raw_page.get("accessibility") or {}
    for violation in accessibility.get("violations") or []:
        selector = violation.get("target_selector") or "__page__"
        page_record["issues_by_selector"][selector].append(
            Issue(
                category=violation.get("rule_id", "accessibility_violation"),
                severity=_map_accessibility_severity(violation.get("impact", "")),
                dimension=Dimension.ACCESSIBILITY,
                message=violation.get("description") or "Accessibility violation detected",
                source_type="crawl",
                evidence={
                    "html_snippet": violation.get("html_snippet", ""),
                    "impact": violation.get("impact", ""),
                },
            )
        )


def _map_console_errors(page_record: dict, raw_page: dict) -> None:
    for error in raw_page.get("console_errors") or []:
        page_record["issues_by_selector"]["__console__"].append(
            Issue(
                category="console_error",
                severity=Severity.MAJOR,
                dimension=Dimension.FUNCTIONAL,
                message=str(error),
                source_type="crawl",
                evidence={},
            )
        )


def _map_failed_requests(page_record: dict, raw_page: dict) -> None:
    for failure in raw_page.get("failed_requests") or []:
        method = failure.get("method", "GET")
        url = failure.get("url", "")
        selector = f"__endpoint__:{method} {url or page_record['url']}"
        message = failure.get("error") or failure.get("status_text") or "Network request failed"
        page_record["issues_by_selector"]["__network__"].append(
            Issue(
                category="network_failure",
                severity=Severity.MAJOR,
                dimension=Dimension.FUNCTIONAL,
                message=f"{method} {url} - {message}".strip(),
                source_type="crawl",
                evidence=failure,
            )
        )


def _build_page_performance_snapshot(raw_page: dict) -> PerformanceSnapshot | None:
    performance = raw_page.get("performance") or {}
    load_time_ms = raw_page.get("load_time_ms")

    if not performance and load_time_ms is None:
        return None

    scalability = _score_lower_is_better(
        performance.get("ttfb_ms") or load_time_ms,
        good=150,
        bad=2500,
    )
    responsiveness = _average(
        _score_lower_is_better(performance.get("lcp_ms"), good=1200, bad=4500),
        _score_lower_is_better(performance.get("fcp_ms"), good=900, bad=3500),
        _score_lower_is_better(load_time_ms, good=1000, bad=6000),
    )
    stability = _average(
        _score_lower_is_better(performance.get("cls"), good=0.02, bad=0.35),
        _score_higher_is_better(1.0 - min(len(raw_page.get("console_errors") or []) / 10.0, 1.0), 0.3, 1.0),
        _score_higher_is_better(1.0 - min(len(raw_page.get("failed_requests") or []) / 10.0, 1.0), 0.3, 1.0),
    )

    return PerformanceSnapshot(
        scalability=round(scalability, 2),
        responsiveness=round(responsiveness, 2),
        stability=round(stability, 2),
    )


def _map_perf_result(page_index: dict[str, dict], page_order: list[str], bundle: DiscoveryScanBundle) -> None:
    perf_result = bundle.perf_result or {}
    bottlenecks = perf_result.get("bottlenecks") or []
    if not bottlenecks:
        return

    for bottleneck in bottlenecks:
        raw_text = str(bottleneck)
        match = _PERF_BOTTLENECK_PATTERN.search(raw_text)
        method = "GET"
        path = "/"
        if match:
            method = match.group("method")
            path = match.group("path")

        page_url = _page_url_for_path(page_index, page_order, bundle.target_url, path)
        page_record = _ensure_page(page_index, page_order, page_url)
        selector = f"__endpoint__:{method} {path}"
        page_record["issues_by_selector"][selector].append(
            Issue(
                category="performance_bottleneck",
                severity=_map_perf_severity(raw_text),
                dimension=Dimension.PERFORMANCE,
                message=raw_text,
                source_type="perf",
                evidence={
                    "method": method,
                    "path": path,
                    "report_path": perf_result.get("report_path", ""),
                    "ai_analysis": perf_result.get("ai_analysis", ""),
                },
            )
        )


def _map_defect_result(page_index: dict[str, dict], page_order: list[str], bundle: DiscoveryScanBundle) -> None:
    defect_result = bundle.defect_result or {}
    pages_analyzed = defect_result.get("pages_analyzed") or []
    report_path = defect_result.get("report_path", "")

    for page_summary in pages_analyzed:
        page_url = page_summary.get("url") or bundle.target_url
        page_record = _ensure_page(page_index, page_order, page_url, title=page_summary.get("page_slug"))
        snapshots = page_summary.get("snapshots") or []
        candidate_snapshots = [s for s in snapshots if s.get("phase") in {"peak", "post"}] or snapshots

        seen: set[tuple[str, str, str]] = set()
        for snapshot in candidate_snapshots:
            for finding in snapshot.get("findings") or []:
                selector = finding.get("element_selector") or "__page__"
                key = (
                    str(finding.get("category", "")),
                    str(selector),
                    str(snapshot.get("phase", "")),
                )
                if key in seen:
                    continue
                seen.add(key)
                page_record["issues_by_selector"][selector].append(
                    Issue(
                        category=str(finding.get("category", "visual_defect")),
                        severity=_map_defect_severity(finding.get("severity", "")),
                        dimension=Dimension.VISUAL,
                        message=finding.get("description") or finding.get("title") or "Visual defect detected",
                        source_type="defect",
                        evidence={
                            "snapshot_phase": snapshot.get("phase"),
                            "report_path": report_path,
                            "screenshot_path": snapshot.get("screenshot_path", ""),
                            "annotated_screenshot_path": snapshot.get("annotated_screenshot_path", ""),
                            "conflicting_selector": finding.get("conflicting_selector"),
                            "drift_px": finding.get("drift_px"),
                            "contrast_ratio": finding.get("contrast_ratio"),
                        },
                    )
                )


def _page_url_for_path(
    page_index: dict[str, dict],
    page_order: list[str],
    target_url: str,
    path: str,
) -> str:
    if not path or path == "/":
        return target_url.rstrip("/") + "/"

    parsed_target = urlparse(target_url)
    candidate_path = path if path.startswith("/") else f"/{path}"

    for url in page_order:
        parsed = urlparse(url)
        if parsed.path == candidate_path:
            return url

    return f"{parsed_target.scheme}://{parsed_target.netloc}{candidate_path}"


def _map_accessibility_severity(impact: str) -> Severity:
    impact = str(impact).lower()
    if impact in {"critical", "serious"}:
        return Severity.CRITICAL if impact == "critical" else Severity.MAJOR
    if impact in {"moderate", "medium"}:
        return Severity.MINOR
    return Severity.INFORMATIONAL


def _map_defect_severity(raw: str) -> Severity:
    raw = str(raw).lower()
    if raw == "critical":
        return Severity.CRITICAL
    if raw in {"high", "medium"}:
        return Severity.MAJOR
    if raw == "low":
        return Severity.MINOR
    return Severity.INFORMATIONAL


def _map_perf_severity(message: str) -> Severity:
    lowered = message.lower()
    if "100.0%" in lowered or "critical" in lowered:
        return Severity.CRITICAL
    if "error_rate" in lowered or "p99" in lowered or "degradation" in lowered:
        return Severity.MAJOR
    return Severity.MINOR


def _score_lower_is_better(value: float | None, good: float, bad: float) -> float:
    if value is None:
        return 100.0
    if value <= good:
        return 100.0
    if value >= bad:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (bad - value) / (bad - good)))


def _score_higher_is_better(value: float | None, bad: float, good: float) -> float:
    if value is None:
        return 100.0
    if value >= good:
        return 100.0
    if value <= bad:
        return 0.0
    return max(0.0, min(100.0, 100.0 * (value - bad) / (good - bad)))


def _average(*values: float) -> float:
    return sum(values) / len(values) if values else 0.0
