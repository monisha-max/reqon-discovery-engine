"""
URL Frontier — Priority Queue + Information Foraging.

Manages which URLs to crawl next based on priority scoring,
and implements adaptive termination using information foraging theory.
"""
from __future__ import annotations

import heapq
import re
from urllib.parse import urlparse

import structlog

from shared.models.crawl_models import DiscoveredURL
from shared.models.page_models import PageType

logger = structlog.get_logger()

# URL patterns that indicate high-priority pages
HIGH_PRIORITY_PATTERNS = [
    (r"/login|/signin|/auth", 0.95),        # Auth pages — critical
    (r"/checkout|/payment|/billing", 0.9),    # Transactional pages
    (r"/form|/submit|/create|/new", 0.85),   # Form pages
    (r"/dashboard|/admin", 0.8),              # Dashboard/admin
    (r"/settings|/account|/profile", 0.75),   # User-facing config
    (r"/search|/filter", 0.7),                # Search functionality
    (r"/api/|/graphql", 0.3),                 # API endpoints — lower priority
    (r"\.(css|js|png|jpg|svg|ico|woff)", 0.0),  # Static assets — skip
]


class URLFrontier:
    """Priority queue for URLs with information foraging-based termination."""

    def __init__(self, max_pages: int = 100, max_depth: int = 5):
        self.max_pages = max_pages
        self.max_depth = max_depth
        self._queue: list[tuple[float, int, DiscoveredURL]] = []  # (neg_priority, counter, url)
        self._counter = 0
        self._visited: set[str] = set()
        self._all_discovered: set[str] = set()

        # Information foraging metrics
        self._pages_crawled = 0
        self._new_urls_per_iteration: list[int] = []
        self._page_types_seen: set[str] = set()

    def add_url(self, url: str, source_url: str = None, depth: int = 0, link_text: str = None):
        """Add a URL to the frontier with automatic priority scoring."""
        normalized = self._normalize_url(url)
        if normalized in self._all_discovered or normalized in self._visited:
            return
        if depth > self.max_depth:
            return
        if self._should_skip(normalized):
            return

        priority = self._calculate_priority(normalized, depth, link_text)
        discovered = DiscoveredURL(
            url=normalized,
            source_url=source_url,
            depth=depth,
            priority=priority,
            link_text=link_text,
        )

        self._all_discovered.add(normalized)
        heapq.heappush(self._queue, (-priority, self._counter, discovered))
        self._counter += 1

    def add_urls(self, urls: list[str], source_url: str = None, depth: int = 0):
        """Add multiple URLs at once."""
        new_count = 0
        for url in urls:
            before = len(self._all_discovered)
            self.add_url(url, source_url=source_url, depth=depth)
            if len(self._all_discovered) > before:
                new_count += 1
        self._new_urls_per_iteration.append(new_count)
        return new_count

    def get_next(self) -> DiscoveredURL | None:
        """Get the highest priority URL to crawl next."""
        while self._queue:
            _, _, discovered = heapq.heappop(self._queue)
            if discovered.url not in self._visited:
                return discovered
        return None

    def get_batch(self, size: int = 5) -> list[DiscoveredURL]:
        """Get a batch of high-priority URLs."""
        batch = []
        while len(batch) < size:
            url = self.get_next()
            if url is None:
                break
            batch.append(url)
        return batch

    def mark_visited(self, url: str, page_type: PageType = None):
        """Mark a URL as visited and track page type coverage."""
        self._visited.add(self._normalize_url(url))
        self._pages_crawled += 1
        if page_type:
            self._page_types_seen.add(page_type.value)

    def should_continue(self) -> tuple[bool, str]:
        """Information Foraging: decide whether to continue crawling.

        Returns (should_continue, reason).
        Uses diminishing returns — if recent iterations yield few new URLs,
        we're approaching saturation.
        """
        # Hard limits
        if self._pages_crawled >= self.max_pages:
            return False, f"max_pages_reached ({self.max_pages})"

        if not self._queue:
            return False, "frontier_empty"

        # Minimum crawl before evaluating
        if self._pages_crawled < 5:
            return True, "minimum_not_reached"

        # Information foraging: check diminishing returns
        if len(self._new_urls_per_iteration) >= 3:
            recent = self._new_urls_per_iteration[-3:]
            avg_new = sum(recent) / len(recent)

            # If last 3 iterations averaged < 2 new URLs, we're saturated
            if avg_new < 2.0:
                return False, f"saturation_detected (avg_new_urls={avg_new:.1f})"

        # Coverage check: how many page types have we seen?
        coverage = len(self._page_types_seen) / 12.0  # 12 possible types
        if coverage >= 0.6 and self._pages_crawled >= 20:
            # Good coverage, check if frontier is mostly low-priority
            if self._queue:
                top_priority = -self._queue[0][0]
                if top_priority < 0.3:
                    return False, f"good_coverage ({coverage:.0%}) + low_priority_remaining"

        return True, "continuing"

    @property
    def stats(self) -> dict:
        return {
            "pages_crawled": self._pages_crawled,
            "frontier_size": len(self._queue),
            "total_discovered": len(self._all_discovered),
            "visited": len(self._visited),
            "page_types_seen": list(self._page_types_seen),
            "coverage": len(self._page_types_seen) / 12.0,
        }

    def _calculate_priority(self, url: str, depth: int, link_text: str = None) -> float:
        """Score URL priority based on patterns and depth."""
        priority = 0.5  # base

        path = urlparse(url).path.lower()
        for pattern, score in HIGH_PRIORITY_PATTERNS:
            if re.search(pattern, path):
                priority = score
                break

        # Depth penalty: shallower pages are generally more important
        depth_penalty = depth * 0.05
        priority = max(0.0, priority - depth_penalty)

        # Boost if link text suggests important page
        if link_text:
            text = link_text.lower()
            if any(kw in text for kw in ["login", "sign in", "dashboard", "settings", "checkout"]):
                priority = min(1.0, priority + 0.2)

        return priority

    def _normalize_url(self, url: str) -> str:
        """Normalize URL for deduplication."""
        parsed = urlparse(url)
        # Remove trailing slash, fragments
        path = parsed.path.rstrip("/") or "/"
        normalized = f"{parsed.scheme}://{parsed.netloc}{path}"
        if parsed.query:
            normalized += f"?{parsed.query}"
        return normalized

    def _should_skip(self, url: str) -> bool:
        """Skip static assets and non-page URLs."""
        skip_extensions = {".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                          ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip"}
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in skip_extensions)
