"""
Auth Handler Node — Detects and handles authentication.

Supports: Cookie/StorageState Replay, Form Login, Token Injection.
Executes auth in an isolated Playwright browser context.
"""
from __future__ import annotations

import json
import os
import time

import structlog
from playwright.async_api import async_playwright

logger = structlog.get_logger()

STORAGE_STATE_DIR = "output/auth"


async def auth_node(state: dict) -> dict:
    """LangGraph node: handle authentication if needed."""
    plan = state.get("plan", {})
    request = state["request"]
    auth_config = request.get("auth_config") if isinstance(request, dict) else None

    if not plan.get("needs_auth", False):
        logger.info("auth_handler.skipped", reason="no_auth_needed")
        return {"auth_success": True, "phase": "auth"}

    if not auth_config:
        logger.warning("auth_handler.no_config", reason="auth_needed_but_no_config_provided")
        return {"auth_success": False, "phase": "auth", "errors": ["Auth needed but no auth config provided"]}

    auth_type = auth_config.get("auth_type", "none") if isinstance(auth_config, dict) else auth_config.auth_type

    logger.info("auth_handler.starting", auth_type=auth_type)

    try:
        if auth_type == "cookie" or auth_type == "storage_state":
            result = await _handle_storage_state(auth_config)
        elif auth_type == "form":
            result = await _handle_form_login(auth_config, request)
        elif auth_type == "token":
            result = await _handle_token_injection(auth_config)
        else:
            logger.info("auth_handler.no_auth_type", auth_type=auth_type)
            return {"auth_success": True, "phase": "auth"}

        return {**result, "phase": "auth"}

    except Exception as e:
        logger.error("auth_handler.failed", error=str(e))
        return {"auth_success": False, "phase": "auth", "errors": [f"Auth failed: {str(e)}"]}


async def _handle_storage_state(auth_config) -> dict:
    """Strategy A: Replay cookies/storageState from a previous session."""
    config = auth_config if isinstance(auth_config, dict) else auth_config.model_dump()
    path = config.get("storage_state_path")

    if path and os.path.exists(path):
        logger.info("auth_handler.storage_state_replay", path=path)
        return {"auth_success": True, "storage_state_path": path}

    if config.get("cookies"):
        os.makedirs(STORAGE_STATE_DIR, exist_ok=True)
        state_path = os.path.join(STORAGE_STATE_DIR, f"session_{int(time.time())}.json")
        state = {"cookies": config["cookies"], "origins": []}
        with open(state_path, "w") as f:
            json.dump(state, f)
        logger.info("auth_handler.cookies_saved", path=state_path)
        return {"auth_success": True, "storage_state_path": state_path}

    return {"auth_success": False, "errors": ["No storage state or cookies provided"]}


async def _handle_form_login(auth_config, request) -> dict:
    """Strategy B: Detect login form fields, fill + submit, capture session."""
    config = auth_config if isinstance(auth_config, dict) else auth_config.model_dump()
    req = request if isinstance(request, dict) else request.model_dump()

    login_url = config.get("login_url") or req.get("target_url")
    username = config.get("username")
    password = config.get("password")

    if not username or not password:
        return {"auth_success": False, "errors": ["Form login requires username and password"]}

    os.makedirs(STORAGE_STATE_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()

        try:
            await page.goto(login_url, wait_until="networkidle", timeout=15000)
            await page.wait_for_timeout(1000)

            # Auto-detect and fill login fields
            # Try common selectors for username
            username_selectors = [
                'input[type="email"]', 'input[name="email"]', 'input[name="username"]',
                'input[id="email"]', 'input[id="username"]', 'input[type="text"]',
                'input[autocomplete="username"]', 'input[autocomplete="email"]',
            ]
            for sel in username_selectors:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.fill(username)
                    logger.info("auth_handler.username_filled", selector=sel)
                    break

            # Fill password
            password_el = await page.query_selector('input[type="password"]')
            if password_el:
                await password_el.fill(password)
                logger.info("auth_handler.password_filled")

            # Submit
            submit_selectors = [
                'button[type="submit"]', 'input[type="submit"]',
                'button:has-text("Log in")', 'button:has-text("Sign in")',
                'button:has-text("Login")', 'button:has-text("Submit")',
            ]
            for sel in submit_selectors:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    await el.click()
                    logger.info("auth_handler.submitted", selector=sel)
                    break

            # Wait for navigation after login
            await page.wait_for_load_state("networkidle", timeout=10000)
            await page.wait_for_timeout(2000)

            # Validate: check if we're still on login page
            current_url = page.url
            is_still_login = any(kw in current_url.lower() for kw in ["/login", "/signin", "/auth"])

            if is_still_login:
                logger.warning("auth_handler.possibly_failed", current_url=current_url)

            # Save session state
            state_path = os.path.join(STORAGE_STATE_DIR, f"session_{int(time.time())}.json")
            await context.storage_state(path=state_path)
            logger.info("auth_handler.session_saved", path=state_path)

            return {
                "auth_success": not is_still_login,
                "storage_state_path": state_path,
            }

        finally:
            await browser.close()


async def _handle_token_injection(auth_config) -> dict:
    """Strategy C: Inject auth token into localStorage/headers."""
    config = auth_config if isinstance(auth_config, dict) else auth_config.model_dump()
    token = config.get("token")

    if not token:
        return {"auth_success": False, "errors": ["Token injection requires a token"]}

    # Store token info — the crawler engines will use this
    os.makedirs(STORAGE_STATE_DIR, exist_ok=True)
    state_path = os.path.join(STORAGE_STATE_DIR, f"token_{int(time.time())}.json")
    state = {
        "cookies": [],
        "origins": [{
            "origin": "",
            "localStorage": [{"name": "auth_token", "value": token}]
        }]
    }
    with open(state_path, "w") as f:
        json.dump(state, f)

    logger.info("auth_handler.token_saved", path=state_path)
    return {"auth_success": True, "storage_state_path": state_path}
