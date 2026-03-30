"""
Scan Manager — in-memory scan state, credential lifecycle, and SSE log queue.

Security contract:
  - _auth_config is cleared to None BEFORE the first await in _run_scan()
  - ScanAwareProcessor strips credential keys from every log event
  - get_scan_status() and get_scan_result() never expose _auth_config
  - Credentials are never written to disk, DB, or logs
"""
from __future__ import annotations

import asyncio
import contextvars
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import structlog

from intelligence.services.runtime import process_final_state
from layer1_orchestrator.orchestrator import run_orchestrator

# ---------------------------------------------------------------------------
# ContextVar — isolates log queues for concurrent scans
# ---------------------------------------------------------------------------

_current_scan_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "scan_id", default=""
)

# ---------------------------------------------------------------------------
# Structlog processor — strips credentials, routes to correct queue
# ---------------------------------------------------------------------------

class ScanAwareProcessor:
    """
    Strips sensitive keys and copies log events into the active scan's queue.
    Registered with structlog at server startup; runs in every log call.
    """
    _STRIP_KEYS = frozenset(
        ("password", "token", "secret", "auth_config", "credentials",
         "authorization", "cookie", "session_token")
    )

    # Map log event prefixes → UI phase names
    _PHASE_MAP = {
        "planner": "plan",
        "auth": "auth",
        "login": "auth",
        "session": "auth",
        "crawler": "crawl",
        "crawl": "crawl",
        "browser": "crawl",
        "page_": "crawl",
        "evaluator": "evaluate",
        "evaluate": "evaluate",
        "classifier": "evaluate",
        "xgboost": "evaluate",
        "perf": "perf",
        "load_engine": "perf",
        "locust": "perf",
        "script_gen": "perf",
        "payload_gen": "perf",
        "endpoint_disc": "perf",
        "defect": "defect",
        "screenshot": "defect",
        "contrast": "defect",
        "layout": "defect",
        "functional": "defect",
        "orchestrator.complete": "complete",
    }

    def _detect_phase(self, event: str) -> Optional[str]:
        el = event.lower()
        if "orchestrator.complete" in el:
            return "complete"
        for prefix, phase in self._PHASE_MAP.items():
            if el.startswith(prefix):
                return phase
        return None

    def __call__(self, logger: Any, method: str, event_dict: dict) -> dict:
        # Strip any credential-adjacent keys
        for key in list(event_dict.keys()):
            if key.lower() in self._STRIP_KEYS:
                event_dict.pop(key)

        # Route to the correct scan queue (no-op if outside a scan task)
        scan_id = _current_scan_id.get("")
        if scan_id and scan_id in _scans:
            record = _scans[scan_id]

            # Update phase from log event name
            event_name = event_dict.get("event", "")
            detected = self._detect_phase(event_name)
            if detected and record.status == "running":
                record.phase = detected

            # Update pages_found live from crawler log events
            if "pages_crawled" in event_dict:
                try:
                    record.pages_found = int(event_dict["pages_crawled"])
                except (ValueError, TypeError):
                    pass
            elif event_name.startswith("crawler.page") and "url" in event_dict:
                record.pages_found += 1

            # Track auth result from auth_handler.complete log event
            if event_name == "auth_handler.complete":
                success = event_dict.get("success")
                if success is True or str(success).lower() == "true":
                    record.auth_status = "success"
                else:
                    record.auth_status = "failed"
                strategy = event_dict.get("strategy", "")
                if strategy:
                    record.auth_strategy = str(strategy)
            elif event_name == "auth_node.skipped":
                record.auth_status = "skipped"

            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            msg = f"[{ts}] {event_name}"
            # Add any extra context keys
            extras = {
                k: v for k, v in event_dict.items()
                if k not in ("event", "level", "timestamp", "_record", "logger")
            }
            if extras:
                extra_str = "  " + "  ".join(f"{k}={v}" for k, v in extras.items())
                msg += extra_str
            try:
                record.log_queue.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # drop log line if queue is full — __DONE__ uses force_done flag

        return event_dict


# ---------------------------------------------------------------------------
# Scan record
# ---------------------------------------------------------------------------

_MAX_LOG_QUEUE = 5000


@dataclass
class ScanRecord:
    scan_id: str
    target_url: str
    status: str = "queued"          # queued | running | done | error
    phase: str = ""
    pages_found: int = 0
    error_message: str = ""
    result: Optional[dict] = None
    log_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=_MAX_LOG_QUEUE))
    done: bool = False              # set to True when scan finishes — SSE fallback
    auth_status: str = "not_attempted"   # not_attempted | success | failed | skipped
    auth_strategy: str = ""              # cookie_replay | form_login | none | …
    _auth_config: Optional[dict] = field(default=None, repr=False)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# In-memory store — keyed by scan_id (UUID)
_scans: dict[str, ScanRecord] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_scan(
    target_url: str,
    auth_config: Optional[dict],
    max_pages: int,
    max_depth: int,
    perf_config: Optional[dict],
    defect_config: Optional[dict],
) -> str:
    """
    Register a new scan and return its scan_id.
    The scan task is launched by the caller (server.py) via asyncio.create_task().
    """
    scan_id = str(uuid.uuid4())
    record = ScanRecord(
        scan_id=scan_id,
        target_url=target_url,
        _auth_config=auth_config,
    )
    _scans[scan_id] = record

    # Store non-sensitive config on the record for _run_scan to read
    record._max_pages = max_pages
    record._max_depth = max_depth
    record._perf_config = perf_config
    record._defect_config = defect_config

    return scan_id


async def run_scan(scan_id: str) -> None:
    """
    Execute the orchestrator for scan_id in an isolated asyncio context.
    MUST be launched via asyncio.create_task() so ContextVar is scoped to this task.
    """
    # Set ContextVar — all log calls in this task will route to this scan's queue
    _current_scan_id.set(scan_id)

    record = _scans.get(scan_id)
    if record is None:
        return

    # -----------------------------------------------------------------------
    # Pull credentials out and CLEAR them from the record BEFORE first await
    # This is the critical security step — after this point _auth_config is None
    # -----------------------------------------------------------------------
    auth_config = record._auth_config
    record._auth_config = None          # cannot be serialised or logged

    max_pages = getattr(record, "_max_pages", 50)
    max_depth = getattr(record, "_max_depth", 5)
    perf_config = getattr(record, "_perf_config", None)
    defect_config = getattr(record, "_defect_config", None)

    record.status = "running"
    record.phase = "starting"

    try:
        final_state = await run_orchestrator(
            target_url=record.target_url,
            auth_config=auth_config,
            max_pages=max_pages,
            max_depth=max_depth,
            thread_id=scan_id,
            perf_config=perf_config,
            defect_config=defect_config,
        )
        auth_config = None              # clear on success path

        # Extract a safe, credential-free summary from final_state
        record.result = _safe_result(final_state)
        try:
            intelligence_result = process_final_state(
                final_state,
                target_url=record.target_url,
                scan_id=scan_id,
            )
            record.result["application_name"] = intelligence_result.get("application_name", "")
            record.result["application_key"] = intelligence_result.get("application_key", "")
            record.result["intelligence"] = _safe_intelligence(intelligence_result)
        except Exception as exc:
            structlog.get_logger().warning(
                "scan_manager.intelligence_degraded",
                scan_id=scan_id,
                error=str(exc),
            )
            _exc_str = str(exc)
            if "No module named" in _exc_str or "neo4j" in _exc_str.lower():
                _friendly = "Intelligence layer unavailable (Neo4j not configured). Historical scoring and trend data require a connected Neo4j instance."
            else:
                _friendly = "Intelligence layer temporarily unavailable."
            record.result["intelligence"] = {
                "status": "degraded",
                "error_message": _friendly,
                "application_score": None,
                "application_grade": None,
                "risk_class": None,
                "trend_indicator": None,
                "page_scores": [],
                "lifecycle_summary": None,
                "page_summaries": [],
                "top_priorities": [],
            }
        record.pages_found = len(final_state.get("pages") or [])
        record.status = "done"
        record.phase = "complete"

    except Exception as exc:
        auth_config = None              # clear on error path
        record.status = "error"
        record.error_message = str(exc)
        record.phase = "error"
        structlog.get_logger().error("scan_manager.scan_failed", scan_id=scan_id, error=str(exc))

    finally:
        auth_config = None              # belt-and-braces
        record.done = True              # SSE generator polls this as fallback
        # Signal SSE stream to close — drain one slot if full to ensure delivery
        if record.log_queue.full():
            try:
                record.log_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            record.log_queue.put_nowait("__DONE__")
        except asyncio.QueueFull:
            pass  # SSE generator will detect record.done=True on next keepalive


def get_scan_status(scan_id: str) -> Optional[dict]:
    record = _scans.get(scan_id)
    if record is None:
        return None
    return {
        "scan_id": record.scan_id,
        "status": record.status,
        "phase": record.phase,
        "pages_found": record.pages_found,
        "error_message": record.error_message,
        "created_at": record.created_at,
        "auth_status": record.auth_status,
        "auth_strategy": record.auth_strategy,
    }


def get_scan_result(scan_id: str) -> Optional[dict]:
    record = _scans.get(scan_id)
    if record is None:
        return None
    return record.result   # None until done; never includes _auth_config


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_result(final_state: dict) -> dict:
    """
    Extract a serialisable, credential-free summary from the orchestrator state.
    Never reads _auth_config — it has already been cleared.
    """
    pages = final_state.get("pages") or []
    result = final_state.get("result") or {}
    perf_result = final_state.get("perf_result")
    defect_result = final_state.get("defect_result")
    errors = final_state.get("errors") or []

    # Pages crawled summary
    pages_summary = [
        {
            "url": p.get("url", ""),
            "page_type": p.get("page_type", ""),
            "title": p.get("title", ""),
        }
        for p in pages[:200]   # cap to avoid huge payloads
    ]

    # Performance summary
    perf_summary = None
    if perf_result and isinstance(perf_result, dict):
        perf_summary = {
            "endpoints_tested": perf_result.get("endpoints_tested", 0),
            "bottlenecks": perf_result.get("bottlenecks", []),
            "ai_analysis": perf_result.get("ai_analysis", ""),
            "recommendations": perf_result.get("recommendations", []),
            "report_path": perf_result.get("report_path", ""),
        }

    # Defect summary
    defect_summary = None
    if defect_result and isinstance(defect_result, dict):
        findings = []
        for page_summary in defect_result.get("pages_analyzed") or []:
            for snapshot in page_summary.get("snapshots") or []:
                for finding in snapshot.get("findings") or []:
                    enriched_finding = dict(finding)
                    enriched_finding.setdefault("url", page_summary.get("url", ""))
                    findings.append(enriched_finding)
        # Count per severity
        sev_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
        for f in findings:
            sev = (f.get("severity") or "info").lower()
            if sev in sev_counts:
                sev_counts[sev] += 1
            else:
                sev_counts["info"] += 1

        defect_summary = {
            "total_findings": defect_result.get("total_defects", len(findings)),
            "critical_count": sev_counts["critical"],
            "high_count": sev_counts["high"],
            "medium_count": sev_counts["medium"],
            "low_count": sev_counts["low"],
            "info_count": sev_counts["info"],
            "report_path": defect_result.get("report_path", ""),
            "top_findings": [
                {
                    "category": f.get("category", ""),
                    "severity": f.get("severity", ""),
                    "description": f.get("description", ""),
                    "url": f.get("url", ""),
                }
                for f in findings[:10]
            ],
        }

    # Coverage score from result dict
    coverage_score = result.get("coverage_score", 0)

    request_data = final_state.get("request") or {}
    return {
        "target_url": result.get("target_url", request_data.get("target_url", "")),
        "pages_crawled": len(pages),
        "pages": pages_summary,
        "coverage_score": coverage_score,
        "perf_result": perf_summary,
        "defect_result": defect_summary,
        "errors": errors[:20],
    }


def _safe_intelligence(intelligence_result: dict) -> dict:
    return {
        "status": "ok",
        "application_score": intelligence_result.get("application_score"),
        "application_grade": (intelligence_result.get("application_score") or {}).get("grade"),
        "risk_class": (intelligence_result.get("application_score") or {}).get("risk_class"),
        "trend_indicator": (intelligence_result.get("application_score") or {}).get("trend_indicator"),
        "lifecycle_summary": intelligence_result.get("lifecycle_summary"),
        "page_scores": intelligence_result.get("page_scores", []),
        "page_summaries": intelligence_result.get("page_summaries", []),
        "top_priorities": intelligence_result.get("top_priorities", []),
    }
