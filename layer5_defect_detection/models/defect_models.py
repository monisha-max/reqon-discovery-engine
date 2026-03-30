from __future__ import annotations

import math
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class DefectSeverity(str, Enum):
    CRITICAL = "critical"   # Overlap on CTA button
    HIGH = "high"           # Text truncated in h1/h2/h3; form fields overlapping
    MEDIUM = "medium"       # Element partially/fully off-screen; nav overflow
    LOW = "low"             # Layout drift > 5px from baseline
    INFO = "info"           # WCAG AA contrast failure


class DefectCategory(str, Enum):
    OVERLAP = "overlap"
    TEXT_TRUNCATION = "text_truncation"
    OVERFLOW = "overflow"
    LAYOUT_DRIFT = "layout_drift"
    CONTRAST = "contrast"
    FORM_COLLISION = "form_collision"
    BROKEN_LINK = "broken_link"
    CONSOLE_ERROR = "console_error"
    NETWORK_FAILURE = "network_failure"
    # DOM behavioral checks
    DOM_STRUCTURAL = "dom_structural"       # Heading hierarchy, duplicate IDs
    FORM_INTEGRITY = "form_integrity"       # Missing labels, missing submit button
    ARIA_VIOLATION = "aria_violation"       # Broken ARIA patterns
    EMPTY_INTERACTIVE = "empty_interactive" # Buttons/links with no accessible label
    MISSING_ALT_TEXT = "missing_alt_text"   # Content images without alt text
    STATE_ANOMALY = "state_anomaly"         # Stuck spinners, visible error states
    EMPTY_CONTAINER = "empty_container"     # Empty tables/lists that should have data
    # Network telemetry
    SLOW_ENDPOINT = "slow_endpoint"         # Requests exceeding latency thresholds
    REQUEST_RETRY = "request_retry"         # Same URL fetched 3+ times (flakiness)
    AUTH_FAILURE = "auth_failure"           # Mid-session 401/403 on XHR/API calls
    SECURITY_HYGIENE = "security_hygiene"   # CSP violations, mixed content, CORS blocks


class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    def intersects(self, other: "BoundingBox") -> bool:
        return not (
            self.right <= other.x
            or other.right <= self.x
            or self.bottom <= other.y
            or other.bottom <= self.y
        )

    def intersection_area(self, other: "BoundingBox") -> float:
        if not self.intersects(other):
            return 0.0
        ix = min(self.right, other.right) - max(self.x, other.x)
        iy = min(self.bottom, other.bottom) - max(self.y, other.y)
        return ix * iy

    def distance_to(self, other: "BoundingBox") -> float:
        dx = abs(self.center_x - other.center_x)
        dy = abs(self.center_y - other.center_y)
        return math.sqrt(dx * dx + dy * dy)

    def contains_point(self, px: float, py: float) -> bool:
        return self.x <= px <= self.right and self.y <= py <= self.bottom


class ElementInfo(BaseModel):
    selector: str
    tag: str
    text: str = ""
    role: str = ""
    bbox: BoundingBox
    is_cta: bool = False         # Matches CTA keyword list
    is_truncated: bool = False   # scrollWidth > clientWidth
    is_off_screen: bool = False  # Outside viewport bounds
    has_overflow_clipping: bool = False
    color: str = ""              # CSS color from getComputedStyle
    background_color: str = ""   # CSS background-color
    font_size: float = 16.0
    is_dynamic: bool = False     # Falls within a masked dynamic region


class DefectFinding(BaseModel):
    defect_id: str = Field(default_factory=lambda: str(uuid4()))
    severity: DefectSeverity
    category: DefectCategory
    title: str
    description: str
    element_selector: str
    element_bbox: BoundingBox
    conflicting_selector: Optional[str] = None
    conflicting_bbox: Optional[BoundingBox] = None
    overlap_area_px: float = 0.0
    drift_px: float = 0.0
    contrast_ratio: Optional[float] = None
    snapshot_phase: str = "baseline"   # baseline | peak | post
    annotation_color: str = "red"


class SnapshotReport(BaseModel):
    phase: str                                   # baseline | peak | post
    url: str
    page_type: str = "unknown"
    page_slug: str = ""                          # sanitized dir name
    screenshot_path: str = ""
    annotated_screenshot_path: Optional[str] = None
    viewport_width: int = 1920
    viewport_height: int = 1080
    findings: list[DefectFinding] = Field(default_factory=list)
    total_elements_analyzed: int = 0
    dynamic_regions_masked: int = 0
    captured_at: str = ""


class RegressionDefect(BaseModel):
    defect: DefectFinding
    introduced_at_phase: str       # "peak" or "post"
    baseline_clear: bool = True


class ComparisonResult(BaseModel):
    baseline_finding_count: int = 0
    peak_finding_count: int = 0
    post_finding_count: int = 0
    regression_defects: list[RegressionDefect] = Field(default_factory=list)
    regression_score: float = 0.0   # 0–100


class PageDefectSummary(BaseModel):
    url: str
    page_type: str
    page_slug: str
    priority_reason: str             # Tier 1 / Tier 2 URL / Tier 3 CLS
    snapshots: list[SnapshotReport] = Field(default_factory=list)
    comparison: Optional[ComparisonResult] = None
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    info_count: int = 0


class DefectDetectionResult(BaseModel):
    target_url: str
    run_id: str = ""
    pages_analyzed: list[PageDefectSummary] = Field(default_factory=list)
    total_priority_pages: int = 0
    total_defects: int = 0
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0
    info_count: int = 0
    max_regression_score: float = 0.0
    report_path: str = ""
    timestamp: str = ""
    duration_seconds: float = 0.0
