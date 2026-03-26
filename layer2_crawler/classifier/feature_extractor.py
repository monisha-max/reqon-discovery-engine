"""
Feature Extractor — Comprehensive feature engineering for page classification.

Extracts 50+ features across three signal groups:
1. DOM Structural Features — element counts, hierarchy, semantic structure
2. URL Pattern Features — path segments, query params, URL structure
3. HTML Statistical Features — text density, tag distribution, code-to-text ratio
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlparse, parse_qs

import numpy as np
import structlog
from bs4 import BeautifulSoup

from shared.models.page_models import PageData

logger = structlog.get_logger()


class FeatureExtractor:
    """Extracts a fixed-length numeric feature vector from a PageData object."""

    # Feature names for interpretability
    FEATURE_NAMES = [
        # DOM counts (15)
        "form_count", "input_count", "button_count", "table_count",
        "image_count", "link_count", "select_count", "textarea_count",
        "iframe_count", "video_count", "canvas_count", "svg_count",
        "heading_total", "list_count", "div_count",
        # DOM ratios (5)
        "input_to_form_ratio", "button_to_form_ratio", "image_to_link_ratio",
        "heading_to_text_ratio", "interactive_density",
        # Semantic structure (10)
        "has_nav", "has_sidebar", "has_footer", "has_header",
        "has_main", "has_search", "has_login_form", "has_charts",
        "has_modal", "has_wizard_indicators",
        # Heading hierarchy (6)
        "h1_count", "h2_count", "h3_count", "h4_count", "h5_count", "h6_count",
        # Form analysis (5)
        "password_field_count", "email_field_count", "checkbox_count",
        "radio_count", "file_input_count",
        # URL features (10)
        "url_depth", "url_has_id", "url_has_uuid", "url_has_query",
        "url_query_param_count", "url_path_length", "url_has_hash",
        "url_segment_count", "url_has_action_verb", "url_has_resource_noun",
        # HTML statistics (8)
        "text_length", "html_length", "text_to_html_ratio",
        "unique_tag_count", "avg_nesting_depth", "total_elements",
        "script_count", "style_count",
        # Page signals (5)
        "status_code_class", "load_time_ms", "console_error_count",
        "failed_request_count", "external_link_count",
    ]

    NUM_FEATURES = len(FEATURE_NAMES)

    def extract(self, page: PageData) -> np.ndarray:
        """Extract feature vector from a PageData object."""
        html = page.html_snippet or ""
        soup = BeautifulSoup(html, "lxml") if html else None

        features = []

        # DOM counts
        features.extend(self._dom_counts(page, soup))
        # DOM ratios
        features.extend(self._dom_ratios(page))
        # Semantic structure
        features.extend(self._semantic_structure(page, soup))
        # Heading hierarchy
        features.extend(self._heading_hierarchy(page))
        # Form analysis
        features.extend(self._form_analysis(soup))
        # URL features
        features.extend(self._url_features(page.url))
        # HTML statistics
        features.extend(self._html_statistics(html, soup))
        # Page signals
        features.extend(self._page_signals(page))

        return np.array(features, dtype=np.float32)

    def extract_batch(self, pages: list[PageData]) -> np.ndarray:
        """Extract features for multiple pages."""
        return np.array([self.extract(p) for p in pages])

    def _dom_counts(self, page: PageData, soup) -> list[float]:
        if not soup:
            return [page.form_count, page.input_count, page.button_count,
                    page.table_count, page.image_count, page.link_count,
                    0, 0, 0, 0, 0, 0,
                    sum(page.heading_counts.values()), 0, 0]

        return [
            page.form_count,
            page.input_count,
            page.button_count,
            page.table_count,
            page.image_count,
            page.link_count,
            len(soup.find_all("select")),
            len(soup.find_all("textarea")),
            len(soup.find_all("iframe")),
            len(soup.find_all("video")),
            len(soup.find_all("canvas")),
            len(soup.find_all("svg")),
            sum(page.heading_counts.values()),
            len(soup.find_all(["ul", "ol"])),
            len(soup.find_all("div")),
        ]

    def _dom_ratios(self, page: PageData) -> list[float]:
        form_count = max(page.form_count, 1)
        link_count = max(page.link_count, 1)
        heading_total = max(sum(page.heading_counts.values()), 1)
        total_interactive = page.input_count + page.button_count + page.link_count

        return [
            page.input_count / form_count,
            page.button_count / form_count,
            page.image_count / link_count,
            heading_total / max(total_interactive, 1),
            total_interactive / max(page.link_count + page.form_count + 1, 1),
        ]

    def _semantic_structure(self, page: PageData, soup) -> list[float]:
        has_modal = False
        has_wizard = False

        if soup:
            has_modal = bool(
                soup.find(class_=lambda c: c and "modal" in str(c).lower()) or
                soup.find(attrs={"role": "dialog"})
            )
            has_wizard = bool(
                soup.find(class_=lambda c: c and any(kw in str(c).lower() for kw in ["step", "wizard", "stepper", "progress"])) or
                soup.find(attrs={"role": "tablist"})
            )

        has_header = False
        if soup:
            has_header = bool(soup.find("header") or soup.find(attrs={"role": "banner"}))

        has_main = False
        if soup:
            has_main = bool(soup.find("main") or soup.find(attrs={"role": "main"}))

        return [
            float(page.has_nav),
            float(page.has_sidebar),
            float(page.has_footer),
            float(has_header),
            float(has_main),
            float(page.has_search),
            float(page.has_login_form),
            float(page.has_charts),
            float(has_modal),
            float(has_wizard),
        ]

    def _heading_hierarchy(self, page: PageData) -> list[float]:
        return [
            float(page.heading_counts.get("h1", 0)),
            float(page.heading_counts.get("h2", 0)),
            float(page.heading_counts.get("h3", 0)),
            float(page.heading_counts.get("h4", 0)),
            float(page.heading_counts.get("h5", 0)),
            float(page.heading_counts.get("h6", 0)),
        ]

    def _form_analysis(self, soup) -> list[float]:
        if not soup:
            return [0.0] * 5

        inputs = soup.find_all("input")
        return [
            float(sum(1 for i in inputs if i.get("type") == "password")),
            float(sum(1 for i in inputs if i.get("type") == "email")),
            float(sum(1 for i in inputs if i.get("type") == "checkbox")),
            float(sum(1 for i in inputs if i.get("type") == "radio")),
            float(sum(1 for i in inputs if i.get("type") == "file")),
        ]

    def _url_features(self, url: str) -> list[float]:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        segments = [s for s in path.split("/") if s]
        query_params = parse_qs(parsed.query)

        # Check for IDs in URL
        has_id = bool(re.search(r"/\d+(/|$)", path))
        has_uuid = bool(re.search(r"/[a-f0-9-]{36}(/|$)", path))

        # Action verbs in URL
        action_verbs = {"create", "edit", "delete", "update", "new", "add", "remove", "submit", "login", "register", "search", "filter"}
        has_action = float(any(s.lower() in action_verbs for s in segments))

        # Resource nouns
        resource_nouns = {"dashboard", "settings", "profile", "admin", "report", "table", "list", "detail", "form"}
        has_resource = float(any(s.lower() in resource_nouns for s in segments))

        return [
            float(len(segments)),  # depth
            float(has_id),
            float(has_uuid),
            float(bool(parsed.query)),
            float(len(query_params)),
            float(len(path)),
            float(bool(parsed.fragment)),
            float(len(segments)),
            has_action,
            has_resource,
        ]

    def _html_statistics(self, html: str, soup) -> list[float]:
        if not soup or not html:
            return [0.0] * 8

        text = soup.get_text(separator=" ", strip=True)
        text_len = len(text)
        html_len = len(html)

        # Unique tags
        all_tags = [tag.name for tag in soup.find_all(True)]
        unique_tags = len(set(all_tags))
        total_elements = len(all_tags)

        # Average nesting depth (sample first 100 elements)
        depths = []
        for tag in list(soup.find_all(True))[:100]:
            depth = len(list(tag.parents)) - 1
            depths.append(depth)
        avg_depth = np.mean(depths) if depths else 0.0

        return [
            float(text_len),
            float(html_len),
            float(text_len / max(html_len, 1)),
            float(unique_tags),
            float(avg_depth),
            float(total_elements),
            float(len(soup.find_all("script"))),
            float(len(soup.find_all("style"))),
        ]

    def _page_signals(self, page: PageData) -> list[float]:
        status_class = 0.0
        if page.status_code:
            status_class = float(page.status_code // 100)  # 2, 3, 4, 5

        return [
            status_class,
            float(page.load_time_ms or 0),
            float(len(page.console_errors)),
            float(len(page.failed_requests)),
            0.0,  # external link count — would need full link list
        ]
