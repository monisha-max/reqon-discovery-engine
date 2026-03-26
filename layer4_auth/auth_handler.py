"""
Layer 4 — Auth Handler Template

Architecture (from PDF):
┌─────────────────────────────────────────────────┐
│              Auth Detection & Routing            │
│  ┌──────────────┐  ┌──────────────┐             │
│  │ Auth Trigger  │  │ Auth Detector│             │
│  │ 401/403 burst │  │ Redirect     │             │
│  │ session expiry│  │ DOM login    │             │
│  │ logout detect │  │ CAPTCHA/2FA  │             │
│  └──────┬───────┘  └──────┬───────┘             │
│         └────────┬────────┘                      │
│         ┌────────▼────────┐                      │
│         │ Strategy Router │                      │
│         │ 1. Cookie Replay│                      │
│         │ 2. Form Login   │                      │
│         │ 3. Token Inject │                      │
│         └─────────────────┘                      │
├─────────────────────────────────────────────────┤
│            Auth Strategy Execution               │
│  Isolated Playwright context                     │
│  Session Validator (proof check)                 │
│  Evidence Builder (trace + reason)               │
├─────────────────────────────────────────────────┤
│          Session Persistence + Refresh           │
│  StorageState Manager (versioned, encrypted)     │
│  Session Lease Distributor (to Layer 2 + 3)      │
│  Expiry Monitor (401/403, idle, logout redirect) │
│  Token Refresher + Re-Auth Fallback              │
└─────────────────────────────────────────────────┘

Connection to Layer 2:
- Layer 4 provides `storage_state_path` to CrawlerAgent and PlaywrightEngine
- Layer 4 monitors for session expiry during crawl and triggers re-auth
- Layer 4 distributes isolated browser contexts per worker

What's already working (in layer1_orchestrator/nodes/auth_handler.py):
- Basic form login detection + fill + submit
- Cookie/StorageState replay
- Token injection
- storageState.json persistence

What your team should build:
- [ ] Auth Trigger: detect mid-crawl auth failures (401/403 burst detection)
- [ ] Auth Detector: classify auth type with confidence (OAuth, SSO, TOTP, form, etc.)
- [ ] Strategy Router: pick lowest-friction auth path automatically
- [ ] Session Validator: proof check (not just "login page disappeared")
- [ ] Evidence Builder: trace.zip + redirect chain + failure reason
- [ ] Expiry Monitor: watch for session death during long crawls
- [ ] Session Distributor: isolated contexts per Playwright worker
- [ ] Re-Auth Fallback: auto re-authenticate when session expires
- [ ] CAPTCHA/2FA handling: detect and gracefully stop or request user session
"""
from __future__ import annotations

import json
import os
import time
from enum import Enum
from typing import Optional

import structlog
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Browser, BrowserContext

logger = structlog.get_logger()


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class AuthType(str, Enum):
    FORM_LOGIN = "form_login"
    COOKIE_REPLAY = "cookie_replay"
    TOKEN_INJECTION = "token_injection"
    OAUTH = "oauth"
    SSO = "sso"
    TOTP = "totp"
    CAPTCHA_BLOCKED = "captcha_blocked"
    UNKNOWN = "unknown"
    NONE = "none"


class AuthDetectionResult(BaseModel):
    """Result of detecting what kind of auth a site uses."""
    needs_auth: bool = False
    auth_type: AuthType = AuthType.NONE
    confidence: float = 0.0
    login_url: Optional[str] = None
    has_captcha: bool = False
    has_2fa: bool = False
    redirect_chain: list[str] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)


class AuthResult(BaseModel):
    """Result of an auth attempt."""
    success: bool = False
    strategy_used: AuthType = AuthType.NONE
    storage_state_path: Optional[str] = None
    session_valid: bool = False
    failure_reason: Optional[str] = None
    evidence: dict = Field(default_factory=dict)  # trace, redirect chain, screenshots


class SessionHealth(BaseModel):
    """Health status of the current auth session."""
    is_valid: bool = True
    refresh_count: int = 0
    auth_failures: int = 0
    session_age_seconds: float = 0.0
    blocked_by_captcha: bool = False
    needs_reauth: bool = False


# ─────────────────────────────────────────────
# Auth Detector
# ─────────────────────────────────────────────

class AuthDetector:
    """Detects whether a site needs auth and what kind.

    TODO for your team:
    - Analyze redirect chains (302 → /login)
    - Check DOM for login signals (password fields, OAuth buttons)
    - Detect CAPTCHA (reCAPTCHA, hCaptcha, Cloudflare)
    - Detect 2FA (TOTP input fields, SMS verification)
    - Return auth_type with confidence score
    """

    async def detect(self, url: str, page=None) -> AuthDetectionResult:
        """Analyze a URL/page to determine auth requirements."""
        # TODO: Implement full detection logic
        # For now, basic detection from the existing auth_handler
        result = AuthDetectionResult()

        if page:
            # Check for password fields
            has_password = await page.evaluate(
                "() => document.querySelector('input[type=\"password\"]') !== null"
            )
            if has_password:
                result.needs_auth = True
                result.auth_type = AuthType.FORM_LOGIN
                result.confidence = 0.9

            # TODO: Check for OAuth buttons (Google, GitHub, etc.)
            # TODO: Check for SSO redirects
            # TODO: Check for CAPTCHA
            # TODO: Check for 2FA fields

        return result


# ─────────────────────────────────────────────
# Strategy Router
# ─────────────────────────────────────────────

class AuthStrategyRouter:
    """Picks the lowest-friction auth strategy.

    Priority order (from PDF):
    1. Cookie/StorageState Replay — fastest, no interaction needed
    2. Form Login Runner — detect fields, fill, submit
    3. Token Injection — localStorage token
    If CAPTCHA/2FA → request user session / stop safely

    TODO for your team:
    - Implement strategy selection based on AuthDetectionResult
    - Handle OAuth redirect flows
    - Handle SSO (SAML, etc.)
    - Implement CAPTCHA detection → graceful stop
    """

    def select_strategy(self, detection: AuthDetectionResult, auth_config: dict) -> AuthType:
        """Select the best auth strategy based on detection + available config."""
        # Strategy 1: If we have a prior session, try replay first
        if auth_config.get("storage_state_path") or auth_config.get("cookies"):
            return AuthType.COOKIE_REPLAY

        # Strategy 2: If we have credentials, try form login
        if auth_config.get("username") and auth_config.get("password"):
            return AuthType.FORM_LOGIN

        # Strategy 3: If we have a token, inject it
        if auth_config.get("token"):
            return AuthType.TOKEN_INJECTION

        # Blocked
        if detection.has_captcha:
            return AuthType.CAPTCHA_BLOCKED

        return detection.auth_type


# ─────────────────────────────────────────────
# Auth Executor
# ─────────────────────────────────────────────

class AuthExecutor:
    """Executes auth strategies in an isolated Playwright context.

    TODO for your team:
    - Implement OAuth flow handler
    - Implement SSO handler
    - Add evidence capture (Playwright trace + screenshots)
    - Session Validator: proof check after login
      (protected route accessible + logged-in marker + no 401/403)
    """

    def __init__(self, storage_dir: str = "output/auth"):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)

    async def execute(self, strategy: AuthType, auth_config: dict, target_url: str) -> AuthResult:
        """Execute the selected auth strategy."""
        if strategy == AuthType.COOKIE_REPLAY:
            return await self._replay_session(auth_config)
        elif strategy == AuthType.FORM_LOGIN:
            return await self._form_login(auth_config, target_url)
        elif strategy == AuthType.TOKEN_INJECTION:
            return await self._inject_token(auth_config)
        elif strategy == AuthType.CAPTCHA_BLOCKED:
            return AuthResult(
                success=False,
                strategy_used=strategy,
                failure_reason="CAPTCHA detected — cannot automate. Provide a manual session.",
            )
        else:
            return AuthResult(success=False, failure_reason=f"Unsupported strategy: {strategy}")

    async def _replay_session(self, auth_config: dict) -> AuthResult:
        """Strategy A: Replay a prior storageState or cookies."""
        path = auth_config.get("storage_state_path")
        if path and os.path.exists(path):
            # TODO: Validate the session is still valid before returning
            return AuthResult(
                success=True,
                strategy_used=AuthType.COOKIE_REPLAY,
                storage_state_path=path,
                session_valid=True,  # TODO: actually validate
            )

        cookies = auth_config.get("cookies")
        if cookies:
            state_path = os.path.join(self.storage_dir, f"session_{int(time.time())}.json")
            with open(state_path, "w") as f:
                json.dump({"cookies": cookies, "origins": []}, f)
            return AuthResult(
                success=True,
                strategy_used=AuthType.COOKIE_REPLAY,
                storage_state_path=state_path,
            )

        return AuthResult(success=False, failure_reason="No session data to replay")

    async def _form_login(self, auth_config: dict, target_url: str) -> AuthResult:
        """Strategy B: Detect form fields, fill, submit, capture session."""
        login_url = auth_config.get("login_url", target_url)
        username = auth_config.get("username")
        password = auth_config.get("password")

        if not username or not password:
            return AuthResult(success=False, failure_reason="Form login requires username + password")

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 1920, "height": 1080})
            page = await context.new_page()

            try:
                await page.goto(login_url, wait_until="networkidle", timeout=15000)
                await page.wait_for_timeout(1000)

                # Fill username (try common selectors)
                for sel in ['input[type="email"]', 'input[name="email"]', 'input[name="username"]',
                            'input[id="email"]', 'input[id="username"]', 'input[type="text"]']:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.fill(username)
                        break

                # Fill password
                pw_el = await page.query_selector('input[type="password"]')
                if pw_el:
                    await pw_el.fill(password)

                # Submit
                for sel in ['button[type="submit"]', 'input[type="submit"]',
                            'button:has-text("Log in")', 'button:has-text("Sign in")',
                            'button:has-text("Login")', 'button:has-text("Submit")']:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        break

                await page.wait_for_load_state("networkidle", timeout=10000)
                await page.wait_for_timeout(2000)

                # Validate login success
                # TODO: Implement proper proof check:
                #   1. Try accessing a protected route
                #   2. Check for logged-in UI marker
                #   3. Verify no 401/403 responses
                still_on_login = any(kw in page.url.lower() for kw in ["/login", "/signin", "/auth"])

                # Save session
                state_path = os.path.join(self.storage_dir, f"session_{int(time.time())}.json")
                await context.storage_state(path=state_path)

                return AuthResult(
                    success=not still_on_login,
                    strategy_used=AuthType.FORM_LOGIN,
                    storage_state_path=state_path,
                    session_valid=not still_on_login,
                    failure_reason="Still on login page after submit" if still_on_login else None,
                )

            finally:
                await browser.close()

    async def _inject_token(self, auth_config: dict) -> AuthResult:
        """Strategy C: Inject token into localStorage."""
        token = auth_config.get("token")
        if not token:
            return AuthResult(success=False, failure_reason="No token provided")

        state_path = os.path.join(self.storage_dir, f"token_{int(time.time())}.json")
        state = {
            "cookies": [],
            "origins": [{"origin": "", "localStorage": [{"name": "auth_token", "value": token}]}]
        }
        with open(state_path, "w") as f:
            json.dump(state, f)

        return AuthResult(
            success=True,
            strategy_used=AuthType.TOKEN_INJECTION,
            storage_state_path=state_path,
        )


# ─────────────────────────────────────────────
# Session Monitor
# ─────────────────────────────────────────────

class SessionMonitor:
    """Monitors session health during long crawls.

    TODO for your team:
    - Watch for 401/403 bursts in crawl telemetry
    - Detect idle timeout patterns
    - Detect logout redirects
    - Trigger re-auth when session expires
    - Notify all workers to refresh their contexts

    This should run as a background task during the crawl.
    It receives telemetry from Layer 2 (failed_requests, status_codes)
    and triggers re-auth through the AuthExecutor when needed.
    """

    def __init__(self):
        self._session_start = time.time()
        self._auth_failure_count = 0
        self._refresh_count = 0
        self._last_valid_check = time.time()

    def report_request(self, status_code: int, url: str):
        """Called by Layer 2 for every response. Tracks auth failures."""
        if status_code in (401, 403):
            self._auth_failure_count += 1
            logger.warning("session_monitor.auth_failure", status=status_code, url=url,
                          total_failures=self._auth_failure_count)

    def get_health(self) -> SessionHealth:
        """Get current session health status."""
        age = time.time() - self._session_start
        needs_reauth = self._auth_failure_count >= 3  # 3 consecutive 401/403 = session dead

        return SessionHealth(
            is_valid=not needs_reauth,
            refresh_count=self._refresh_count,
            auth_failures=self._auth_failure_count,
            session_age_seconds=age,
            needs_reauth=needs_reauth,
        )

    def reset_after_reauth(self):
        """Reset counters after successful re-authentication."""
        self._auth_failure_count = 0
        self._refresh_count += 1
        self._last_valid_check = time.time()


# ─────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────

class AuthHandler:
    """Main Layer 4 entry point. Orchestrates detection → routing → execution → monitoring.

    Usage by Layer 1:
        handler = AuthHandler()
        result = await handler.authenticate(target_url, auth_config)
        # Pass result.storage_state_path to CrawlerAgent and PlaywrightEngine

    Usage by Layer 2 (during crawl):
        handler.monitor.report_request(status_code, url)
        health = handler.monitor.get_health()
        if health.needs_reauth:
            result = await handler.re_authenticate()
    """

    def __init__(self):
        self.detector = AuthDetector()
        self.router = AuthStrategyRouter()
        self.executor = AuthExecutor()
        self.monitor = SessionMonitor()
        self._last_result: Optional[AuthResult] = None

    async def authenticate(self, target_url: str, auth_config: dict) -> AuthResult:
        """Full auth flow: detect → route → execute → validate."""
        logger.info("auth_handler.starting", url=target_url)

        # Step 1: Detect auth type
        detection = await self.detector.detect(target_url)

        # Step 2: Override with user-provided config if available
        if auth_config and auth_config.get("auth_type"):
            detection.needs_auth = True

        if not detection.needs_auth and not auth_config:
            logger.info("auth_handler.no_auth_needed")
            return AuthResult(success=True, strategy_used=AuthType.NONE)

        # Step 3: Select strategy
        strategy = self.router.select_strategy(detection, auth_config or {})
        logger.info("auth_handler.strategy_selected", strategy=strategy.value)

        # Step 4: Execute
        result = await self.executor.execute(strategy, auth_config or {}, target_url)
        self._last_result = result

        logger.info(
            "auth_handler.complete",
            success=result.success,
            strategy=result.strategy_used.value,
            storage_state=result.storage_state_path,
        )

        return result

    async def re_authenticate(self) -> AuthResult:
        """Re-authenticate using the last successful strategy.

        TODO: Implement refresh token flow first, fall back to full re-auth.
        """
        if self._last_result and self._last_result.storage_state_path:
            logger.info("auth_handler.re_authenticating")
            # TODO: Try refresh first, then full re-auth
            self.monitor.reset_after_reauth()
            return self._last_result

        return AuthResult(success=False, failure_reason="No prior auth to refresh")
