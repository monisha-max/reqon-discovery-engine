from __future__ import annotations

from typing import Any

from config.settings import settings
from intelligence.models.contracts import AuditLogResponse, ScanEventHistoryResponse, ScoreHistoryResponse
from intelligence.services.ingestion import ScanIngestionService
from intelligence.services.normalizer import build_discovery_bundle, normalize_discovery_bundle


def process_final_state(final_state: dict, *, target_url: str, scan_id: str) -> dict[str, Any]:
    from intelligence.repositories.factory import get_intelligence_store

    tenant_id = settings.REQON_DEFAULT_TENANT
    bundle = build_discovery_bundle(
        final_state=final_state,
        target_url=target_url,
        scan_id=scan_id,
    )
    scan_request = normalize_discovery_bundle(bundle)
    store = get_intelligence_store()
    store.ping()
    service = ScanIngestionService(
        graph_store=store,
        history_store=store,
        audit_store=store,
        event_broker=store,
    )
    response = service.ingest_scan(scan_request, tenant_id=tenant_id)
    return response.model_dump(mode="json")


def application_history(application_key: str) -> dict[str, Any]:
    from intelligence.repositories.factory import get_intelligence_store

    tenant_id = settings.REQON_DEFAULT_TENANT
    store = get_intelligence_store()
    store.ping()
    response = ScoreHistoryResponse(
        tenant_id=tenant_id,
        entity_type="application",
        entity_key=application_key,
        entries=store.list_entries(tenant_id, "application", application_key),
    )
    return response.model_dump(mode="json")


def page_history(page_url: str) -> dict[str, Any]:
    from intelligence.repositories.factory import get_intelligence_store

    tenant_id = settings.REQON_DEFAULT_TENANT
    store = get_intelligence_store()
    store.ping()
    response = ScoreHistoryResponse(
        tenant_id=tenant_id,
        entity_type="page",
        entity_key=page_url,
        entries=store.list_entries(tenant_id, "page", page_url),
    )
    return response.model_dump(mode="json")


def audit_history(limit: int = 100) -> dict[str, Any]:
    from intelligence.repositories.factory import get_intelligence_store

    tenant_id = settings.REQON_DEFAULT_TENANT
    store = get_intelligence_store()
    store.ping()
    response = AuditLogResponse(
        tenant_id=tenant_id,
        entries=store.list_recent(tenant_id, limit),
    )
    return response.model_dump(mode="json")


def event_history(limit: int = 100) -> dict[str, Any]:
    from intelligence.repositories.factory import get_intelligence_store

    tenant_id = settings.REQON_DEFAULT_TENANT
    store = get_intelligence_store()
    store.ping()
    response = ScanEventHistoryResponse(
        tenant_id=tenant_id,
        events=store.recent_events(tenant_id, limit),
    )
    return response.model_dump(mode="json")
