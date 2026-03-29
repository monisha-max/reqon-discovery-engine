"""
Endpoint Discoverer — finds all HTTP endpoints to performance-test.

Two discovery modes:
  1. OpenAPI / Swagger spec  → parse paths, methods, schemas directly
  2. Crawled pages (Layer 2) → extract form actions, URL patterns, probe for JSON APIs
     + GPT-4o-mini to infer endpoints from page content

Outputs a deduplicated list of DiscoveredEndpoint objects ranked by priority.
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from layer3_performance.models.perf_models import DiscoveredEndpoint, EndpointSource, PathParameter

logger = structlog.get_logger()

# REST resource patterns — used to identify parameterized paths
_ID_PATTERN = re.compile(r"/(\d+|[0-9a-f\-]{36})(/|$)")
_REST_RESOURCE = re.compile(r"^/(?:api/)?(?:v\d+/)?([a-z_\-]+)(?:/(\d+|[0-9a-f\-]{36}))?", re.I)

# Paths that are very likely API endpoints
_API_PATH_HINTS = re.compile(r"/api/|/v\d+/|/graphql|/rest/|/service/", re.I)

# Static asset extensions to skip
_SKIP_EXTENSIONS = {
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".eot", ".pdf", ".zip", ".map",
}

# High-priority path keywords
_PRIORITY_MAP = [
    (re.compile(r"/login|/auth|/signin", re.I), 0.95),
    (re.compile(r"/checkout|/payment|/order", re.I), 0.90),
    (re.compile(r"/api/|/v\d+/", re.I), 0.85),
    (re.compile(r"/graphql", re.I), 0.85),
    (re.compile(r"/search|/filter", re.I), 0.75),
    (re.compile(r"/user|/profile|/account", re.I), 0.70),
    (re.compile(r"/admin|/dashboard", re.I), 0.70),
]


class EndpointDiscoverer:
    """Discovers HTTP endpoints from OpenAPI specs or crawled page data."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self._seen: set[str] = set()  # dedup key: "METHOD:path_template"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def discover_from_spec(self, spec_path: str) -> list[DiscoveredEndpoint]:
        """Parse a Swagger 2.0 or OpenAPI 3.x spec file/URL."""
        try:
            spec = await self._load_spec(spec_path)
        except Exception as e:
            logger.warning("endpoint_discoverer.spec_load_failed", error=str(e))
            return []

        endpoints = self._parse_openapi(spec)
        logger.info("endpoint_discoverer.spec_parsed", count=len(endpoints), spec=spec_path)
        return endpoints

    async def discover_from_pages(self, pages: list[dict]) -> list[DiscoveredEndpoint]:
        """
        Extract endpoints from already-crawled Layer 2 PageData dicts.
        No re-crawling needed — reuses what the crawler already found.
        """
        endpoints: list[DiscoveredEndpoint] = []

        # 1. Form action extraction
        endpoints.extend(self._extract_form_endpoints(pages))

        # 2. URL pattern analysis (REST-style paths)
        endpoints.extend(self._extract_url_pattern_endpoints(pages))

        # 3. Probe discovered URLs for JSON APIs
        api_endpoints = await self._probe_json_endpoints(pages)
        endpoints.extend(api_endpoints)

        # 4. AI inference from page content
        ai_endpoints = await self._ai_infer_endpoints(pages)
        endpoints.extend(ai_endpoints)

        # Deduplicate and sort by priority
        unique = self._deduplicate(endpoints)
        unique.sort(key=lambda e: e.priority, reverse=True)

        logger.info("endpoint_discoverer.pages_parsed", count=len(unique))
        return unique

    # ------------------------------------------------------------------
    # OpenAPI / Swagger Parser
    # ------------------------------------------------------------------

    async def _load_spec(self, spec_path: str) -> dict:
        """Load spec from file path or URL."""
        if spec_path.startswith("http://") or spec_path.startswith("https://"):
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(spec_path)
                resp.raise_for_status()
                content = resp.text
        else:
            with open(spec_path, "r", encoding="utf-8") as f:
                content = f.read()

        # Try JSON first, then YAML
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            try:
                import yaml
                return yaml.safe_load(content)
            except ImportError:
                raise RuntimeError("pyyaml not installed — cannot parse YAML spec")

    def _parse_openapi(self, spec: dict) -> list[DiscoveredEndpoint]:
        """Handle both Swagger 2.0 and OpenAPI 3.x."""
        endpoints: list[DiscoveredEndpoint] = []

        # Determine base URL from spec
        if "servers" in spec:  # OpenAPI 3.x
            server_url = spec["servers"][0].get("url", self.base_url)
            if not server_url.startswith("http"):
                server_url = self.base_url + server_url
        elif "host" in spec:   # Swagger 2.0
            scheme = spec.get("schemes", ["https"])[0]
            base_path = spec.get("basePath", "")
            server_url = f"{scheme}://{spec['host']}{base_path}"
        else:
            server_url = self.base_url

        paths = spec.get("paths", {})
        for path_template, path_item in paths.items():
            for method in ["get", "post", "put", "delete", "patch", "head", "options"]:
                operation = path_item.get(method)
                if not operation:
                    continue

                parameters = self._parse_parameters(operation.get("parameters", []) + path_item.get("parameters", []))
                request_schema = self._parse_request_body(operation, spec)

                # Fill in sample path params to create a concrete URL
                sample_url = self._fill_path_params(server_url + path_template, parameters)

                priority = self._score_priority(path_template)
                key = f"{method.upper()}:{path_template}"
                if key in self._seen:
                    continue
                self._seen.add(key)

                endpoints.append(DiscoveredEndpoint(
                    url=sample_url,
                    method=method.upper(),
                    path_template=path_template,
                    source=EndpointSource.OPENAPI,
                    parameters=parameters,
                    request_schema=request_schema,
                    auth_required=bool(operation.get("security") or spec.get("security")),
                    priority=priority,
                    description=operation.get("summary") or operation.get("description", ""),
                ))

        return endpoints

    def _parse_parameters(self, raw_params: list) -> list[PathParameter]:
        params = []
        for p in raw_params:
            if not isinstance(p, dict):
                continue
            schema = p.get("schema", {})
            params.append(PathParameter(
                name=p.get("name", ""),
                location=p.get("in", "query"),
                required=p.get("required", False),
                schema_type=schema.get("type", p.get("type", "string")),
                example=str(schema.get("example", p.get("example", "1"))),
            ))
        return params

    def _parse_request_body(self, operation: dict, spec: dict) -> Optional[dict]:
        """Extract JSON schema from requestBody (OpenAPI 3) or body parameters (Swagger 2)."""
        # OpenAPI 3.x
        rb = operation.get("requestBody", {})
        if rb:
            content = rb.get("content", {})
            for mime in ["application/json", "application/x-www-form-urlencoded"]:
                if mime in content:
                    return content[mime].get("schema")

        # Swagger 2.0 body parameter
        for param in operation.get("parameters", []):
            if isinstance(param, dict) and param.get("in") == "body":
                return param.get("schema")

        return None

    def _fill_path_params(self, url: str, params: list[PathParameter]) -> str:
        """Replace {param} placeholders with sample values."""
        for p in params:
            if p.location == "path":
                example = p.example or ("1" if p.schema_type in ("integer", "number") else "sample")
                url = url.replace("{" + p.name + "}", str(example))
        return url

    # ------------------------------------------------------------------
    # Page-based Discovery
    # ------------------------------------------------------------------

    def _extract_form_endpoints(self, pages: list[dict]) -> list[DiscoveredEndpoint]:
        """Extract form action URLs and methods from HTML snippets."""
        endpoints = []
        form_action_pattern = re.compile(
            r'<form[^>]+action=["\']([^"\']+)["\'][^>]*(?:method=["\'](\w+)["\'])?',
            re.I,
        )
        for page in pages:
            html = page.get("html_snippet", "") or ""
            for match in form_action_pattern.finditer(html):
                action, method = match.group(1), (match.group(2) or "POST").upper()
                if not action or action.startswith("#") or action.startswith("javascript:"):
                    continue
                full_url = urljoin(self.base_url, action)
                path = urlparse(full_url).path
                if self._should_skip(path):
                    continue
                key = f"{method}:{path}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                endpoints.append(DiscoveredEndpoint(
                    url=full_url,
                    method=method,
                    path_template=self._templatize(path),
                    source=EndpointSource.HTML_FORM,
                    priority=self._score_priority(path),
                ))
        return endpoints

    def _extract_url_pattern_endpoints(self, pages: list[dict]) -> list[DiscoveredEndpoint]:
        """Infer REST endpoints from crawled URL patterns."""
        endpoints = []
        for page in pages:
            url = page.get("url", "")
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc not in self.base_url:
                continue
            path = parsed.path
            if self._should_skip(path):
                continue

            # Only include paths that look like API/resource paths
            if _API_PATH_HINTS.search(path) or self._is_rest_resource(path):
                template = self._templatize(path)
                key = f"GET:{template}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                endpoints.append(DiscoveredEndpoint(
                    url=url,
                    method="GET",
                    path_template=template,
                    source=EndpointSource.CRAWL,
                    priority=self._score_priority(path),
                ))

        return endpoints

    async def _probe_json_endpoints(self, pages: list[dict]) -> list[DiscoveredEndpoint]:
        """
        Quick async probe of discovered URLs to confirm JSON API endpoints.
        Uses httpx with a short timeout — doesn't follow full crawl.
        """
        endpoints = []
        candidate_paths: set[str] = set()

        for page in pages:
            url = page.get("url", "")
            path = urlparse(url).path
            if _API_PATH_HINTS.search(path) and not self._should_skip(path):
                candidate_paths.add(path)

        if not candidate_paths:
            return endpoints

        async with httpx.AsyncClient(base_url=self.base_url, timeout=5.0, follow_redirects=True) as client:
            for path in list(candidate_paths)[:20]:  # cap at 20 probes
                try:
                    resp = await client.get(path, headers={"Accept": "application/json"})
                    content_type = resp.headers.get("content-type", "")
                    if "json" in content_type:
                        template = self._templatize(path)
                        key = f"GET:{template}"
                        if key not in self._seen:
                            self._seen.add(key)
                            endpoints.append(DiscoveredEndpoint(
                                url=self.base_url + path,
                                method="GET",
                                path_template=template,
                                source=EndpointSource.JS_FETCH,
                                priority=self._score_priority(path),
                            ))
                except Exception:
                    pass  # Probe failure is expected for some paths

        return endpoints

    async def _ai_infer_endpoints(self, pages: list[dict]) -> list[DiscoveredEndpoint]:
        """Infer additional endpoints from page content using OpenAI / Anthropic."""
        from layer3_performance.llm_client import call_llm
        from config.settings import settings
        if not (settings.OPENAI_API_KEY or settings.ANTHROPIC_API_KEY) or not pages:
            return []

        page_summaries = []
        for p in pages[:10]:
            page_summaries.append({
                "url": p.get("url", ""),
                "title": p.get("title", ""),
                "form_count": p.get("form_count", 0),
                "markdown_snippet": (p.get("markdown_content") or "")[:500],
            })

        prompt = f"""You are an API discovery agent analyzing a web application.

Base URL: {self.base_url}
Crawled pages summary:
{json.dumps(page_summaries, indent=2)}

Based on the page structure, URLs, and content, identify HTTP API endpoints that likely exist but may not be directly visible in the HTML.
Focus on REST APIs, form submission endpoints, and data-loading endpoints.

Respond ONLY with a JSON array (no markdown):
[
  {{"path": "/api/users", "method": "GET", "description": "List users"}},
  {{"path": "/api/users/{{id}}", "method": "GET", "description": "Get user by ID"}},
  {{"path": "/api/login", "method": "POST", "description": "User authentication"}}
]

Return at most 10 endpoints. Only include endpoints you are confident exist."""

        content = await call_llm(prompt, max_tokens=600, temperature=0.2)
        if content is None:
            return []

        try:
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            inferred = json.loads(content)
            endpoints = []
            for item in inferred:
                path = item.get("path", "")
                method = item.get("method", "GET").upper()
                if not path:
                    continue
                key = f"{method}:{path}"
                if key in self._seen:
                    continue
                self._seen.add(key)
                endpoints.append(DiscoveredEndpoint(
                    url=self.base_url + path,
                    method=method,
                    path_template=path,
                    source=EndpointSource.AI_INFER,
                    priority=self._score_priority(path),
                    description=item.get("description", ""),
                ))
            logger.info("endpoint_discoverer.ai_inferred", count=len(endpoints))
            return endpoints
        except Exception as e:
            logger.warning("endpoint_discoverer.ai_failed", error=str(e))
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _templatize(self, path: str) -> str:
        """Replace numeric IDs and UUIDs with {id} placeholders."""
        path = re.sub(r"/\d+(/|$)", r"/{id}\1", path)
        path = re.sub(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(/|$)", r"/{id}\1", path, flags=re.I)
        return path

    def _is_rest_resource(self, path: str) -> bool:
        """True if path looks like /resource or /resource/{id}."""
        return bool(_REST_RESOURCE.match(path))

    def _should_skip(self, path: str) -> bool:
        path_lower = path.lower()
        return any(path_lower.endswith(ext) for ext in _SKIP_EXTENSIONS)

    def _score_priority(self, path: str) -> float:
        for pattern, score in _PRIORITY_MAP:
            if pattern.search(path):
                return score
        return 0.5

    def _deduplicate(self, endpoints: list[DiscoveredEndpoint]) -> list[DiscoveredEndpoint]:
        """Keep highest-priority endpoint for each METHOD:path_template."""
        best: dict[str, DiscoveredEndpoint] = {}
        for ep in endpoints:
            key = f"{ep.method}:{ep.path_template}"
            if key not in best or ep.priority > best[key].priority:
                best[key] = ep
        return list(best.values())
