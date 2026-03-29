"""
ReQon Web Server entrypoint.

Usage:
    python run_server.py                  # stable mode (default, no auto-reload)
    python run_server.py --port 9000
    python run_server.py --dev            # enable auto-reload for development

Opens:  http://localhost:8765
"""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the ReQon web server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Enable auto-reload (dev only — restarts kill in-flight scans)",
    )
    args = parser.parse_args()

    mode = "DEV (auto-reload ON — do not run scans)" if args.dev else "STABLE"
    print(f"\n  ReQon Discovery Engine  [{mode}]")
    print(f"  ─────────────────────────────────────────")
    print(f"  Web UI  →  http://localhost:{args.port}")
    print(f"  API     →  http://localhost:{args.port}/api/scan")
    print(f"  Reports →  http://localhost:{args.port}/output/")
    print(f"  ─────────────────────────────────────────\n")

    uvicorn.run(
        "api.server:app",
        host=args.host,
        port=args.port,
        reload=args.dev,      # OFF by default — auto-reload kills in-flight scans
        log_level="info",
    )


if __name__ == "__main__":
    main()
