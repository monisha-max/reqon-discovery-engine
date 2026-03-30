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


def print_perf_results(perf_result: dict):
    """Pretty-print performance test results."""
    if not perf_result:
        return

    console.print(Panel(
        f"Endpoints Discovered: {perf_result.get('endpoints_discovered', 0)}\n"
        f"Endpoints Tested: {perf_result.get('endpoints_tested', 0)}\n"
        f"Test Runs: {len(perf_result.get('test_runs', []))}\n"
        f"Bottlenecks Found: {len(perf_result.get('bottlenecks', []))}",
        title="Performance Test Summary",
        border_style="cyan",
    ))

    # Per-test-run tables
    for run in perf_result.get("test_runs", []):
        test_type = run.get("test_type", "").upper()
        console.print(
            f"\n[bold]{test_type} TEST[/bold] — "
            f"{run.get('peak_users')} users | "
            f"{run.get('duration_seconds')}s | "
            f"{run.get('total_requests', 0):,} requests | "
            f"Error rate: {run.get('overall_error_rate', 0):.1%} | "
            f"RPS: {run.get('overall_rps', 0):.1f}"
        )

        metrics = run.get("endpoint_metrics", [])
        if metrics:
            perf_table = Table(title=f"{test_type} — Endpoint Metrics")
            perf_table.add_column("Endpoint", style="cyan", max_width=50)
            perf_table.add_column("Method", style="magenta")
            perf_table.add_column("p50", justify="right")
            perf_table.add_column("p90", justify="right")
            perf_table.add_column("p99", justify="right")
            perf_table.add_column("Err%", justify="right")
            perf_table.add_column("RPS", justify="right")
            perf_table.add_column("Bottleneck", style="red")

            for m in metrics:
                is_bn = "YES" if m.get("is_bottleneck") else "-"
                perf_table.add_row(
                    m.get("endpoint", "")[:50],
                    m.get("method", ""),
                    f"{m.get('p50_ms', 0):.0f}ms",
                    f"{m.get('p90_ms', 0):.0f}ms",
                    f"{m.get('p99_ms', 0):.0f}ms",
                    f"{m.get('error_rate', 0):.1%}",
                    f"{m.get('requests_per_second', 0):.1f}",
                    is_bn,
                )
            console.print(perf_table)

    # Bottlenecks
    bottlenecks = perf_result.get("bottlenecks", [])
    if bottlenecks:
        bn_text = "\n".join(f"• {b}" for b in bottlenecks)
        console.print(Panel(bn_text, title="Bottlenecks Detected", border_style="red"))

    # AI Analysis
    ai_analysis = perf_result.get("ai_analysis", "")
    if ai_analysis:
        console.print(Panel(ai_analysis, title="AI Performance Analysis", border_style="blue"))

    # Recommendations
    recommendations = perf_result.get("recommendations", [])
    if recommendations:
        rec_text = "\n".join(f"{i+1}. {r}" for i, r in enumerate(recommendations))
        console.print(Panel(rec_text, title="Recommendations", border_style="green"))

    # Script path
    script_path = perf_result.get("generated_script_path", "")
    if script_path:
        console.print(f"\n[dim]Locust script saved to {script_path}[/dim]")


def print_defect_results(defect_result: dict):
    """Pretty-print Layer 5 defect detection results."""
    if not defect_result:
        return

    reg_score = defect_result.get("max_regression_score", 0.0)
    score_color = "red" if reg_score >= 40 else "yellow" if reg_score >= 10 else "green"

    summary = (
        f"Pages Analyzed: {defect_result.get('total_priority_pages', 0)}\n"
        f"Total Defects: {defect_result.get('total_defects', 0)}\n"
        f"  Critical: {defect_result.get('critical_count', 0)}\n"
        f"  High:     {defect_result.get('high_count', 0)}\n"
        f"  Medium:   {defect_result.get('medium_count', 0)}\n"
        f"  Low:      {defect_result.get('low_count', 0)}\n"
        f"  Info:     {defect_result.get('info_count', 0)}\n"
        f"[{score_color}]Max Regression Score: {reg_score:.1f}/100[/{score_color}]"
    )
    console.print(Panel(summary, title="Defect Detection Summary", border_style=score_color))

    # Per-page regression summary
    pages = defect_result.get("pages_analyzed", [])
    if pages:
        tbl = Table(title="High-Priority Page Defects")
        tbl.add_column("Page Type", style="magenta")
        tbl.add_column("URL", style="cyan", max_width=50)
        tbl.add_column("Priority", style="dim")
        tbl.add_column("Critical", justify="right", style="red")
        tbl.add_column("High", justify="right", style="yellow")
        tbl.add_column("Regression Score", justify="right")

        for p in pages:
            comp = p.get("comparison") or {}
            score = comp.get("regression_score", 0.0)
            tbl.add_row(
                p.get("page_type", "?").upper(),
                p.get("url", "")[:50],
                p.get("priority_reason", "")[:30],
                str(p.get("critical_count", 0)),
                str(p.get("high_count", 0)),
                f"{score:.1f}",
            )
        console.print(tbl)

    report_path = defect_result.get("report_path", "")
    if report_path:
        console.print(f"\n[dim]Defect report saved to {report_path}[/dim]")


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

    # Performance testing flags (Layer 3)
    parser.add_argument("--perf-test", action="store_true", default=True, help="Run performance tests after crawling")
    # Defect detection flags (Layer 5)
    parser.add_argument(
        "--defect-detection",
        action="store_true",
        default=True,
        help="Run visual defect detection in parallel with load testing (requires --perf-test)",
    )
    parser.add_argument(
        "--defect-viewport",
        default="1920x1080",
        metavar="WxH",
        help="Viewport size for defect capture, e.g. 1920x1080 (default: 1920x1080)",
    )
    parser.add_argument(
        "--defect-max-pages",
        type=int,
        default=10,
        help="Max high-priority pages to scan per defect detection run (default: 10)",
    )
    parser.add_argument(
        "--test-type",
        choices=["load", "stress", "soak", "all"],
        default="all",
        help="Performance test type(s) to run (default: all)",
    )
    parser.add_argument("--openapi-spec", default=None, help="Path or URL to Swagger/OpenAPI spec")
    parser.add_argument("--load-users", type=int, default=10, help="Concurrent users for load test")
    parser.add_argument("--load-duration", type=int, default=30, help="Load test duration in seconds")
    parser.add_argument("--stress-users", type=int, default=50, help="Max users for stress test")
    parser.add_argument("--stress-duration", type=int, default=60, help="Stress test duration in seconds")
    parser.add_argument("--soak-users", type=int, default=10, help="Concurrent users for soak test")
    parser.add_argument("--soak-duration", type=int, default=120, help="Soak test duration in seconds")

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

    # Build defect config (Layer 5)
    defect_config = None
    if args.defect_detection:
        try:
            vw, vh = args.defect_viewport.split("x")
            viewport = {"width": int(vw), "height": int(vh)}
        except (ValueError, AttributeError):
            viewport = {"width": 1920, "height": 1080}
        defect_config = {
            "enabled": True,
            "viewport": viewport,
            "max_pages": args.defect_max_pages,
        }

    # Build perf config
    perf_config = None
    if args.perf_test:
        test_types = (
            ["load", "stress", "soak"] if args.test_type == "all" else [args.test_type]
        )
        perf_config = {
            "test_types": test_types,
            "openapi_spec_path": args.openapi_spec,
            "load_users": args.load_users,
            "load_duration_seconds": args.load_duration,
            "stress_max_users": args.stress_users,
            "stress_duration_seconds": args.stress_duration,
            "soak_users": args.soak_users,
            "soak_duration_seconds": args.soak_duration,
        }

    startup_info = (
        f"Target: {args.url}\n"
        f"Max Pages: {args.max_pages}\n"
        f"Max Depth: {args.max_depth}\n"
        f"Auth: {args.auth_type or 'none'}\n"
        f"Performance Testing: {'YES — ' + args.test_type.upper() if args.perf_test else 'no'}\n"
        f"Defect Detection: {'YES — viewport ' + args.defect_viewport if args.defect_detection else 'no'}"
    )
    console.print(Panel(startup_info, title="ReQon Bug & Hygiene Discovery Engine", border_style="blue"))

    start = time.time()
    state = await run_orchestrator(
        target_url=args.url,
        auth_config=auth_config,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        perf_config=perf_config,
        defect_config=defect_config,
    )
    elapsed = time.time() - start

    console.print(f"\n[bold green]Scan complete in {elapsed:.1f}s[/bold green]\n")
    print_results(state)

    if state.get("perf_result"):
        print_perf_results(state["perf_result"])

    if state.get("defect_result"):
        print_defect_results(state["defect_result"])

    # Intelligence scoring (wire through same path as API)
    await _run_intelligence(state, args.url)


async def _run_intelligence(state, target_url):
    """Run intelligence scoring and display results in CLI."""
    try:
        from intelligence.services.runtime import process_final_state
        import uuid

        scan_id = str(uuid.uuid4())[:8]
        intel = process_final_state(state, target_url, scan_id)

        if not intel:
            return

        app_score = intel.get("application_score", {})
        lifecycle = intel.get("lifecycle_summary", {})
        page_scores = intel.get("page_scores", [])

        # Main score panel
        grade = app_score.get("grade", "?")
        score = app_score.get("adjusted_score", 0)
        risk = app_score.get("risk_class", "?")
        trend = app_score.get("trend_indicator", "?")

        grade_color = {"A": "green", "B": "green", "C": "yellow", "D": "red", "F": "red"}.get(grade, "white")

        console.print(Panel(
            f"[bold {grade_color}]Grade: {grade}[/bold {grade_color}]  |  "
            f"Score: [bold]{score:.1f}[/bold]/100  |  "
            f"Risk: {risk}  |  "
            f"Trend: {trend}\n\n"
            f"New: {lifecycle.get('new_issues', 0)}  |  "
            f"Recurring: {lifecycle.get('recurring_issues', 0)}  |  "
            f"Resolved: {lifecycle.get('resolved_issues', 0)}  |  "
            f"Regressions: {lifecycle.get('regressions', 0)}",
            title="Intelligence — Hygiene Score",
            border_style=grade_color,
        ))

        # Page scores table
        if page_scores:
            from rich.table import Table as RichTable
            t = RichTable(title="Page Scores")
            t.add_column("Page", style="cyan", max_width=55)
            t.add_column("Score", justify="right", style="bold")
            t.add_column("Grade", justify="center")
            t.add_column("Risk", justify="center")
            t.add_column("Issues", justify="right")

            for ps in page_scores:
                pg = ps.get("grade", "?")
                gc = {"A": "green", "B": "green", "C": "yellow", "D": "red", "F": "red"}.get(pg, "white")
                t.add_row(
                    ps.get("url", "")[:55],
                    f"{ps.get('adjusted_score', 0):.1f}",
                    f"[{gc}]{pg}[/{gc}]",
                    ps.get("risk_class", "?"),
                    str(ps.get("issue_count", 0)),
                )
            console.print(t)

    except ImportError:
        console.print("[dim]Intelligence layer not available (Neo4j not configured)[/dim]")
    except Exception as e:
        console.print(f"[dim]Intelligence scoring skipped: {e}[/dim]")


if __name__ == "__main__":
    asyncio.run(main())
