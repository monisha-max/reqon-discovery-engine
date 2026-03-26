"""
LLM Page Labeler — Uses GPT to classify pages and generate training data.

This is the "teacher" in the self-learning loop:
1. LLM analyzes DOM features + HTML snippet + URL
2. Produces a page type label with confidence
3. Labels are stored as training data for the local XGBoost model
"""
from __future__ import annotations

import json
from typing import Optional

import structlog

from config.settings import settings
from shared.models.page_models import PageData, PageType

logger = structlog.get_logger()

CLASSIFICATION_PROMPT = """You are a web page classifier for an autonomous QA testing engine.

Classify this page into exactly ONE of these 12 types:
- dashboard: KPIs, charts, widgets, analytics overview
- list_table: Data tables, listings, search results grids
- form: Input forms (registration, contact, data entry) — NOT login
- wizard: Multi-step processes (checkout, onboarding, setup flows)
- report: Read-only reports, exported data views
- detail: Single item view (product page, article, record detail)
- settings: Configuration pages, preferences, account settings
- auth: Login, register, forgot password, SSO pages
- landing: Marketing pages, homepages, hero sections
- search: Search results pages, filter-heavy pages
- profile: User profile, account overview
- error: 404, 500, error pages, not found

Page Information:
- URL: {url}
- Title: {title}
- Status Code: {status_code}
- DOM Features:
  - Forms: {form_count}, Inputs: {input_count}, Buttons: {button_count}
  - Tables: {table_count}, Images: {image_count}, Links: {link_count}
  - Has Login Form: {has_login_form}, Has Charts: {has_charts}
  - Has Nav: {has_nav}, Has Sidebar: {has_sidebar}, Has Search: {has_search}
  - Headings: {headings}
- HTML Preview (first 1500 chars):
{html_preview}

Respond ONLY with JSON (no markdown):
{{"page_type": "one_of_the_12_types", "confidence": 0.0_to_1.0, "reasoning": "brief explanation"}}"""


async def llm_classify_page(page: PageData) -> tuple[PageType, float, str]:
    """Classify a page using the LLM. Returns (type, confidence, reasoning)."""
    if not settings.OPENAI_API_KEY:
        return PageType.UNKNOWN, 0.0, "no_api_key"

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

        # Prepare HTML preview — strip scripts/styles for cleaner context
        html_preview = _clean_html_preview(page.html_snippet or "")

        prompt = CLASSIFICATION_PROMPT.format(
            url=page.url,
            title=page.title or "N/A",
            status_code=page.status_code or "N/A",
            form_count=page.form_count,
            input_count=page.input_count,
            button_count=page.button_count,
            table_count=page.table_count,
            image_count=page.image_count,
            link_count=page.link_count,
            has_login_form=page.has_login_form,
            has_charts=page.has_charts,
            has_nav=page.has_nav,
            has_sidebar=page.has_sidebar,
            has_search=page.has_search,
            headings=json.dumps(page.heading_counts),
            html_preview=html_preview[:1500],
        )

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]

        result = json.loads(content)
        page_type_str = result.get("page_type", "unknown")
        confidence = float(result.get("confidence", 0.5))
        reasoning = result.get("reasoning", "")

        # Map to PageType enum
        try:
            page_type = PageType(page_type_str)
        except ValueError:
            page_type = PageType.UNKNOWN
            confidence = 0.0

        logger.info(
            "llm_labeler.classified",
            url=page.url[:80],
            page_type=page_type.value,
            confidence=round(confidence, 2),
            reasoning=reasoning[:100],
        )

        return page_type, confidence, reasoning

    except Exception as e:
        logger.warning("llm_labeler.failed", url=page.url[:80], error=str(e))
        return PageType.UNKNOWN, 0.0, f"error: {str(e)}"


def _clean_html_preview(html: str) -> str:
    """Strip script/style tags for cleaner LLM context."""
    import re
    # Remove script and style blocks
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Collapse whitespace
    html = re.sub(r'\s+', ' ', html)
    return html.strip()
