"""
Functional Analyzer — detects broken links, console errors, network failures,
slow endpoints, request retries, mid-session auth failures, and CSP/security
hygiene issues.

Broken links: uses in-browser fetch() to check same-origin anchor hrefs (up to
_MAX_LINKS_TO_CHECK) on the already-loaded page. Cross-origin links are skipped
to avoid CORS issues.

Console errors / network failures: captured via Playwright event listeners set
up BEFORE navigation. The defect orchestrator passes pre-collected events to
check_events(); this module just turns them into DefectFinding objects.

Network telemetry (additions 1–3): captured via page.on("request/response")
listeners in ScreenshotCapture.capture(monitor_events=True), stored as
page._reqon_responses. check_network_telemetry() reads that list and emits:
  - SLOW_ENDPOINT   — XHR/fetch responses that took > threshold ms
  - REQUEST_RETRY   — same URL fetched 3+ times in one page load
  - AUTH_FAILURE    — 401/403 on XHR/API calls (not full-page navigation)

CSP / security hygiene (addition 4): check_events() now filters console
messages for known security warning patterns before emitting CONSOLE_ERROR,
routing them to SECURITY_HYGIENE instead.
"""
from __future__ import annotations

from typing import Optional
from uuid import uuid4

import structlog

from layer5_defect_detection.models.defect_models import (
    BoundingBox,
    DefectCategory,
    DefectFinding,
    DefectSeverity,
)

# ---------------------------------------------------------------------------
# Network telemetry thresholds
# ---------------------------------------------------------------------------

# Requests slower than these are flagged (XHR/fetch only)
_SLOW_THRESHOLD_MS_MEDIUM = 3_000   # 3 s → MEDIUM
_SLOW_THRESHOLD_MS_HIGH   = 8_000   # 8 s → HIGH

# Same URL fetched this many times in one page load → retry pattern
_RETRY_COUNT_THRESHOLD = 3

# Resource types that represent actual API / data calls (not page assets)
_API_RESOURCE_TYPES = {"xhr", "fetch"}

# Console message substrings that indicate a security-hygiene issue
# (checked case-insensitively)
_SECURITY_PATTERNS: list[str] = [
    "content-security-policy",
    "mixed content",
    "blocked by cors",
    "cross-origin",
    "refused to load",
    "unsafe-inline",
    "unsafe-eval",
    "violated the following content security policy",
]

logger = structlog.get_logger()

# Maximum number of same-origin links to probe per page (avoids slow fetches)
_MAX_LINKS_TO_CHECK = 20

# JS that collects same-origin anchor hrefs and probes them with fetch()
_BROKEN_LINK_JS = """
async (maxLinks) => {
    const origin = window.location.origin;
    const anchors = [...document.querySelectorAll('a[href]')];
    const sameOrigin = anchors
        .map(a => ({ href: a.href, text: (a.textContent || '').trim().substring(0, 60), selector: a.id ? '#' + a.id : a.className ? 'a.' + a.className.split(' ')[0] : 'a' }))
        .filter(a => a.href.startsWith(origin) && !a.href.includes('#') && a.href !== origin + '/' && a.href !== origin)
        .slice(0, maxLinks);

    const results = [];
    for (const link of sameOrigin) {
        try {
            const resp = await fetch(link.href, { method: 'HEAD', redirect: 'follow', signal: AbortSignal.timeout(5000) });
            if (!resp.ok && resp.status !== 0) {
                results.push({ href: link.href, status: resp.status, text: link.text, selector: link.selector });
            }
        } catch (e) {
            // Network error counts as broken
            results.push({ href: link.href, status: 0, text: link.text, selector: link.selector, error: String(e) });
        }
    }
    return results;
}
"""

# Placeholder bounding box for findings without a visible element
_ZERO_BBOX = BoundingBox(x=0, y=0, width=0, height=0)


class FunctionalAnalyzer:
    """Check for broken links, console errors, and tracked network failures."""

    async def check_broken_links(
        self,
        page: object,
        phase: str,
    ) -> list[DefectFinding]:
        """
        Probe same-origin anchor hrefs on the already-loaded page.
        Returns findings for links that return 4xx/5xx or fail to connect.
        """
        try:
            broken: list[dict] = await page.evaluate(_BROKEN_LINK_JS, _MAX_LINKS_TO_CHECK)
        except Exception as exc:
            logger.warning("functional_analyzer.broken_link_js_failed", error=str(exc))
            return []

        findings = []
        for link in (broken or []):
            status = link.get("status", 0)
            href   = link.get("href", "")
            text   = link.get("text", "")
            sel    = link.get("selector", "a")

            if status == 0:
                title = f"Link unreachable: {href[:80]}"
                desc  = f"Anchor '{text[:50] or href[:50]}' could not be fetched (network error / timeout)."
                sev   = DefectSeverity.MEDIUM
            elif 400 <= status < 500:
                title = f"Broken link HTTP {status}: {href[:70]}"
                desc  = f"Anchor '{text[:50] or href[:50]}' returns HTTP {status}."
                sev   = DefectSeverity.HIGH if status == 404 else DefectSeverity.MEDIUM
            else:
                title = f"Link error HTTP {status}: {href[:70]}"
                desc  = f"Anchor '{text[:50] or href[:50]}' returns HTTP {status} (server error)."
                sev   = DefectSeverity.HIGH

            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=sev,
                category=DefectCategory.BROKEN_LINK,
                title=title,
                description=desc,
                element_selector=sel,
                element_bbox=_ZERO_BBOX,
                snapshot_phase=phase,
                annotation_color="orange",
            ))

        if findings:
            logger.info(
                "functional_analyzer.broken_links_found",
                phase=phase,
                count=len(findings),
            )
        return findings

    def check_network_telemetry(
        self,
        responses: list[dict],
        phase: str,
    ) -> list[DefectFinding]:
        """
        Analyse completed network responses collected by ScreenshotCapture and
        emit findings for slow endpoints, request retries, and mid-session auth
        failures.

        Args:
            responses: List of {url, status, resource_type, timing_ms} dicts
                       collected via page._reqon_responses.
            phase: snapshot phase label ("baseline" | "peak" | "post")

        Returns:
            List of DefectFinding objects (SLOW_ENDPOINT, REQUEST_RETRY, AUTH_FAILURE)
        """
        findings: list[DefectFinding] = []

        # Only analyse XHR/fetch responses — page navigations and static assets
        # (scripts, stylesheets, images) are excluded to keep signal/noise high.
        api_responses = [r for r in responses if r.get("resource_type") in _API_RESOURCE_TYPES]

        # Addition 1 — Slow endpoints
        for resp in api_responses:
            timing = resp.get("timing_ms")
            if timing is None:
                continue
            url = resp.get("url", "")
            status = resp.get("status", 0)
            if timing >= _SLOW_THRESHOLD_MS_HIGH:
                sev = DefectSeverity.HIGH
            elif timing >= _SLOW_THRESHOLD_MS_MEDIUM:
                sev = DefectSeverity.MEDIUM
            else:
                continue
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=sev,
                category=DefectCategory.SLOW_ENDPOINT,
                title=f"Slow endpoint ({timing} ms): {url[:70]}",
                description=(
                    f"API call to '{url}' took {timing} ms to respond "
                    f"(HTTP {status}). "
                    f"Threshold: {_SLOW_THRESHOLD_MS_MEDIUM} ms (MEDIUM), "
                    f"{_SLOW_THRESHOLD_MS_HIGH} ms (HIGH). "
                    f"Slow endpoints degrade perceived performance and may cause "
                    f"client-side timeouts."
                ),
                element_selector=url[:100],
                element_bbox=_ZERO_BBOX,
                snapshot_phase=phase,
                annotation_color="orange",
            ))

        # Addition 2 — Request retries
        url_counts: dict[str, int] = {}
        for resp in api_responses:
            url = resp.get("url", "")
            if url:
                url_counts[url] = url_counts.get(url, 0) + 1

        for url, count in url_counts.items():
            if count >= _RETRY_COUNT_THRESHOLD:
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.REQUEST_RETRY,
                    title=f"Request retry pattern ({count}×): {url[:70]}",
                    description=(
                        f"'{url}' was fetched {count} times during a single page load. "
                        f"This indicates a retry loop, polling without back-off, "
                        f"a token refresh cycle, or a race condition causing repeated calls."
                    ),
                    element_selector=url[:100],
                    element_bbox=_ZERO_BBOX,
                    snapshot_phase=phase,
                    annotation_color="orange",
                ))

        # Addition 3 — Mid-session auth failures (401/403 on API calls)
        # Full-page 401/403 navigation failures are already caught by broken_links;
        # this targets silent XHR/fetch auth failures that leave the page looking
        # normal while data silently fails to load.
        for resp in api_responses:
            status = resp.get("status", 0)
            if status not in (401, 403):
                continue
            url = resp.get("url", "")
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.HIGH,
                category=DefectCategory.AUTH_FAILURE,
                title=f"Auth failure HTTP {status} on API call: {url[:70]}",
                description=(
                    f"XHR/fetch request to '{url}' returned HTTP {status}. "
                    f"This is a mid-session authentication failure — the session "
                    f"may have expired, the token is invalid, or the user lacks "
                    f"permission. The page may render silently broken."
                ),
                element_selector=url[:100],
                element_bbox=_ZERO_BBOX,
                snapshot_phase=phase,
                annotation_color="red",
            ))

        if findings:
            logger.info(
                "functional_analyzer.network_telemetry_findings",
                phase=phase,
                count=len(findings),
            )
        return findings

    def check_events(
        self,
        console_errors: list[str],
        failed_requests: list[dict],
        phase: str,
    ) -> list[DefectFinding]:
        """
        Convert pre-collected Playwright console errors and failed network
        requests into DefectFinding objects.

        Args:
            console_errors: List of console error message strings captured
                            via page.on("console") before navigation.
            failed_requests: List of {url, failure_text} dicts captured via
                             page.on("requestfailed") before navigation.
            phase: snapshot phase label ("baseline" | "peak" | "post")
        """
        findings: list[DefectFinding] = []

        for msg in console_errors:
            # Addition 4 — route security-related console messages to their own
            # category instead of the generic CONSOLE_ERROR bucket.
            msg_lower = msg.lower()
            is_security = any(pat in msg_lower for pat in _SECURITY_PATTERNS)

            if is_security:
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.SECURITY_HYGIENE,
                    title=f"Security policy violation: {msg[:80]}",
                    description=(
                        f"Browser reported a security policy issue during page load: {msg}. "
                        f"This may indicate a missing or misconfigured Content-Security-Policy, "
                        f"mixed HTTP/HTTPS content, or a blocked cross-origin request."
                    ),
                    element_selector="window",
                    element_bbox=_ZERO_BBOX,
                    snapshot_phase=phase,
                    annotation_color="red",
                ))
            else:
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.CONSOLE_ERROR,
                    title=f"Console error: {msg[:80]}",
                    description=f"Browser console error logged during page load: {msg}",
                    element_selector="window",
                    element_bbox=_ZERO_BBOX,
                    snapshot_phase=phase,
                    annotation_color="red",
                ))

        for req in failed_requests:
            url = req.get("url", "")
            failure = req.get("failure_text", "unknown")
            # Skip browser-level noise (cancelled requests, service worker aborts)
            if failure.lower() in ("net::err_aborted", "net::err_blocked_by_client"):
                continue
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.HIGH,
                category=DefectCategory.NETWORK_FAILURE,
                title=f"Network failure: {url[:70]}",
                description=f"Request to '{url}' failed: {failure}",
                element_selector=url[:100],
                element_bbox=_ZERO_BBOX,
                snapshot_phase=phase,
                annotation_color="red",
            ))

        return findings
