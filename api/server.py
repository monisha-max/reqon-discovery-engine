"""
FastAPI server for ReQon Web UI.

Routes:
  GET  /                          → ui/index.html (SPA shell)
  POST /api/scan                  → start a new scan
  GET  /api/scan/{id}/status      → poll scan status
  GET  /api/scan/{id}/result      → fetch final result
  GET  /api/scan/{id}/stream      → SSE live log stream
  GET  /output/**                 → StaticFiles — open HTML reports in browser

Credential security: scan_manager.py clears _auth_config before first await.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Optional

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from api.scan_manager import (
    ScanAwareProcessor,
    create_scan,
    get_scan_result,
    get_scan_status,
    run_scan,
)

# ---------------------------------------------------------------------------
# Configure structlog — inject ScanAwareProcessor once at startup
# ---------------------------------------------------------------------------

_scan_processor = ScanAwareProcessor()


def _configure_structlog() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _scan_processor,                         # routes logs to SSE queue
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),   # INFO
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="ReQon Discovery Engine", version="1.0.0")

_UI_PATH = Path(__file__).parent.parent / "ui" / "index.html"
_OUTPUT_DIR = Path(__file__).parent.parent / "output"


@app.on_event("startup")
async def _startup() -> None:
    _configure_structlog()
    # Ensure output dir exists so StaticFiles mount doesn't fail
    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# Mount output directory for serving HTML reports directly in browser
app.mount("/output", StaticFiles(directory=str(_OUTPUT_DIR)), name="output")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AuthConfig(BaseModel):
    username: str = ""
    password: str = ""
    auth_type: str = "form"          # form | header | basic | cookie_replay
    login_url: str = ""
    cookie_string: str = ""          # for cookie_replay: "name=val; name2=val2"


def _parse_cookie_string(cookie_str: str, target_url: str) -> list[dict]:
    """
    Parse a browser cookie string into Playwright's cookie list format.

    Accepts both forms:
      "access_token=eyJ...; refresh_token=eyJ..."   ← name=value (preferred)
      "eyJ..."                                        ← raw JWT value only (fallback)

    JWT values are base64url-encoded and contain no '=' signs, so the fallback
    handles the common case where a user pastes just the token value.
    """
    from urllib.parse import urlparse
    domain = urlparse(target_url).hostname or ""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, _, value = part.partition("=")
            name = name.strip()
            value = value.strip()
            if name and value:
                cookies.append({"name": name, "value": value, "domain": domain, "path": "/"})
        else:
            # Raw token value with no name — treat as access_token
            cookies.append({"name": "access_token", "value": part, "domain": domain, "path": "/"})
    return cookies


class ScanRequest(BaseModel):
    target_url: str
    auth: Optional[AuthConfig] = None
    max_pages: int = Field(default=50, ge=1, le=500)
    max_depth: int = Field(default=5, ge=1, le=20)
    enable_perf: bool = True
    enable_defect: bool = True
    test_types: list[str] = Field(default_factory=lambda: ["load"])


class ScanStarted(BaseModel):
    scan_id: str
    message: str = "Scan started"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def serve_ui() -> HTMLResponse:
    if not _UI_PATH.exists():
        raise HTTPException(status_code=404, detail="UI not found — check ui/index.html")
    return HTMLResponse(content=_UI_PATH.read_text(encoding="utf-8"))


@app.post("/api/scan", response_model=ScanStarted)
async def start_scan(body: ScanRequest) -> ScanStarted:
    # Build auth_config dict (same shape main.py uses) — may be None
    auth_config: Optional[dict] = None
    if body.auth:
        structlog.get_logger().info(
            "server.auth_received",
            auth_type=body.auth.auth_type,
            has_cookie_string=bool(body.auth.cookie_string),
            cookie_string_len=len(body.auth.cookie_string) if body.auth.cookie_string else 0,
        )
        if body.auth.auth_type == "cookie_replay" and body.auth.cookie_string:
            cookies = _parse_cookie_string(body.auth.cookie_string, body.target_url)
            structlog.get_logger().info(
                "server.cookies_parsed",
                cookie_count=len(cookies),
                cookie_names=[c["name"] for c in cookies],
            )
            if cookies:
                auth_config = {
                    "auth_type": "cookie_replay",
                    "cookies": cookies,          # Playwright cookie list format
                    "target_url": body.target_url,
                }
        elif body.auth.username:
            auth_config = {
                "username": body.auth.username,
                "password": body.auth.password,   # cleared in scan_manager before first await
                "auth_type": body.auth.auth_type,
                "login_url": body.auth.login_url or body.target_url,
            }

    perf_config: Optional[dict[str, Any]] = None
    if body.enable_perf:
        perf_config = {
            "test_types": body.test_types,
            "load_users": 20,
            "load_duration_seconds": 120,
        }

    defect_config: Optional[dict[str, Any]] = None
    if body.enable_defect:
        defect_config = {}

    scan_id = create_scan(
        target_url=body.target_url,
        auth_config=auth_config,
        max_pages=body.max_pages,
        max_depth=body.max_depth,
        perf_config=perf_config,
        defect_config=defect_config,
    )

    # Launch scan in background — ContextVar scoped to this new task
    asyncio.create_task(run_scan(scan_id))

    return ScanStarted(scan_id=scan_id)


@app.get("/api/scan/{scan_id}/status")
async def scan_status(scan_id: str) -> dict:
    status = get_scan_status(scan_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    return status


@app.get("/api/scan/{scan_id}/result")
async def scan_result(scan_id: str) -> dict:
    status = get_scan_status(scan_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    if status["status"] not in ("done", "error"):
        raise HTTPException(status_code=202, detail="Scan still running")
    result = get_scan_result(scan_id)
    return result or {}


@app.get("/api/scan/{scan_id}/stream")
async def scan_stream(scan_id: str) -> StreamingResponse:
    """Server-Sent Events stream for live log output."""
    status = get_scan_status(scan_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Scan not found")

    from api.scan_manager import _scans  # import here to avoid circular at module load

    async def event_generator():
        record = _scans.get(scan_id)
        if record is None:
            yield "data: Scan not found\n\n"
            return

        while True:
            try:
                msg = await asyncio.wait_for(record.log_queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                # Keepalive — also check if scan finished (fallback if __DONE__ was dropped)
                if record.done:
                    yield "data: __DONE__\n\n"
                    break
                yield ": keepalive\n\n"
                continue

            if msg == "__DONE__":
                yield "data: __DONE__\n\n"
                break

            # Escape newlines — SSE messages must be single-line data fields
            safe = msg.replace("\n", " ").replace("\r", "")
            yield f"data: {safe}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
