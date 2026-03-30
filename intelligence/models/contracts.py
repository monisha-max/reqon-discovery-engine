from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Severity(str, Enum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    INFORMATIONAL = "informational"


class Dimension(str, Enum):
    ACCESSIBILITY = "accessibility"
    PERFORMANCE = "performance"
    SEO = "seo"
    VISUAL = "visual"
    FUNCTIONAL = "functional"
    SECURITY = "security"


class PerformanceSnapshot(BaseModel):
    scalability: float = Field(ge=0, le=100)
    responsiveness: float = Field(ge=0, le=100)
    stability: float = Field(ge=0, le=100)


class Issue(BaseModel):
    category: str = Field(min_length=1, max_length=128)
    severity: Severity
    dimension: Dimension
    message: str = Field(min_length=1, max_length=2000)
    occurrence_count: int = Field(default=1, ge=1)
    regression_flag: bool = False
    source_type: str = Field(default="crawl", min_length=1, max_length=64)
    evidence: dict[str, Any] = Field(default_factory=dict)


class Element(BaseModel):
    selector: str = Field(min_length=1, max_length=500)
    issues: list[Issue] = Field(default_factory=list)


class Page(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, max_length=255)
    page_type: str = Field(default="unknown", max_length=64)
    elements: list[Element] = Field(default_factory=list)
    performance_snapshot: PerformanceSnapshot | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)


class ScanRequest(BaseModel):
    scan_id: str = Field(min_length=1, max_length=128)
    application_name: str = Field(min_length=1, max_length=255)
    application_key: str = Field(min_length=1, max_length=500)
    scanned_at: datetime
    pages: list[Page] = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scanned_at")
    @classmethod
    def ensure_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class DiscoveryScanBundle(BaseModel):
    target_url: str = Field(min_length=1, max_length=1000)
    scan_id: str = Field(min_length=1, max_length=128)
    scanned_at: datetime
    pages: list[dict[str, Any]] = Field(default_factory=list)
    perf_result: dict[str, Any] | None = None
    defect_result: dict[str, Any] | None = None
    scan_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scanned_at")
    @classmethod
    def ensure_bundle_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value


class DimensionBreakdown(BaseModel):
    dimension: Dimension
    penalty: float
    issue_count: int


class PageScore(BaseModel):
    application_key: str
    url: str
    issue_count: int
    base_score: float
    adjusted_score: float
    risk_score: float
    risk_class: str
    trend_indicator: str
    grade: str
    priority_flags: list[str]
    dimension_breakdown: list[DimensionBreakdown]


class ApplicationScore(BaseModel):
    application_name: str
    application_key: str
    base_score: float
    adjusted_score: float
    risk_score: float
    risk_class: str
    trend_indicator: str = "stable"
    grade: str
    priority_flags: list[str]


class LifecycleSummary(BaseModel):
    new_issues: int
    recurring_issues: int
    regressions: int
    resolved_issues: int


class PageIngestSummary(BaseModel):
    url: str
    active_issue_count: int
    new_issues: int
    recurring_issues: int
    regressions: int
    resolved_issues: int


class ScanIngestResponse(BaseModel):
    tenant_id: str
    scan_id: str
    application_name: str
    application_key: str
    scanned_at: datetime
    application_score: ApplicationScore
    page_scores: list[PageScore]
    lifecycle_summary: LifecycleSummary
    page_summaries: list[PageIngestSummary]
    top_priorities: list[dict[str, Any]]


class ScoreHistoryEntry(BaseModel):
    scan_id: str
    scanned_at: datetime
    entity_type: str
    entity_key: str
    base_score: float
    adjusted_score: float
    risk_score: float
    risk_class: str
    trend_indicator: str
    grade: str


class ScoreHistoryResponse(BaseModel):
    tenant_id: str
    entity_type: str
    entity_key: str
    entries: list[ScoreHistoryEntry]


class AuditLogEntry(BaseModel):
    timestamp: datetime
    action: str
    entity_type: str
    entity_key: str
    scan_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class AuditLogResponse(BaseModel):
    tenant_id: str
    entries: list[AuditLogEntry]


class ScanEventMessage(BaseModel):
    timestamp: datetime
    tenant_id: str
    scan_id: str
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ScanEventHistoryResponse(BaseModel):
    tenant_id: str
    events: list[ScanEventMessage]
