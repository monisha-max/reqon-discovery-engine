from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CrawlScope(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    SINGLE_PAGE = "single_page"


class AuthConfig(BaseModel):
    """Authentication configuration provided by the user."""
    auth_type: Optional[str] = None  # "form", "cookie", "token", "none"
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    cookies: Optional[dict] = None
    token: Optional[str] = None
    storage_state_path: Optional[str] = None


class CrawlRequest(BaseModel):
    """User input to start a scan."""
    target_url: str
    auth_config: Optional[AuthConfig] = None
    scope: CrawlScope = CrawlScope.FULL
    max_pages: int = 100
    max_depth: int = 5


class DiscoveredURL(BaseModel):
    """A URL found during crawling."""
    url: str
    source_url: Optional[str] = None
    depth: int = 0
    priority: float = 0.5  # 0.0 (low) to 1.0 (high)
    link_text: Optional[str] = None
