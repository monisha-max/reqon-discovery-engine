"""
Layer 4 — Auth Handler

Architecture:
  AuthDetector  → detects auth type + confidence from live page signals
  AuthStrategyRouter → picks lowest-friction strategy
  AuthExecutor  → executes strategy in isolated Playwright context
                  with evidence capture (trace + redirect chain)
  SessionMonitor → watches 401/403 bursts + logout redirects during crawl
  AuthHandler   → orchestrates all four above; main entry point

Wired into Layer 1 via layer1_orchestrator/nodes/auth_handler.py.
SessionMonitor singleton is shared with Layer 2 via layer4_auth/monitor_singleton.py.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import structlog
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = structlog.get_logger()


# ─────────────────────────────────────────────
# Failure reason taxonomy
# ─────────────────────────────────────────────

class AuthFailureReason(str, Enum):
    WRONG_CREDENTIALS    = "wrong_credentials"
    CAPTCHA_BLOCKED      = "captcha_blocked"
    SESSION_EXPIRED      = "session_expired"
    NETWORK_TIMEOUT      = "network_timeout"
    TWO_FA_REQUIRED      = "2fa_required"
    SSO_REDIRECT         = "sso_redirect"
    OAUTH_REDIRECT       = "oauth_redirect"
    NO_LOGIN_FORM        = "no_login_form"
    NO_CONFIG            = "no_config"
    UNKNOWN              = "unknown"


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class AuthType(str, Enum):
    FORM_LOGIN      = "form_login"
    COOKIE_REPLAY   = "cookie_replay"
    TOKEN_INJECTION = "token_injection"
    OAUTH           = "oauth"
    SSO             = "sso"
    TOTP            = "totp"
    CAPTCHA_BLOCKED = "captcha_blocked"
    UNKNOWN         = "unknown"
    NONE            = "none"


class AuthDetectionResult(BaseModel):
    needs_auth:    bool      = False
    auth_type:     AuthType  = AuthType.NONE
    confidence:    float     = 0.0
    login_url:     Optional[str] = None
    has_captcha:   bool      = False
    has_2fa:       bool      = False
    has_oauth:     bool      = False
    has_sso:       bool      = False
    redirect_chain: list[str] = Field(default_factory=list)
    evidence:      dict      = Field(default_factory=dict)


class AuthResult(BaseModel):
    success:            bool     = False
    strategy_used:      AuthType = AuthType.NONE
    storage_state_path: Optional[str] = None
    session_valid:      bool     = False
    failure_reason:     Optional[str] = None
    failure_code:       Optional[AuthFailureReason] = None
    evidence:           dict     = Field(default_factory=dict)


class SessionHealth(BaseModel):
    is_valid:            bool  = True
    refresh_count:       int   = 0
    auth_failures:       int   = 0
    logout_redirects:    int   = 0
    session_age_seconds: float = 0.0
    blocked_by_captcha:  bool  = False
    needs_reauth:        bool  = False


# ─────────────────────────────────────────────
# Auth Detector
# ─────────────────────────────────────────────

# OAuth provider button patterns (text + href signals)
_OAUTH_TEXT_PATTERNS = re.compile(
    r"(continue|sign in|log in|login).*(with|using|via)?\s*(google|github|microsoft|apple|facebook|twitter|linkedin|slack|okta|azure)",
    re.IGNORECASE,
)
_OAUTH_HREF_PATTERNS = re.compile(
    r"accounts\.google\.com|github\.com/login/oauth|login\.microsoftonline\.com"
    r"|appleid\.apple\.com|facebook\.com/dialog|api\.twitter\.com/oauth"
    r"|linkedin\.com/oauth|slack\.com/oauth|okta\.com|auth0\.com",
    re.IGNORECASE,
)

# SSO signals: redirect to external IdP domain that is NOT the target
_SSO_DOMAINS = re.compile(
    r"sso\.|saml\.|idp\.|auth\.|login\.|account\.|identity\.|okta\.com"
    r"|onelogin\.com|pingidentity\.com|shibboleth",
    re.IGNORECASE,
)

# CAPTCHA signals
_CAPTCHA_SELECTORS = [
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'div.g-recaptcha',
    'div.h-captcha',
    '[data-sitekey]',
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
]

# 2FA / TOTP signals (appear after first submit)
_TOTP_SELECTORS = [
    'input[autocomplete="one-time-code"]',
    'input[name*="otp"]',
    'input[name*="mfa"]',
    'input[name*="totp"]',
    'input[name*="verification"]',
    'input[name*="token"][type="text"]',
    'input[name*="code"][maxlength="6"]',
    'input[placeholder*="verification code"]',
    'input[placeholder*="one-time"]',
]

# Logged-in marker selectors (post-login proof)
_LOGGED_IN_MARKERS = [
    '[data-testid*="user"]', '[data-testid*="avatar"]', '[data-testid*="profile"]',
    '[class*="user-avatar"]', '[class*="user-menu"]', '[class*="profile"]',
    'button:has-text("Logout")', 'button:has-text("Log out")', 'button:has-text("Sign out")',
    'a:has-text("Logout")', 'a:has-text("Log out")', 'a:has-text("Sign out")',
    '[aria-label*="user menu"]', '[aria-label*="account menu"]',
    '[class*="account"]', '[id*="user-menu"]',
]


class AuthDetector:
    """Detects auth type and confidence from live page signals."""

    async def detect(self, url: str, page: Optional[Page] = None) -> AuthDetectionResult:
        """
        Navigate to url (or use provided page) and inspect DOM signals.
        Returns AuthDetectionResult with auth_type + confidence.
        """
        if page is not None:
            return await self._inspect_page(page, url, [url])

        # Launch our own browser for detection
        redirect_chain: list[str] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )
            detection_page = await context.new_page()

            # Track redirect chain
            detection_page.on(
                "response",
                lambda r: redirect_chain.append(r.url) if r.status in (301, 302, 303, 307, 308) else None
            )

            try:
                await detection_page.goto(url, wait_until="networkidle", timeout=20000)
                await detection_page.wait_for_timeout(800)
                result = await self._inspect_page(detection_page, url, redirect_chain)
            except Exception as e:
                logger.warning("auth_detector.navigation_failed", url=url, error=str(e))
                result = AuthDetectionResult(failure_reason=str(e))
            finally:
                await browser.close()

        return result

    async def _inspect_page(self, page: Page, original_url: str, redirect_chain: list[str]) -> AuthDetectionResult:
        result = AuthDetectionResult(redirect_chain=redirect_chain)

        # Build config object once — passed as single arg to page.evaluate
        _js_config = {
            "oauthTextPattern":  _OAUTH_TEXT_PATTERNS.pattern,
            "oauthHrefPattern":  _OAUTH_HREF_PATTERNS.pattern,
            "ssoDomainsPattern": _SSO_DOMAINS.pattern,
            "captchaSelectors":  _CAPTCHA_SELECTORS,
            "totpSelectors":     _TOTP_SELECTORS,
        }

        try:
            signals = await page.evaluate("""(cfg) => {
                const getText = el => (el.textContent || el.value || el.getAttribute('aria-label') || '').trim();
                const hasSelector = sel => { try { return !!document.querySelector(sel); } catch(e) { return false; } };
                const allHrefs = [...document.querySelectorAll('a[href]')].map(a => a.href);

                // Password field
                const hasPassword = hasSelector('input[type="password"]');

                // Username field
                const hasUsername = hasSelector(
                    'input[type="email"], input[name="email"], input[name="username"], input[type="text"][name*="user"]'
                );

                // OAuth: button/link text matches pattern
                const allBtnText = [...document.querySelectorAll('button, a, [role="button"]')]
                    .map(el => getText(el)).join(' ');
                const hasOauthText = new RegExp(cfg.oauthTextPattern, 'i').test(allBtnText);

                // OAuth: href matches known provider
                const hasOauthHref = allHrefs.some(h => new RegExp(cfg.oauthHrefPattern, 'i').test(h));

                // SSO: landed on a known IdP domain
                const currentDomain = window.location.hostname;
                const hasSsoSignal = new RegExp(cfg.ssoDomainsPattern, 'i').test(currentDomain);

                // CAPTCHA
                const hasCaptcha = cfg.captchaSelectors.some(sel => hasSelector(sel));

                // Cloudflare challenge page
                const isCloudflare = document.title.includes('Just a moment') ||
                    hasSelector('[class*="cf-challenge"]') ||
                    hasSelector('#challenge-form');

                // 2FA fields
                const has2FA = cfg.totpSelectors.some(sel => hasSelector(sel));

                return {
                    hasPassword, hasUsername,
                    hasOauthText, hasOauthHref,
                    hasSsoSignal, hasCaptcha: hasCaptcha || isCloudflare,
                    has2FA,
                    currentUrl: window.location.href,
                    pageTitle: document.title,
                };
            }""", _js_config)
        except Exception as e:
            logger.warning("auth_detector.inspect_failed", error=str(e))
            return result

        result.evidence = signals

        # CAPTCHA: highest priority — blocks automation
        if signals.get("hasCaptcha"):
            result.needs_auth = True
            result.has_captcha = True
            result.auth_type = AuthType.CAPTCHA_BLOCKED
            result.confidence = 0.95
            logger.info("auth_detector.captcha_detected", url=signals.get("currentUrl"))
            return result

        # 2FA field visible immediately (unusual but possible)
        if signals.get("has2FA") and not signals.get("hasPassword"):
            result.needs_auth = True
            result.has_2fa = True
            result.auth_type = AuthType.TOTP
            result.confidence = 0.85
            return result

        # OAuth button/link detected
        if signals.get("hasOauthText") or signals.get("hasOauthHref"):
            result.needs_auth = True
            result.has_oauth = True
            # May also have a form (mixed page)
            if signals.get("hasPassword"):
                result.auth_type = AuthType.FORM_LOGIN
                result.confidence = 0.75
                result.evidence["also_has_oauth"] = True
            else:
                result.auth_type = AuthType.OAUTH
                result.confidence = 0.85
            return result

        # SSO redirect (landed on different IdP domain)
        if signals.get("hasSsoSignal"):
            result.needs_auth = True
            result.has_sso = True
            result.auth_type = AuthType.SSO
            result.confidence = 0.80
            result.login_url = signals.get("currentUrl")
            return result

        # Standard form login
        if signals.get("hasPassword"):
            result.needs_auth = True
            result.auth_type = AuthType.FORM_LOGIN
            result.confidence = 0.9
            result.login_url = signals.get("currentUrl")
            return result

        # No strong auth signal
        result.needs_auth = False
        result.auth_type = AuthType.NONE
        result.confidence = 0.7
        return result


# ─────────────────────────────────────────────
# Strategy Router
# ─────────────────────────────────────────────

class AuthStrategyRouter:
    """Picks the lowest-friction auth strategy."""

    def select_strategy(self, detection: AuthDetectionResult, auth_config: dict) -> AuthType:
        # CAPTCHA: cannot automate regardless
        if detection.has_captcha:
            return AuthType.CAPTCHA_BLOCKED

        # 2FA immediately visible: cannot automate
        if detection.has_2fa and not auth_config.get("totp_secret"):
            return AuthType.TOTP

        # Priority 1: prior session
        if auth_config.get("storage_state_path") or auth_config.get("cookies"):
            return AuthType.COOKIE_REPLAY

        # Priority 2: credentials → form login (works for OAuth pages with form too)
        if auth_config.get("username") and auth_config.get("password"):
            if detection.auth_type == AuthType.OAUTH and not detection.evidence.get("also_has_oauth"):
                return AuthType.OAUTH  # pure OAuth, no form
            return AuthType.FORM_LOGIN

        # Priority 3: token
        if auth_config.get("token"):
            return AuthType.TOKEN_INJECTION

        # SSO: flag it
        if detection.has_sso:
            return AuthType.SSO

        return detection.auth_type


# ─────────────────────────────────────────────
# Auth Executor
# ─────────────────────────────────────────────

class AuthExecutor:
    """Executes auth strategies in an isolated Playwright context with evidence capture."""

    def __init__(self, storage_dir: str = "output/auth"):
        self.storage_dir = storage_dir
        os.makedirs(storage_dir, exist_ok=True)

    def _versioned_dir(self, target_url: str) -> str:
        """Create a versioned directory: output/auth/<domain>/<timestamp>/"""
        domain = re.sub(r"[^\w]", "_", urlparse(target_url).netloc or "unknown")
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.storage_dir, domain, run_id)
        os.makedirs(path, exist_ok=True)
        return path

    async def execute(self, strategy: AuthType, auth_config: dict, target_url: str) -> AuthResult:
        if strategy == AuthType.COOKIE_REPLAY:
            return await self._replay_session(auth_config)
        elif strategy == AuthType.FORM_LOGIN:
            return await self._form_login(auth_config, target_url)
        elif strategy == AuthType.TOKEN_INJECTION:
            return await self._inject_token(auth_config, target_url)
        elif strategy == AuthType.CAPTCHA_BLOCKED:
            return AuthResult(
                success=False,
                strategy_used=strategy,
                failure_reason="CAPTCHA detected — provide a manual session via --auth-type cookie",
                failure_code=AuthFailureReason.CAPTCHA_BLOCKED,
            )
        elif strategy == AuthType.OAUTH:
            return await self._oauth_graceful_stop(auth_config, target_url)
        elif strategy == AuthType.SSO:
            return AuthResult(
                success=False,
                strategy_used=strategy,
                failure_reason="SSO/SAML redirect detected — provide a manual session cookie",
                failure_code=AuthFailureReason.SSO_REDIRECT,
            )
        elif strategy == AuthType.TOTP:
            return AuthResult(
                success=False,
                strategy_used=strategy,
                failure_reason="2FA required — provide a manual session cookie or totp_secret",
                failure_code=AuthFailureReason.TWO_FA_REQUIRED,
            )
        else:
            return AuthResult(
                success=False,
                failure_reason=f"No handler for strategy: {strategy}",
                failure_code=AuthFailureReason.UNKNOWN,
            )

    # ------------------------------------------------------------------
    # Strategy A: Cookie / StorageState Replay
    # ------------------------------------------------------------------

    async def _replay_session(self, auth_config: dict) -> AuthResult:
        path = auth_config.get("storage_state_path")
        if path and os.path.exists(path):
            logger.info("auth_executor.session_replay", path=path)
            # Validate the JSON is well-formed
            try:
                with open(path) as f:
                    state = json.load(f)
                has_cookies = bool(state.get("cookies"))
                has_origins = bool(state.get("origins"))
                logger.info("auth_executor.session_replay_valid",
                            cookies=len(state.get("cookies", [])),
                            origins=len(state.get("origins", [])))
            except Exception as e:
                return AuthResult(
                    success=False,
                    strategy_used=AuthType.COOKIE_REPLAY,
                    failure_reason=f"StorageState file is corrupt: {e}",
                    failure_code=AuthFailureReason.UNKNOWN,
                )
            return AuthResult(
                success=True,
                strategy_used=AuthType.COOKIE_REPLAY,
                storage_state_path=path,
                session_valid=True,
                evidence={"has_cookies": has_cookies, "has_origins": has_origins},
            )

        cookies = auth_config.get("cookies")
        if cookies:
            run_dir = self._versioned_dir(auth_config.get("target_url", "unknown"))
            state_path = os.path.join(run_dir, "session.json")
            with open(state_path, "w") as f:
                json.dump({"cookies": cookies, "origins": []}, f, indent=2)
            logger.info("auth_executor.cookies_saved", path=state_path)
            return AuthResult(
                success=True,
                strategy_used=AuthType.COOKIE_REPLAY,
                storage_state_path=state_path,
                session_valid=True,
            )

        return AuthResult(
            success=False,
            failure_reason="No storage_state_path or cookies provided",
            failure_code=AuthFailureReason.NO_CONFIG,
        )

    # ------------------------------------------------------------------
    # Strategy B: Form Login (multi-step + proof check)
    # ------------------------------------------------------------------

    async def _form_login(self, auth_config: dict, target_url: str) -> AuthResult:
        login_url = auth_config.get("login_url") or target_url
        username  = auth_config.get("username")
        password  = auth_config.get("password")
        proof_url = auth_config.get("proof_url")  # optional protected route to verify

        if not username or not password:
            return AuthResult(
                success=False,
                failure_reason="Form login requires username + password",
                failure_code=AuthFailureReason.NO_CONFIG,
            )

        run_dir = self._versioned_dir(target_url)
        state_path = os.path.join(run_dir, "session.json")
        trace_path = os.path.join(run_dir, "trace.zip")
        redirect_chain: list[dict] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1920, "height": 1080},
                ignore_https_errors=True,
            )

            # Start trace for debugging
            await context.tracing.start(screenshots=True, snapshots=True)

            page = await context.new_page()

            # Track redirect chain
            page.on("response", lambda r: redirect_chain.append({
                "url": r.url, "status": r.status
            }) if r.status in (200, 301, 302, 303, 307, 308, 401, 403) else None)

            try:
                await page.goto(login_url, wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(800)

                # --- Step 1: Fill username ---
                filled_username = False
                username_selectors = [
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[autocomplete="email"]',
                    'input[name="username"]',
                    'input[autocomplete="username"]',
                    'input[id="email"]',
                    'input[id="username"]',
                    'input[id="user"]',
                    'input[type="text"]:visible',
                ]
                for sel in username_selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.fill(username)
                            logger.info("auth_executor.username_filled", selector=sel)
                            filled_username = True
                            break
                    except Exception:
                        continue

                # --- Step 2: Multi-step check — is password on same page? ---
                pw_el = await page.query_selector('input[type="password"]')
                password_visible = pw_el and await pw_el.is_visible() if pw_el else False

                if filled_username and not password_visible:
                    # Try clicking Next / Continue to advance to password step
                    next_selectors = [
                        'button:has-text("Next")', 'button:has-text("Continue")',
                        'button:has-text("Proceed")', 'button[type="submit"]',
                        'input[type="submit"]',
                    ]
                    for sel in next_selectors:
                        try:
                            el = await page.query_selector(sel)
                            if el and await el.is_visible():
                                await el.click()
                                logger.info("auth_executor.multi_step_next_clicked", selector=sel)
                                break
                        except Exception:
                            continue

                    # Wait for password field to appear (up to 5s)
                    try:
                        await page.wait_for_selector(
                            'input[type="password"]',
                            state="visible",
                            timeout=5000,
                        )
                        pw_el = await page.query_selector('input[type="password"]')
                        password_visible = True
                        logger.info("auth_executor.multi_step_password_appeared")
                    except Exception:
                        logger.warning("auth_executor.multi_step_password_not_found")

                # --- Step 3: Fill password ---
                if password_visible and pw_el:
                    await pw_el.fill(password)
                    logger.info("auth_executor.password_filled")
                else:
                    await context.tracing.stop(path=trace_path)
                    await browser.close()
                    return AuthResult(
                        success=False,
                        strategy_used=AuthType.FORM_LOGIN,
                        failure_reason="Could not locate password field",
                        failure_code=AuthFailureReason.NO_LOGIN_FORM,
                        evidence={"trace_path": trace_path, "redirect_chain": redirect_chain},
                    )

                # --- Step 4: Submit ---
                submit_selectors = [
                    'button[type="submit"]',
                    'input[type="submit"]',
                    'button:has-text("Log in")',
                    'button:has-text("Sign in")',
                    'button:has-text("Login")',
                    'button:has-text("Submit")',
                    'button:has-text("Continue")',
                ]
                for sel in submit_selectors:
                    try:
                        el = await page.query_selector(sel)
                        if el and await el.is_visible():
                            await el.click()
                            logger.info("auth_executor.submitted", selector=sel)
                            break
                    except Exception:
                        continue

                # Wait for post-submit navigation
                try:
                    await page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                await page.wait_for_timeout(1500)

                # --- Step 5: Check for 2FA after submit ---
                for totp_sel in _TOTP_SELECTORS:
                    try:
                        el = await page.query_selector(totp_sel)
                        if el and await el.is_visible():
                            await context.tracing.stop(path=trace_path)
                            await browser.close()
                            logger.warning("auth_executor.2fa_required")
                            return AuthResult(
                                success=False,
                                strategy_used=AuthType.FORM_LOGIN,
                                failure_reason="2FA required after login — provide a manual session cookie",
                                failure_code=AuthFailureReason.TWO_FA_REQUIRED,
                                evidence={"trace_path": trace_path, "redirect_chain": redirect_chain},
                            )
                    except Exception:
                        continue

                # --- Step 6: Proof check ---
                proof = await self._proof_check(page, proof_url, redirect_chain)

                # Save session regardless of proof (partial success is better than nothing)
                await context.storage_state(path=state_path)
                logger.info("auth_executor.session_saved", path=state_path)

                # Stop trace only on failure
                if not proof["success"]:
                    await context.tracing.stop(path=trace_path)
                    logger.warning("auth_executor.login_proof_failed",
                                   reason=proof["reason"], current_url=page.url)
                    return AuthResult(
                        success=False,
                        strategy_used=AuthType.FORM_LOGIN,
                        storage_state_path=state_path,
                        failure_reason=proof["reason"],
                        failure_code=AuthFailureReason.WRONG_CREDENTIALS,
                        evidence={
                            "trace_path": trace_path,
                            "redirect_chain": redirect_chain,
                            "final_url": page.url,
                            "proof_signals": proof,
                        },
                    )
                else:
                    # Discard trace on success to save disk
                    try:
                        await context.tracing.stop(path=trace_path)
                        os.remove(trace_path)
                    except Exception:
                        pass

                logger.info("auth_executor.login_success",
                            strategy="form_login", url=page.url,
                            proof_signals=proof.get("signals_matched", []))

                return AuthResult(
                    success=True,
                    strategy_used=AuthType.FORM_LOGIN,
                    storage_state_path=state_path,
                    session_valid=True,
                    evidence={
                        "final_url": page.url,
                        "redirect_chain": redirect_chain,
                        "proof_signals": proof.get("signals_matched", []),
                    },
                )

            except Exception as e:
                try:
                    await context.tracing.stop(path=trace_path)
                except Exception:
                    pass
                await browser.close()
                failure_code = AuthFailureReason.NETWORK_TIMEOUT if "timeout" in str(e).lower() else AuthFailureReason.UNKNOWN
                return AuthResult(
                    success=False,
                    strategy_used=AuthType.FORM_LOGIN,
                    failure_reason=str(e),
                    failure_code=failure_code,
                    evidence={"trace_path": trace_path, "redirect_chain": redirect_chain},
                )
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

    async def _proof_check(
        self,
        page: Page,
        proof_url: Optional[str],
        redirect_chain: list[dict],
    ) -> dict:
        """
        Verify login succeeded using multiple signals.
        Returns dict: {success, reason, signals_matched}
        """
        signals_matched = []
        signals_failed  = []

        # Signal 1: URL is not a login page
        current_url = page.url.lower()
        is_on_login  = any(kw in current_url for kw in ["/login", "/signin", "/sign-in", "/auth", "/log-in"])
        if not is_on_login:
            signals_matched.append("url_not_login_page")
        else:
            signals_failed.append("still_on_login_url")

        # Signal 2: No recent 401/403 in redirect chain
        recent_statuses = [r["status"] for r in redirect_chain[-10:]]
        if 401 not in recent_statuses and 403 not in recent_statuses:
            signals_matched.append("no_401_403")
        else:
            signals_failed.append("got_401_or_403")

        # Signal 3: Logged-in DOM markers present
        logged_in_marker_found = False
        for sel in _LOGGED_IN_MARKERS:
            try:
                el = await page.query_selector(sel)
                if el and await el.is_visible():
                    logged_in_marker_found = True
                    signals_matched.append(f"logged_in_marker:{sel}")
                    break
            except Exception:
                continue
        if not logged_in_marker_found:
            # Not a hard failure — many apps don't have obvious markers
            signals_failed.append("no_logged_in_marker_found")

        # Signal 4: No error message visible
        try:
            error_el = await page.query_selector(
                '[class*="error"], [class*="alert-danger"], [class*="invalid"], '
                '[role="alert"]:visible, [class*="login-error"]'
            )
            if error_el:
                error_text = await error_el.text_content() or ""
                if any(kw in error_text.lower() for kw in ["invalid", "incorrect", "wrong", "failed", "error"]):
                    signals_failed.append(f"error_message_visible:{error_text[:80]}")
                else:
                    signals_matched.append("no_error_message")
            else:
                signals_matched.append("no_error_message")
        except Exception:
            pass

        # Signal 5: Probe a protected route (optional)
        if proof_url:
            try:
                probe_resp = await page.goto(proof_url, wait_until="networkidle", timeout=8000)
                if probe_resp and probe_resp.status == 200:
                    signals_matched.append(f"protected_route_accessible:{proof_url}")
                elif probe_resp:
                    signals_failed.append(f"protected_route_status:{probe_resp.status}")
            except Exception as e:
                signals_failed.append(f"protected_route_error:{str(e)[:50]}")

        # Decision: need at least 2 positive signals and URL must not be login page
        strong_success = len(signals_matched) >= 2 and "still_on_login_url" not in signals_failed
        reason = (
            "Login successful"
            if strong_success
            else f"Login failed. Issues: {', '.join(signals_failed)}"
        )

        return {
            "success": strong_success,
            "reason": reason,
            "signals_matched": signals_matched,
            "signals_failed": signals_failed,
        }

    # ------------------------------------------------------------------
    # Strategy C: Token injection
    # ------------------------------------------------------------------

    async def _inject_token(self, auth_config: dict, target_url: str) -> AuthResult:
        token = auth_config.get("token")
        token_key = auth_config.get("token_key", "auth_token")
        if not token:
            return AuthResult(
                success=False,
                failure_reason="Token injection requires a token value",
                failure_code=AuthFailureReason.NO_CONFIG,
            )

        run_dir = self._versioned_dir(target_url)
        state_path = os.path.join(run_dir, "session.json")
        origin = f"{urlparse(target_url).scheme}://{urlparse(target_url).netloc}"
        state = {
            "cookies": [],
            "origins": [{
                "origin": origin,
                "localStorage": [{"name": token_key, "value": token}]
            }]
        }
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

        logger.info("auth_executor.token_injected", key=token_key, path=state_path)
        return AuthResult(
            success=True,
            strategy_used=AuthType.TOKEN_INJECTION,
            storage_state_path=state_path,
            session_valid=True,
            evidence={"token_key": token_key, "origin": origin},
        )

    # ------------------------------------------------------------------
    # Strategy D: OAuth graceful stop
    # ------------------------------------------------------------------

    async def _oauth_graceful_stop(self, auth_config: dict, target_url: str) -> AuthResult:
        """
        OAuth flows require user interaction in a browser.
        We cannot automate them without provider SDK support.
        Instruct the user to provide a session cookie instead.
        """
        logger.warning("auth_executor.oauth_cannot_automate", url=target_url)
        return AuthResult(
            success=False,
            strategy_used=AuthType.OAUTH,
            failure_reason=(
                "OAuth provider flow detected. Cannot automate browser-based OAuth. "
                "Steps to get a session: 1) Log in manually, 2) Export cookies from browser DevTools, "
                "3) Re-run with --auth-type cookie and --storage-state-path <path>"
            ),
            failure_code=AuthFailureReason.OAUTH_REDIRECT,
        )


# ─────────────────────────────────────────────
# Session Monitor
# ─────────────────────────────────────────────

class SessionMonitor:
    """
    Watches session health during long crawls.
    Called by Layer 2 PlaywrightEngine on every network response.
    """

    def __init__(self):
        self._session_start      = time.time()
        self._auth_failure_count = 0
        self._logout_redirects   = 0
        self._refresh_count      = 0
        self._last_valid_check   = time.time()
        self._last_config: Optional[dict]  = None
        self._last_url: Optional[str]      = None

    def report_request(self, status_code: int, url: str):
        """Called by Layer 2 for every response."""
        if status_code in (401, 403):
            self._auth_failure_count += 1
            logger.warning("session_monitor.auth_failure",
                           status=status_code, url=url,
                           total=self._auth_failure_count)

    def report_redirect(self, from_url: str, to_url: str):
        """Called when a redirect is detected. Identifies logout redirects."""
        to_lower = to_url.lower()
        login_keywords = ["/login", "/signin", "/sign-in", "/auth", "/log-in", "/session/new"]
        if any(kw in to_lower for kw in login_keywords):
            self._logout_redirects += 1
            logger.warning("session_monitor.logout_redirect_detected",
                           from_url=from_url, to_url=to_url,
                           total_redirects=self._logout_redirects)

    def get_health(self) -> SessionHealth:
        age = time.time() - self._session_start
        # Need reauth if: 3+ consecutive 401/403 OR any logout redirect
        needs_reauth = self._auth_failure_count >= 3 or self._logout_redirects >= 1
        return SessionHealth(
            is_valid=not needs_reauth,
            refresh_count=self._refresh_count,
            auth_failures=self._auth_failure_count,
            logout_redirects=self._logout_redirects,
            session_age_seconds=age,
            needs_reauth=needs_reauth,
        )

    def reset_after_reauth(self):
        self._auth_failure_count = 0
        self._logout_redirects   = 0
        self._refresh_count     += 1
        self._last_valid_check   = time.time()
        logger.info("session_monitor.reset", refresh_count=self._refresh_count)


# ─────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────

class AuthHandler:
    """
    Layer 4 main entry point.
    Orchestrates: detect → route → execute → monitor.

    Usage from Layer 1:
        handler = AuthHandler()
        result = await handler.authenticate(target_url, auth_config)
        # result.storage_state_path → pass to CrawlerAgent

    Usage from Layer 2 (during crawl):
        handler.monitor.report_request(status_code, url)
        handler.monitor.report_redirect(from_url, to_url)
        if handler.monitor.get_health().needs_reauth:
            result = await handler.re_authenticate()
    """

    def __init__(self):
        self.detector  = AuthDetector()
        self.router    = AuthStrategyRouter()
        self.executor  = AuthExecutor()
        self.monitor   = SessionMonitor()
        self._last_result: Optional[AuthResult] = None
        self._last_config: Optional[dict]        = None
        self._last_url: Optional[str]            = None

    async def authenticate(self, target_url: str, auth_config: dict) -> AuthResult:
        """Full auth flow: detect → route → execute."""
        logger.info("auth_handler.starting", url=target_url)
        self._last_url    = target_url
        self._last_config = auth_config

        # Step 1: Detect (skip if user explicitly specified auth_type)
        if auth_config and auth_config.get("auth_type") in (
            "cookie", "storage_state", "token", "cookie_replay"
        ):
            # User knows exactly what they want — skip detection entirely
            detection = AuthDetectionResult(needs_auth=True)
            strategy_map = {
                "cookie":        AuthType.COOKIE_REPLAY,
                "storage_state": AuthType.COOKIE_REPLAY,
                "cookie_replay": AuthType.COOKIE_REPLAY,
                "token":         AuthType.TOKEN_INJECTION,
            }
            strategy = strategy_map.get(auth_config["auth_type"], AuthType.UNKNOWN)
        elif auth_config and auth_config.get("auth_type") == "form":
            detection = AuthDetectionResult(needs_auth=True, auth_type=AuthType.FORM_LOGIN)
            strategy = AuthType.FORM_LOGIN
        else:
            # Auto-detect from live page
            detection = await self.detector.detect(target_url)
            if not detection.needs_auth and not auth_config:
                logger.info("auth_handler.no_auth_needed")
                return AuthResult(success=True, strategy_used=AuthType.NONE)
            strategy = self.router.select_strategy(detection, auth_config or {})

        logger.info("auth_handler.strategy_selected",
                    strategy=strategy.value,
                    detected_type=detection.auth_type.value,
                    confidence=detection.confidence)

        # Step 2: Execute
        result = await self.executor.execute(strategy, auth_config or {}, target_url)
        self._last_result = result

        logger.info(
            "auth_handler.complete",
            success=result.success,
            strategy=result.strategy_used.value,
            storage_state=result.storage_state_path,
            failure_reason=result.failure_reason,
        )
        return result

    async def re_authenticate(self) -> AuthResult:
        """
        Re-authenticate using the last stored config.
        Called by Layer 2 when monitor detects session death.
        """
        if self._last_config is None or self._last_url is None:
            return AuthResult(
                success=False,
                failure_reason="No prior auth config stored for re-authentication",
                failure_code=AuthFailureReason.UNKNOWN,
            )

        logger.info("auth_handler.re_authenticating", url=self._last_url)
        result = await self.authenticate(self._last_url, self._last_config)
        if result.success:
            self.monitor.reset_after_reauth()
        return result
