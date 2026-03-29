from __future__ import annotations

from enum import Enum
from typing import Any, List, Optional, Union

from pydantic import BaseModel, Field


class CrawlScope(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    SINGLE_PAGE = "single_page"


class AuthConfig(BaseModel):
    """Authentication configuration provided by the user."""
    auth_type: Optional[str] = None  # "form", "cookie", "token", "cookie_replay", "none"
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    # cookies can be a list of Playwright cookie dicts [{"name":..,"value":..,"domain":..}]
    # or a plain {name: value} dict (legacy format)
    cookies: Optional[Union[List[dict], dict]] = None
    token: Optional[str] = None
    storage_state_path: Optional[str] = None
    target_url: Optional[str] = None   # used by cookie_replay to build storage state path


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
