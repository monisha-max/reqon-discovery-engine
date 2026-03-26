"""
Crawl4AI Engine — The Discovery (Breadth) Engine.

Handles fast breadth-first sweep of the application.
Outputs LLM-friendly Markdown, extracts all links, processes pages in bulk.
Typically covers 50-100+ pages in minutes.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import structlog
from bs4 import BeautifulSoup
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

from shared.models.page_models import PageData

logger = structlog.get_logger()


class Crawl4AIEngine:
    """Fast breadth-first crawler using Crawl4AI for LLM-ready content extraction."""

    def __init__(self, storage_state_path: Optional[str] = None):
        self.storage_state_path = storage_state_path
        self._crawler: Optional[AsyncWebCrawler] = None

    async def start(self):
        browser_config = BrowserConfig(
            headless=True,
            verbose=False,
        )
        self._crawler = AsyncWebCrawler(config=browser_config)
        await self._crawler.start()
        logger.info("crawl4ai_engine.started")

    async def stop(self):
        if self._crawler:
            await self._crawler.close()
            logger.info("crawl4ai_engine.stopped")

    async def crawl_page(self, url: str, depth: int = 0) -> Optional[PageData]:
        """Crawl a single page and extract content + links."""
        if not self._crawler:
            await self.start()

        start_time = time.time()
        try:
            run_config = CrawlerRunConfig(
                word_count_threshold=10,
                exclude_external_links=False,
                process_iframes=False,
            )

            result = await self._crawler.arun(url=url, config=run_config)

            if not result.success:
                logger.warning("crawl4ai_engine.page_failed", url=url, error=result.error_message)
                return None

            # Extract all internal links
            links = []
            base_domain = urlparse(url).netloc
            if result.links:
                for link_group in [result.links.get("internal", []), result.links.get("external", [])]:
                    for link in link_group:
                        href = link.get("href", "") if isinstance(link, dict) else str(link)
                        if href and urlparse(href).netloc == base_domain:
                            links.append(href)

            elapsed = (time.time() - start_time) * 1000

            # Extract DOM features from HTML
            dom_info = self._extract_dom_from_html(result.html) if result.html else {}

            page = PageData(
                url=url,
                title=result.metadata.get("title", "") if result.metadata else "",
                status_code=result.status_code,
                markdown_content=result.markdown[:10000] if result.markdown else None,
                html_snippet=result.html[:5000] if result.html else None,
                links=list(set(links)),
                link_count=len(links),
                load_time_ms=elapsed,
                depth=depth,
                **dom_info,
            )

            logger.info("crawl4ai_engine.page_crawled", url=url, links_found=len(links), time_ms=round(elapsed))
            return page

        except Exception as e:
            logger.error("crawl4ai_engine.error", url=url, error=str(e))
            return None

    def _extract_dom_from_html(self, html: str) -> dict:
        """Extract DOM structural features from raw HTML using BeautifulSoup."""
        try:
            soup = BeautifulSoup(html, "lxml")

            # Heading counts
            headings = {}
            for i in range(1, 7):
                count = len(soup.find_all(f"h{i}"))
                if count > 0:
                    headings[f"h{i}"] = count

            # Detect password field (login form indicator)
            has_password = bool(soup.find("input", {"type": "password"}))
            form_count = len(soup.find_all("form"))

            return {
                "form_count": form_count,
                "input_count": len(soup.find_all(["input", "textarea", "select"])),
                "button_count": len(soup.find_all(["button"])) + len(soup.find_all("input", {"type": "submit"})),
                "table_count": len(soup.find_all("table")),
                "image_count": len(soup.find_all("img")),
                "heading_counts": headings,
                "has_nav": bool(soup.find("nav") or soup.find(attrs={"role": "navigation"})),
                "has_sidebar": bool(soup.find("aside") or soup.find(class_=lambda c: c and "sidebar" in str(c).lower())),
                "has_footer": bool(soup.find("footer") or soup.find(attrs={"role": "contentinfo"})),
                "has_search": bool(soup.find("input", {"type": "search"}) or soup.find(attrs={"role": "search"}) or soup.find(class_=lambda c: c and "search" in str(c).lower())),
                "has_login_form": has_password and form_count > 0,
                "has_charts": bool(soup.find("canvas") or soup.find(class_=lambda c: c and any(kw in str(c).lower() for kw in ["chart", "graph", "recharts"]))),
            }
        except Exception:
            return {}

    async def crawl_batch(self, urls: list[tuple[str, int]]) -> list[PageData]:
        """Crawl multiple URLs concurrently. Each tuple is (url, depth)."""
        tasks = [self.crawl_page(url, depth) for url, depth in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        pages = []
        for r in results:
            if isinstance(r, PageData):
                pages.append(r)
            elif isinstance(r, Exception):
                logger.error("crawl4ai_engine.batch_error", error=str(r))
        return pages
