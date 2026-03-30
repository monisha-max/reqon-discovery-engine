from __future__ import annotations

from intelligence.models.contracts import (
    LifecycleSummary,
    PageIngestSummary,
    ScanIngestResponse,
    ScanRequest,
)
from intelligence.repositories.contracts import (
    AuditLogStore,
    KnowledgeGraphStore,
    ScanEventBroker,
    ScoreHistoryStore,
)
from intelligence.services.scoring import DeterministicScoringService
from intelligence.services.telemetry import TelemetryService
from intelligence.services.trends import TrendService


class ScanIngestionService:
    """
    Coordinates the integrated intelligence path for one completed discovery scan.
    """

    def __init__(
        self,
        graph_store: KnowledgeGraphStore,
        history_store: ScoreHistoryStore,
        audit_store: AuditLogStore,
        event_broker: ScanEventBroker,
        scoring_service: DeterministicScoringService | None = None,
    ) -> None:
        self.graph_store = graph_store
        self.scoring_service = scoring_service or DeterministicScoringService()
        self.trend_service = TrendService(history_store=history_store)
        self.history_store = history_store
        self.telemetry_service = TelemetryService(
            audit_store=audit_store,
            event_broker=event_broker,
        )

    def ingest_scan(self, payload: ScanRequest, tenant_id: str) -> ScanIngestResponse:
        self.telemetry_service.publish_event(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            event_type="scan.received",
            payload={
                "application_name": payload.application_name,
                "page_count": len(payload.pages),
            },
        )
        self.telemetry_service.write_audit(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            action="scan_received",
            entity_type="scan",
            entity_key=payload.scan_id,
            details={
                "application_name": payload.application_name,
                "application_key": payload.application_key,
                "page_count": len(payload.pages),
            },
        )

        persisted = self.graph_store.ingest_scan(payload=payload, tenant_id=tenant_id)
        self.telemetry_service.publish_event(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            event_type="scan.persisted",
            payload={
                "new_issues": persisted.lifecycle_summary.new_issues,
                "recurring_issues": persisted.lifecycle_summary.recurring_issues,
                "regressions": persisted.lifecycle_summary.regressions,
                "resolved_issues": persisted.lifecycle_summary.resolved_issues,
            },
        )
        application_score, page_scores = self.scoring_service.score_scan(
            payload=persisted.enriched_scan,
            tenant_id=tenant_id,
        )
        application_score = self.trend_service.apply_application_trend(
            tenant_id=tenant_id,
            score=application_score,
        )
        page_scores = self.trend_service.apply_page_trends(
            tenant_id=tenant_id,
            page_scores=page_scores,
        )

        self.history_store.record_application_score(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            scanned_at=payload.scanned_at,
            application_score=application_score,
        )
        self.history_store.record_page_scores(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            scanned_at=payload.scanned_at,
            page_scores=page_scores,
        )

        self.telemetry_service.publish_event(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            event_type="scan.scored",
            payload={
                "application_score": application_score.adjusted_score,
                "risk_class": application_score.risk_class,
            },
        )
        self.telemetry_service.write_audit(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            action="scan_scored",
            entity_type="application",
            entity_key=payload.application_key,
            details={
                "application_name": payload.application_name,
                "adjusted_score": application_score.adjusted_score,
                "trend_indicator": application_score.trend_indicator,
                "risk_class": application_score.risk_class,
            },
        )
        self.telemetry_service.publish_event(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            event_type="scan.completed",
            payload={"graph_snapshot_count": len(persisted.graph_snapshots)},
        )

        top_priorities = self._build_top_priorities(page_scores)

        return ScanIngestResponse(
            tenant_id=tenant_id,
            scan_id=persisted.scan_id,
            application_name=persisted.application_name,
            application_key=persisted.application_key,
            scanned_at=persisted.scanned_at,
            application_score=application_score,
            page_scores=page_scores,
            lifecycle_summary=LifecycleSummary(
                new_issues=persisted.lifecycle_summary.new_issues,
                recurring_issues=persisted.lifecycle_summary.recurring_issues,
                regressions=persisted.lifecycle_summary.regressions,
                resolved_issues=persisted.lifecycle_summary.resolved_issues,
            ),
            page_summaries=[
                PageIngestSummary(
                    url=summary.url,
                    active_issue_count=summary.active_issue_count,
                    new_issues=summary.new_issues,
                    recurring_issues=summary.recurring_issues,
                    regressions=summary.regressions,
                    resolved_issues=summary.resolved_issues,
                )
                for summary in persisted.page_summaries
            ],
            top_priorities=top_priorities,
        )

    def _build_top_priorities(self, page_scores):
        ranked = sorted(
            page_scores,
            key=lambda score: (score.adjusted_score, -score.issue_count),
        )
        priorities: list[dict] = []
        for score in ranked[:5]:
            priorities.append(
                {
                    "entity_type": "page",
                    "entity_key": score.url,
                    "adjusted_score": score.adjusted_score,
                    "risk_class": score.risk_class,
                    "priority_flags": score.priority_flags,
                }
            )
        return priorities
