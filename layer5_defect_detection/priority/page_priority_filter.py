"""
Priority Page Filter — selects high-risk pages for defect detection.

Reuses existing PageType enum and PageData.performance.cls from Layer 2.
No re-classification — reads what the crawler already determined.

Tier 1: PageType is AUTH, WIZARD, FORM, or DASHBOARD
Tier 2: URL matches known high-risk path patterns (fallback for low-confidence classifications)
Tier 3: Crawl-time CLS > 0.1 (already showed layout instability before load)
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import structlog

logger = structlog.get_logger()

# Tier 1: PageType values that are always high priority
TIER1_PAGE_TYPES = {"auth", "wizard", "form", "dashboard"}

# Tier 2: URL path patterns for pages that carry financial/auth/data risk
TIER2_URL_PATTERNS: list[tuple[str, str]] = [
    # Auth
    (r"/login", "auth"),
    (r"/signin", "auth"),
    (r"/sign-in", "auth"),
    (r"/register", "auth"),
    (r"/signup", "auth"),
    (r"/sign-up", "auth"),
    (r"/forgot", "auth"),
    (r"/reset-password", "auth"),
    (r"/auth", "auth"),
    # Checkout / Payment
    (r"/checkout", "checkout"),
    (r"/payment", "payment"),
    (r"/cart", "cart"),
    (r"/order", "order"),
    (r"/billing", "billing"),
    (r"/subscribe", "subscribe"),
    (r"/book", "booking"),
    # Dashboards / Data
    (r"/dashboard", "dashboard"),
    (r"/overview", "dashboard"),
    (r"/analytics", "dashboard"),
    (r"/reports?$", "dashboard"),
    (r"/report/", "dashboard"),
]

# Tier 3: CLS threshold — layout shift already happening at crawl time
CLS_INSTABILITY_THRESHOLD = 0.1


def _get_page_type(page: dict) -> str:
    return (page.get("page_type") or "unknown").lower()


def _get_cls(page: dict) -> float:
    perf = page.get("performance") or {}
    if isinstance(perf, dict):
        return float(perf.get("cls") or 0.0)
    # pydantic model serialized as object with attribute
    cls_val = getattr(perf, "cls", None)
    return float(cls_val) if cls_val is not None else 0.0


def _url_tier2_match(url: str) -> str | None:
    """Returns a label if URL matches Tier 2 patterns, else None."""
    path = urlparse(url).path.lower()
    for pattern, label in TIER2_URL_PATTERNS:
        if re.search(pattern, path):
            return label
    return None


def _page_slug(page: dict) -> str:
    """Stable directory-safe slug: {page_type}_{path_segment}"""
    page_type = _get_page_type(page)
    url = page.get("url", "")
    path = urlparse(url).path.strip("/").replace("/", "_") or "index"
    # Keep it short and filesystem-safe
    path = re.sub(r"[^a-z0-9_-]", "", path.lower())[:40]
    return f"{page_type}_{path}" if path else page_type


def get_priority_pages(
    pages: list[dict],
    max_pages: int = 10,
) -> list[dict]:
    """
    Filter crawled pages to high-priority targets for defect detection.

    Returns pages sorted by tier (Tier 1 first), deduplicated by URL,
    capped at max_pages. Each returned dict has '_priority_tier' and
    '_priority_reason' keys added.
    """
    seen_urls: set[str] = set()
    tier1: list[dict] = []
    tier2: list[dict] = []
    tier3: list[dict] = []

    for page in pages:
        url = page.get("url", "")
        if not url or url in seen_urls:
            continue

        page_type = _get_page_type(page)
        confidence = float(page.get("page_type_confidence") or 0.0)

        # Tier 1: PageType is directly high-priority
        if page_type in TIER1_PAGE_TYPES:
            enriched = dict(page)
            enriched["_priority_tier"] = 1
            enriched["_priority_reason"] = f"Tier 1 — page_type={page_type}"
            enriched["_page_slug"] = _page_slug(page)
            tier1.append(enriched)
            seen_urls.add(url)
            continue

        # Tier 2: URL pattern match — trust the URL over the classifier.
        # A page at /login is high-priority regardless of what the classifier said.
        url_label = _url_tier2_match(url)
        if url_label:
            enriched = dict(page)
            enriched["_priority_tier"] = 2
            enriched["_priority_reason"] = f"Tier 2 — URL matches /{url_label} pattern"
            enriched["_page_slug"] = _page_slug(page)
            tier2.append(enriched)
            seen_urls.add(url)
            continue

        # Tier 3: CLS instability at crawl time
        cls = _get_cls(page)
        if cls > CLS_INSTABILITY_THRESHOLD:
            enriched = dict(page)
            enriched["_priority_tier"] = 3
            enriched["_priority_reason"] = f"Tier 3 — CLS={cls:.3f} > {CLS_INSTABILITY_THRESHOLD} at crawl time"
            enriched["_page_slug"] = _page_slug(page)
            tier3.append(enriched)
            seen_urls.add(url)

    # Sort Tier 1 by page_type priority: auth/wizard first, then form, then dashboard
    _tier1_order = {"auth": 0, "wizard": 1, "form": 2, "dashboard": 3}
    tier1.sort(key=lambda p: _tier1_order.get(_get_page_type(p), 99))

    # Sort Tier 3 by CLS descending (most unstable first)
    tier3.sort(key=lambda p: _get_cls(p), reverse=True)

    result = (tier1 + tier2 + tier3)[:max_pages]

    logger.info(
        "page_priority_filter.selected",
        total_input=len(pages),
        tier1=len(tier1),
        tier2=len(tier2),
        tier3=len(tier3),
        selected=len(result),
        max_pages=max_pages,
    )

    return result


def probe_priority_paths(base_url: str) -> list[dict]:
    """
    Build synthetic page dicts for common high-risk paths on base_url.

    Used as a fallback when crawled pages contain no priority pages
    (e.g., a news/social site where auth pages weren't visited during crawl).
    Each returned dict is compatible with get_priority_pages() output.
    """
    from urllib.parse import urljoin

    PROBE_PATHS: list[tuple[str, str, str]] = [
        ("/login",    "auth",      "login"),
        ("/signin",   "auth",      "signin"),
        ("/register", "auth",      "register"),
        ("/signup",   "auth",      "signup"),
        ("/checkout", "checkout",  "checkout"),
        ("/cart",     "cart",      "cart"),
        ("/dashboard","dashboard", "dashboard"),
    ]

    synthetic: list[dict] = []
    for path, label, slug_suffix in PROBE_PATHS:
        url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
        synthetic.append({
            "url": url,
            "page_type": label,
            "page_type_confidence": 0.0,   # not crawled, confidence unknown
            "performance": {},
            "_priority_tier": 2,
            "_priority_reason": f"Tier 2 — probed path {path} (not in crawl)",
            "_page_slug": f"{label}_{slug_suffix}",
        })

    return synthetic
