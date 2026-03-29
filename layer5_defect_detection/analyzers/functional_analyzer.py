"""
Functional Analyzer — detects broken links, console errors, and network failures.

Broken links: uses in-browser fetch() to check same-origin anchor hrefs (up to
_MAX_LINKS_TO_CHECK) on the already-loaded page. Cross-origin links are skipped
to avoid CORS issues.

Console errors / network failures: captured via Playwright event listeners set
up BEFORE navigation. The defect orchestrator passes pre-collected events to
check_events(); this module just turns them into DefectFinding objects.
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
