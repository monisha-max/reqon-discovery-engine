"""
Crawler Agent — Combines Crawl4AI (breadth) + Playwright (depth).

Improvements over v1:
- Concurrent crawling (5-10 pages at once via asyncio.gather)
- SPA detection + Playwright fallback when Crawl4AI fails
- Interactive element exploration discovers hidden URLs
- Hidden URLs from button clicks/modals fed back into frontier
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import structlog

from layer2_crawler.engines.crawl4ai_engine import Crawl4AIEngine
from layer2_crawler.engines.playwright_engine import PlaywrightEngine
from layer2_crawler.classifier.page_classifier import classify_page
from layer2_crawler.frontier.url_frontier import URLFrontier
from shared.models.crawl_models import CrawlRequest, DiscoveredURL
from shared.models.page_models import CrawlResult, PageData, PageType

logger = structlog.get_logger()

# Page types that warrant deep Playwright analysis
HIGH_PRIORITY_PAGE_TYPES = {
    PageType.AUTH, PageType.FORM, PageType.WIZARD,
    PageType.DASHBOARD, PageType.SETTINGS, PageType.SEARCH,
}

# Concurrency settings
CRAWL4AI_CONCURRENCY = 5  # fast engine — run 5 at once
PLAYWRIGHT_CONCURRENCY = 2  # heavy engine — run 2 at once


class CrawlerAgent:
    """Dual-engine crawler with concurrent crawling and SPA handling."""

    def __init__(self, request: CrawlRequest, storage_state_path: Optional[str] = None):
        self.request = request
        self.crawl4ai = Crawl4AIEngine(storage_state_path=storage_state_path)
        self.playwright = PlaywrightEngine(storage_state_path=storage_state_path)
        self.frontier = URLFrontier(
            max_pages=request.max_pages,
            max_depth=request.max_depth,
        )
        self.pages: list[PageData] = []
        self._is_spa = False
        self._spa_framework = None
        self._playwright_semaphore = asyncio.Semaphore(PLAYWRIGHT_CONCURRENCY)

    async def start(self):
        await self.crawl4ai.start()
        await self.playwright.start()
        logger.info("crawler_agent.started", target=self.request.target_url)

    async def stop(self):
        await self.crawl4ai.stop()
        await self.playwright.stop()
        logger.info("crawler_agent.stopped")

    async def crawl(self) -> CrawlResult:
        """Execute the full crawl with concurrency and adaptive termination."""
        start_time = time.time()
        await self.start()

        try:
            # Seed the frontier
            self.frontier.add_url(self.request.target_url, depth=0)

            # Probe common SPA routes to improve discovery on JS-heavy apps
            base = self.request.target_url.rstrip("/")
            common_paths = [
                "/login", "/signin", "/register", "/signup", "/dashboard",
                "/settings", "/profile", "/search", "/cart", "/checkout",
                "/admin", "/about", "/contact", "/help", "/faq",
            ]
            for path in common_paths:
                self.frontier.add_url(base + path, source_url=base, depth=1)

            iteration = 0
            while True:
                iteration += 1

                should_continue, reason = self.frontier.should_continue()
                if not should_continue:
                    logger.info("crawler_agent.stopping", reason=reason, stats=self.frontier.stats)
                    break

                batch = self.frontier.get_batch(size=CRAWL4AI_CONCURRENCY)
                if not batch:
                    logger.info("crawler_agent.frontier_empty")
                    break

                logger.info(
                    "crawler_agent.iteration",
                    iteration=iteration,
                    batch_size=len(batch),
                    stats=self.frontier.stats,
                )

                # Phase 1: Concurrent fast crawl with Crawl4AI
                crawl_tasks = [
                    self._crawl_single(discovered_url)
                    for discovered_url in batch
                ]
                results = await asyncio.gather(*crawl_tasks, return_exceptions=True)

                # Collect pages that need deep analysis
                deep_analysis_queue = []

                for result in results:
                    if isinstance(result, Exception):
                        logger.error("crawler_agent.crawl_error", error=str(result))
                        continue
                    if result is None:
                        continue

                    page, discovered_url = result

                    # Check if Crawl4AI failed (SPA/JS-heavy) — fallback to Playwright
                    if page is None or (page.link_count == 0 and iteration == 1):
                        if not self._is_spa:
                            # Try Playwright for the first failure to detect SPA
                            spa_page = await self.playwright.crawl_spa_page(
                                discovered_url.url, discovered_url.depth
                            )
                            if spa_page and spa_page.is_spa:
                                self._is_spa = True
                                self._spa_framework = spa_page.spa_framework
                                logger.info(
                                    "crawler_agent.spa_detected",
                                    framework=self._spa_framework,
                                    url=discovered_url.url,
                                )
                            if spa_page:
                                page = spa_page

                    if page is None:
                        self.frontier.mark_visited(discovered_url.url)
                        continue

                    # Classify
                    page_type, confidence = await classify_page(page)
                    page.page_type = page_type
                    page.page_type_confidence = confidence

                    # Queue for deep analysis if high priority
                    if page_type in HIGH_PRIORITY_PAGE_TYPES and confidence > 0.3:
                        if page.crawl_method != "playwright":
                            deep_analysis_queue.append((page, discovered_url))
                        else:
                            # Already deeply analyzed — add hidden URLs to frontier
                            self._process_hidden_urls(page, discovered_url)

                    # Add links to frontier
                    new_count = self.frontier.add_urls(
                        page.links,
                        source_url=page.url,
                        depth=discovered_url.depth + 1,
                    )

                    self.frontier.mark_visited(discovered_url.url, page_type=page_type)
                    self.pages.append(page)

                    logger.info(
                        "crawler_agent.page_processed",
                        url=page.url[:80],
                        type=page_type.value,
                        confidence=round(confidence, 2),
                        new_links=new_count,
                        method=page.crawl_method,
                    )

                # Phase 2: Concurrent deep analysis with Playwright
                if deep_analysis_queue:
                    deep_tasks = [
                        self._deep_analyze(page, discovered_url)
                        for page, discovered_url in deep_analysis_queue
                    ]
                    deep_results = await asyncio.gather(*deep_tasks, return_exceptions=True)

                    for i, result in enumerate(deep_results):
                        if isinstance(result, Exception):
                            logger.error("crawler_agent.deep_error", error=str(result))
                            continue
                        if result is None:
                            continue

                        # Replace the shallow page with the deep one
                        original_page, _ = deep_analysis_queue[i]
                        idx = next((j for j, p in enumerate(self.pages) if p.url == original_page.url), None)
                        if idx is not None:
                            self.pages[idx] = result

            elapsed = time.time() - start_time

            result = CrawlResult(
                target_url=self.request.target_url,
                pages=self.pages,
                total_urls_discovered=self.frontier.stats["total_discovered"],
                total_pages_crawled=self.frontier.stats["pages_crawled"],
                crawl_duration_seconds=round(elapsed, 2),
                coverage_score=self.frontier.stats["coverage"],
                is_spa=self._is_spa,
                spa_framework=self._spa_framework,
            )

            logger.info(
                "crawler_agent.complete",
                pages_crawled=result.total_pages_crawled,
                urls_discovered=result.total_urls_discovered,
                duration=result.crawl_duration_seconds,
                coverage=result.coverage_score,
                page_types=self.frontier.stats["page_types_seen"],
                is_spa=self._is_spa,
            )

            return result

        finally:
            await self.stop()

    async def _crawl_single(self, discovered_url: DiscoveredURL) -> tuple[Optional[PageData], DiscoveredURL] | None:
        """Crawl a single URL with Crawl4AI (or Playwright for SPA sites)."""
        try:
            if self._is_spa:
                # SPA mode: use Playwright for everything
                async with self._playwright_semaphore:
                    page = await self.playwright.crawl_spa_page(discovered_url.url, discovered_url.depth)
            else:
                page = await self.crawl4ai.crawl_page(discovered_url.url, discovered_url.depth)

            return (page, discovered_url)
        except Exception as e:
            logger.error("crawler_agent.crawl_single_error", url=discovered_url.url, error=str(e))
            self.frontier.mark_visited(discovered_url.url)
            return None

    async def _deep_analyze(self, shallow_page: PageData, discovered_url: DiscoveredURL) -> Optional[PageData]:
        """Deep Playwright analysis for high-priority pages."""
        async with self._playwright_semaphore:
            deep_page = await self.playwright.analyze_page(discovered_url.url, discovered_url.depth)

        if not deep_page:
            return None

        # Merge: keep deep data, supplement with Crawl4AI's markdown
        deep_page.markdown_content = shallow_page.markdown_content
        deep_page.page_type = shallow_page.page_type
        deep_page.page_type_confidence = shallow_page.page_type_confidence

        # Combine links
        all_links = list(set(shallow_page.links + deep_page.links))
        deep_page.links = all_links
        deep_page.link_count = len(all_links)

        # Add hidden URLs from interactive exploration to frontier
        self._process_hidden_urls(deep_page, discovered_url)

        logger.info(
            "crawler_agent.deep_analyzed",
            url=deep_page.url[:80],
            a11y_violations=deep_page.accessibility.total_violations if deep_page.accessibility else 0,
            interactive=len(deep_page.interactive_elements),
            hidden_urls=len(deep_page.hidden_urls_discovered),
            perf_fcp=deep_page.performance.fcp_ms if deep_page.performance else None,
        )

        return deep_page

    def _process_hidden_urls(self, page: PageData, discovered_url: DiscoveredURL):
        """Feed hidden URLs (from interactive exploration) back into the frontier."""
        if page.hidden_urls_discovered:
            new_count = self.frontier.add_urls(
                page.hidden_urls_discovered,
                source_url=page.url,
                depth=discovered_url.depth + 1,
            )
            if new_count > 0:
                logger.info(
                    "crawler_agent.hidden_urls_added",
                    url=page.url[:80],
                    new_urls=new_count,
                )
