"""
Planner Node — Analyzes target URL and generates a strategic crawl plan.

Uses LLM if available, falls back to rule-based planning.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import structlog

from config.settings import settings
from shared.models.state_models import CrawlPlan, OrchestratorState

logger = structlog.get_logger()

LLM_PLANNER_PROMPT = """You are a QA strategist for an autonomous web testing engine.
Given a target URL, analyze it and produce a crawl strategy.

Target URL: {url}
Auth config provided: {has_auth}

Respond ONLY with valid JSON (no markdown, no explanation):
{{
  "expected_page_types": ["list of expected page types from: dashboard, list_table, form, wizard, report, detail, settings, auth, landing, search, profile, error"],
  "needs_auth": true/false,
  "priority_areas": ["list of areas to focus on, e.g. 'checkout_flow', 'admin_panel', 'user_settings'"],
  "estimated_page_count": number,
  "strategy_notes": "brief strategy description"
}}"""


async def plan_node(state: dict) -> dict:
    """LangGraph node: analyze target and create crawl strategy."""
    request = state["request"]
    target_url = request["target_url"] if isinstance(request, dict) else request.target_url
    auth_config = request.get("auth_config") if isinstance(request, dict) else request.auth_config

    # Diagnostic: log what auth_config arrived with
    auth_type_received = None
    if isinstance(auth_config, dict):
        auth_type_received = auth_config.get("auth_type")
    logger.info(
        "planner.analyzing",
        url=target_url,
        has_auth_config=auth_config is not None,
        auth_type=auth_type_received,
    )

    # Try LLM planner first, fall back to rules
    plan = None
    if settings.OPENAI_API_KEY:
        plan = await _llm_plan(target_url, auth_config)

    if not plan:
        plan = _rule_based_plan(target_url, auth_config)

    # Final override (belt-and-braces): if auth_config was provided, needs_auth must be True
    if auth_config:
        auth_dict = auth_config if isinstance(auth_config, dict) else {}
        if auth_dict.get("auth_type") and auth_dict["auth_type"] != "none":
            plan["needs_auth"] = True
        if auth_dict.get("cookies") or auth_dict.get("username") or auth_dict.get("login_url"):
            plan["needs_auth"] = True

    logger.info(
        "planner.plan_created",
        needs_auth=plan["needs_auth"],
        expected_types=plan["expected_page_types"],
        strategy=plan["strategy_notes"],
    )

    return {"plan": plan, "phase": "plan"}


async def _llm_plan(target_url: str, auth_config=None) -> dict | None:
    """Generate a crawl plan using OpenAI LLM."""
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

        has_auth = bool(auth_config and (
            (isinstance(auth_config, dict) and auth_config.get("auth_type", "none") != "none") or
            (hasattr(auth_config, "auth_type") and auth_config.auth_type != "none")
        ))

        prompt = LLM_PLANNER_PROMPT.format(url=target_url, has_auth=has_auth)

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )

        content = response.choices[0].message.content.strip()
        # Strip markdown code fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]

        plan = json.loads(content)

        # Override needs_auth if user provided auth config
        if auth_config:
            auth_dict = auth_config if isinstance(auth_config, dict) else auth_config.model_dump()
            if auth_dict.get("auth_type") and auth_dict["auth_type"] != "none":
                plan["needs_auth"] = True
            if auth_dict.get("login_url") or auth_dict.get("username"):
                plan["needs_auth"] = True

        logger.info("planner.llm_success", model="gpt-4o-mini")
        return plan

    except Exception as e:
        logger.warning("planner.llm_failed", error=str(e), fallback="rule_based")
        return None


def _rule_based_plan(target_url: str, auth_config=None) -> dict:
    """Generate a crawl plan using URL analysis heuristics."""
    parsed = urlparse(target_url)
    domain = parsed.netloc.lower()
    path = parsed.path.lower()

    expected_page_types = []
    priority_areas = []
    needs_auth = False
    estimated_pages = 30
    strategy_notes = ""

    # Auth detection
    if auth_config:
        auth_dict = auth_config if isinstance(auth_config, dict) else auth_config.model_dump()
        if auth_dict.get("auth_type") and auth_dict["auth_type"] != "none":
            needs_auth = True
        if auth_dict.get("login_url") or auth_dict.get("username") or auth_dict.get("cookies"):
            needs_auth = True

    # Domain-based heuristics
    if any(kw in domain for kw in ["app.", "dashboard.", "admin.", "portal."]):
        expected_page_types.extend(["dashboard", "settings", "form", "list_table"])
        priority_areas.append("authenticated_app_pages")
        needs_auth = True
        estimated_pages = 50
        strategy_notes = "Likely a web application with authenticated content. Prioritize forms, dashboards, and settings."

    elif any(kw in domain for kw in ["shop.", "store.", "checkout."]):
        expected_page_types.extend(["landing", "list_table", "detail", "form", "wizard"])
        priority_areas.append("product_and_checkout_flows")
        estimated_pages = 80
        strategy_notes = "E-commerce site. Prioritize product pages, cart, and checkout flows."

    elif any(kw in domain for kw in ["docs.", "wiki.", "help.", "support."]):
        expected_page_types.extend(["detail", "search", "list_table"])
        priority_areas.append("content_navigation")
        estimated_pages = 60
        strategy_notes = "Documentation/content site. Focus on navigation structure and search."

    else:
        # Generic site analysis
        expected_page_types.extend(["landing", "detail", "form", "list_table"])
        priority_areas.append("main_navigation")
        estimated_pages = 40
        strategy_notes = "General web application. Crawl main navigation and discover page types."

    # Path-based refinements
    if "/login" in path or "/auth" in path:
        needs_auth = True
    if "/admin" in path:
        priority_areas.append("admin_panel")
        needs_auth = True

    return {
        "expected_page_types": expected_page_types,
        "needs_auth": needs_auth,
        "priority_areas": priority_areas,
        "estimated_page_count": estimated_pages,
        "strategy_notes": strategy_notes,
    }
