"""
Evidence Builder — produces structured JSON and HTML defect reports.

JSON: complete machine-readable output for CI integration.
HTML: self-contained gallery with annotated screenshots + sortable findings table.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from layer5_defect_detection.models.defect_models import (
    DefectDetectionResult,
    DefectSeverity,
)

_SEVERITY_BADGE: dict[str, str] = {
    "critical": "background:#dc2626;color:#fff",
    "high":     "background:#ea580c;color:#fff",
    "medium":   "background:#ca8a04;color:#fff",
    "low":      "background:#16a34a;color:#fff",
    "info":     "background:#2563eb;color:#fff",
}


class EvidenceBuilder:
    def __init__(self, output_dir: str = "output/defect_reports") -> None:
        self.output_dir = output_dir

    def build_json_report(self, result: DefectDetectionResult, run_id: str) -> str:
        path = os.path.join(self.output_dir, f"defect_report_{run_id}.json")
        os.makedirs(self.output_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.model_dump(), f, indent=2, default=str)
        return path

    def build_html_report(self, result: DefectDetectionResult, run_id: str) -> str:
        path = os.path.join(self.output_dir, f"defect_report_{run_id}.html")
        os.makedirs(self.output_dir, exist_ok=True)
        html = _render_html(result, run_id)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        return path

    def write_priority_pages_manifest(
        self, priority_pages: list[dict], run_dir: str
    ) -> str:
        path = os.path.join(run_dir, "priority_pages.json")
        os.makedirs(run_dir, exist_ok=True)
        manifest = [
            {
                "url": p.get("url"),
                "page_type": p.get("page_type"),
                "page_type_confidence": p.get("page_type_confidence"),
                "priority_tier": p.get("_priority_tier"),
                "priority_reason": p.get("_priority_reason"),
                "page_slug": p.get("_page_slug"),
            }
            for p in priority_pages
        ]
        with open(path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        return path


def _render_html(result: DefectDetectionResult, run_id: str) -> str:
    ts = result.timestamp or datetime.now(timezone.utc).isoformat()

    # Summary bar
    summary_html = f"""
    <div class="summary">
        <span class="badge critical">{result.critical_count} Critical</span>
        <span class="badge high">{result.high_count} High</span>
        <span class="badge medium">{result.medium_count} Medium</span>
        <span class="badge low">{result.low_count} Low</span>
        <span class="badge info">{result.info_count} Info</span>
        &nbsp;&nbsp;
        <strong>Max Regression Score:</strong> {result.max_regression_score:.1f}/100
        &nbsp;|&nbsp;
        <strong>Pages Analyzed:</strong> {result.total_priority_pages}
        &nbsp;|&nbsp;
        <strong>Duration:</strong> {result.duration_seconds:.1f}s
    </div>
    """

    # Per-page sections
    pages_html = ""
    for page_summary in result.pages_analyzed:
        comparison = page_summary.comparison
        reg_score = comparison.regression_score if comparison else 0.0
        reg_count = len(comparison.regression_defects) if comparison else 0

        pages_html += f"""
        <section class="page-section">
            <h2>{page_summary.page_type.upper()} — <a href="{page_summary.url}" target="_blank">{page_summary.url}</a></h2>
            <p><em>{page_summary.priority_reason}</em></p>
            <p>
                <strong>Regression Score:</strong> {reg_score:.1f}/100 &nbsp;|&nbsp;
                <strong>New defects under load:</strong> {reg_count}
            </p>
            <div class="screenshots">
        """

        for snapshot in page_summary.snapshots:
            ann_path = snapshot.annotated_screenshot_path or snapshot.screenshot_path
            # Make path relative for HTML display
            rel_path = os.path.relpath(ann_path, os.path.dirname(result.report_path)) if ann_path else ""
            rel_path = rel_path.replace("\\", "/")
            pages_html += f"""
                <div class="snapshot">
                    <h3>{snapshot.phase.upper()} ({len(snapshot.findings)} findings)</h3>
                    <img src="{rel_path}" alt="{snapshot.phase} screenshot" loading="lazy">
                </div>
            """

        pages_html += "</div>"

        # Findings table for this page
        all_findings = [f for s in page_summary.snapshots for f in s.findings]
        if all_findings:
            pages_html += """
            <table>
                <thead>
                    <tr>
                        <th>Phase</th><th>Severity</th><th>Category</th>
                        <th>Title</th><th>Selector</th>
                    </tr>
                </thead>
                <tbody>
            """
            for f in all_findings:
                badge_style = _SEVERITY_BADGE.get(f.severity.value, "")
                pages_html += f"""
                    <tr>
                        <td>{f.snapshot_phase}</td>
                        <td><span class="badge" style="{badge_style}">{f.severity.value}</span></td>
                        <td>{f.category.value.replace("_", " ")}</td>
                        <td>{f.title}</td>
                        <td><code>{f.element_selector[:80]}</code></td>
                    </tr>
                """
            pages_html += "</tbody></table>"

        # Regression defects callout
        if comparison and comparison.regression_defects:
            pages_html += "<h3>Regression Defects (new under load)</h3><ul>"
            for rd in comparison.regression_defects:
                badge_style = _SEVERITY_BADGE.get(rd.defect.severity.value, "")
                pages_html += (
                    f'<li><span class="badge" style="{badge_style}">'
                    f'{rd.defect.severity.value}</span> '
                    f'[{rd.introduced_at_phase}] {rd.defect.title} — '
                    f'<code>{rd.defect.element_selector[:60]}</code></li>'
                )
            pages_html += "</ul>"

        pages_html += "</section>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Defect Detection Report — {run_id}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 20px; background: #f8fafc; color: #1e293b; }}
  h1   {{ color: #0f172a; border-bottom: 2px solid #e2e8f0; padding-bottom: 8px; }}
  h2   {{ color: #1e40af; margin-top: 32px; }}
  h3   {{ color: #374151; }}
  .summary {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
              padding: 16px; margin: 16px 0; display: flex; gap: 12px; flex-wrap: wrap;
              align-items: center; }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px;
            font-size: 12px; font-weight: 600; }}
  .badge.critical {{ background:#dc2626;color:#fff }}
  .badge.high     {{ background:#ea580c;color:#fff }}
  .badge.medium   {{ background:#ca8a04;color:#fff }}
  .badge.low      {{ background:#16a34a;color:#fff }}
  .badge.info     {{ background:#2563eb;color:#fff }}
  .page-section {{ background:#fff; border:1px solid #e2e8f0; border-radius:8px;
                   padding:20px; margin:20px 0; }}
  .screenshots  {{ display:flex; gap:16px; flex-wrap:wrap; margin:16px 0; }}
  .snapshot     {{ flex:1; min-width:300px; }}
  .snapshot img {{ width:100%; border:1px solid #cbd5e1; border-radius:4px; }}
  table {{ width:100%; border-collapse:collapse; margin:16px 0; font-size:13px; }}
  th    {{ background:#f1f5f9; text-align:left; padding:8px; border-bottom:2px solid #e2e8f0; }}
  td    {{ padding:6px 8px; border-bottom:1px solid #f1f5f9; vertical-align:top; }}
  tr:hover td {{ background:#f8fafc; }}
  code  {{ background:#f1f5f9; padding:1px 4px; border-radius:3px; font-size:11px; }}
  a     {{ color:#2563eb; }}
  ul    {{ padding-left:20px; }}
  li    {{ margin:4px 0; }}
</style>
</head>
<body>
<h1>Defect Detection Report</h1>
<p><strong>Target:</strong> {result.target_url} &nbsp;|&nbsp;
   <strong>Run ID:</strong> {run_id} &nbsp;|&nbsp;
   <strong>Generated:</strong> {ts}</p>
{summary_html}
{pages_html}
</body>
</html>"""
