from __future__ import annotations

import math
from collections import defaultdict

from intelligence.models.contracts import (
    ApplicationScore,
    Dimension,
    DimensionBreakdown,
    Page,
    PageScore,
    ScanRequest,
    Severity,
)


SEVERITY_WEIGHTS = {
    Severity.CRITICAL: 10.0,
    Severity.MAJOR: 5.0,
    Severity.MINOR: 2.0,
    Severity.INFORMATIONAL: 0.5,
}

GRADE_THRESHOLDS = (
    (90.0, "A"),
    (80.0, "B"),
    (70.0, "C"),
    (60.0, "D"),
    (0.0, "F"),
)


class DeterministicScoringService:
    def score_scan(self, payload: ScanRequest, tenant_id: str) -> tuple[ApplicationScore, list[PageScore]]:
        page_scores = [self._score_page(page, payload.application_key) for page in payload.pages]

        app_base = self._average(score.base_score for score in page_scores)
        app_adjusted = self._average(score.adjusted_score for score in page_scores)
        app_risk = self._average(score.risk_score for score in page_scores)
        app_risk_class = self._risk_class(app_risk)
        app_priority_flags = self._priority_flags(
            issue_count=sum(page.issue_count for page in page_scores),
            risk_class=app_risk_class,
            adjusted_score=app_adjusted,
        )

        application_score = ApplicationScore(
            application_name=payload.application_name,
            application_key=payload.application_key,
            base_score=round(app_base, 2),
            adjusted_score=round(app_adjusted, 2),
            risk_score=round(app_risk, 3),
            risk_class=app_risk_class,
            grade=self._grade(app_adjusted),
            priority_flags=app_priority_flags,
        )

        return application_score, page_scores

    def _score_page(self, page: Page, application_key: str) -> PageScore:
        dimension_penalties: dict[Dimension, float] = defaultdict(float)
        dimension_counts: dict[Dimension, int] = defaultdict(int)
        issue_count = 0
        recurrence_events = 0

        for element in page.elements:
            for issue in element.issues:
                issue_count += 1
                recurrence_events += max(issue.occurrence_count - 1, 0)
                penalty = self._issue_penalty(
                    issue.severity,
                    issue.occurrence_count,
                    issue.regression_flag,
                )
                dimension_penalties[issue.dimension] += penalty
                dimension_counts[issue.dimension] += 1

        performance_penalty = self._performance_penalty(page)
        if performance_penalty > 0:
            dimension_penalties[Dimension.PERFORMANCE] += performance_penalty

        total_issue_penalty = sum(dimension_penalties.values())
        base_score = max(0.0, 100.0 - (total_issue_penalty * 2.0))

        risk_score = self._heuristic_risk(
            page=page,
            issue_count=issue_count,
            recurrence_events=recurrence_events,
        )
        adjusted_score = max(0.0, base_score / (1.0 + (risk_score * 0.15)))
        risk_class = self._risk_class(risk_score)
        priority_flags = self._priority_flags(
            issue_count=issue_count,
            risk_class=risk_class,
            adjusted_score=adjusted_score,
        )

        dimension_breakdown = [
            DimensionBreakdown(
                dimension=dimension,
                penalty=round(penalty, 2),
                issue_count=dimension_counts.get(dimension, 0),
            )
            for dimension, penalty in sorted(dimension_penalties.items(), key=lambda item: item[0].value)
        ]

        return PageScore(
            application_key=application_key,
            url=page.url,
            issue_count=issue_count,
            base_score=round(base_score, 2),
            adjusted_score=round(adjusted_score, 2),
            risk_score=round(risk_score, 3),
            risk_class=risk_class,
            trend_indicator="stable",
            grade=self._grade(adjusted_score),
            priority_flags=priority_flags,
            dimension_breakdown=dimension_breakdown,
        )

    def _issue_penalty(self, severity: Severity, occurrence_count: int, regression_flag: bool) -> float:
        severity_weight = SEVERITY_WEIGHTS[severity]
        recurrence_multiplier = 1.0 + math.log(occurrence_count + 1.0)
        regression_multiplier = 1.25 if regression_flag else 1.0
        return severity_weight * recurrence_multiplier * regression_multiplier

    def _performance_penalty(self, page: Page) -> float:
        snapshot = page.performance_snapshot
        if snapshot is None:
            return 0.0
        degradation = (
            (100.0 - snapshot.scalability)
            + (100.0 - snapshot.responsiveness)
            + (100.0 - snapshot.stability)
        ) / 3.0
        return degradation / 5.0

    def _heuristic_risk(self, page: Page, issue_count: int, recurrence_events: int) -> float:
        element_count = max(len(page.elements), 1)
        defect_density = min(issue_count / element_count, 5.0) / 5.0
        recurrence_ratio = min(recurrence_events / max(issue_count, 1), 1.0)

        snapshot = page.performance_snapshot
        performance_degradation = 0.0
        if snapshot is not None:
            performance_degradation = (
                (100.0 - snapshot.scalability)
                + (100.0 - snapshot.responsiveness)
                + (100.0 - snapshot.stability)
            ) / 300.0

        return max(
            0.0,
            min(
                1.0,
                (defect_density * 0.45)
                + (recurrence_ratio * 0.3)
                + (performance_degradation * 0.25),
            ),
        )

    def _grade(self, score: float) -> str:
        for threshold, grade in GRADE_THRESHOLDS:
            if score >= threshold:
                return grade
        return "F"

    def _risk_class(self, risk_score: float) -> str:
        if risk_score >= 0.85:
            return "critical"
        if risk_score >= 0.65:
            return "high"
        if risk_score >= 0.35:
            return "medium"
        return "low"

    def _priority_flags(self, issue_count: int, risk_class: str, adjusted_score: float) -> list[str]:
        flags: list[str] = []
        if risk_class in {"high", "critical"}:
            flags.append("risk_hotspot")
        if adjusted_score < 70:
            flags.append("score_below_threshold")
        if issue_count >= 10:
            flags.append("high_issue_volume")
        return flags

    def _average(self, values) -> float:
        values = list(values)
        if not values:
            return 0.0
        return sum(values) / len(values)
