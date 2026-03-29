"""
Auth Handler Node — thin LangGraph wrapper around Layer 4 AuthHandler.

All auth logic lives in layer4_auth/auth_handler.py.
This node wires the result into the LangGraph state and registers
the SessionMonitor singleton for Layer 2 to use during crawling.
"""
from __future__ import annotations

import structlog

from layer4_auth.auth_handler import AuthHandler, AuthType
from layer4_auth.monitor_singleton import set_active_monitor

logger = structlog.get_logger()

# Module-level singleton so re_authenticate() can be called from crawler_node
_auth_handler: AuthHandler = AuthHandler()


def get_auth_handler() -> AuthHandler:
    """Return the shared AuthHandler (used by crawler_node for re-auth)."""
    return _auth_handler


async def auth_node(state: dict) -> dict:
    """LangGraph node: authenticate and inject session into graph state."""
    plan         = state.get("plan", {})
    request      = state["request"]
    auth_config  = request.get("auth_config") if isinstance(request, dict) else None

    if not plan.get("needs_auth", False):
        logger.info("auth_node.skipped", reason="plan says no auth needed")
        return {"auth_success": True, "phase": "auth"}

    if not auth_config:
        logger.warning("auth_node.no_config", reason="auth required but no config provided")
        return {
            "auth_success": False,
            "phase": "auth",
            "errors": ["Auth required but no auth_config provided in request"],
        }

    target_url = request.get("target_url", "") if isinstance(request, dict) else ""

    # Normalise CLI shorthand → internal dict keys
    if isinstance(auth_config, dict):
        config = auth_config
    else:
        config = auth_config.model_dump()

    logger.info("auth_node.starting", auth_type=config.get("auth_type"), url=target_url)

    result = await _auth_handler.authenticate(target_url, config)

    # Register monitor so Layer 2 can call report_request() / report_redirect()
    set_active_monitor(_auth_handler.monitor)

    if result.success:
        logger.info(
            "auth_node.success",
            strategy=result.strategy_used.value,
            storage_state=result.storage_state_path,
        )
        return {
            "auth_success": True,
            "storage_state_path": result.storage_state_path,
            "auth_session": result.evidence,
            "phase": "auth",
        }
    else:
        # Non-blocking: log failure but allow crawl to proceed unauthenticated
        # (user may want partial coverage even if auth fails)
        logger.warning(
            "auth_node.failed_proceeding_unauthenticated",
            strategy=result.strategy_used.value,
            failure=result.failure_reason,
            failure_code=result.failure_code,
            evidence=result.evidence,
        )
        errors = [f"Auth failed [{result.failure_code}]: {result.failure_reason}"]
        return {
            "auth_success": False,
            "phase": "auth",
            "errors": errors,
        }
