from intelligence.models.contracts import ApplicationScore, PageScore
from intelligence.repositories.contracts import ScoreHistoryStore


class TrendService:
    def __init__(self, history_store: ScoreHistoryStore) -> None:
        self.history_store = history_store

    def apply_application_trend(self, tenant_id: str, score: ApplicationScore) -> ApplicationScore:
        previous = self.history_store.latest(
            tenant_id=tenant_id,
            entity_type="application",
            entity_key=score.application_key,
        )
        return score.model_copy(
            update={"trend_indicator": self._classify(score.adjusted_score, previous.adjusted_score if previous else None)}
        )

    def apply_page_trends(self, tenant_id: str, page_scores: list[PageScore]) -> list[PageScore]:
        enriched: list[PageScore] = []
        for score in page_scores:
            previous = self.history_store.latest(
                tenant_id=tenant_id,
                entity_type="page",
                entity_key=score.url,
            )
            enriched.append(
                score.model_copy(
                    update={"trend_indicator": self._classify(score.adjusted_score, previous.adjusted_score if previous else None)}
                )
            )
        return enriched

    def _classify(self, current_score: float, previous_score: float | None) -> str:
        if previous_score is None:
            return "new"

        delta = current_score - previous_score
        if abs(delta) < 1.0:
            return "stable"
        if delta > 0:
            return "improving"
        return "declining"
