"""
Session Monitor Singleton — shared between Layer 4 and Layer 2.

Layer 4 (AuthHandler) sets the active monitor after authentication.
Layer 2 (PlaywrightEngine) calls report_request() / report_redirect()
on every network response during crawling.

This module avoids circular imports by acting as a neutral intermediary.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from layer4_auth.auth_handler import SessionMonitor

_active_monitor: Optional["SessionMonitor"] = None


def set_active_monitor(monitor: "SessionMonitor") -> None:
    """Called by AuthHandler after authentication completes."""
    global _active_monitor
    _active_monitor = monitor


def get_active_monitor() -> Optional["SessionMonitor"]:
    """Called by PlaywrightEngine on every response."""
    return _active_monitor


def report_request(status_code: int, url: str) -> None:
    """Convenience: report a response to the active monitor if one exists."""
    if _active_monitor is not None:
        _active_monitor.report_request(status_code, url)


def report_redirect(from_url: str, to_url: str) -> None:
    """Convenience: report a redirect to the active monitor if one exists."""
    if _active_monitor is not None:
        _active_monitor.report_redirect(from_url, to_url)
