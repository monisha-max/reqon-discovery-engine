from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse


WHITESPACE_PATTERN = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    normalized = value.strip().lower()
    normalized = WHITESPACE_PATTERN.sub(" ", normalized)
    return normalized


def build_application_key(target_url: str) -> str:
    parsed = urlparse(target_url)
    origin = f"{parsed.scheme}://{parsed.netloc}".strip().lower()
    return normalize_text(origin or target_url)


def build_issue_key(
    tenant_id: str,
    page_url: str,
    selector: str,
    category: str,
    message: str,
    source_type: str = "crawl",
) -> str:
    raw_identity = "::".join(
        [
            normalize_text(tenant_id),
            normalize_text(page_url),
            normalize_text(selector),
            normalize_text(category),
            normalize_text(message),
            normalize_text(source_type),
        ]
    )
    return hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()
