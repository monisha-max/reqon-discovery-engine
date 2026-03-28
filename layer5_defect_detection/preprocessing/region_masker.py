"""
Region Masker — identifies known dynamic UI regions via page.evaluate()
so they can be excluded from defect analysis.

Dynamic regions: cookie banners, toast notifications, chat widgets, ads, GDPR overlays.
Elements whose center point falls within any masked region are skipped.
"""
from __future__ import annotations

import structlog

from layer5_defect_detection.models.defect_models import BoundingBox

logger = structlog.get_logger()

# CSS selectors for known dynamic / noise-generating regions
DYNAMIC_REGION_SELECTORS = [
    # Cookie consent
    "#cookieConsent",
    ".cookie-banner",
    "[id*='cookie']",
    "[class*='cookie-banner']",
    "[class*='consent']",
    ".cc-window",
    "#onetrust-banner-sdk",
    "[id*='onetrust']",
    # Toast / notification / alert
    ".toast",
    ".notification",
    "[role='alert']",
    "[role='status']",
    ".snackbar",
    ".flash-message",
    "[class*='toast']",
    "[class*='snackbar']",
    # Live chat widgets
    "#intercom-container",
    ".intercom-lightweight-app",
    "[id*='hubspot']",
    ".drift-widget",
    "#fc_widget",
    "#launcher",
    ".chat-widget",
    "[class*='chat-bubble']",
    "[class*='chat-widget']",
    # GDPR / overlays
    ".gdpr-overlay",
    "#gdpr-banner",
    ".modal-backdrop",
    # Ads
    "ins.adsbygoogle",
    "[id*='ad-container']",
    ".ad-banner",
    "[class*='advertisement']",
]

_EXTRACT_JS = """
(selectors) => {
    const regions = [];
    for (const sel of selectors) {
        try {
            const els = document.querySelectorAll(sel);
            els.forEach(el => {
                const r = el.getBoundingClientRect();
                const absY = r.top + window.scrollY;
                const absX = r.left + window.scrollX;
                if (r.width > 0 && r.height > 0) {
                    regions.push({
                        x: absX, y: absY,
                        width: r.width, height: r.height
                    });
                }
            });
        } catch(e) {}
    }
    return regions;
}
"""


async def get_masked_regions(page: object) -> list[BoundingBox]:
    """
    Extract bounding boxes of known dynamic UI regions on the current page.

    Args:
        page: Playwright Page object

    Returns:
        List of BoundingBox for each dynamic region found.
    """
    try:
        raw = await page.evaluate(_EXTRACT_JS, DYNAMIC_REGION_SELECTORS)
        regions = [BoundingBox(**r) for r in (raw or [])]
        logger.debug("region_masker.found", count=len(regions))
        return regions
    except Exception as exc:
        logger.warning("region_masker.failed", error=str(exc))
        return []


def is_in_masked_region(bbox: BoundingBox, masked_regions: list[BoundingBox]) -> bool:
    """Return True if the center of bbox falls within any masked region."""
    cx, cy = bbox.center_x, bbox.center_y
    return any(r.contains_point(cx, cy) for r in masked_regions)
