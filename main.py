"""
ReQon Bug & Hygiene Discovery Engine — Main Entry Point.

Usage:
    python main.py <url> [--max-pages 50] [--max-depth 3]
    python main.py https://example.com
    python main.py https://myapp.com --auth-type form --username admin --password secret
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import structlog
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from layer1_orchestrator.orchestrator import run_orchestrator

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(colors=True),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO level
)

console = Console()


def print_results(state: dict):
    """Pretty-print the crawl results."""
    result = state.get("result", {})
    pages = state.get("pages", [])

    # Summary panel
    iterations = result.get("iterations", state.get("iteration", "N/A"))
    summary = (
        f"Target: {result.get('target_url', 'N/A')}\n"
        f"Pages Crawled: {result.get('total_pages_crawled', 0)}\n"
        f"URLs Discovered: {result.get('total_urls_discovered', 0)}\n"
        f"ReAct Iterations: {iterations}\n"
        f"Coverage Score: {result.get('coverage_score', 0):.0%}"
    )
    console.print(Panel(summary, title="Crawl Summary", border_style="green"))

    # Pages table
    if pages:
        table = Table(title="Discovered Pages")
        table.add_column("URL", style="cyan", max_width=60)
        table.add_column("Type", style="magenta")
        table.add_column("Confidence", justify="right")
        table.add_column("Status", justify="right")
        table.add_column("Links", justify="right")
        table.add_column("Screenshot", style="dim")

        for p in pages:
            table.add_row(
                p.get("url", "")[:60],
                p.get("page_type", "unknown"),
                f"{p.get('page_type_confidence', 0):.0%}",
                str(p.get("status_code", "")),
                str(p.get("link_count", 0)),
                "Yes" if p.get("screenshot_path") else "-",
            )
        console.print(table)

    # Page type distribution
    type_counts: dict[str, int] = {}
    for p in pages:
        pt = p.get("page_type", "unknown")
        type_counts[pt] = type_counts.get(pt, 0) + 1

    if type_counts:
        dist_table = Table(title="Page Type Distribution")
        dist_table.add_column("Page Type", style="magenta")
        dist_table.add_column("Count", justify="right", style="green")
        for pt, count in sorted(type_counts.items(), key=lambda x: -x[1]):
            dist_table.add_row(pt, str(count))
        console.print(dist_table)

    # Errors
    errors = state.get("errors", [])
    if errors:
        for err in errors:
            console.print(f"[red]Error: {err}[/red]")

    # Telemetry
    total_console_errors = sum(len(p.get("console_errors", [])) for p in pages)
    total_failed_requests = sum(len(p.get("failed_requests", [])) for p in pages)
    if total_console_errors or total_failed_requests:
        console.print(
            Panel(
                f"Console Errors: {total_console_errors}\nFailed Network Requests: {total_failed_requests}",
                title="Telemetry Summary",
                border_style="yellow",
            )
        )

    # Performance (Core Web Vitals)
    perf_pages = [p for p in pages if p.get("performance") and p["performance"].get("fcp_ms")]
    if perf_pages:
        perf_table = Table(title="Core Web Vitals")
        perf_table.add_column("URL", style="cyan", max_width=50)
        perf_table.add_column("FCP", justify="right")
        perf_table.add_column("LCP", justify="right")
        perf_table.add_column("CLS", justify="right")
        perf_table.add_column("TTFB", justify="right")
        perf_table.add_column("Resources", justify="right")

        for p in perf_pages:
            perf = p["performance"]
            perf_table.add_row(
                p.get("url", "")[:50],
                f"{perf.get('fcp_ms', 0):.0f}ms" if perf.get("fcp_ms") else "-",
                f"{perf.get('lcp_ms', 0):.0f}ms" if perf.get("lcp_ms") else "-",
                f"{perf.get('cls', 0):.3f}" if perf.get("cls") else "-",
                f"{perf.get('ttfb_ms', 0):.0f}ms" if perf.get("ttfb_ms") else "-",
                str(perf.get("total_resources", 0)),
            )
        console.print(perf_table)

    # Accessibility
    a11y_pages = [p for p in pages if p.get("accessibility") and p["accessibility"].get("total_violations", 0) > 0]
    if a11y_pages:
        total_violations = sum(p["accessibility"]["total_violations"] for p in a11y_pages)
        critical = sum(p["accessibility"].get("critical_count", 0) for p in a11y_pages)
        serious = sum(p["accessibility"].get("serious_count", 0) for p in a11y_pages)

        a11y_summary = (
            f"Total Violations: {total_violations}\n"
            f"Critical: {critical}\n"
            f"Serious: {serious}\n"
            f"Pages with Issues: {len(a11y_pages)}"
        )
        border = "red" if critical > 0 else "yellow"
        console.print(Panel(a11y_summary, title="Accessibility Summary", border_style=border))

    # Interactive Elements
    interactive_pages = [p for p in pages if p.get("interactive_elements")]
    if interactive_pages:
        total_interactive = sum(len(p["interactive_elements"]) for p in interactive_pages)
        total_hidden_urls = sum(len(p.get("hidden_urls_discovered", [])) for p in pages)
        console.print(
            Panel(
                f"Interactive Elements Found: {total_interactive}\n"
                f"Hidden URLs Discovered: {total_hidden_urls}",
                title="Interactive Exploration",
                border_style="blue",
            )
        )

    # Save full results to JSON
    output_path = os.path.join("output", "crawl_result.json")
    os.makedirs("output", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(state, f, indent=2, default=str)
    console.print(f"\n[dim]Full results saved to {output_path}[/dim]")


async def main():
    parser = argparse.ArgumentParser(description="ReQon Bug & Hygiene Discovery Engine")
    parser.add_argument("url", help="Target URL to scan")
    parser.add_argument("--max-pages", type=int, default=50, help="Maximum pages to crawl")
    parser.add_argument("--max-depth", type=int, default=5, help="Maximum crawl depth")
    parser.add_argument("--auth-type", choices=["form", "cookie", "token", "none"], default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--login-url", default=None)

    args = parser.parse_args()

    # Build auth config
    auth_config = None
    if args.auth_type and args.auth_type != "none":
        auth_config = {
            "auth_type": args.auth_type,
            "login_url": args.login_url,
            "username": args.username,
            "password": args.password,
            "token": args.token,
        }

    console.print(Panel(
        f"Target: {args.url}\nMax Pages: {args.max_pages}\nMax Depth: {args.max_depth}\nAuth: {args.auth_type or 'none'}",
        title="ReQon Bug & Hygiene Discovery Engine",
        border_style="blue",
    ))

    start = time.time()
    state = await run_orchestrator(
        target_url=args.url,
        auth_config=auth_config,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
    )
    elapsed = time.time() - start

    console.print(f"\n[bold green]Scan complete in {elapsed:.1f}s[/bold green]\n")
    print_results(state)


if __name__ == "__main__":
    asyncio.run(main())
