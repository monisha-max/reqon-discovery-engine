"""
Evaluator Node — ReAct "Observe + Reason" step.

After each crawl batch, the evaluator:
1. Observes: what page types have we found? what's the coverage?
2. Reasons: are we missing expected page types? should we reprioritize?
3. Decides: continue crawling, adjust strategy, or stop

This is what makes the orchestrator truly ReAct — not just plan-once-execute.
"""
from __future__ import annotations

import json
from typing import Optional

import structlog

from config.settings import settings

logger = structlog.get_logger()


async def evaluate_node(state: dict) -> dict:
    """LangGraph node: evaluate crawl progress and decide next action."""
    pages = state.get("pages", [])
    plan = state.get("plan", {})
    iteration = state.get("iteration", 0)
    should_continue = state.get("should_continue", True)
    continue_reason = state.get("continue_reason", "")
    type_dist = state.get("page_type_distribution", {})
    coverage = state.get("coverage_score", 0)
    frontier_stats = state.get("frontier_stats", {})

    # If frontier says stop, respect it
    if not should_continue:
        logger.info("evaluator.stopping", reason=continue_reason)
        return {
            "should_continue": False,
            "phase": "evaluate",
        }

    # Minimum iterations before evaluating
    if iteration < 2:
        logger.info("evaluator.too_early", iteration=iteration)
        return {"should_continue": True, "phase": "evaluate"}

    # Check: are we finding what we expected?
    expected_types = set(plan.get("expected_page_types", []))
    found_types = set(type_dist.keys()) - {"unknown"}
    missing_types = expected_types - found_types

    # Try LLM-based evaluation if available
    if settings.OPENAI_API_KEY and iteration % 2 == 0:  # every other iteration
        llm_decision = await _llm_evaluate(
            type_dist, plan, coverage, frontier_stats, iteration, len(pages)
        )
        if llm_decision:
            return {**llm_decision, "phase": "evaluate"}

    # Rule-based evaluation
    decision = _rule_based_evaluate(
        type_dist, expected_types, found_types, missing_types,
        coverage, frontier_stats, iteration, len(pages)
    )

    logger.info(
        "evaluator.decision",
        iteration=iteration,
        should_continue=decision["should_continue"],
        reasoning=decision.get("reasoning", ""),
        found_types=list(found_types),
        missing_types=list(missing_types),
        coverage=round(coverage, 2),
    )

    return {**decision, "phase": "evaluate"}


async def _llm_evaluate(type_dist, plan, coverage, frontier_stats, iteration, total_pages) -> Optional[dict]:
    """Use LLM to make a smarter continue/stop decision."""
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

        prompt = f"""You are the decision engine for an autonomous web crawler.

Current crawl state:
- Iteration: {iteration}
- Pages crawled: {total_pages}
- Page type distribution: {json.dumps(type_dist)}
- Coverage score: {coverage:.0%}
- Frontier remaining: {frontier_stats.get('frontier_size', 0)} URLs
- Expected page types from plan: {plan.get('expected_page_types', [])}
- Strategy: {plan.get('strategy_notes', '')}

Should the crawler continue? Consider:
1. Are we finding diverse page types or stuck on one type?
2. Have we found the high-priority page types (forms, auth, dashboards)?
3. Is the frontier exhausted or still has high-value URLs?
4. Have we reached a point of diminishing returns?

Respond ONLY with JSON:
{{"should_continue": true/false, "reasoning": "brief explanation", "reprioritize": "optional hint for crawler"}}"""

        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=200,
        )

        content = response.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0]

        result = json.loads(content)
        logger.info(
            "evaluator.llm_decision",
            should_continue=result.get("should_continue"),
            reasoning=result.get("reasoning", "")[:100],
        )
        return {
            "should_continue": result.get("should_continue", True),
            "reasoning": result.get("reasoning", ""),
        }

    except Exception as e:
        logger.warning("evaluator.llm_failed", error=str(e))
        return None


def _rule_based_evaluate(type_dist, expected, found, missing, coverage, frontier_stats, iteration, total_pages) -> dict:
    """Rule-based evaluation logic."""

    # Good coverage + enough pages → stop
    if coverage >= 0.5 and total_pages >= 15:
        return {
            "should_continue": False,
            "reasoning": f"Good coverage ({coverage:.0%}) with {total_pages} pages. Found types: {list(found)}",
        }

    # Too many iterations without new types → stop
    if iteration >= 6 and len(found) <= 2:
        return {
            "should_continue": False,
            "reasoning": f"After {iteration} iterations, only found {len(found)} page types. Likely a uniform site.",
        }

    # Max iterations guard
    if iteration >= 10:
        return {
            "should_continue": False,
            "reasoning": f"Reached max iterations ({iteration})",
        }

    # Still missing high-priority types and frontier has URLs → continue
    high_priority_missing = missing & {"form", "auth", "dashboard", "settings"}
    if high_priority_missing and frontier_stats.get("frontier_size", 0) > 0:
        return {
            "should_continue": True,
            "reasoning": f"Still looking for: {list(high_priority_missing)}. Frontier has {frontier_stats['frontier_size']} URLs.",
        }

    # Default: continue if frontier has URLs and we haven't hit 50 pages
    if frontier_stats.get("frontier_size", 0) > 0 and total_pages < 50:
        return {
            "should_continue": True,
            "reasoning": f"Continuing exploration. {total_pages} pages, {frontier_stats['frontier_size']} in frontier.",
        }

    return {
        "should_continue": False,
        "reasoning": "Default stop — sufficient exploration done.",
    }
