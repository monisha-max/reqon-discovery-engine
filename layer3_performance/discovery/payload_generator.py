"""
Payload Generator — AI + Faker-powered realistic request payload generation.

For each discovered endpoint, generates:
  - Realistic JSON request bodies (respecting OpenAPI schemas)
  - Realistic query parameters
  - Sample path parameter values

Uses GPT-4o-mini to generate payloads that match the endpoint's semantic purpose.
Falls back to schema-driven Faker generation when no LLM is available.
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional

import structlog

from layer3_performance.models.perf_models import DiscoveredEndpoint

logger = structlog.get_logger()


class PayloadGenerator:
    """Generates realistic HTTP request payloads for load testing."""

    def __init__(self):
        self._faker = None  # lazy init

    def _get_faker(self):
        if self._faker is None:
            try:
                from faker import Faker
                self._faker = Faker()
            except ImportError:
                self._faker = False  # Faker not installed
        return self._faker if self._faker else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def generate_for_endpoints(
        self,
        endpoints: list[DiscoveredEndpoint],
    ) -> list[DiscoveredEndpoint]:
        """
        Populate sample_payload on each endpoint.
        Returns the same list with payloads filled in.
        """
        # Batch POST/PUT/PATCH endpoints for LLM (they need bodies)
        needs_body = [ep for ep in endpoints if ep.method in ("POST", "PUT", "PATCH")]
        gets = [ep for ep in endpoints if ep.method not in ("POST", "PUT", "PATCH")]

        if needs_body:
            await self._llm_generate_batch(needs_body)

        # Fill in any that LLM missed, and handle GETs
        for ep in endpoints:
            if ep.sample_payload is None:
                ep.sample_payload = self._schema_or_faker_payload(ep)

        logger.info("payload_generator.complete", total=len(endpoints), with_body=len(needs_body))
        return endpoints

    # ------------------------------------------------------------------
    # LLM Batch Generation
    # ------------------------------------------------------------------

    async def _llm_generate_batch(self, endpoints: list[DiscoveredEndpoint]):
        """Generate realistic payloads using OpenAI (with Anthropic fallback)."""
        from layer3_performance.llm_client import call_llm

        ep_descriptions = []
        for i, ep in enumerate(endpoints):
            desc = {
                "index": i,
                "method": ep.method,
                "path": ep.path_template,
                "description": ep.description or "",
            }
            if ep.request_schema:
                desc["schema"] = ep.request_schema
            ep_descriptions.append(desc)

        prompt = f"""You are a load testing engineer generating realistic API request payloads.

For each endpoint below, generate ONE realistic JSON request body that a real user would submit.
Use realistic-looking data (real names, valid emails, plausible values — not 'test' or 'string').

Endpoints:
{json.dumps(ep_descriptions, indent=2)}

Respond ONLY with a JSON array indexed by the endpoint index (no markdown):
[
  {{"index": 0, "payload": {{"email": "alice@example.com", "password": "SecurePass123!"}}}},
  {{"index": 1, "payload": {{"name": "Product A", "price": 29.99, "category": "electronics"}}}}
]"""

        content = await call_llm(prompt, max_tokens=1500, temperature=0.4)
        if content is None:
            return

        try:
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            results = json.loads(content)
            for item in results:
                idx = item.get("index")
                payload = item.get("payload")
                if idx is not None and payload and 0 <= idx < len(endpoints):
                    endpoints[idx].sample_payload = payload
            logger.info("payload_generator.llm_success", count=len(results))
        except Exception as e:
            logger.warning("payload_generator.llm_parse_failed", error=str(e))

    # ------------------------------------------------------------------
    # Schema / Faker Fallback
    # ------------------------------------------------------------------

    def _schema_or_faker_payload(self, ep: DiscoveredEndpoint) -> Optional[dict]:
        """Generate payload from OpenAPI schema or pure Faker heuristics."""
        if ep.method in ("GET", "DELETE", "HEAD"):
            return None  # No body for read/delete operations

        if ep.request_schema:
            return self._payload_from_schema(ep.request_schema)

        # No schema — use path-based heuristics
        return self._heuristic_payload(ep.path_template)

    def _payload_from_schema(self, schema: dict, depth: int = 0) -> Any:
        """Recursively generate a value from a JSON Schema node."""
        if depth > 4:
            return {}

        faker = self._get_faker()
        schema_type = schema.get("type", "object")
        fmt = schema.get("format", "")
        enum_vals = schema.get("enum")

        if enum_vals:
            return enum_vals[0]

        if schema_type == "object":
            result = {}
            properties = schema.get("properties", {})
            required = schema.get("required", list(properties.keys()))
            for prop, prop_schema in properties.items():
                if prop in required:
                    result[prop] = self._payload_from_schema(prop_schema, depth + 1)
            return result

        if schema_type == "array":
            items_schema = schema.get("items", {"type": "string"})
            return [self._payload_from_schema(items_schema, depth + 1)]

        if schema_type == "string":
            if faker:
                if fmt == "email" or "email" in schema.get("description", "").lower():
                    return faker.email()
                if fmt == "date-time":
                    return faker.iso8601()
                if fmt == "date":
                    return str(faker.date())
                if fmt == "uuid":
                    return str(faker.uuid4())
                if "name" in schema.get("description", "").lower():
                    return faker.name()
                if "phone" in schema.get("description", "").lower():
                    return faker.phone_number()
                if "address" in schema.get("description", "").lower():
                    return faker.address()
                return faker.word()
            return "sample_value"

        if schema_type in ("integer", "number"):
            minimum = schema.get("minimum", 1)
            maximum = schema.get("maximum", 100)
            if faker:
                return faker.random_int(min=int(minimum), max=int(maximum))
            return minimum

        if schema_type == "boolean":
            return True

        return {}

    def _heuristic_payload(self, path_template: str) -> dict:
        """Generate a payload based purely on the path template semantics."""
        faker = self._get_faker()
        path = path_template.lower()

        if faker:
            if any(kw in path for kw in ["/login", "/auth", "/signin"]):
                return {"email": faker.email(), "password": "TestPass123!"}
            if any(kw in path for kw in ["/register", "/signup", "/user"]):
                return {
                    "name": faker.name(),
                    "email": faker.email(),
                    "password": "TestPass123!",
                }
            if any(kw in path for kw in ["/product", "/item", "/listing"]):
                return {
                    "name": faker.catch_phrase(),
                    "price": round(faker.random.uniform(5.0, 500.0), 2),
                    "description": faker.sentence(),
                }
            if any(kw in path for kw in ["/order", "/checkout", "/cart"]):
                return {
                    "items": [{"product_id": faker.random_int(1, 100), "quantity": faker.random_int(1, 5)}],
                    "shipping_address": faker.address(),
                }
            if any(kw in path for kw in ["/comment", "/review", "/post"]):
                return {"content": faker.paragraph(), "rating": faker.random_int(1, 5)}
            if any(kw in path for kw in ["/search", "/query"]):
                return {"q": faker.word(), "page": 1, "limit": 20}
            # Generic fallback
            return {"name": faker.word(), "value": faker.random_int(1, 100)}

        # No Faker — minimal fallback
        return {"data": "test_value"}
