"""
Locust Script Generator — AI writes realistic HttpUser test scripts.

GPT-4o-mini generates a complete Locust script with:
  - Realistic task weights (not uniform random)
  - User journey patterns (browse → search → action)
  - Proper auth header injection
  - Multiple payloads per POST endpoint

Falls back to a template-based generator if LLM is unavailable.
"""
from __future__ import annotations

import json
import os
import re
import textwrap

import structlog

from layer3_performance.models.perf_models import DiscoveredEndpoint

logger = structlog.get_logger()


def _extract_session_cookies(storage_state_path: str | None) -> dict[str, str] | None:
    """
    Read a Playwright storage_state JSON file and return a flat {name: value}
    cookie dict suitable for Locust's self.client.cookies.update().

    Returns None if the path is absent or the file is unreadable.
    """
    if not storage_state_path:
        return None
    try:
        with open(storage_state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
        cookies = {}
        for cookie in state.get("cookies", []):
            name = cookie.get("name", "")
            value = cookie.get("value", "")
            if name and value:
                cookies[name] = value
        return cookies if cookies else None
    except Exception as exc:
        logger.warning("script_generator.cookie_extract_failed", error=str(exc))
        return None


class ScriptGenerator:
    """Generates Locust HttpUser Python scripts for load testing."""

    def __init__(self, output_dir: str = "output"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate(
        self,
        endpoints: list[DiscoveredEndpoint],
        base_url: str,
        auth_headers: dict[str, str] | None = None,
        storage_state_path: str | None = None,
        script_name: str = "locust_script.py",
    ) -> str:
        """
        Generate a Locust test script.
        Returns the path to the saved script file.
        """
        # Extract cookies from storage state (Playwright session.json)
        session_cookies = _extract_session_cookies(storage_state_path)

        # Try LLM generation first
        script = await self._llm_generate(endpoints, base_url, auth_headers, session_cookies)

        if not script:
            # Fallback: template-based generation
            script = self._template_generate(endpoints, base_url, auth_headers, session_cookies)

        script_path = os.path.join(self.output_dir, script_name)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script)

        logger.info("script_generator.saved", path=script_path)
        return script_path

    # ------------------------------------------------------------------
    # LLM Generation
    # ------------------------------------------------------------------

    async def _llm_generate(
        self,
        endpoints: list[DiscoveredEndpoint],
        base_url: str,
        auth_headers: dict[str, str] | None,
        session_cookies: dict[str, str] | None = None,
    ) -> str | None:
        """Generate a Locust script using OpenAI (with Anthropic fallback)."""
        from layer3_performance.llm_client import call_llm

        ep_list = []
        for ep in endpoints[:20]:  # cap at 20 to stay within token budget
            entry = {
                "method": ep.method,
                "path": ep.path_template,
                "priority": ep.priority,
            }
            if ep.sample_payload:
                entry["sample_payload"] = ep.sample_payload
            if ep.description:
                entry["description"] = ep.description
            ep_list.append(entry)

        auth_note = ""
        bearer_token = None
        if auth_headers:
            auth_note = f"Auth headers to include on every request: {json.dumps(auth_headers)}"
            bearer_token = auth_headers.get("Authorization", "").removeprefix("Bearer ").strip() or None
        if session_cookies:
            # Also derive a Bearer token from the access_token cookie if present
            if not bearer_token:
                bearer_token = session_cookies.get("access_token") or session_cookies.get("token")
            auth_note += (
                f"\nIn on_start(), set BOTH:"
                f"\n  1. self.client.cookies.update({json.dumps(session_cookies)})  # cookie auth"
            )
            if bearer_token:
                auth_note += (
                    f'\n  2. self.client.headers.update({{"Authorization": "Bearer {bearer_token}"}})  # header auth'
                    f"\n(Many APIs require both cookie AND Authorization header for POST/DELETE requests.)"
                )
            auth_note += "\nDo NOT include a login task — the user is already authenticated via session cookies."

        prompt = f"""You are a performance testing expert writing a Locust load test script.

Target application: {base_url}
{auth_note}

Endpoints to test:
{json.dumps(ep_list, indent=2)}

Write a complete, production-quality Locust script that:
1. Uses realistic task weights based on endpoint priority (higher priority = higher weight)
2. Simulates a realistic user journey (browse first, then interact, then submit)
3. Uses the sample_payload values for POST/PUT/PATCH requests
4. Injects auth cookies AND Authorization header in on_start() if provided
5. Uses between(1, 3) wait time to simulate human behavior
6. Handles responses gracefully (check status codes)
7. Uses @task decorators with integer weights

CRITICAL RULES — you MUST follow these or the stats will be wrong:
- Paths containing {{id}} or {{uuid}} or any {{param}} placeholder MUST use random.randint(1, 1000)
  or random.choice([...]) at runtime — NEVER hardcode specific IDs, NEVER loop over a range of IDs.
  Example: self.client.get(f"/api/items/{{random.randint(1, 1000)}}", name="/api/items/{{id}}")
- ALWAYS pass name=<path_template_string> to every self.client call so Locust aggregates
  all parameterized requests under one stat row instead of thousands of separate rows.
  Example: self.client.get(f"/api/recipes/{{random.randint(1,100)}}", name="/api/recipes/{{id}}")
- Import random at the top of the script.
- One @task method per endpoint template — never generate multiple tasks for the same template.
- If session cookies are provided, do NOT add a login/authenticate task — the user is pre-authenticated.

Output ONLY the complete Python script. No markdown, no explanation.
Start with: import random
from locust import HttpUser, task, between"""

        script = await call_llm(prompt, max_tokens=2500, temperature=0.3)
        if script is None:
            return None

        # Strip markdown fences if present
        if script.startswith("```"):
            script = script.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        # Validate it's a Python script
        if "HttpUser" in script and "def " in script and "from locust" in script:
            logger.info("script_generator.llm_success")
            return script

        logger.warning("script_generator.llm_invalid_output")
        return None

    # ------------------------------------------------------------------
    # Template Fallback
    # ------------------------------------------------------------------

    def _template_generate(
        self,
        endpoints: list[DiscoveredEndpoint],
        base_url: str,
        auth_headers: dict[str, str] | None,
        session_cookies: dict[str, str] | None = None,
    ) -> str:
        """Generate a Locust script from a template when LLM is unavailable."""

        task_methods = []
        for ep in endpoints[:15]:
            method = ep.method.lower()
            path = ep.path_template
            # Safe Python method name
            safe_name = re.sub(r"[^a-zA-Z0-9]", "_", path.strip("/")).strip("_") or "root"
            safe_name = f"{method}_{safe_name}"[:60]
            weight = max(1, int(ep.priority * 10))

            # Replace {param} placeholders with runtime random.randint() calls so that
            # Locust tracks all parameterized requests under a single stat row (via name=).
            has_params = "{" in path
            if has_params:
                # Build an f-string URL that resolves {param} at runtime
                runtime_path = re.sub(r"\{[^}]+\}", "{random.randint(1, 1000)}", path)
                name_arg = f', name="{path}"'
            else:
                runtime_path = path
                name_arg = ""

            if method in ("post", "put", "patch") and ep.sample_payload:
                payload_str = json.dumps(ep.sample_payload)
                if has_params:
                    url_expr = f'f"{runtime_path}"'
                else:
                    url_expr = f'"{runtime_path}"'
                body = textwrap.indent(
                    f'response = self.client.{method}({url_expr}, json={payload_str}, headers=self._headers{name_arg})\n'
                    f'if response.status_code >= 400:\n'
                    f'    response.failure(f"Got {{response.status_code}}")\n',
                    "        ",
                )
            else:
                if has_params:
                    url_expr = f'f"{runtime_path}"'
                else:
                    url_expr = f'"{runtime_path}"'
                body = textwrap.indent(
                    f'response = self.client.{method}({url_expr}, headers=self._headers{name_arg})\n'
                    f'if response.status_code >= 400:\n'
                    f'    response.failure(f"Got {{response.status_code}}")\n',
                    "        ",
                )

            task_methods.append(
                f"    @task({weight})\n"
                f"    def {safe_name}(self):\n"
                f"{body}"
            )

        auth_header_str = json.dumps(auth_headers or {})
        cookie_line = ""
        bearer_line = ""
        if session_cookies:
            cookie_line = f"        self.client.cookies.update({json.dumps(session_cookies)})"
            # Also set Authorization Bearer header — many APIs need both cookie + header for POST/DELETE
            bearer_token = (
                session_cookies.get("access_token")
                or session_cookies.get("token")
                or session_cookies.get("auth_token")
            )
            if bearer_token:
                bearer_line = f'        self.client.headers.update({{"Authorization": "Bearer {bearer_token}"}})'

        script = f'''"""
Auto-generated Locust load test script.
Target: {base_url}
Generated by ReQon Performance Testing Layer.
"""
import json
import random
from locust import HttpUser, task, between


class ReQonUser(HttpUser):
    host = "{base_url}"
    wait_time = between(1, 3)

    def on_start(self):
        """Initialize auth headers and session cookies for this virtual user."""
        self._headers = {auth_header_str}
{cookie_line}
{bearer_line}

{"".join(task_methods)}
'''
        return script
