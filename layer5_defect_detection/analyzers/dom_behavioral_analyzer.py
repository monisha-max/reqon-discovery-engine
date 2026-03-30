"""
DOM Behavioral Analyzer — structural and semantic DOM checks.

Runs entirely via page.evaluate() — no pixel processing, no network calls.
Each check is an independent JS snippet that returns a list of raw findings,
converted to DefectFinding objects in Python.

Check 1 — Heading Hierarchy
    Detects heading level gaps (h1→h3 skips) and multiple h1s on a single page.

Check 2 — Form Structural Integrity
    For every <form>, verifies: has a submit trigger, all inputs have an
    accessible label, required fields are marked.

Check 3 — Empty Interactive Elements
    Buttons, links, and role="button" elements with no visible text,
    no aria-label, and no title are invisible to assistive technology
    and confusing for sighted users.

Check 4 — Duplicate IDs
    Multiple elements sharing the same id break label associations,
    anchor links, and JS getElementById targeting.

Check 5 — Alt Text on Meaningful Images
    Content images (<img> inside <figure>, <a>, <article>, or with significant
    dimensions) that have no alt attribute or a non-empty alt. Purely decorative
    images that are role="presentation" or aria-hidden are skipped.

Check 6 — Broken ARIA
    Three targeted violations with high false-positive resistance:
    (a) aria-hidden="true" on a focusable element (CRITICAL — keyboard trap);
    (b) aria-labelledby pointing to a non-existent ID (HIGH — silent label loss);
    (c) required ARIA children missing for composite roles like listbox, grid,
        tablist, menu (MEDIUM — broken semantics).

Check 7 — Stuck Loading States and Visible Error States
    After the page has settled, scans for elements that indicate an in-progress
    or failed state: visible spinners / aria-busy elements (MEDIUM) and visible
    error/alert containers with non-empty text (HIGH).

Check 8 — Empty Data Containers
    Tables, lists, and grid containers that have zero visible data rows/items
    but no accompanying empty-state message. Distinguishes intentional empty
    states (those that contain a child with text like "No results") from broken
    data fetches where the container is silently empty.
"""
from __future__ import annotations

from uuid import uuid4

import structlog

from layer5_defect_detection.models.defect_models import (
    BoundingBox,
    DefectCategory,
    DefectFinding,
    DefectSeverity,
)


logger = structlog.get_logger()

# Placeholder bbox for findings without a single pinpointed element
_ZERO_BBOX = BoundingBox(x=0, y=0, width=0, height=0)


# ---------------------------------------------------------------------------
# JavaScript payloads
# ---------------------------------------------------------------------------

_HEADING_HIERARCHY_JS = """
() => {
    const headings = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')]
        .filter(h => {
            const s = window.getComputedStyle(h);
            return s.display !== 'none' && s.visibility !== 'hidden';
        });

    const findings = [];

    // Multiple h1s
    const h1s = headings.filter(h => h.tagName === 'H1');
    if (h1s.length > 1) {
        const selectors = h1s.map(h => {
            if (h.id) return '#' + CSS.escape(h.id);
            const cls = (h.className || '').toString().trim().split(/\\s+/)[0];
            return cls ? 'h1.' + CSS.escape(cls) : 'h1';
        });
        findings.push({
            check: 'multiple_h1',
            count: h1s.length,
            selectors: selectors.slice(0, 5),
            text_samples: h1s.slice(0, 3).map(h => (h.textContent || '').trim().substring(0, 60)),
        });
    }

    // Level gaps
    let prevLevel = 0;
    for (const h of headings) {
        const level = parseInt(h.tagName[1], 10);
        if (prevLevel > 0 && level > prevLevel + 1) {
            const sel = h.id ? '#' + CSS.escape(h.id) : h.tagName.toLowerCase();
            findings.push({
                check: 'heading_gap',
                from_level: prevLevel,
                to_level: level,
                selector: sel,
                text: (h.textContent || '').trim().substring(0, 80),
                bbox: (() => {
                    const r = h.getBoundingClientRect();
                    return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
                })(),
            });
        }
        prevLevel = level;
    }

    return findings;
}
"""


_FORM_INTEGRITY_JS = """
() => {
    const findings = [];

    const getSelector = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const cls = (el.className || '').toString().trim().split(/\\s+/)[0];
        const tag = el.tagName.toLowerCase();
        return cls ? tag + '.' + CSS.escape(cls) : tag;
    };

    const getBbox = (el) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
    };

    const forms = [...document.querySelectorAll('form')].filter(f => {
        const s = window.getComputedStyle(f);
        return s.display !== 'none' && s.visibility !== 'hidden';
    });

    for (const form of forms) {
        const formSel = getSelector(form);

        // Check 1: Does the form have a submit trigger?
        const hasSubmit = form.querySelector(
            'button[type="submit"], button:not([type]), input[type="submit"], [role="button"]'
        );
        if (!hasSubmit) {
            findings.push({
                check: 'missing_submit',
                form_selector: formSel,
                bbox: getBbox(form),
            });
        }

        // Check 2: Each visible input/select/textarea has an accessible label
        const controls = [...form.querySelectorAll('input, select, textarea')]
            .filter(inp => {
                const t = (inp.getAttribute('type') || '').toLowerCase();
                if (['hidden', 'submit', 'button', 'reset', 'image'].includes(t)) return false;
                const s = window.getComputedStyle(inp);
                return s.display !== 'none' && s.visibility !== 'hidden';
            });

        for (const inp of controls) {
            const hasAriaLabel    = inp.getAttribute('aria-label') && inp.getAttribute('aria-label').trim();
            const ariaLabelledBy  = inp.getAttribute('aria-labelledby');
            const hasLabelledBy   = ariaLabelledBy && document.getElementById(ariaLabelledBy);
            const hasForLabel     = inp.id && document.querySelector('label[for="' + CSS.escape(inp.id) + '"]');
            const hasWrappedLabel = inp.closest('label');
            const hasTitle        = inp.getAttribute('title') && inp.getAttribute('title').trim();
            const hasPlaceholder  = inp.getAttribute('placeholder') && inp.getAttribute('placeholder').trim();

            if (!hasAriaLabel && !hasLabelledBy && !hasForLabel && !hasWrappedLabel && !hasTitle) {
                findings.push({
                    check: 'missing_label',
                    input_selector: getSelector(inp),
                    input_type: inp.getAttribute('type') || inp.tagName.toLowerCase(),
                    has_placeholder_only: !!hasPlaceholder,
                    bbox: getBbox(inp),
                });
            }
        }
    }

    return findings;
}
"""


_EMPTY_INTERACTIVE_JS = """
() => {
    const findings = [];

    const getBbox = (el) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
    };

    const getSelector = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const cls = (el.className || '').toString().trim().split(/\\s+/)[0];
        const tag = el.tagName.toLowerCase();
        return cls ? tag + '.' + CSS.escape(cls) : tag;
    };

    const candidates = [
        ...document.querySelectorAll('button, a[href], [role="button"], [role="link"]')
    ];

    for (const el of candidates) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') continue;

        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;

        const visibleText    = (el.textContent || '').trim();
        const ariaLabel      = (el.getAttribute('aria-label') || '').trim();
        const title          = (el.getAttribute('title') || '').trim();
        const ariaLabelledBy = el.getAttribute('aria-labelledby');
        const labelledByText = ariaLabelledBy
            ? (document.getElementById(ariaLabelledBy)?.textContent || '').trim()
            : '';

        // Has an img child with alt text → accessible
        const imgWithAlt = el.querySelector('img[alt]:not([alt=""])');
        // Has an SVG with title → accessible
        const svgTitle = el.querySelector('svg title');

        const isAccessible = visibleText || ariaLabel || title || labelledByText || imgWithAlt || svgTitle;

        if (!isAccessible) {
            findings.push({
                selector: getSelector(el),
                tag: el.tagName.toLowerCase(),
                role: el.getAttribute('role') || '',
                bbox: getBbox(el),
            });
        }
    }

    return findings;
}
"""


_DUPLICATE_IDS_JS = """
() => {
    const all = [...document.querySelectorAll('[id]')];
    const counts = {};
    const examples = {};

    for (const el of all) {
        const id = el.id;
        if (!id) continue;
        counts[id] = (counts[id] || 0) + 1;
        if (!examples[id]) {
            const r = el.getBoundingClientRect();
            examples[id] = {
                tag: el.tagName.toLowerCase(),
                bbox: { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height },
                text: (el.textContent || '').trim().substring(0, 60),
            };
        }
    }

    const findings = [];
    for (const [id, count] of Object.entries(counts)) {
        if (count > 1) {
            findings.push({ id, count, ...examples[id] });
        }
    }
    return findings;
}
"""


_ALT_TEXT_JS = """
() => {
    const findings = [];

    // Signals that an image is a content image rather than purely decorative
    const isContentImage = (img) => {
        // Explicitly marked decorative
        if (img.getAttribute('role') === 'presentation') return false;
        if (img.getAttribute('role') === 'none') return false;
        if (img.getAttribute('aria-hidden') === 'true') return false;

        const r = img.getBoundingClientRect();
        // Too small to be meaningful content (icon threshold: 24x24)
        if (r.width < 24 || r.height < 24) return false;

        // Inside a meaningful container → treat as content
        if (img.closest('figure, a, article, [role="img"], picture')) return true;

        // Large standalone image → content
        if (r.width >= 100 || r.height >= 100) return true;

        return false;
    };

    const imgs = [...document.querySelectorAll('img')].filter(img => {
        const s = window.getComputedStyle(img);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        return isContentImage(img);
    });

    for (const img of imgs) {
        const alt = img.getAttribute('alt');
        const src = (img.getAttribute('src') || img.getAttribute('data-src') || '').substring(0, 80);
        const r = img.getBoundingClientRect();
        const bbox = { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };

        const getSelector = (el) => {
            if (el.id) return '#' + CSS.escape(el.id);
            const cls = (el.className || '').toString().trim().split(/\\s+/)[0];
            return cls ? 'img.' + CSS.escape(cls) : 'img';
        };

        if (alt === null) {
            // No alt attribute at all
            findings.push({ issue: 'missing_alt', selector: getSelector(img), src, bbox });
        } else if (alt.trim() === '') {
            // Empty alt on a content image — only flag if truly content (large or anchored)
            const r2 = img.getBoundingClientRect();
            const isLarge = r2.width >= 150 || r2.height >= 150;
            const inLink = !!img.closest('a');
            if (isLarge || inLink) {
                findings.push({ issue: 'empty_alt_on_content', selector: getSelector(img), src, bbox });
            }
        }
    }

    return findings;
}
"""


_BROKEN_ARIA_JS = """
() => {
    const findings = [];

    const getBbox = (el) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
    };

    const getSelector = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const cls = (el.className || '').toString().trim().split(/\\s+/)[0];
        const tag = el.tagName.toLowerCase();
        return cls ? tag + '.' + CSS.escape(cls) : tag;
    };

    // (a) aria-hidden="true" on a natively focusable element
    const focusableHidden = [...document.querySelectorAll(
        'a[href][aria-hidden="true"], button[aria-hidden="true"], input[aria-hidden="true"], ' +
        'select[aria-hidden="true"], textarea[aria-hidden="true"], [tabindex][aria-hidden="true"]'
    )].filter(el => {
        const tabIdx = el.getAttribute('tabindex');
        if (tabIdx !== null && parseInt(tabIdx, 10) < 0) return false; // explicitly removed from tab order
        const s = window.getComputedStyle(el);
        return s.display !== 'none' && s.visibility !== 'hidden';
    });

    for (const el of focusableHidden) {
        findings.push({
            issue: 'aria_hidden_focusable',
            selector: getSelector(el),
            tag: el.tagName.toLowerCase(),
            bbox: getBbox(el),
            text: (el.textContent || '').trim().substring(0, 60),
        });
    }

    // (b) aria-labelledby pointing to a non-existent ID
    const labelledBy = [...document.querySelectorAll('[aria-labelledby]')];
    for (const el of labelledBy) {
        const ids = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean);
        const broken = ids.filter(id => !document.getElementById(id));
        if (broken.length > 0) {
            findings.push({
                issue: 'broken_aria_labelledby',
                selector: getSelector(el),
                tag: el.tagName.toLowerCase(),
                broken_ids: broken,
                bbox: getBbox(el),
                text: (el.textContent || '').trim().substring(0, 60),
            });
        }
    }

    // (c) Composite roles missing required owned children
    // Spec: https://www.w3.org/TR/wai-aria-1.2/#mustContain
    const REQUIRED_CHILDREN = {
        listbox:   ['option'],
        grid:      ['row', 'rowgroup'],
        tablist:   ['tab'],
        menu:      ['menuitem', 'menuitemcheckbox', 'menuitemradio'],
        menubar:   ['menuitem', 'menuitemcheckbox', 'menuitemradio'],
        tree:      ['treeitem'],
        treegrid:  ['row'],
        radiogroup:['radio'],
    };

    for (const [role, requiredChildren] of Object.entries(REQUIRED_CHILDREN)) {
        const containers = [...document.querySelectorAll('[role="' + role + '"]')].filter(el => {
            const s = window.getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden';
        });
        for (const el of containers) {
            const hasChild = requiredChildren.some(childRole =>
                el.querySelector('[role="' + childRole + '"]')
            );
            if (!hasChild) {
                findings.push({
                    issue: 'missing_required_children',
                    selector: getSelector(el),
                    role,
                    required_children: requiredChildren,
                    bbox: getBbox(el),
                });
            }
        }
    }

    return findings;
}
"""


_STATE_ANOMALY_JS = """
() => {
    const findings = [];

    const getBbox = (el) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
    };

    const getSelector = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const cls = (el.className || '').toString().trim().split(/\\s+/)[0];
        const tag = el.tagName.toLowerCase();
        return cls ? tag + '.' + CSS.escape(cls) : tag;
    };

    const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        if (parseFloat(s.opacity) < 0.05) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };

    // (a) Stuck loading / spinner indicators
    const LOADING_SELECTORS = [
        '[aria-busy="true"]',
        '[data-loading="true"]',
        '[data-state="loading"]',
        '.loading', '.is-loading', '.spinner',
        '.skeleton', '[class*="skeleton"]',
        '[class*="loading"]', '[class*="spinner"]',
        '[role="progressbar"]:not([aria-valuenow])',
    ];

    const seen = new WeakSet();
    for (const sel of LOADING_SELECTORS) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); } catch(e) { continue; }
        for (const el of nodes) {
            if (seen.has(el) || !isVisible(el)) continue;
            seen.add(el);
            findings.push({
                issue: 'stuck_loading',
                selector: getSelector(el),
                tag: el.tagName.toLowerCase(),
                matched_selector: sel,
                bbox: getBbox(el),
            });
        }
    }

    // (b) Visible error / alert states with non-empty text
    const ERROR_SELECTORS = [
        '[role="alert"]',
        '[role="alertdialog"]',
        '.error', '.is-error', '.has-error',
        '.alert-danger', '.alert-error',
        '[class*="error-message"]', '[class*="error-text"]',
        '[class*="error-banner"]', '[class*="alert-danger"]',
        '[data-testid*="error"]', '[data-cy*="error"]',
    ];

    const seenErr = new WeakSet();
    for (const sel of ERROR_SELECTORS) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); } catch(e) { continue; }
        for (const el of nodes) {
            if (seenErr.has(el) || !isVisible(el)) continue;
            const text = (el.textContent || '').trim();
            if (!text) continue;  // Empty error container — skip
            seenErr.add(el);
            findings.push({
                issue: 'visible_error_state',
                selector: getSelector(el),
                tag: el.tagName.toLowerCase(),
                matched_selector: sel,
                text: text.substring(0, 120),
                bbox: getBbox(el),
            });
        }
    }

    return findings;
}
"""


_EMPTY_CONTAINER_JS = """
() => {
    const findings = [];

    const getBbox = (el) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + window.scrollX, y: r.top + window.scrollY, width: r.width, height: r.height };
    };

    const getSelector = (el) => {
        if (el.id) return '#' + CSS.escape(el.id);
        const cls = (el.className || '').toString().trim().split(/\\s+/)[0];
        const tag = el.tagName.toLowerCase();
        return cls ? tag + '.' + CSS.escape(cls) : tag;
    };

    const isVisible = (el) => {
        const s = window.getComputedStyle(el);
        if (s.display === 'none' || s.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };

    // Check if a container has an intentional empty-state message
    const hasEmptyStateMessage = (container) => {
        const text = (container.textContent || '').trim().toLowerCase();
        const EMPTY_STATE_PHRASES = [
            'no results', 'no data', 'no items', 'no records', 'empty',
            'nothing found', 'no entries', 'no rows', 'no content',
            'no matches', 'no orders', 'no history', 'not found',
        ];
        return EMPTY_STATE_PHRASES.some(p => text.includes(p));
    };

    // (a) Tables with visible tbody but no data rows
    const tables = [...document.querySelectorAll('table')].filter(isVisible);
    for (const table of tables) {
        const tbodies = [...table.querySelectorAll('tbody')];
        for (const tbody of tbodies) {
            const rows = [...tbody.querySelectorAll('tr')].filter(tr => isVisible(tr));
            if (rows.length === 0 && !hasEmptyStateMessage(table)) {
                findings.push({
                    container_type: 'table',
                    selector: getSelector(table),
                    bbox: getBbox(table),
                });
            }
        }
    }

    // (b) Lists (ul/ol with data-* or list-class hints) with no visible items
    const LIST_SELECTORS = [
        'ul[class*="list"]', 'ul[class*="items"]', 'ul[class*="results"]',
        'ol[class*="list"]', 'ol[class*="items"]',
        '[role="list"]', '[role="listbox"]',
        '[class*="data-list"]', '[class*="result-list"]', '[class*="item-list"]',
    ];

    const seenLists = new WeakSet();
    for (const sel of LIST_SELECTORS) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); } catch(e) { continue; }
        for (const el of nodes) {
            if (seenLists.has(el) || !isVisible(el)) continue;
            seenLists.add(el);
            const items = [...el.children].filter(c => isVisible(c));
            if (items.length === 0 && !hasEmptyStateMessage(el)) {
                findings.push({
                    container_type: 'list',
                    selector: getSelector(el),
                    bbox: getBbox(el),
                });
            }
        }
    }

    // (c) Generic data grid / card containers
    const GRID_SELECTORS = [
        '[class*="data-grid"]', '[class*="datagrid"]',
        '[class*="card-grid"]', '[class*="results-grid"]',
        '[role="grid"]', '[role="table"]',
        '[class*="feed"]', '[class*="stream"]',
    ];

    const seenGrids = new WeakSet();
    for (const sel of GRID_SELECTORS) {
        let nodes;
        try { nodes = document.querySelectorAll(sel); } catch(e) { continue; }
        for (const el of nodes) {
            if (seenGrids.has(el) || !isVisible(el)) continue;
            seenGrids.add(el);
            const children = [...el.children].filter(c => isVisible(c));
            if (children.length === 0 && !hasEmptyStateMessage(el)) {
                findings.push({
                    container_type: 'grid',
                    selector: getSelector(el),
                    bbox: getBbox(el),
                });
            }
        }
    }

    return findings;
}
"""


# ---------------------------------------------------------------------------
# Analyzer class
# ---------------------------------------------------------------------------

class DOMBehavioralAnalyzer:
    """
    Runs DOM behavioral checks 1–4 on a Playwright page.

    All checks use page.evaluate() — no screenshots, no network calls.
    Results are DefectFinding objects compatible with the rest of Layer 5.
    """

    async def analyze(self, page: object, phase: str) -> list[DefectFinding]:
        """
        Run all four behavioral checks and return combined findings.

        Args:
            page: Playwright Page (already navigated, DOM settled)
            phase: snapshot phase label ("baseline" | "peak" | "post" | viewport name)

        Returns:
            List of DefectFinding objects
        """
        findings: list[DefectFinding] = []

        findings.extend(await self._check_heading_hierarchy(page, phase))
        findings.extend(await self._check_form_integrity(page, phase))
        findings.extend(await self._check_empty_interactive(page, phase))
        findings.extend(await self._check_duplicate_ids(page, phase))
        findings.extend(await self._check_alt_text(page, phase))
        findings.extend(await self._check_broken_aria(page, phase))
        findings.extend(await self._check_state_anomalies(page, phase))
        findings.extend(await self._check_empty_containers(page, phase))

        logger.info(
            "dom_behavioral_analyzer.done",
            phase=phase,
            findings=len(findings),
        )
        return findings

    # ------------------------------------------------------------------
    # Check 1 — Heading Hierarchy
    # ------------------------------------------------------------------

    async def _check_heading_hierarchy(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_HEADING_HIERARCHY_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.heading_js_failed", error=str(exc))
            return []

        findings = []
        for item in raw or []:
            check = item.get("check")

            if check == "multiple_h1":
                count = item.get("count", 2)
                selectors = item.get("selectors", [])
                samples = item.get("text_samples", [])
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.DOM_STRUCTURAL,
                    title=f"Multiple H1 headings ({count} found)",
                    description=(
                        f"Page contains {count} visible <h1> elements. "
                        f"There should be exactly one H1 per page for correct document structure. "
                        f"Found: {', '.join(selectors[:3])}. "
                        f"Samples: {' | '.join(samples[:2])}"
                    ),
                    element_selector=selectors[0] if selectors else "h1",
                    element_bbox=_ZERO_BBOX,
                    snapshot_phase=phase,
                    annotation_color="yellow",
                ))

            elif check == "heading_gap":
                from_level = item.get("from_level", 0)
                to_level = item.get("to_level", 0)
                selector = item.get("selector", "h" + str(to_level))
                text = item.get("text", "")
                raw_bbox = item.get("bbox", {})
                bbox = _bbox_from_raw(raw_bbox)
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.DOM_STRUCTURAL,
                    title=f"Heading level gap: H{from_level} → H{to_level}",
                    description=(
                        f"Heading jumps from H{from_level} to H{to_level}, "
                        f"skipping level(s) in between. "
                        f"Element: '{text[:60]}' ({selector}). "
                        f"This breaks document outline and screen-reader navigation."
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="yellow",
                ))

        return findings

    # ------------------------------------------------------------------
    # Check 2 — Form Structural Integrity
    # ------------------------------------------------------------------

    async def _check_form_integrity(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_FORM_INTEGRITY_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.form_js_failed", error=str(exc))
            return []

        findings = []
        for item in raw or []:
            check = item.get("check")

            if check == "missing_submit":
                form_sel = item.get("form_selector", "form")
                bbox = _bbox_from_raw(item.get("bbox", {}))
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.HIGH,
                    category=DefectCategory.FORM_INTEGRITY,
                    title=f"Form has no submit trigger: {form_sel}",
                    description=(
                        f"Form '{form_sel}' contains no submit button "
                        f"(<button type='submit'>, <button>, or <input type='submit'>). "
                        f"Users cannot complete this form without keyboard shortcut knowledge."
                    ),
                    element_selector=form_sel,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="orange",
                ))

            elif check == "missing_label":
                inp_sel = item.get("input_selector", "input")
                inp_type = item.get("input_type", "text")
                has_placeholder_only = item.get("has_placeholder_only", False)
                bbox = _bbox_from_raw(item.get("bbox", {}))
                placeholder_note = (
                    " (has placeholder text, but placeholder is not a label substitute)"
                    if has_placeholder_only else ""
                )
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.FORM_INTEGRITY,
                    title=f"Input missing accessible label: {inp_sel}",
                    description=(
                        f"<{inp_type}> field '{inp_sel}' has no associated <label>, "
                        f"aria-label, aria-labelledby, or title attribute{placeholder_note}. "
                        f"Screen readers cannot identify the field's purpose."
                    ),
                    element_selector=inp_sel,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="orange",
                ))

        return findings

    # ------------------------------------------------------------------
    # Check 3 — Empty Interactive Elements
    # ------------------------------------------------------------------

    async def _check_empty_interactive(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_EMPTY_INTERACTIVE_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.empty_interactive_js_failed", error=str(exc))
            return []

        findings = []
        for item in raw or []:
            selector = item.get("selector", "button")
            tag = item.get("tag", "button")
            role = item.get("role", "")
            bbox = _bbox_from_raw(item.get("bbox", {}))
            role_note = f" (role={role})" if role else ""
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.HIGH,
                category=DefectCategory.EMPTY_INTERACTIVE,
                title=f"Empty interactive element: {selector}",
                description=(
                    f"<{tag}>{role_note} element '{selector}' has no visible text, "
                    f"aria-label, title, or accessible image. "
                    f"It is invisible to screen readers and its purpose cannot be determined."
                ),
                element_selector=selector,
                element_bbox=bbox,
                snapshot_phase=phase,
                annotation_color="orange",
            ))

        return findings

    # ------------------------------------------------------------------
    # Check 4 — Duplicate IDs
    # ------------------------------------------------------------------

    async def _check_duplicate_ids(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_DUPLICATE_IDS_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.duplicate_ids_js_failed", error=str(exc))
            return []

        findings = []
        for item in raw or []:
            id_val = item.get("id", "")
            count = item.get("count", 2)
            tag = item.get("tag", "element")
            text = item.get("text", "")
            bbox = _bbox_from_raw(item.get("bbox", {}))
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.MEDIUM,
                category=DefectCategory.DOM_STRUCTURAL,
                title=f"Duplicate id=\"{id_val}\" ({count} occurrences)",
                description=(
                    f"id=\"{id_val}\" appears on {count} elements (first: <{tag}> '{text[:50]}'). "
                    f"Duplicate IDs break <label for>, aria-labelledby, anchor links, "
                    f"and document.getElementById() — only the first element is targeted."
                ),
                element_selector=f"#{id_val}",
                element_bbox=bbox,
                snapshot_phase=phase,
                annotation_color="yellow",
            ))

        return findings


    # ------------------------------------------------------------------
    # Check 5 — Alt Text on Meaningful Images
    # ------------------------------------------------------------------

    async def _check_alt_text(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_ALT_TEXT_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.alt_text_js_failed", error=str(exc))
            return []

        findings = []
        for item in raw or []:
            issue = item.get("issue")
            selector = item.get("selector", "img")
            src = item.get("src", "")
            bbox = _bbox_from_raw(item.get("bbox", {}))

            if issue == "missing_alt":
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.MISSING_ALT_TEXT,
                    title=f"Image missing alt attribute: {selector}",
                    description=(
                        f"Content image '{selector}' (src: {src}) has no alt attribute. "
                        f"Screen readers will announce the filename instead of a description. "
                        f"Add alt='' if decorative, or a descriptive alt text if meaningful."
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="orange",
                ))
            elif issue == "empty_alt_on_content":
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.MISSING_ALT_TEXT,
                    title=f"Content image has empty alt: {selector}",
                    description=(
                        f"Image '{selector}' (src: {src}) has alt='' but appears to be a "
                        f"content image (large dimensions or inside a link). "
                        f"Empty alt marks images as decorative — add a description if the image conveys meaning."
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="yellow",
                ))

        return findings

    # ------------------------------------------------------------------
    # Check 6 — Broken ARIA
    # ------------------------------------------------------------------

    async def _check_broken_aria(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_BROKEN_ARIA_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.broken_aria_js_failed", error=str(exc))
            return []

        findings = []
        for item in raw or []:
            issue = item.get("issue")
            selector = item.get("selector", "element")
            tag = item.get("tag", "element")
            bbox = _bbox_from_raw(item.get("bbox", {}))
            text = item.get("text", "")

            if issue == "aria_hidden_focusable":
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.CRITICAL,
                    category=DefectCategory.ARIA_VIOLATION,
                    title=f"aria-hidden on focusable element: {selector}",
                    description=(
                        f"<{tag}> '{selector}' has aria-hidden=\"true\" but is keyboard-focusable. "
                        f"This creates a keyboard trap — the element receives focus but is "
                        f"invisible to screen readers. "
                        f"Either remove aria-hidden or add tabindex=\"-1\"."
                        + (f" Text: '{text[:50]}'" if text else "")
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="red",
                ))

            elif issue == "broken_aria_labelledby":
                broken_ids = item.get("broken_ids", [])
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.HIGH,
                    category=DefectCategory.ARIA_VIOLATION,
                    title=f"aria-labelledby references missing ID(s): {selector}",
                    description=(
                        f"<{tag}> '{selector}' has aria-labelledby pointing to "
                        f"ID(s) that do not exist in the DOM: {', '.join(broken_ids)}. "
                        f"The element will have no accessible name at runtime."
                        + (f" Text: '{text[:50]}'" if text else "")
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="red",
                ))

            elif issue == "missing_required_children":
                role = item.get("role", "")
                required = item.get("required_children", [])
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.ARIA_VIOLATION,
                    title=f"role=\"{role}\" missing required child role(s): {selector}",
                    description=(
                        f"<{tag}> '{selector}' has role=\"{role}\" but contains none of the "
                        f"required owned elements: {', '.join(required)}. "
                        f"Assistive technology will not be able to navigate the widget correctly."
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="orange",
                ))

        return findings

    # ------------------------------------------------------------------
    # Check 7 — Stuck Loading States and Visible Error States
    # ------------------------------------------------------------------

    async def _check_state_anomalies(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_STATE_ANOMALY_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.state_anomaly_js_failed", error=str(exc))
            return []

        findings = []
        for item in raw or []:
            issue = item.get("issue")
            selector = item.get("selector", "element")
            tag = item.get("tag", "element")
            matched_sel = item.get("matched_selector", "")
            bbox = _bbox_from_raw(item.get("bbox", {}))

            if issue == "stuck_loading":
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.MEDIUM,
                    category=DefectCategory.STATE_ANOMALY,
                    title=f"Stuck loading indicator: {selector}",
                    description=(
                        f"<{tag}> '{selector}' matches loading/spinner pattern "
                        f"('{matched_sel}') and is still visible after page load settled. "
                        f"This may indicate a failed async data fetch, a race condition, "
                        f"or a perpetually spinning UI component."
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="orange",
                ))

            elif issue == "visible_error_state":
                error_text = item.get("text", "")
                findings.append(DefectFinding(
                    defect_id=str(uuid4()),
                    severity=DefectSeverity.HIGH,
                    category=DefectCategory.STATE_ANOMALY,
                    title=f"Visible error state on page: {selector}",
                    description=(
                        f"<{tag}> '{selector}' matches error/alert pattern "
                        f"('{matched_sel}') and is visible with text: \"{error_text[:80]}\". "
                        f"An error state is rendered on what should be a normal page — "
                        f"likely a failed operation, broken API call, or unhandled exception."
                    ),
                    element_selector=selector,
                    element_bbox=bbox,
                    snapshot_phase=phase,
                    annotation_color="red",
                ))

        return findings

    # ------------------------------------------------------------------
    # Check 8 — Empty Data Containers
    # ------------------------------------------------------------------

    async def _check_empty_containers(
        self, page: object, phase: str
    ) -> list[DefectFinding]:
        try:
            raw: list[dict] = await page.evaluate(_EMPTY_CONTAINER_JS)
        except Exception as exc:
            logger.warning("dom_behavioral.empty_container_js_failed", error=str(exc))
            return []

        _TYPE_LABEL = {"table": "table", "list": "list/ul", "grid": "data grid"}

        findings = []
        for item in raw or []:
            container_type = item.get("container_type", "container")
            selector = item.get("selector", container_type)
            bbox = _bbox_from_raw(item.get("bbox", {}))
            label = _TYPE_LABEL.get(container_type, container_type)
            findings.append(DefectFinding(
                defect_id=str(uuid4()),
                severity=DefectSeverity.MEDIUM,
                category=DefectCategory.EMPTY_CONTAINER,
                title=f"Empty {label} with no empty-state message: {selector}",
                description=(
                    f"'{selector}' is a visible {label} container with zero data rows/items "
                    f"and no recognisable empty-state message (e.g. 'No results'). "
                    f"This likely indicates a failed data fetch, broken API, or missing "
                    f"conditional render — users see a blank container with no explanation."
                ),
                element_selector=selector,
                element_bbox=bbox,
                snapshot_phase=phase,
                annotation_color="yellow",
            ))

        return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bbox_from_raw(raw: dict) -> BoundingBox:
    """Convert a raw {x, y, width, height} dict from JS into a BoundingBox."""
    try:
        return BoundingBox(
            x=float(raw.get("x", 0)),
            y=float(raw.get("y", 0)),
            width=float(raw.get("width", 0)),
            height=float(raw.get("height", 0)),
        )
    except Exception:
        return _ZERO_BBOX
