"""
Page Type Classifier — Hybrid self-learning classification pipeline.

Architecture:
  DOM Features + URL Features + HTML Statistics → Feature Extraction (64 features)
      ↓
  XGBoost Ensemble (local, fast, free) → if confidence > threshold → DONE
      ↓ (low confidence)
  LLM Labeler (GPT-4o-mini) → high-quality label → DONE
      ↓
  Label + Features → saved to training set → XGBoost retrains periodically

The system starts by using the LLM for everything (cold start).
As it accumulates labels, XGBoost takes over for most classifications.
Low-confidence XGBoost predictions still get sent to the LLM for correction.
This is genuinely self-learning: it gets better and cheaper the more you use it.

Fallback: If no API key is available, uses rule-based heuristics.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import structlog

from layer2_crawler.classifier.feature_extractor import FeatureExtractor
from layer2_crawler.classifier.xgboost_classifier import XGBoostPageClassifier, XGBOOST_CONFIDENCE_THRESHOLD
from shared.models.page_models import PageData, PageType

logger = structlog.get_logger()

# Singleton instances
_feature_extractor = FeatureExtractor()
_xgboost_classifier = XGBoostPageClassifier()

# URL patterns for rule-based fallback
URL_PATTERNS: dict[PageType, list[str]] = {
    PageType.AUTH: [r"/login", r"/signin", r"/sign-in", r"/register", r"/signup", r"/sign-up", r"/forgot", r"/reset-password", r"/auth"],
    PageType.DASHBOARD: [r"/dashboard", r"/home$", r"/overview", r"/analytics"],
    PageType.SETTINGS: [r"/settings", r"/preferences", r"/config", r"/account"],
    PageType.PROFILE: [r"/profile", r"/user/", r"/me$"],
    PageType.SEARCH: [r"/search", r"\?q=", r"\?query="],
    PageType.ERROR: [r"/404", r"/500", r"/error", r"/not-found"],
}


async def classify_page(page: PageData) -> tuple[PageType, float]:
    """Classify a page using the best available method.

    Priority:
    1. XGBoost (if trained and confident) — free, fast
    2. LLM (if API key available) — accurate, costs tokens
    3. Rule-based heuristics — always available fallback

    Returns (page_type, confidence).
    """
    # Extract features
    features = _feature_extractor.extract(page)

    # Strategy 1: Try XGBoost first (free, fast)
    if _xgboost_classifier.is_ready:
        xgb_type, xgb_confidence = _xgboost_classifier.predict(features)

        if xgb_confidence >= XGBOOST_CONFIDENCE_THRESHOLD:
            logger.info(
                "page_classifier.xgboost",
                url=page.url[:80],
                page_type=xgb_type.value,
                confidence=round(xgb_confidence, 2),
                training_size=_xgboost_classifier.training_size,
            )
            return xgb_type, xgb_confidence

        # Low confidence — fall through to LLM for correction
        logger.info(
            "page_classifier.xgboost_low_confidence",
            url=page.url[:80],
            xgb_type=xgb_type.value,
            xgb_confidence=round(xgb_confidence, 2),
        )

    # Strategy 2: LLM classification (accurate, adds training data)
    from config.settings import settings
    if settings.OPENAI_API_KEY:
        from layer2_crawler.classifier.llm_labeler import llm_classify_page

        llm_type, llm_confidence, reasoning = await llm_classify_page(page)

        if llm_type != PageType.UNKNOWN and llm_confidence > 0.3:
            # Add to training data for XGBoost
            _xgboost_classifier.add_training_sample(features, llm_type.value, llm_confidence)

            logger.info(
                "page_classifier.llm",
                url=page.url[:80],
                page_type=llm_type.value,
                confidence=round(llm_confidence, 2),
                reasoning=reasoning[:80],
                training_size=_xgboost_classifier.training_size,
            )
            return llm_type, llm_confidence

    # Strategy 3: Rule-based fallback
    rule_type, rule_confidence = _rule_based_classify(page)

    # Still add to training data if reasonably confident
    if rule_type != PageType.UNKNOWN and rule_confidence > 0.5:
        _xgboost_classifier.add_training_sample(features, rule_type.value, rule_confidence * 0.7)

    logger.info(
        "page_classifier.rule_based",
        url=page.url[:80],
        page_type=rule_type.value,
        confidence=round(rule_confidence, 2),
    )
    return rule_type, rule_confidence


def get_classifier_stats() -> dict:
    """Get current classifier status for reporting."""
    return {
        "xgboost_ready": _xgboost_classifier.is_ready,
        "training_samples": _xgboost_classifier.training_size,
        "min_samples_needed": 20,
    }


def _rule_based_classify(page: PageData) -> tuple[PageType, float]:
    """Fallback rule-based classification using DOM + URL heuristics."""
    scores: dict[PageType, float] = {pt: 0.0 for pt in PageType}

    # URL patterns
    path = urlparse(page.url).path.lower()
    query = urlparse(page.url).query.lower()
    full_url_path = path + ("?" + query if query else "")
    for page_type, patterns in URL_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, full_url_path):
                scores[page_type] += 3.0
                break

    # DOM signals
    if page.has_login_form:
        scores[PageType.AUTH] += 5.0
    if page.form_count >= 1 and page.input_count >= 5 and not page.has_login_form:
        scores[PageType.FORM] += 4.0
    if page.has_charts:
        scores[PageType.DASHBOARD] += 4.0
    if page.table_count >= 1:
        scores[PageType.LIST_TABLE] += 3.0
    if page.has_search:
        scores[PageType.SEARCH] += 2.0
    if page.image_count >= 3 and page.has_nav and page.has_footer:
        scores[PageType.LANDING] += 2.5
    title = (page.title or "").lower()
    if any(kw in title for kw in ["404", "not found", "error", "500"]):
        scores[PageType.ERROR] += 5.0
    if page.status_code and page.status_code >= 400:
        scores[PageType.ERROR] += 4.0

    best_type = max(scores, key=scores.get)
    best_score = scores[best_type]
    if best_score == 0:
        return PageType.UNKNOWN, 0.0

    total = sum(s for s in scores.values() if s > 0) or 1.0
    confidence = min(best_score / total, 1.0)
    return best_type, confidence
