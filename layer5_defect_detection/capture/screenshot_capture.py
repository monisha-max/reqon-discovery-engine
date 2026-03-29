"""
Screenshot Capture — manages its own async_playwright session for Layer 5.

Layer 2's Playwright browser is already closed by the time Layer 5 runs,
so this class launches a fresh browser context for each defect detection run.

Usage:
    capture = ScreenshotCapture(target_url, storage_state_path, output_dir)
    await capture.start()
    screenshot_path, page = await capture.capture("baseline", url)
    # ... analyze page while open ...
    await page.close()
    await capture.stop()

Or the higher-level:
    screenshot_path, dom_data = await capture.capture_and_release("baseline", url)
"""
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any, Optional

import structlog
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

logger = structlog.get_logger()

# Default viewport matches Layer 2's PlaywrightEngine convention
DEFAULT_VIEWPORT = {"width": 1920, "height": 1080}

# How long to wait after navigation before screenshot (ms)
_SETTLE_MS = 1500


class ScreenshotCapture:
    """Owns a single Playwright browser session for all three capture phases."""

    def __init__(
        self,
        target_url: str,
        storage_state_path: Optional[str],
        output_dir: str = "output/defect_reports",
        viewport: Optional[dict] = None,
    ) -> None:
        self.target_url = target_url
        self.storage_state_path = storage_state_path
        self.output_dir = output_dir
        self.viewport = viewport or DEFAULT_VIEWPORT

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def start(self) -> None:
        """Launch browser and create context (reuse for all captures)."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)

        context_kwargs: dict[str, Any] = {"viewport": self.viewport}
        if self.storage_state_path and os.path.exists(self.storage_state_path):
            context_kwargs["storage_state"] = self.storage_state_path
            logger.info("screenshot_capture.using_auth_state", path=self.storage_state_path)

        self._context = await self._browser.new_context(**context_kwargs)
        logger.info("screenshot_capture.started", viewport=self.viewport)

    async def stop(self) -> None:
        """Close browser and playwright session."""
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as exc:
            logger.warning("screenshot_capture.stop_error", error=str(exc))
        finally:
            self._context = None
            self._browser = None
            self._playwright = None

    async def capture(
        self,
        phase: str,
        url: Optional[str] = None,
        monitor_events: bool = False,
    ) -> tuple[str, Page]:
        """
        Navigate to url (or target_url), take a full-page screenshot, return
        (screenshot_path, live_page). Caller MUST close the page when done.

        If monitor_events=True, console errors and failed requests are attached
        to page._reqon_console_errors and page._reqon_failed_requests so the
        caller can retrieve them after navigation.
        """
        nav_url = url or self.target_url
        page: Page = await self._context.new_page()

        if monitor_events:
            page._reqon_console_errors: list[str] = []
            page._reqon_failed_requests: list[dict] = []

            def _on_console(msg: Any) -> None:
                if msg.type == "error":
                    page._reqon_console_errors.append(msg.text)

            def _on_requestfailed(request: Any) -> None:
                page._reqon_failed_requests.append({
                    "url": request.url,
                    "failure_text": request.failure or "unknown",
                })

            page.on("console", _on_console)
            page.on("requestfailed", _on_requestfailed)

        try:
            await page.goto(nav_url, wait_until="networkidle", timeout=30_000)
        except Exception:
            # Fallback: domcontentloaded is more reliable on slow pages under load
            try:
                await page.goto(nav_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                logger.warning(
                    "screenshot_capture.navigation_failed",
                    url=nav_url, phase=phase, error=str(exc),
                )

        await page.wait_for_timeout(_SETTLE_MS)

        os.makedirs(self.output_dir, exist_ok=True)
        ts = int(time.time())
        filename = f"defect_{phase}_{ts}.png"
        screenshot_path = os.path.join(self.output_dir, filename)

        await page.screenshot(path=screenshot_path, full_page=True)
        logger.info("screenshot_capture.captured", phase=phase, url=nav_url, path=screenshot_path)

        return screenshot_path, page

    async def capture_and_release(
        self, phase: str, url: Optional[str] = None
    ) -> tuple[str, dict]:
        """
        Capture screenshot, extract serializable DOM snapshot, close page.

        Returns (screenshot_path, dom_snapshot_dict) — no live Page objects
        are held across async boundaries.
        """
        screenshot_path, page = await self.capture(phase, url)
        try:
            dom_snapshot = await _extract_basic_dom(page)
        except Exception as exc:
            logger.warning("screenshot_capture.dom_extract_failed", error=str(exc))
            dom_snapshot = {}
        finally:
            await page.close()

        return screenshot_path, dom_snapshot


async def _extract_basic_dom(page: Page) -> dict:
    """
    Extract lightweight DOM metadata for logging/debugging.
    Heavy analysis is done by LayoutAnalyzer on a live page.
    """
    try:
        return await page.evaluate("""
        () => ({
            title: document.title,
            url: window.location.href,
            element_count: document.querySelectorAll('*').length,
            interactive_count: document.querySelectorAll(
                'button, a[href], input, select, textarea'
            ).length,
        })
        """)
    except Exception:
        return {}
