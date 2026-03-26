"""
Crawler Node — Wraps Layer 2's CrawlerAgent for the LangGraph state machine.

Now supports iterative crawling: each invocation crawls a batch,
returns results, and the orchestrator decides whether to continue.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from layer2_crawler.engines.crawl4ai_engine import Crawl4AIEngine
from layer2_crawler.engines.playwright_engine import PlaywrightEngine
from layer2_crawler.classifier.page_classifier import classify_page, get_classifier_stats
from layer2_crawler.frontier.url_frontier import URLFrontier
from shared.models.crawl_models import CrawlRequest
from shared.models.page_models import PageData, PageType

logger = structlog.get_logger()

HIGH_PRIORITY_PAGE_TYPES = {
    PageType.AUTH, PageType.FORM, PageType.WIZARD,
    PageType.DASHBOARD, PageType.SETTINGS, PageType.SEARCH,
}

# Persistent engines across iterations (initialized on first call)
_crawl4ai: Optional[Crawl4AIEngine] = None
_playwright: Optional[PlaywrightEngine] = None
_frontier: Optional[URLFrontier] = None
_playwright_semaphore = asyncio.Semaphore(2)


async def _get_engines(storage_state_path: Optional[str] = None):
    global _crawl4ai, _playwright
    if _crawl4ai is None:
        _crawl4ai = Crawl4AIEngine(storage_state_path=storage_state_path)
        await _crawl4ai.start()
    if _playwright is None:
        _playwright = PlaywrightEngine(storage_state_path=storage_state_path)
        await _playwright.start()
    return _crawl4ai, _playwright


async def cleanup_engines():
    global _crawl4ai, _playwright, _frontier
    if _crawl4ai:
        await _crawl4ai.stop()
        _crawl4ai = None
    if _playwright:
        await _playwright.stop()
        _playwright = None
    _frontier = None


async def crawl_batch_node(state: dict) -> dict:
    """LangGraph node: crawl one batch of URLs and return results for evaluation."""
    global _frontier

    request_data = state["request"]
    storage_state_path = state.get("storage_state_path")
    existing_pages = state.get("pages", [])
    iteration = state.get("iteration", 0) + 1

    if isinstance(request_data, dict):
        request = CrawlRequest(**request_data)
    else:
        request = request_data

    crawl4ai, playwright = await _get_engines(storage_state_path)

    # Initialize frontier on first iteration
    if _frontier is None:
        _frontier = URLFrontier(max_pages=request.max_pages, max_depth=request.max_depth)
        _frontier.add_url(request.target_url, depth=0)
        # Re-register already visited URLs
        for p in existing_pages:
            url = p["url"] if isinstance(p, dict) else p.url
            _frontier.mark_visited(url)

    logger.info(
        "crawl_batch_node.starting",
        iteration=iteration,
        frontier_size=_frontier.stats["frontier_size"],
        pages_so_far=len(existing_pages),
    )

    # Get batch
    batch = _frontier.get_batch(size=5)
    if not batch:
        return {
            "pages": existing_pages,
            "iteration": iteration,
            "phase": "crawl",
            "frontier_stats": _frontier.stats,
            "new_urls_this_iteration": 0,
            "should_continue": False,
        }

    # Concurrent crawl
    new_pages = []
    crawl_tasks = [crawl4ai.crawl_page(d.url, d.depth) for d in batch]
    results = await asyncio.gather(*crawl_tasks, return_exceptions=True)

    deep_queue = []

    for i, result in enumerate(results):
        if isinstance(result, Exception) or result is None:
            _frontier.mark_visited(batch[i].url)
            # SPA fallback for first iteration failures
            if iteration == 1 and (isinstance(result, Exception) or result is None):
                async with _playwright_semaphore:
                    result = await playwright.crawl_spa_page(batch[i].url, batch[i].depth)
            if result is None:
                continue

        page = result
        page_type, confidence = await classify_page(page)
        page.page_type = page_type
        page.page_type_confidence = confidence

        # Queue deep analysis
        if page_type in HIGH_PRIORITY_PAGE_TYPES and confidence > 0.3 and page.crawl_method != "playwright":
            deep_queue.append((page, batch[i]))

        # Add links to frontier
        new_count = _frontier.add_urls(page.links, source_url=page.url, depth=batch[i].depth + 1)
        _frontier.mark_visited(page.url, page_type=page_type)
        new_pages.append(page)

    # Concurrent deep analysis
    if deep_queue:
        async def _deep(page, disc):
            async with _playwright_semaphore:
                deep = await playwright.analyze_page(disc.url, disc.depth)
            if deep:
                deep.markdown_content = page.markdown_content
                deep.page_type = page.page_type
                deep.page_type_confidence = page.page_type_confidence
                deep.links = list(set(page.links + deep.links))
                deep.link_count = len(deep.links)
                # Feed hidden URLs back
                if deep.hidden_urls_discovered:
                    _frontier.add_urls(deep.hidden_urls_discovered, source_url=deep.url, depth=disc.depth + 1)
                return deep
            return page

        deep_results = await asyncio.gather(*[_deep(p, d) for p, d in deep_queue], return_exceptions=True)
        for j, dr in enumerate(deep_results):
            if isinstance(dr, PageData):
                idx = next((k for k, p in enumerate(new_pages) if p.url == deep_queue[j][0].url), None)
                if idx is not None:
                    new_pages[idx] = dr

    # Convert to dicts and merge
    new_page_dicts = [p.model_dump() for p in new_pages]
    all_pages = existing_pages + new_page_dicts

    should_continue, reason = _frontier.should_continue()

    # Page type distribution for replanning
    type_dist = {}
    for p in all_pages:
        pt = p.get("page_type", "unknown") if isinstance(p, dict) else p.page_type
        type_dist[pt] = type_dist.get(pt, 0) + 1

    logger.info(
        "crawl_batch_node.complete",
        iteration=iteration,
        new_pages=len(new_pages),
        total_pages=len(all_pages),
        should_continue=should_continue,
        reason=reason,
        type_distribution=type_dist,
        classifier_stats=get_classifier_stats(),
    )

    return {
        "pages": all_pages,
        "iteration": iteration,
        "phase": "crawl",
        "frontier_stats": _frontier.stats,
        "new_urls_this_iteration": len(new_pages),
        "should_continue": should_continue,
        "continue_reason": reason,
        "page_type_distribution": type_dist,
        "coverage_score": _frontier.stats["coverage"],
    }
