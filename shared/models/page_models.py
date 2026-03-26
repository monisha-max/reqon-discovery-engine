from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PageType(str, Enum):
    DASHBOARD = "dashboard"
    LIST_TABLE = "list_table"
    FORM = "form"
    WIZARD = "wizard"
    REPORT = "report"
    DETAIL = "detail"
    SETTINGS = "settings"
    AUTH = "auth"
    LANDING = "landing"
    SEARCH = "search"
    PROFILE = "profile"
    ERROR = "error"
    UNKNOWN = "unknown"


class PerformanceMetrics(BaseModel):
    """Core Web Vitals and performance data."""
    # Core Web Vitals
    lcp_ms: Optional[float] = None           # Largest Contentful Paint
    fid_ms: Optional[float] = None           # First Input Delay
    cls: Optional[float] = None              # Cumulative Layout Shift
    fcp_ms: Optional[float] = None           # First Contentful Paint
    ttfb_ms: Optional[float] = None          # Time to First Byte
    # Resource metrics
    dom_content_loaded_ms: Optional[float] = None
    load_event_ms: Optional[float] = None
    total_resources: int = 0
    total_transfer_bytes: int = 0
    js_heap_size_bytes: int = 0


class AccessibilityIssue(BaseModel):
    """A single accessibility violation."""
    rule_id: str
    description: str
    impact: str  # critical, serious, moderate, minor
    target_selector: str
    html_snippet: str = ""


class AccessibilitySnapshot(BaseModel):
    """Accessibility analysis results."""
    violations: list[AccessibilityIssue] = Field(default_factory=list)
    total_violations: int = 0
    critical_count: int = 0
    serious_count: int = 0
    # Accessibility tree summary
    landmark_roles: list[str] = Field(default_factory=list)
    aria_labels_count: int = 0
    images_without_alt: int = 0
    inputs_without_label: int = 0


class InteractiveElement(BaseModel):
    """An interactive element discovered on the page."""
    selector: str
    tag: str
    element_type: str  # button, dropdown, modal_trigger, tab, accordion, link
    text: Optional[str] = None
    is_visible: bool = True
    state_change_detected: bool = False
    new_urls_discovered: list[str] = Field(default_factory=list)


class PageData(BaseModel):
    """Extracted data from a single crawled page."""
    url: str
    title: Optional[str] = None
    status_code: Optional[int] = None

    # Content
    markdown_content: Optional[str] = None
    html_snippet: Optional[str] = None  # first 5000 chars of HTML
    links: list[str] = Field(default_factory=list)

    # Screenshots
    screenshot_path: Optional[str] = None

    # DOM Analysis
    form_count: int = 0
    input_count: int = 0
    button_count: int = 0
    table_count: int = 0
    image_count: int = 0
    link_count: int = 0
    heading_counts: dict[str, int] = Field(default_factory=dict)
    has_nav: bool = False
    has_sidebar: bool = False
    has_footer: bool = False
    has_search: bool = False
    has_login_form: bool = False
    has_charts: bool = False

    # Telemetry
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[dict] = Field(default_factory=list)
    load_time_ms: Optional[float] = None

    # Performance (Core Web Vitals)
    performance: Optional[PerformanceMetrics] = None

    # Accessibility
    accessibility: Optional[AccessibilitySnapshot] = None

    # Interactive Elements
    interactive_elements: list[InteractiveElement] = Field(default_factory=list)
    hidden_urls_discovered: list[str] = Field(default_factory=list)

    # SPA Detection
    is_spa: bool = False
    spa_framework: Optional[str] = None  # react, angular, vue, etc.

    # Classification
    page_type: PageType = PageType.UNKNOWN
    page_type_confidence: float = 0.0

    # Metadata
    depth: int = 0
    crawl_method: str = "crawl4ai"  # crawl4ai, playwright, spa_fallback


class CrawlResult(BaseModel):
    """Complete result of crawling a site."""
    target_url: str
    pages: list[PageData] = Field(default_factory=list)
    total_urls_discovered: int = 0
    total_pages_crawled: int = 0
    crawl_duration_seconds: float = 0.0
    coverage_score: float = 0.0
    is_spa: bool = False
    spa_framework: Optional[str] = None
