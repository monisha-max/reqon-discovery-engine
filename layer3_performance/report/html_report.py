"""
HTML Report Generator for Layer 3 Performance Testing.

Produces a single self-contained HTML file with:
  - Executive summary (AI analysis or rule-based fallback)
  - Per-test-type tables (p50/p90/p95/p99, error rate, RPS, bottleneck flag)
  - Soak degradation section (time-series slope analysis)
  - Bottleneck list
  - Recommendations
"""
from __future__ import annotations

import html
import os
from datetime import datetime, timezone

from layer3_performance.models.perf_models import (
    PerformanceTestResult,
    TestRunResult,
    TestType,
)


def build_html_report(result: PerformanceTestResult, output_dir: str) -> str:
    """
    Render result as an HTML file, save it to output_dir, and return the path.
    """
    os.makedirs(output_dir, exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(output_dir, f"perf_report_{run_ts}.html")

    body = _render_body(result)
    page = _wrap_page(
        title=f"Performance Report — {html.escape(result.target_url)}",
        body=body,
    )

    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(page)

    return report_path


# ---------------------------------------------------------------------------
# Body sections
# ---------------------------------------------------------------------------

def _render_body(result: PerformanceTestResult) -> str:
    sections = []

    # Hero summary
    sections.append(_hero(result))

    # Bottleneck list
    if result.bottlenecks:
        sections.append(_bottleneck_list(result.bottlenecks))

    # Per-test-run tables
    for run in result.test_runs:
        sections.append(_run_table(run))

    # Soak degradation
    for run in result.test_runs:
        if run.test_type == TestType.SOAK and run.soak_degradations:
            sections.append(_soak_section(run))

    # AI analysis + recommendations
    if result.ai_analysis:
        sections.append(_analysis_section(result))

    return "\n".join(sections)


def _hero(result: PerformanceTestResult) -> str:
    total_requests = sum(r.total_requests for r in result.test_runs)
    max_rps = max((r.peak_rps for r in result.test_runs), default=0.0)
    overall_err = (
        max((r.overall_error_rate for r in result.test_runs), default=0.0)
        if result.test_runs else 0.0
    )
    bottleneck_count = len(result.bottlenecks)
    badge_color = "red" if bottleneck_count > 0 else "green"

    return f"""
<div class="hero">
  <h1>Performance Test Report</h1>
  <p class="target">{html.escape(result.target_url)}</p>
  <div class="stats-row">
    <div class="stat"><span class="num">{result.endpoints_tested}</span><span class="lbl">Endpoints Tested</span></div>
    <div class="stat"><span class="num">{total_requests:,}</span><span class="lbl">Total Requests</span></div>
    <div class="stat"><span class="num">{max_rps:.1f}</span><span class="lbl">Peak RPS</span></div>
    <div class="stat"><span class="num">{overall_err:.1%}</span><span class="lbl">Max Error Rate</span></div>
    <div class="stat"><span class="num" style="color:{badge_color}">{bottleneck_count}</span><span class="lbl">Bottlenecks</span></div>
  </div>
  <p class="meta">Run at {html.escape(result.timestamp)} &nbsp;|&nbsp; Duration {result.total_duration_seconds:.0f}s</p>
</div>
"""


def _bottleneck_list(bottlenecks: list[str]) -> str:
    items = "\n".join(f"<li>{html.escape(b)}</li>" for b in bottlenecks)
    return f"""
<section>
  <h2>⚠ Bottlenecks Detected</h2>
  <ul class="bottleneck-list">{items}</ul>
</section>
"""


def _run_table(run: TestRunResult) -> str:
    if not run.endpoint_metrics:
        return f"""
<section>
  <h2>{run.test_type.value.title()} Test — No Endpoint Data</h2>
  <p class="muted">No CSV metrics collected for this run.</p>
</section>
"""

    rows = []
    for m in sorted(run.endpoint_metrics, key=lambda x: x.p99_ms, reverse=True):
        flag = "🔴" if m.is_bottleneck else ""
        reason = html.escape(m.bottleneck_reason or "")
        deg = f"{m.degradation_factor:.1f}×" if m.degradation_factor else "—"
        rows.append(f"""
<tr class="{'bottleneck-row' if m.is_bottleneck else ''}">
  <td>{flag} {html.escape(m.method)}</td>
  <td>{html.escape(m.endpoint)}</td>
  <td>{m.p50_ms:.0f}</td>
  <td>{m.p90_ms:.0f}</td>
  <td>{m.p95_ms:.0f}</td>
  <td class="{'slow' if m.p99_ms > 2000 else ''}">{m.p99_ms:.0f}</td>
  <td class="{'high-err' if m.error_rate > 0.05 else ''}">{m.error_rate:.1%}</td>
  <td>{m.requests_per_second:.1f}</td>
  <td>{m.total_requests:,}</td>
  <td>{deg}</td>
  <td class="reason-col">{reason}</td>
</tr>""")

    return f"""
<section>
  <h2>{run.test_type.value.title()} Test
    <span class="sub">{run.peak_users} users @ {run.spawn_rate:.1f}/s · {run.duration_seconds}s · {run.overall_rps:.1f} RPS · {run.overall_error_rate:.1%} err</span>
  </h2>
  <div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Method</th><th>Endpoint</th>
        <th>p50</th><th>p90</th><th>p95</th><th>p99 (ms)</th>
        <th>Err%</th><th>RPS</th><th>Requests</th><th>Degradation</th><th>Bottleneck Reason</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </div>
</section>
"""


def _soak_section(run: TestRunResult) -> str:
    rows = []
    for d in run.soak_degradations:
        icon = "🔴" if d.is_degrading else "🟡"
        rows.append(f"""
<tr>
  <td>{icon} {html.escape(d.method)}</td>
  <td>{html.escape(d.endpoint)}</td>
  <td>{d.start_p95_ms:.0f}</td>
  <td>{d.end_p95_ms:.0f}</td>
  <td class="{'slow' if d.is_degrading else ''}">{d.p95_slope_ms_per_min:.1f} ms/min</td>
  <td>{html.escape(d.degradation_summary)}</td>
</tr>""")

    return f"""
<section>
  <h2>Soak Test — Time-Series Degradation</h2>
  <div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th>Method</th><th>Endpoint</th><th>Start p95 (ms)</th><th>End p95 (ms)</th>
        <th>Slope</th><th>Summary</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  </div>
</section>
"""


def _analysis_section(result: PerformanceTestResult) -> str:
    recs = ""
    if result.recommendations:
        items = "\n".join(f"<li>{html.escape(r)}</li>" for r in result.recommendations)
        recs = f"<h3>Recommendations</h3><ol>{items}</ol>"

    return f"""
<section>
  <h2>AI Analysis</h2>
  <p class="analysis-text">{html.escape(result.ai_analysis)}</p>
  {recs}
</section>
"""


# ---------------------------------------------------------------------------
# Page chrome
# ---------------------------------------------------------------------------

def _wrap_page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
          background: #0f1117; color: #e2e8f0; line-height: 1.6; }}
  a {{ color: #63b3ed; }}
  .hero {{ background: linear-gradient(135deg,#1a202c,#2d3748); padding: 40px;
           border-bottom: 2px solid #4a5568; }}
  .hero h1 {{ font-size: 1.8rem; color: #f7fafc; }}
  .target {{ color: #63b3ed; font-size: 1rem; margin: 6px 0 20px; }}
  .stats-row {{ display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 12px; }}
  .stat {{ background: #2d3748; border-radius: 8px; padding: 14px 20px; min-width: 110px; text-align: center; }}
  .stat .num {{ display: block; font-size: 1.6rem; font-weight: 700; color: #f7fafc; }}
  .stat .lbl {{ font-size: 0.75rem; color: #a0aec0; }}
  .meta {{ font-size: 0.8rem; color: #718096; margin-top: 8px; }}
  section {{ padding: 30px 40px; border-bottom: 1px solid #2d3748; }}
  h2 {{ font-size: 1.2rem; color: #f7fafc; margin-bottom: 16px; }}
  .sub {{ font-size: 0.85rem; color: #718096; font-weight: 400; margin-left: 10px; }}
  h3 {{ color: #e2e8f0; margin: 16px 0 8px; }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th {{ background: #2d3748; color: #a0aec0; padding: 8px 12px; text-align: left;
        white-space: nowrap; position: sticky; top: 0; }}
  td {{ padding: 7px 12px; border-bottom: 1px solid #2d3748; vertical-align: top; }}
  tr:hover td {{ background: #1a202c; }}
  .bottleneck-row td {{ background: #2d1f1f; }}
  .slow {{ color: #fc8181; font-weight: 600; }}
  .high-err {{ color: #fc8181; }}
  .reason-col {{ font-size: 0.78rem; color: #a0aec0; max-width: 320px; }}
  .bottleneck-list {{ list-style: none; padding-left: 0; }}
  .bottleneck-list li {{ background: #2d1f1f; border-left: 3px solid #fc8181;
                         padding: 8px 14px; margin-bottom: 6px; border-radius: 4px;
                         font-size: 0.88rem; }}
  .analysis-text {{ background: #1a202c; border-left: 3px solid #63b3ed;
                    padding: 14px 18px; border-radius: 4px; color: #cbd5e0; }}
  ol {{ padding-left: 20px; }}
  ol li {{ margin-bottom: 6px; }}
  .muted {{ color: #718096; }}
</style>
</head>
<body>
{body}
</body>
</html>"""
