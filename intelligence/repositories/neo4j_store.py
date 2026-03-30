from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

from neo4j import GraphDatabase

from intelligence.models.contracts import AuditLogEntry, PageScore, ScanEventMessage, ScoreHistoryEntry
from intelligence.repositories.contracts import (
    AuditLogStore,
    IssueLifecycleState,
    KnowledgeGraphStore,
    LifecycleSummaryRecord,
    PageIngestSummaryRecord,
    PersistedScanRecord,
    ScanEventBroker,
    ScoreHistoryStore,
)
from intelligence.services.identity import build_issue_key


class Neo4jIntelligenceStore(KnowledgeGraphStore, ScoreHistoryStore, AuditLogStore, ScanEventBroker):
    def __init__(self, uri: str, username: str, password: str, database: str) -> None:
        self.driver = GraphDatabase.driver(uri, auth=(username, password))
        self.database = database
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def close(self) -> None:
        self.driver.close()

    def ping(self) -> None:
        self.driver.verify_connectivity()

    def ingest_scan(self, payload, tenant_id: str) -> PersistedScanRecord:
        page_summaries: list[PageIngestSummaryRecord] = []
        lifecycle_summary = LifecycleSummaryRecord()
        enriched_pages = []
        graph_snapshots: list[dict[str, Any]] = []

        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._merge_application_and_scan,
                tenant_id,
                payload.application_key,
                payload.application_name,
                payload.scan_id,
                payload.scanned_at.isoformat(),
                payload.metadata,
            )

            for page in payload.pages:
                session.execute_write(
                    self._merge_page,
                    tenant_id,
                    payload.application_key,
                    payload.application_name,
                    payload.scan_id,
                    payload.scanned_at.isoformat(),
                    page.url,
                    page.title,
                    page.page_type,
                    page.evidence,
                )
                previous_keys = set(
                    session.execute_read(
                        self._get_active_issue_keys,
                        tenant_id,
                        payload.application_key,
                        page.url,
                    )
                )

                current_keys: set[str] = set()
                enriched_elements = []
                page_new = 0
                page_recurring = 0
                page_regressions = 0

                for element in page.elements:
                    enriched_issues = []
                    for issue in element.issues:
                        issue_key = build_issue_key(
                            tenant_id=tenant_id,
                            page_url=page.url,
                            selector=element.selector,
                            category=issue.category,
                            message=issue.message,
                            source_type=issue.source_type,
                        )
                        current_keys.add(issue_key)
                        state = session.execute_read(
                            self._get_issue_state,
                            tenant_id,
                            payload.application_key,
                            page.url,
                            issue_key,
                        )

                        if state is None:
                            occurrence_count = 1
                            regression_flag = False
                            page_new += 1
                        elif state.status == "resolved":
                            occurrence_count = state.occurrence_count + 1
                            regression_flag = True
                            page_regressions += 1
                        else:
                            occurrence_count = state.occurrence_count + 1
                            regression_flag = False
                            page_recurring += 1

                        session.execute_write(
                            self._upsert_issue,
                            tenant_id,
                            payload.application_key,
                            payload.scan_id,
                            payload.scanned_at.isoformat(),
                            page.url,
                            element.selector,
                            issue_key,
                            issue.category,
                            issue.message,
                            issue.dimension.value,
                            issue.severity.value,
                            issue.source_type,
                            issue.evidence,
                            occurrence_count,
                            regression_flag,
                        )

                        enriched_issues.append(
                            issue.model_copy(
                                update={
                                    "occurrence_count": occurrence_count,
                                    "regression_flag": regression_flag,
                                }
                            )
                        )

                    if enriched_issues:
                        enriched_elements.append(element.model_copy(update={"issues": enriched_issues}))

                resolved_keys = previous_keys - current_keys
                for resolved_key in resolved_keys:
                    session.execute_write(
                        self._mark_issue_resolved,
                        tenant_id,
                        payload.application_key,
                        page.url,
                        resolved_key,
                    )

                page_summary = PageIngestSummaryRecord(
                    url=page.url,
                    active_issue_count=len(current_keys),
                    new_issues=page_new,
                    recurring_issues=page_recurring,
                    regressions=page_regressions,
                    resolved_issues=len(resolved_keys),
                )
                page_summaries.append(page_summary)
                lifecycle_summary.new_issues += page_new
                lifecycle_summary.recurring_issues += page_recurring
                lifecycle_summary.regressions += page_regressions
                lifecycle_summary.resolved_issues += len(resolved_keys)

                enriched_page = page.model_copy(update={"elements": enriched_elements})
                enriched_pages.append(enriched_page)
                graph_snapshots.append(
                    {
                        "application_key": payload.application_key,
                        "page_url": page.url,
                        "page_type": page.page_type,
                        "issue_count": len(current_keys),
                    }
                )

        enriched_scan = payload.model_copy(update={"pages": enriched_pages})
        return PersistedScanRecord(
            tenant_id=tenant_id,
            scan_id=payload.scan_id,
            scanned_at=payload.scanned_at,
            application_name=payload.application_name,
            application_key=payload.application_key,
            enriched_scan=enriched_scan,
            page_summaries=page_summaries,
            lifecycle_summary=lifecycle_summary,
            graph_snapshots=graph_snapshots,
        )

    def list_graph_snapshots(self, tenant_id: str) -> list[dict]:
        return []

    def latest(self, tenant_id: str, entity_type: str, entity_key: str) -> ScoreHistoryEntry | None:
        with self.driver.session(database=self.database) as session:
            record = session.execute_read(self._latest_score, tenant_id, entity_type, entity_key)
        return self._score_from_record(record) if record else None

    def record_application_score(self, tenant_id: str, scan_id: str, scanned_at: datetime, application_score) -> ScoreHistoryEntry:
        entry = ScoreHistoryEntry(
            scan_id=scan_id,
            scanned_at=scanned_at,
            entity_type="application",
            entity_key=application_score.application_key,
            base_score=application_score.base_score,
            adjusted_score=application_score.adjusted_score,
            risk_score=application_score.risk_score,
            risk_class=application_score.risk_class,
            trend_indicator=application_score.trend_indicator,
            grade=application_score.grade,
        )
        with self.driver.session(database=self.database) as session:
            session.execute_write(
                self._create_score_snapshot,
                tenant_id,
                scan_id,
                entry,
                {"application_key": application_score.application_key},
            )
        return entry

    def record_page_scores(self, tenant_id: str, scan_id: str, scanned_at: datetime, page_scores: list[PageScore]) -> list[ScoreHistoryEntry]:
        entries: list[ScoreHistoryEntry] = []
        with self.driver.session(database=self.database) as session:
            for score in page_scores:
                entry = ScoreHistoryEntry(
                    scan_id=scan_id,
                    scanned_at=scanned_at,
                    entity_type="page",
                    entity_key=score.url,
                    base_score=score.base_score,
                    adjusted_score=score.adjusted_score,
                    risk_score=score.risk_score,
                    risk_class=score.risk_class,
                    trend_indicator=score.trend_indicator,
                    grade=score.grade,
                )
                session.execute_write(
                    self._create_score_snapshot,
                    tenant_id,
                    scan_id,
                    entry,
                    {
                        "page_url": score.url,
                        "application_key": score.application_key,
                    },
                )
                entries.append(entry)
        return entries

    def list_entries(self, tenant_id: str, entity_type: str, entity_key: str) -> list[ScoreHistoryEntry]:
        with self.driver.session(database=self.database) as session:
            rows = session.execute_read(self._list_scores, tenant_id, entity_type, entity_key)
        return [self._score_from_record(row) for row in rows]

    def write(self, tenant_id: str, entry: AuditLogEntry) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._create_audit_event, tenant_id, entry)

    def list_recent(self, tenant_id: str, limit: int = 100) -> list[AuditLogEntry]:
        with self.driver.session(database=self.database) as session:
            rows = session.execute_read(self._list_audit_events, tenant_id, limit)
        return [self._audit_from_record(row) for row in rows]

    def publish(self, tenant_id: str, event: ScanEventMessage) -> None:
        with self.driver.session(database=self.database) as session:
            session.execute_write(self._create_scan_event, tenant_id, event)
        for queue in list(self._subscribers.get(tenant_id, set())):
            queue.put_nowait(event)

    def subscribe(self, tenant_id: str):
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(tenant_id, set()).add(queue)
        return queue

    def unsubscribe(self, tenant_id: str, queue) -> None:
        self._subscribers.setdefault(tenant_id, set()).discard(queue)

    def recent_events(self, tenant_id: str, limit: int = 100) -> list[ScanEventMessage]:
        with self.driver.session(database=self.database) as session:
            rows = session.execute_read(self._list_scan_events, tenant_id, limit)
        return [self._event_from_record(row) for row in rows]

    @staticmethod
    def _merge_application_and_scan(tx, tenant_id, application_key, application_name, scan_id, scanned_at, metadata):
        tx.run(
            """
            MERGE (app:Application {tenant_id: $tenant_id, application_key: $application_key})
            ON CREATE SET app.application_name = $application_name, app.first_seen = $scanned_at
            SET app.application_name = $application_name, app.last_seen = $scanned_at
            MERGE (scan:Scan {tenant_id: $tenant_id, scan_id: $scan_id})
            SET scan.scanned_at = $scanned_at, scan.metadata_json = $metadata_json
            MERGE (app)-[:HAS_SCAN]->(scan)
            """,
            tenant_id=tenant_id,
            application_key=application_key,
            application_name=application_name,
            scan_id=scan_id,
            scanned_at=scanned_at,
            metadata_json=json.dumps(metadata or {}),
        )

    @staticmethod
    def _merge_page(tx, tenant_id, application_key, application_name, scan_id, scanned_at, page_url, title, page_type, evidence):
        tx.run(
            """
            MATCH (app:Application {tenant_id: $tenant_id, application_key: $application_key})
            MATCH (scan:Scan {tenant_id: $tenant_id, scan_id: $scan_id})
            MERGE (page:Page {tenant_id: $tenant_id, application_key: $application_key, url: $page_url})
            ON CREATE SET page.first_seen = $scanned_at
            SET page.title = $title, page.page_type = $page_type, page.last_seen = $scanned_at, page.evidence_json = $evidence_json
            MERGE (app)-[:HAS_PAGE]->(page)
            MERGE (scan)-[:HAS_PAGE]->(page)
            """,
            tenant_id=tenant_id,
            application_key=application_key,
            scan_id=scan_id,
            scanned_at=scanned_at,
            page_url=page_url,
            title=title,
            page_type=page_type,
            evidence_json=json.dumps(evidence or {}),
        )

    @staticmethod
    def _get_active_issue_keys(tx, tenant_id, application_key, page_url):
        result = tx.run(
            """
            MATCH (:Page {tenant_id: $tenant_id, application_key: $application_key, url: $page_url})
                  -[:TRACKS_ISSUE]->(fingerprint:IssueFingerprint)
            WHERE fingerprint.status = 'active'
            RETURN fingerprint.issue_key AS issue_key
            """,
            tenant_id=tenant_id,
            application_key=application_key,
            page_url=page_url,
        )
        return [row["issue_key"] for row in result]

    @staticmethod
    def _get_issue_state(tx, tenant_id, application_key, page_url, issue_key):
        record = tx.run(
            """
            MATCH (:Page {tenant_id: $tenant_id, application_key: $application_key, url: $page_url})
                  -[:TRACKS_ISSUE]->(fingerprint:IssueFingerprint {issue_key: $issue_key})
            RETURN fingerprint.issue_key AS issue_key,
                   fingerprint.page_url AS page_url,
                   fingerprint.first_seen AS first_seen,
                   fingerprint.last_seen AS last_seen,
                   fingerprint.occurrence_count AS occurrence_count,
                   fingerprint.regression_flag AS regression_flag,
                   fingerprint.status AS status
            """,
            tenant_id=tenant_id,
            application_key=application_key,
            page_url=page_url,
            issue_key=issue_key,
        ).single()
        if record is None:
            return None
        return IssueLifecycleState(
            issue_key=record["issue_key"],
            page_url=record["page_url"],
            first_seen=_parse_dt(record["first_seen"]),
            last_seen=_parse_dt(record["last_seen"]),
            occurrence_count=int(record["occurrence_count"] or 1),
            regression_flag=bool(record["regression_flag"]),
            status=record["status"] or "active",
        )

    @staticmethod
    def _upsert_issue(
        tx,
        tenant_id,
        application_key,
        scan_id,
        scanned_at,
        page_url,
        selector,
        issue_key,
        category,
        message,
        dimension,
        severity,
        source_type,
        evidence,
        occurrence_count,
        regression_flag,
    ):
        tx.run(
            """
            MATCH (scan:Scan {tenant_id: $tenant_id, scan_id: $scan_id})
            MATCH (page:Page {tenant_id: $tenant_id, application_key: $application_key, url: $page_url})
            MERGE (fingerprint:IssueFingerprint {tenant_id: $tenant_id, application_key: $application_key, issue_key: $issue_key})
            ON CREATE SET fingerprint.first_seen = $scanned_at
            SET fingerprint.page_url = $page_url,
                fingerprint.selector = $selector,
                fingerprint.category = $category,
                fingerprint.message = $message,
                fingerprint.dimension = $dimension,
                fingerprint.severity = $severity,
                fingerprint.source_type = $source_type,
                fingerprint.occurrence_count = $occurrence_count,
                fingerprint.regression_flag = $regression_flag,
                fingerprint.status = 'active',
                fingerprint.last_seen = $scanned_at
            MERGE (page)-[:TRACKS_ISSUE]->(fingerprint)
            MERGE (instance:IssueInstance {tenant_id: $tenant_id, scan_id: $scan_id, issue_key: $issue_key})
            SET instance.page_url = $page_url,
                instance.selector = $selector,
                instance.category = $category,
                instance.message = $message,
                instance.dimension = $dimension,
                instance.severity = $severity,
                instance.source_type = $source_type,
                instance.scanned_at = $scanned_at,
                instance.evidence_json = $evidence_json
            MERGE (page)-[:HAS_ISSUE]->(instance)
            MERGE (scan)-[:HAS_ISSUE]->(instance)
            MERGE (instance)-[:INSTANCE_OF]->(fingerprint)
            """,
            tenant_id=tenant_id,
            application_key=application_key,
            scan_id=scan_id,
            page_url=page_url,
            selector=selector,
            issue_key=issue_key,
            category=category,
            message=message,
            dimension=dimension,
            severity=severity,
            source_type=source_type,
            occurrence_count=occurrence_count,
            regression_flag=regression_flag,
            scanned_at=scanned_at,
            evidence_json=json.dumps(evidence or {}),
        )

    @staticmethod
    def _mark_issue_resolved(tx, tenant_id, application_key, page_url, issue_key):
        tx.run(
            """
            MATCH (:Page {tenant_id: $tenant_id, application_key: $application_key, url: $page_url})
                  -[:TRACKS_ISSUE]->(fingerprint:IssueFingerprint {issue_key: $issue_key})
            SET fingerprint.status = 'resolved', fingerprint.regression_flag = false
            """,
            tenant_id=tenant_id,
            application_key=application_key,
            page_url=page_url,
            issue_key=issue_key,
        )

    @staticmethod
    def _create_score_snapshot(tx, tenant_id, scan_id, entry: ScoreHistoryEntry, relation_context: dict[str, str]):
        score_id = f"{scan_id}:{entry.entity_type}:{entry.entity_key}"
        tx.run(
            """
            MATCH (scan:Scan {tenant_id: $tenant_id, scan_id: $scan_id})
            MERGE (score:ScoreSnapshot {tenant_id: $tenant_id, score_id: $score_id})
            SET score.entity_type = $entity_type,
                score.entity_key = $entity_key,
                score.scan_id = $scan_id,
                score.scanned_at = $scanned_at,
                score.base_score = $base_score,
                score.adjusted_score = $adjusted_score,
                score.risk_score = $risk_score,
                score.risk_class = $risk_class,
                score.trend_indicator = $trend_indicator,
                score.grade = $grade
            MERGE (scan)-[:HAS_SCORE]->(score)
            """,
            tenant_id=tenant_id,
            scan_id=scan_id,
            score_id=score_id,
            entity_type=entry.entity_type,
            entity_key=entry.entity_key,
            scanned_at=entry.scanned_at.isoformat(),
            base_score=entry.base_score,
            adjusted_score=entry.adjusted_score,
            risk_score=entry.risk_score,
            risk_class=entry.risk_class,
            trend_indicator=entry.trend_indicator,
            grade=entry.grade,
        )
        if entry.entity_type == "application":
            tx.run(
                """
                MATCH (app:Application {tenant_id: $tenant_id, application_key: $application_key})
                MATCH (score:ScoreSnapshot {tenant_id: $tenant_id, score_id: $score_id})
                MERGE (app)-[:HAS_SCORE]->(score)
                """,
                tenant_id=tenant_id,
                application_key=relation_context["application_key"],
                score_id=score_id,
            )
        elif entry.entity_type == "page":
            tx.run(
                """
                MATCH (page:Page {tenant_id: $tenant_id, application_key: $application_key, url: $page_url})
                MATCH (score:ScoreSnapshot {tenant_id: $tenant_id, score_id: $score_id})
                MERGE (page)-[:HAS_SCORE]->(score)
                """,
                tenant_id=tenant_id,
                application_key=relation_context["application_key"],
                page_url=relation_context["page_url"],
                score_id=score_id,
            )

    @staticmethod
    def _latest_score(tx, tenant_id, entity_type, entity_key):
        return tx.run(
            """
            MATCH (score:ScoreSnapshot {tenant_id: $tenant_id, entity_type: $entity_type, entity_key: $entity_key})
            RETURN score
            ORDER BY score.scanned_at DESC
            LIMIT 1
            """,
            tenant_id=tenant_id,
            entity_type=entity_type,
            entity_key=entity_key,
        ).single()

    @staticmethod
    def _list_scores(tx, tenant_id, entity_type, entity_key):
        return list(
            tx.run(
                """
                MATCH (score:ScoreSnapshot {tenant_id: $tenant_id, entity_type: $entity_type, entity_key: $entity_key})
                RETURN score
                ORDER BY score.scanned_at ASC
                """,
                tenant_id=tenant_id,
                entity_type=entity_type,
                entity_key=entity_key,
            )
        )

    @staticmethod
    def _create_audit_event(tx, tenant_id, entry: AuditLogEntry):
        tx.run(
            """
            CREATE (audit:AuditEvent {
                tenant_id: $tenant_id,
                audit_id: $audit_id,
                timestamp: $timestamp,
                action: $action,
                entity_type: $entity_type,
                entity_key: $entity_key,
                scan_id: $scan_id,
                details_json: $details_json
            })
            WITH audit
            OPTIONAL MATCH (scan:Scan {tenant_id: $tenant_id, scan_id: $scan_id})
            FOREACH (_ IN CASE WHEN scan IS NULL THEN [] ELSE [1] END |
                MERGE (scan)-[:HAS_AUDIT]->(audit)
            )
            """,
            tenant_id=tenant_id,
            scan_id=entry.scan_id,
            audit_id=str(uuid.uuid4()),
            timestamp=entry.timestamp.isoformat(),
            action=entry.action,
            entity_type=entry.entity_type,
            entity_key=entry.entity_key,
            details_json=json.dumps(entry.details or {}),
        )

    @staticmethod
    def _list_audit_events(tx, tenant_id, limit):
        return list(
            tx.run(
                """
                MATCH (audit:AuditEvent {tenant_id: $tenant_id})
                RETURN audit
                ORDER BY audit.timestamp DESC
                LIMIT $limit
                """,
                tenant_id=tenant_id,
                limit=limit,
            )
        )

    @staticmethod
    def _create_scan_event(tx, tenant_id, event: ScanEventMessage):
        tx.run(
            """
            CREATE (evt:ScanEvent {
                tenant_id: $tenant_id,
                event_id: $event_id,
                timestamp: $timestamp,
                scan_id: $scan_id,
                event_type: $event_type,
                payload_json: $payload_json
            })
            WITH evt
            OPTIONAL MATCH (scan:Scan {tenant_id: $tenant_id, scan_id: $scan_id})
            FOREACH (_ IN CASE WHEN scan IS NULL THEN [] ELSE [1] END |
                MERGE (scan)-[:HAS_EVENT]->(evt)
            )
            """,
            tenant_id=tenant_id,
            scan_id=event.scan_id,
            event_id=str(uuid.uuid4()),
            timestamp=event.timestamp.isoformat(),
            event_type=event.event_type,
            payload_json=json.dumps(event.payload or {}),
        )

    @staticmethod
    def _list_scan_events(tx, tenant_id, limit):
        return list(
            tx.run(
                """
                MATCH (evt:ScanEvent {tenant_id: $tenant_id})
                RETURN evt
                ORDER BY evt.timestamp DESC
                LIMIT $limit
                """,
                tenant_id=tenant_id,
                limit=limit,
            )
        )

    @staticmethod
    def _score_from_record(record) -> ScoreHistoryEntry:
        node = record["score"]
        return ScoreHistoryEntry(
            scan_id=node["scan_id"],
            scanned_at=_parse_dt(node["scanned_at"]),
            entity_type=node["entity_type"],
            entity_key=node["entity_key"],
            base_score=float(node["base_score"]),
            adjusted_score=float(node["adjusted_score"]),
            risk_score=float(node["risk_score"]),
            risk_class=node["risk_class"],
            trend_indicator=node["trend_indicator"],
            grade=node["grade"],
        )

    @staticmethod
    def _audit_from_record(record) -> AuditLogEntry:
        node = record["audit"]
        return AuditLogEntry(
            timestamp=_parse_dt(node["timestamp"]),
            action=node["action"],
            entity_type=node["entity_type"],
            entity_key=node["entity_key"],
            scan_id=node["scan_id"],
            details=json.loads(node["details_json"] or "{}"),
        )

    @staticmethod
    def _event_from_record(record) -> ScanEventMessage:
        node = record["evt"]
        return ScanEventMessage(
            timestamp=_parse_dt(node["timestamp"]),
            tenant_id=node["tenant_id"],
            scan_id=node["scan_id"],
            event_type=node["event_type"],
            payload=json.loads(node["payload_json"] or "{}"),
        )


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if value is None:
        return datetime.utcnow()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
