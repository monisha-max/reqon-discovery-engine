from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intelligence.models.contracts import (
    AuditLogEntry,
    Page,
    PageScore,
    ScanEventMessage,
    ScanRequest,
    ScoreHistoryEntry,
)


@dataclass(slots=True)
class IssueLifecycleState:
    issue_key: str
    page_url: str
    first_seen: datetime
    last_seen: datetime
    occurrence_count: int
    regression_flag: bool
    status: str


@dataclass(slots=True)
class PageIngestSummaryRecord:
    url: str
    active_issue_count: int
    new_issues: int
    recurring_issues: int
    regressions: int
    resolved_issues: int


@dataclass(slots=True)
class LifecycleSummaryRecord:
    new_issues: int = 0
    recurring_issues: int = 0
    regressions: int = 0
    resolved_issues: int = 0


@dataclass(slots=True)
class PersistedScanRecord:
    tenant_id: str
    scan_id: str
    scanned_at: datetime
    application_name: str
    application_key: str
    enriched_scan: ScanRequest
    page_summaries: list[PageIngestSummaryRecord]
    lifecycle_summary: LifecycleSummaryRecord
    graph_snapshots: list[dict]


class KnowledgeGraphStore:
    def ingest_scan(self, payload: ScanRequest, tenant_id: str) -> PersistedScanRecord:  # pragma: no cover - interface
        raise NotImplementedError

    def list_graph_snapshots(self, tenant_id: str) -> list[dict]:  # pragma: no cover - interface
        raise NotImplementedError


class ScoreHistoryStore:
    def latest(self, tenant_id: str, entity_type: str, entity_key: str) -> ScoreHistoryEntry | None:  # pragma: no cover - interface
        raise NotImplementedError

    def record_application_score(
        self,
        tenant_id: str,
        scan_id: str,
        scanned_at: datetime,
        application_score,
    ) -> ScoreHistoryEntry:  # pragma: no cover - interface
        raise NotImplementedError

    def record_page_scores(
        self,
        tenant_id: str,
        scan_id: str,
        scanned_at: datetime,
        page_scores: list[PageScore],
    ) -> list[ScoreHistoryEntry]:  # pragma: no cover - interface
        raise NotImplementedError

    def list_entries(
        self,
        tenant_id: str,
        entity_type: str,
        entity_key: str,
    ) -> list[ScoreHistoryEntry]:  # pragma: no cover - interface
        raise NotImplementedError


class AuditLogStore:
    def write(self, tenant_id: str, entry: AuditLogEntry) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def list_recent(self, tenant_id: str, limit: int = 100) -> list[AuditLogEntry]:  # pragma: no cover - interface
        raise NotImplementedError


class ScanEventBroker:
    def publish(self, tenant_id: str, event: ScanEventMessage) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def subscribe(self, tenant_id: str):  # pragma: no cover - interface
        raise NotImplementedError

    def unsubscribe(self, tenant_id: str, queue) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def recent_events(self, tenant_id: str, limit: int = 100) -> list[ScanEventMessage]:  # pragma: no cover - interface
        raise NotImplementedError
