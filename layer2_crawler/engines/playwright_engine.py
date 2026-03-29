"""
Playwright Engine — The Deep Analysis Engine.

Capabilities:
- Full-page screenshots
- DOM structural analysis
- Console error + network failure monitoring
- Core Web Vitals (LCP, FCP, CLS, TTFB)
- Accessibility snapshot (ARIA roles, labels, violations)
- Interactive element exploration (buttons, dropdowns, modals, tabs)
- SPA/JS-heavy page handling
"""
from __future__ import annotations

import os
import time
from typing import Optional
from urllib.parse import urlparse

import structlog
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

import layer4_auth.monitor_singleton as _monitor
from shared.models.page_models import (
    AccessibilityIssue, AccessibilitySnapshot, InteractiveElement,
    PageData, PerformanceMetrics,
)

logger = structlog.get_logger()


class PlaywrightEngine:
    """Deep browser-level analysis engine using Playwright."""

    def __init__(self, storage_state_path: Optional[str] = None, screenshot_dir: str = "output/screenshots"):
        self.storage_state_path = storage_state_path
        self.screenshot_dir = screenshot_dir
        self._playwright = None
        self._browser: Optional[Browser] = None

    async def start(self):
        os.makedirs(self.screenshot_dir, exist_ok=True)
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("playwright_engine.started")

    async def stop(self):
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("playwright_engine.stopped")

    async def _create_context(self) -> BrowserContext:
        context_args = {
            "viewport": {"width": 1920, "height": 1080},
            "ignore_https_errors": True,
        }
        if self.storage_state_path and os.path.exists(self.storage_state_path):
            context_args["storage_state"] = self.storage_state_path
        return await self._browser.new_context(**context_args)

    async def analyze_page(self, url: str, depth: int = 0) -> Optional[PageData]:
        """Deep analysis: screenshots, DOM, console, network, performance, a11y, interactions."""
        if not self._browser:
            await self.start()

        context = await self._create_context()
        page = await context.new_page()

        console_errors = []
        failed_requests = []
        network_requests = []

        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("requestfailed", lambda req: failed_requests.append({
            "url": req.url, "method": req.method, "failure": req.failure,
        }))
        page.on("requestfinished", lambda req: network_requests.append({
            "url": req.url, "method": req.method,
        }))

        # Report every response to SessionMonitor (tracks 401/403 + logout redirects)
        def _on_response(response):
            _monitor.report_request(response.status, response.url)

        def _on_request(request):
            # Detect redirects: if a request has a redirected-from, report it
            redirected_from = request.redirected_from
            if redirected_from:
                _monitor.report_redirect(redirected_from.url, request.url)

        page.on("response", _on_response)
        page.on("request", _on_request)

        start_time = time.time()
        try:
            response = await page.goto(url, wait_until="networkidle", timeout=30000)
            status_code = response.status if response else None
            elapsed = (time.time() - start_time) * 1000

            await page.wait_for_timeout(1000)

            # Screenshot
            safe_name = urlparse(url).path.replace("/", "_").strip("_") or "index"
            screenshot_path = os.path.join(self.screenshot_dir, f"{safe_name}_{int(time.time())}.png")
            await page.screenshot(path=screenshot_path, full_page=True)

            # Parallel extraction
            dom_info = await self._extract_dom_info(page)
            title = await page.title()
            links = await self._extract_links(page, url)
            performance = await self._extract_performance(page)
            accessibility = await self._extract_accessibility(page)
            spa_info = await self._detect_spa(page)
            interactive_elements, hidden_urls = await self._explore_interactive_elements(page, url)

            page_data = PageData(
                url=url,
                title=title,
                status_code=status_code,
                links=links,
                screenshot_path=screenshot_path,
                link_count=len(links),
                console_errors=console_errors,
                failed_requests=failed_requests,
                load_time_ms=elapsed,
                depth=depth,
                performance=performance,
                accessibility=accessibility,
                interactive_elements=interactive_elements,
                hidden_urls_discovered=hidden_urls,
                is_spa=spa_info.get("is_spa", False),
                spa_framework=spa_info.get("framework"),
                crawl_method="playwright",
                **dom_info,
            )

            logger.info(
                "playwright_engine.page_analyzed",
                url=url,
                status=status_code,
                console_errors=len(console_errors),
                failed_requests=len(failed_requests),
                a11y_violations=accessibility.total_violations if accessibility else 0,
                interactive_found=len(interactive_elements),
                hidden_urls=len(hidden_urls),
                is_spa=spa_info.get("is_spa", False),
                time_ms=round(elapsed),
            )
            return page_data

        except Exception as e:
            logger.error("playwright_engine.error", url=url, error=str(e))
            return None
        finally:
            await page.close()
            await context.close()

    async def crawl_spa_page(self, url: str, depth: int = 0) -> Optional[PageData]:
        """Fallback crawler for SPA/JS-heavy pages where Crawl4AI fails."""
        return await self.analyze_page(url, depth)

    async def _extract_links(self, page: Page, base_url: str) -> list[str]:
        """Extract internal links from the rendered page."""
        links = await page.eval_on_selector_all(
            "a[href]",
            "elements => elements.map(el => el.href).filter(h => h && !h.startsWith('javascript:'))"
        )
        base_domain = urlparse(base_url).netloc
        return list(set(l for l in links if urlparse(l).netloc == base_domain))

    async def _extract_dom_info(self, page: Page) -> dict:
        """Extract structural DOM information."""
        return await page.evaluate("""() => {
            const count = (sel) => document.querySelectorAll(sel).length;
            const exists = (sel) => document.querySelector(sel) !== null;
            const headings = {};
            for (let i = 1; i <= 6; i++) {
                const c = count('h' + i);
                if (c > 0) headings['h' + i] = c;
            }
            const inputs = document.querySelectorAll('input');
            let hasPassword = false;
            inputs.forEach(inp => { if (inp.type === 'password') hasPassword = true; });
            const hasCharts = exists('canvas') || exists('[class*="chart"]') ||
                exists('[class*="graph"]') || exists('[class*="recharts"]') || exists('[class*="highcharts"]');
            return {
                form_count: count('form'),
                input_count: count('input, textarea, select'),
                button_count: count('button, [role="button"], input[type="submit"]'),
                table_count: count('table'),
                image_count: count('img'),
                heading_counts: headings,
                has_nav: exists('nav, [role="navigation"]'),
                has_sidebar: exists('aside, [class*="sidebar"], [class*="side-nav"]'),
                has_footer: exists('footer, [role="contentinfo"]'),
                has_search: exists('input[type="search"], [class*="search"], [role="search"]'),
                has_login_form: hasPassword && count('form') > 0,
                has_charts: hasCharts,
            };
        }""")

    async def _extract_performance(self, page: Page) -> PerformanceMetrics:
        """Extract Core Web Vitals and performance metrics."""
        try:
            metrics = await page.evaluate("""() => {
                const perf = performance.getEntriesByType('navigation')[0] || {};
                const paint = performance.getEntriesByType('paint') || [];
                const fcp = paint.find(p => p.name === 'first-contentful-paint');

                // LCP via PerformanceObserver snapshot
                let lcp = null;
                const lcpEntries = performance.getEntriesByType('largest-contentful-paint');
                if (lcpEntries && lcpEntries.length > 0) {
                    lcp = lcpEntries[lcpEntries.length - 1].startTime;
                }

                // CLS approximation
                let cls = 0;
                const layoutShifts = performance.getEntriesByType('layout-shift');
                if (layoutShifts) {
                    layoutShifts.forEach(entry => {
                        if (!entry.hadRecentInput) cls += entry.value;
                    });
                }

                // Resource count and total size
                const resources = performance.getEntriesByType('resource') || [];
                let totalTransfer = 0;
                resources.forEach(r => { totalTransfer += r.transferSize || 0; });

                return {
                    fcp_ms: fcp ? fcp.startTime : null,
                    lcp_ms: lcp,
                    cls: cls || null,
                    ttfb_ms: perf.responseStart ? perf.responseStart - perf.requestStart : null,
                    dom_content_loaded_ms: perf.domContentLoadedEventEnd || null,
                    load_event_ms: perf.loadEventEnd || null,
                    total_resources: resources.length,
                    total_transfer_bytes: totalTransfer,
                };
            }""")

            # JS heap size (Chrome only)
            try:
                heap = await page.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : 0")
            except Exception:
                heap = 0

            return PerformanceMetrics(
                fcp_ms=metrics.get("fcp_ms"),
                lcp_ms=metrics.get("lcp_ms"),
                cls=metrics.get("cls"),
                ttfb_ms=metrics.get("ttfb_ms"),
                dom_content_loaded_ms=metrics.get("dom_content_loaded_ms"),
                load_event_ms=metrics.get("load_event_ms"),
                total_resources=metrics.get("total_resources", 0),
                total_transfer_bytes=metrics.get("total_transfer_bytes", 0),
                js_heap_size_bytes=heap,
            )
        except Exception as e:
            logger.warning("playwright_engine.performance_error", error=str(e))
            return PerformanceMetrics()

    async def _extract_accessibility(self, page: Page) -> AccessibilitySnapshot:
        """Extract accessibility violations and ARIA information."""
        try:
            a11y_data = await page.evaluate("""() => {
                const violations = [];

                // Check images without alt
                const imgs = document.querySelectorAll('img');
                let imgsNoAlt = 0;
                imgs.forEach(img => {
                    if (!img.alt && !img.getAttribute('aria-label') && !img.getAttribute('aria-labelledby')) {
                        imgsNoAlt++;
                        violations.push({
                            rule_id: 'image-alt',
                            description: 'Image missing alt text',
                            impact: 'critical',
                            target_selector: img.tagName.toLowerCase() +
                                (img.className ? '.' + img.className.split(' ')[0] : '') +
                                (img.id ? '#' + img.id : ''),
                            html_snippet: img.outerHTML.substring(0, 200),
                        });
                    }
                });

                // Check inputs without labels
                const inputs = document.querySelectorAll('input, textarea, select');
                let inputsNoLabel = 0;
                inputs.forEach(inp => {
                    if (inp.type === 'hidden' || inp.type === 'submit' || inp.type === 'button') return;
                    const id = inp.id;
                    const hasLabel = id && document.querySelector('label[for="' + id + '"]');
                    const hasAriaLabel = inp.getAttribute('aria-label') || inp.getAttribute('aria-labelledby');
                    const wrappedInLabel = inp.closest('label');
                    if (!hasLabel && !hasAriaLabel && !wrappedInLabel) {
                        inputsNoLabel++;
                        violations.push({
                            rule_id: 'label',
                            description: 'Form input missing associated label',
                            impact: 'critical',
                            target_selector: inp.tagName.toLowerCase() +
                                '[type="' + (inp.type || 'text') + '"]' +
                                (inp.name ? '[name="' + inp.name + '"]' : ''),
                            html_snippet: inp.outerHTML.substring(0, 200),
                        });
                    }
                });

                // Check color contrast (simplified — checks if any text is very light on white)
                // Check heading hierarchy
                const headings = document.querySelectorAll('h1, h2, h3, h4, h5, h6');
                let lastLevel = 0;
                headings.forEach(h => {
                    const level = parseInt(h.tagName[1]);
                    if (level > lastLevel + 1 && lastLevel > 0) {
                        violations.push({
                            rule_id: 'heading-order',
                            description: 'Heading levels should increase by one (skipped from h' + lastLevel + ' to h' + level + ')',
                            impact: 'moderate',
                            target_selector: h.tagName.toLowerCase(),
                            html_snippet: h.outerHTML.substring(0, 200),
                        });
                    }
                    lastLevel = level;
                });

                // Check buttons without accessible name
                document.querySelectorAll('button, [role="button"]').forEach(btn => {
                    const text = btn.textContent.trim();
                    const ariaLabel = btn.getAttribute('aria-label');
                    if (!text && !ariaLabel) {
                        violations.push({
                            rule_id: 'button-name',
                            description: 'Button has no accessible name',
                            impact: 'critical',
                            target_selector: btn.tagName.toLowerCase() +
                                (btn.className ? '.' + btn.className.split(' ')[0] : ''),
                            html_snippet: btn.outerHTML.substring(0, 200),
                        });
                    }
                });

                // Check links without href or text
                document.querySelectorAll('a').forEach(a => {
                    if (!a.href && !a.getAttribute('aria-label')) return;
                    const text = a.textContent.trim();
                    const ariaLabel = a.getAttribute('aria-label');
                    if (!text && !ariaLabel) {
                        violations.push({
                            rule_id: 'link-name',
                            description: 'Link has no accessible name',
                            impact: 'serious',
                            target_selector: 'a' + (a.className ? '.' + a.className.split(' ')[0] : ''),
                            html_snippet: a.outerHTML.substring(0, 200),
                        });
                    }
                });

                // Landmark roles
                const landmarks = [];
                document.querySelectorAll('[role]').forEach(el => {
                    landmarks.push(el.getAttribute('role'));
                });
                ['nav', 'main', 'header', 'footer', 'aside', 'form'].forEach(tag => {
                    if (document.querySelector(tag)) landmarks.push(tag);
                });

                // ARIA labels count
                const ariaLabels = document.querySelectorAll('[aria-label], [aria-labelledby], [aria-describedby]').length;

                return {
                    violations: violations,
                    images_without_alt: imgsNoAlt,
                    inputs_without_label: inputsNoLabel,
                    landmark_roles: [...new Set(landmarks)],
                    aria_labels_count: ariaLabels,
                };
            }""")

            violations = [AccessibilityIssue(**v) for v in a11y_data.get("violations", [])]

            return AccessibilitySnapshot(
                violations=violations,
                total_violations=len(violations),
                critical_count=sum(1 for v in violations if v.impact == "critical"),
                serious_count=sum(1 for v in violations if v.impact == "serious"),
                landmark_roles=a11y_data.get("landmark_roles", []),
                aria_labels_count=a11y_data.get("aria_labels_count", 0),
                images_without_alt=a11y_data.get("images_without_alt", 0),
                inputs_without_label=a11y_data.get("inputs_without_label", 0),
            )
        except Exception as e:
            logger.warning("playwright_engine.accessibility_error", error=str(e))
            return AccessibilitySnapshot()

    async def _detect_spa(self, page: Page) -> dict:
        """Detect if the page is a Single Page Application and identify the framework."""
        try:
            return await page.evaluate("""() => {
                const result = { is_spa: false, framework: null };

                // React
                if (document.querySelector('[data-reactroot]') || document.querySelector('#__next') ||
                    window.__REACT_DEVTOOLS_GLOBAL_HOOK__ || document.querySelector('#root[data-reactroot]')) {
                    result.is_spa = true;
                    result.framework = document.querySelector('#__next') ? 'nextjs' : 'react';
                }

                // Angular
                if (document.querySelector('[ng-version]') || document.querySelector('app-root') ||
                    window.getAllAngularTestabilities) {
                    result.is_spa = true;
                    result.framework = 'angular';
                }

                // Vue
                if (document.querySelector('[data-v-]') || document.querySelector('#__nuxt') ||
                    window.__VUE__) {
                    result.is_spa = true;
                    result.framework = document.querySelector('#__nuxt') ? 'nuxt' : 'vue';
                }

                // Svelte
                if (document.querySelector('[class*="svelte-"]')) {
                    result.is_spa = true;
                    result.framework = 'svelte';
                }

                // Generic SPA signals
                if (!result.is_spa) {
                    const hasRouter = document.querySelector('[data-router]') ||
                        document.querySelector('router-view') ||
                        document.querySelector('router-outlet');
                    const hasMinimalHTML = document.body.children.length <= 3;
                    const hasAppDiv = document.querySelector('#app') || document.querySelector('#root');
                    if ((hasRouter || hasMinimalHTML) && hasAppDiv) {
                        result.is_spa = true;
                        result.framework = 'unknown_spa';
                    }
                }

                return result;
            }""")
        except Exception:
            return {"is_spa": False, "framework": None}

    async def _explore_interactive_elements(self, page: Page, base_url: str) -> tuple[list[InteractiveElement], list[str]]:
        """Discover interactive elements and click them to find hidden states/URLs."""
        interactive_elements = []
        hidden_urls = []
        base_domain = urlparse(base_url).netloc

        try:
            # Find clickable elements that might reveal hidden content
            elements_data = await page.evaluate("""() => {
                const elements = [];
                const seen = new Set();

                // Buttons (not submit/reset)
                document.querySelectorAll('button:not([type="submit"]):not([type="reset"]), [role="button"]').forEach(el => {
                    const text = el.textContent.trim().substring(0, 50);
                    const sel = el.id ? '#' + el.id :
                        (el.className ? el.tagName.toLowerCase() + '.' + el.className.split(' ')[0] : el.tagName.toLowerCase());
                    if (!seen.has(sel) && el.offsetParent !== null) {
                        seen.add(sel);
                        elements.push({ selector: sel, tag: el.tagName, element_type: 'button', text: text });
                    }
                });

                // Dropdowns / selects that might trigger navigation
                document.querySelectorAll('[data-toggle="dropdown"], [aria-haspopup="true"], details > summary').forEach(el => {
                    const text = el.textContent.trim().substring(0, 50);
                    const sel = el.id ? '#' + el.id :
                        (el.className ? el.tagName.toLowerCase() + '.' + el.className.split(' ')[0] : el.tagName.toLowerCase());
                    if (!seen.has(sel)) {
                        seen.add(sel);
                        elements.push({ selector: sel, tag: el.tagName, element_type: 'dropdown', text: text });
                    }
                });

                // Tabs
                document.querySelectorAll('[role="tab"], [data-toggle="tab"], .tab, .nav-tab').forEach(el => {
                    const text = el.textContent.trim().substring(0, 50);
                    const sel = el.id ? '#' + el.id :
                        (el.className ? el.tagName.toLowerCase() + '.' + el.className.split(' ')[0] : el.tagName.toLowerCase());
                    if (!seen.has(sel)) {
                        seen.add(sel);
                        elements.push({ selector: sel, tag: el.tagName, element_type: 'tab', text: text });
                    }
                });

                // Accordion triggers
                document.querySelectorAll('[data-toggle="collapse"], [aria-expanded], .accordion-header, .accordion-button').forEach(el => {
                    const text = el.textContent.trim().substring(0, 50);
                    const sel = el.id ? '#' + el.id :
                        (el.className ? el.tagName.toLowerCase() + '.' + el.className.split(' ')[0] : el.tagName.toLowerCase());
                    if (!seen.has(sel)) {
                        seen.add(sel);
                        elements.push({ selector: sel, tag: el.tagName, element_type: 'accordion', text: text });
                    }
                });

                // Modal triggers
                document.querySelectorAll('[data-toggle="modal"], [data-bs-toggle="modal"], [aria-haspopup="dialog"]').forEach(el => {
                    const text = el.textContent.trim().substring(0, 50);
                    const sel = el.id ? '#' + el.id :
                        (el.className ? el.tagName.toLowerCase() + '.' + el.className.split(' ')[0] : el.tagName.toLowerCase());
                    if (!seen.has(sel)) {
                        seen.add(sel);
                        elements.push({ selector: sel, tag: el.tagName, element_type: 'modal_trigger', text: text });
                    }
                });

                return elements.slice(0, 20);  // Cap at 20 to avoid slow crawls
            }""")

            # Try clicking each interactive element to discover hidden URLs
            for elem_data in elements_data:
                try:
                    selector = elem_data["selector"]
                    el = await page.query_selector(selector)
                    if not el or not await el.is_visible():
                        interactive_elements.append(InteractiveElement(**elem_data, is_visible=False))
                        continue

                    # Snapshot links before click
                    links_before = set(await page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)"
                    ))

                    # Click and wait briefly
                    await el.click(timeout=3000)
                    await page.wait_for_timeout(800)

                    # Snapshot links after click
                    links_after = set(await page.eval_on_selector_all(
                        "a[href]", "els => els.map(e => e.href)"
                    ))

                    new_links = links_after - links_before
                    internal_new = [l for l in new_links if urlparse(l).netloc == base_domain]

                    state_changed = len(new_links) > 0

                    interactive_elements.append(InteractiveElement(
                        **elem_data,
                        is_visible=True,
                        state_change_detected=state_changed,
                        new_urls_discovered=internal_new,
                    ))

                    hidden_urls.extend(internal_new)

                except Exception:
                    interactive_elements.append(InteractiveElement(**elem_data))
                    continue

        except Exception as e:
            logger.warning("playwright_engine.interactive_error", error=str(e))

        hidden_urls = list(set(hidden_urls))
        if hidden_urls:
            logger.info("playwright_engine.hidden_urls_found", count=len(hidden_urls))

        return interactive_elements, hidden_urls
