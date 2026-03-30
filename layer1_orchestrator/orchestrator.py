"""
Meta-Orchestrator — LangGraph State Machine with ReAct Loop.

Flow: PLAN → AUTH (conditional) → [CRAWL_BATCH → EVALUATE]* → FINALIZE

The ReAct loop:
  Reason (plan/evaluate) → Act (crawl batch) → Observe (evaluate) → Repeat

Features:
- Iterative crawl-evaluate loop (not one-shot)
- LangGraph checkpointing for crash recovery
- Mid-crawl replanning based on what's being discovered
- Redis state persistence
"""
from __future__ import annotations

import time
from typing import Annotated, Any, TypedDict

import structlog
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from layer1_orchestrator.nodes.planner import plan_node
from layer1_orchestrator.nodes.auth_handler import auth_node
from layer1_orchestrator.nodes.crawler_node import crawl_batch_node, cleanup_engines
from layer1_orchestrator.nodes.evaluator import evaluate_node
from layer1_orchestrator.nodes.perf_test_node import perf_test_node
from layer1_orchestrator.nodes.defect_detect_node import defect_detect_node
from shared.state.redis_state import RedisStateManager

logger = structlog.get_logger()


class GraphState(TypedDict, total=False):
    """State that flows through the LangGraph state machine."""
    # Input
    request: dict

    # Phase
    phase: str

    # Plan
    plan: dict

    # Auth
    auth_success: bool
    auth_session: dict
    storage_state_path: str

    # Crawl state (iterative)
    pages: list[dict]
    iteration: int
    should_continue: bool
    continue_reason: str
    new_urls_this_iteration: int
    frontier_stats: dict
    page_type_distribution: dict
    coverage_score: float

    # ReAct reasoning
    reasoning: str

    # Result
    result: dict

    # Performance testing (Layer 3)
    perf_config: dict       # PerfTestRequest fields from CLI
    perf_result: dict       # PerformanceTestResult dict

    # Defect detection (Layer 5)
    defect_config: dict     # Triggers Layer 5; populated with snapshot_artifacts by perf_test_node
    defect_result: dict     # DefectDetectionResult dict

    # Errors
    errors: list[str]


def should_auth(state: GraphState) -> str:
    """Conditional edge: decide whether to run auth before crawling."""
    plan = state.get("plan", {})
    needs_auth = plan.get("needs_auth", False)
    auth_config = state.get("request", {}).get("auth_config")
    logger.info(
        "orchestrator.auth_decision",
        needs_auth=needs_auth,
        has_auth_config=auth_config is not None,
    )
    if needs_auth:
        logger.info("orchestrator.routing_to_auth")
        return "auth"
    logger.info("orchestrator.skipping_auth")
    return "crawl_batch"


def check_auth_result(state: GraphState) -> str:
    """After auth, proceed to crawl."""
    if state.get("auth_success", False):
        logger.info("orchestrator.auth_success")
    else:
        logger.warning("orchestrator.auth_failed_proceeding_anyway")
    return "crawl_batch"


def should_continue_crawling(state: GraphState) -> str:
    """ReAct decision: continue crawling or finalize?"""
    if state.get("should_continue", False):
        logger.info(
            "orchestrator.react_continue",
            iteration=state.get("iteration", 0),
            reasoning=state.get("reasoning", "")[:80],
        )
        return "crawl_batch"

    logger.info(
        "orchestrator.react_stop",
        iteration=state.get("iteration", 0),
        reasoning=state.get("reasoning", "")[:80],
        total_pages=len(state.get("pages", [])),
    )
    return "finalize"


async def finalize_node(state: dict) -> dict:
    """Final node: compile results and clean up."""
    pages = state.get("pages", [])

    # Clean up persistent engines
    await cleanup_engines()

    result = {
        "target_url": state["request"].get("target_url", ""),
        "pages": pages,
        "total_urls_discovered": state.get("frontier_stats", {}).get("total_discovered", 0),
        "total_pages_crawled": len(pages),
        "coverage_score": state.get("coverage_score", 0),
        "page_type_distribution": state.get("page_type_distribution", {}),
        "iterations": state.get("iteration", 0),
    }

    # Persist final state to Redis
    try:
        redis = RedisStateManager()
        await redis.connect()
        await redis.set("last_crawl_result", result, ttl=86400)
        await redis.push_to_stream("crawl_events", {
            "event": "crawl_complete",
            "target_url": result["target_url"],
            "pages_crawled": result["total_pages_crawled"],
            "coverage": result["coverage_score"],
        })
        await redis.disconnect()
    except Exception as e:
        logger.warning("orchestrator.redis_persist_failed", error=str(e))

    logger.info(
        "orchestrator.finalized",
        pages_crawled=len(pages),
        iterations=state.get("iteration", 0),
        coverage=state.get("coverage_score", 0),
    )

    return {"result": result, "phase": "complete"}


def should_run_post_processing(state: GraphState) -> str:
    """Conditional edge: run perf, defect-only, or end after finalize."""
    if state.get("perf_config"):
        logger.info("orchestrator.routing_to_perf_tests")
        return "perf_test"

    defect_config = state.get("defect_config") or {}
    if defect_config.get("enabled"):
        logger.info("orchestrator.routing_to_defect_detection")
        return "defect_detect"
    return END


def should_run_defect_detection(state: GraphState) -> str:
    """Conditional edge: run defect detection after perf when enabled."""
    defect_config = state.get("defect_config") or {}
    if defect_config.get("enabled"):
        logger.info("orchestrator.routing_to_defect_detection")
        return "defect_detect"
    return END


def build_graph() -> StateGraph:
    """Build the LangGraph state machine with ReAct loop + optional Layer 3."""
    graph = StateGraph(GraphState)

    # Nodes
    graph.add_node("plan", plan_node)
    graph.add_node("auth", auth_node)
    graph.add_node("crawl_batch", crawl_batch_node)
    graph.add_node("evaluate", evaluate_node)
    graph.add_node("finalize", finalize_node)
    graph.add_node("perf_test", perf_test_node)
    graph.add_node("defect_detect", defect_detect_node)

    # Entry
    graph.set_entry_point("plan")

    # PLAN → AUTH or CRAWL_BATCH
    graph.add_conditional_edges("plan", should_auth, {
        "auth": "auth",
        "crawl_batch": "crawl_batch",
    })

    # AUTH → CRAWL_BATCH
    graph.add_conditional_edges("auth", check_auth_result, {
        "crawl_batch": "crawl_batch",
    })

    # CRAWL_BATCH → EVALUATE (always)
    graph.add_edge("crawl_batch", "evaluate")

    # EVALUATE → CRAWL_BATCH (continue) or FINALIZE (stop)
    # This is the ReAct loop
    graph.add_conditional_edges("evaluate", should_continue_crawling, {
        "crawl_batch": "crawl_batch",
        "finalize": "finalize",
    })

    # FINALIZE → PERF_TEST (if enabled) or END
    graph.add_conditional_edges("finalize", should_run_post_processing, {
        "perf_test": "perf_test",
        "defect_detect": "defect_detect",
        END: END,
    })

    # PERF_TEST → DEFECT_DETECT (if visual capture ran) or END
    graph.add_conditional_edges("perf_test", should_run_defect_detection, {
        "defect_detect": "defect_detect",
        END: END,
    })

    # DEFECT_DETECT → END
    graph.add_edge("defect_detect", END)

    return graph


async def run_orchestrator(
    target_url: str,
    auth_config: dict = None,
    max_pages: int = 100,
    max_depth: int = 5,
    thread_id: str = "default",
    perf_config: dict = None,
    defect_config: dict = None,
) -> dict:
    """Main entry point: run the full orchestration pipeline with checkpointing."""
    logger.info("orchestrator.starting", url=target_url)

    request = {
        "target_url": target_url,
        "auth_config": auth_config,
        "scope": "full",
        "max_pages": max_pages,
        "max_depth": max_depth,
    }

    initial_state: GraphState = {
        "request": request,
        "phase": "start",
        "pages": [],
        "iteration": 0,
        "should_continue": True,
        "errors": [],
        "perf_config": perf_config or {},
        "defect_config": defect_config or {},
    }

    # Build graph with checkpointing
    graph = build_graph()
    checkpointer = MemorySaver()
    app = graph.compile(checkpointer=checkpointer)

    # Publish start event to Redis
    try:
        redis = RedisStateManager()
        await redis.connect()
        await redis.push_to_stream("crawl_events", {
            "event": "crawl_started",
            "target_url": target_url,
            "thread_id": thread_id,
        })
        await redis.disconnect()
    except Exception:
        pass

    # Run with checkpointing config
    config = {"configurable": {"thread_id": thread_id}}
    final_state = await app.ainvoke(initial_state, config)

    logger.info(
        "orchestrator.complete",
        phase=final_state.get("phase"),
        pages_crawled=len(final_state.get("pages", [])),
        coverage=final_state.get("coverage_score", 0),
        iterations=final_state.get("iteration", 0),
    )

    return final_state
