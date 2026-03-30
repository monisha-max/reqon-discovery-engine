from __future__ import annotations

from datetime import datetime, timezone

from intelligence.models.contracts import AuditLogEntry, ScanEventMessage
from intelligence.repositories.contracts import AuditLogStore, ScanEventBroker


class TelemetryService:
    def __init__(self, audit_store: AuditLogStore, event_broker: ScanEventBroker) -> None:
        self.audit_store = audit_store
        self.event_broker = event_broker

    def publish_event(
        self,
        tenant_id: str,
        scan_id: str,
        event_type: str,
        payload: dict,
    ) -> None:
        event = ScanEventMessage(
            timestamp=datetime.now(timezone.utc),
            tenant_id=tenant_id,
            scan_id=scan_id,
            event_type=event_type,
            payload=payload,
        )
        self.event_broker.publish(tenant_id=tenant_id, event=event)

    def write_audit(
        self,
        tenant_id: str,
        scan_id: str,
        action: str,
        entity_type: str,
        entity_key: str,
        details: dict,
    ) -> None:
        self.audit_store.write(
            tenant_id=tenant_id,
            entry=AuditLogEntry(
                timestamp=datetime.now(timezone.utc),
                action=action,
                entity_type=entity_type,
                entity_key=entity_key,
                scan_id=scan_id,
                details=details,
            ),
        )
