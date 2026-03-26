"""
XGBoost Page Classifier — Local ML model trained from LLM-generated labels.

The self-learning loop:
1. LLM classifies pages during crawl (high accuracy, costs API calls)
2. Each label + features are saved to a training dataset
3. When enough data accumulates, XGBoost is trained locally
4. Future pages are classified by XGBoost (free, fast, no API needed)
5. Low-confidence XGBoost predictions get sent to LLM for correction → more training data

This makes the system genuinely self-learning: it gets better the more you use it.
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

import numpy as np
import structlog
import xgboost as xgb
from sklearn.preprocessing import LabelEncoder

from shared.models.page_models import PageType

logger = structlog.get_logger()

TRAINING_DATA_DIR = "output/training_data"
MODEL_PATH = "output/models/page_classifier.json"
LABEL_ENCODER_PATH = "output/models/label_encoder.json"

# Minimum samples needed before training
MIN_TRAINING_SAMPLES = 20
# Minimum confidence from XGBoost to trust it (otherwise ask LLM)
XGBOOST_CONFIDENCE_THRESHOLD = 0.6


class XGBoostPageClassifier:
    """Local XGBoost model for page classification, trained from LLM labels."""

    def __init__(self):
        self._model: Optional[xgb.XGBClassifier] = None
        self._label_encoder = LabelEncoder()
        self._is_trained = False
        self._training_features: list[np.ndarray] = []
        self._training_labels: list[str] = []
        self._load_model()
        self._load_training_data()

    @property
    def is_ready(self) -> bool:
        """Whether the model is trained and ready for inference."""
        return self._is_trained

    @property
    def training_size(self) -> int:
        return len(self._training_labels)

    def predict(self, features: np.ndarray) -> tuple[PageType, float]:
        """Classify a page using the local XGBoost model.

        Returns (page_type, confidence).
        If not trained, returns (UNKNOWN, 0.0).
        """
        if not self._is_trained:
            return PageType.UNKNOWN, 0.0

        try:
            X = features.reshape(1, -1)
            proba = self._model.predict_proba(X)[0]
            pred_idx = np.argmax(proba)
            confidence = float(proba[pred_idx])

            label = self._label_encoder.inverse_transform([pred_idx])[0]
            try:
                page_type = PageType(label)
            except ValueError:
                page_type = PageType.UNKNOWN

            return page_type, confidence

        except Exception as e:
            logger.warning("xgboost_classifier.predict_error", error=str(e))
            return PageType.UNKNOWN, 0.0

    def add_training_sample(self, features: np.ndarray, label: str, confidence: float = 1.0):
        """Add a labeled sample to the training set. Only keeps high-confidence labels."""
        if confidence < 0.5:
            return

        self._training_features.append(features)
        self._training_labels.append(label)
        self._save_training_sample(features, label, confidence)

        logger.debug(
            "xgboost_classifier.sample_added",
            label=label,
            total_samples=len(self._training_labels),
        )

        # Auto-retrain when we hit milestones
        n = len(self._training_labels)
        if n >= MIN_TRAINING_SAMPLES and (n == MIN_TRAINING_SAMPLES or n % 25 == 0):
            self.train()

    def train(self):
        """Train the XGBoost model on accumulated labeled data."""
        if len(self._training_labels) < MIN_TRAINING_SAMPLES:
            logger.info(
                "xgboost_classifier.not_enough_data",
                samples=len(self._training_labels),
                needed=MIN_TRAINING_SAMPLES,
            )
            return

        X = np.array(self._training_features)
        y_raw = np.array(self._training_labels)

        # Need at least 2 classes
        unique_classes = np.unique(y_raw)
        if len(unique_classes) < 2:
            logger.info("xgboost_classifier.not_enough_classes", classes=len(unique_classes))
            return

        self._label_encoder.fit(y_raw)
        y = self._label_encoder.transform(y_raw)

        self._model = xgb.XGBClassifier(
            n_estimators=100,
            max_depth=6,
            learning_rate=0.1,
            objective="multi:softprob",
            num_class=len(unique_classes),
            eval_metric="mlogloss",
            use_label_encoder=False,
            verbosity=0,
        )

        self._model.fit(X, y)
        self._is_trained = True

        self._save_model()

        logger.info(
            "xgboost_classifier.trained",
            samples=len(y),
            classes=list(unique_classes),
            feature_importance_top5=self._top_features(5),
        )

    def _top_features(self, n: int = 5) -> dict:
        """Get top N most important features."""
        if not self._model:
            return {}
        from layer2_crawler.classifier.feature_extractor import FeatureExtractor
        importances = self._model.feature_importances_
        indices = np.argsort(importances)[-n:][::-1]
        names = FeatureExtractor.FEATURE_NAMES
        return {names[i]: round(float(importances[i]), 3) for i in indices if i < len(names)}

    def _save_model(self):
        """Persist the trained model to disk."""
        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        self._model.save_model(MODEL_PATH)
        # Save label encoder
        with open(LABEL_ENCODER_PATH, "w") as f:
            json.dump({"classes": list(self._label_encoder.classes_)}, f)
        logger.info("xgboost_classifier.model_saved", path=MODEL_PATH)

    def _load_model(self):
        """Load a previously trained model."""
        if os.path.exists(MODEL_PATH) and os.path.exists(LABEL_ENCODER_PATH):
            try:
                self._model = xgb.XGBClassifier()
                self._model.load_model(MODEL_PATH)
                with open(LABEL_ENCODER_PATH, "r") as f:
                    data = json.load(f)
                self._label_encoder.fit(data["classes"])
                self._is_trained = True
                logger.info("xgboost_classifier.model_loaded", path=MODEL_PATH)
            except Exception as e:
                logger.warning("xgboost_classifier.load_failed", error=str(e))
                self._is_trained = False

    def _save_training_sample(self, features: np.ndarray, label: str, confidence: float):
        """Append a training sample to disk for persistence across runs."""
        os.makedirs(TRAINING_DATA_DIR, exist_ok=True)
        filepath = os.path.join(TRAINING_DATA_DIR, "samples.jsonl")
        sample = {
            "features": features.tolist(),
            "label": label,
            "confidence": confidence,
            "timestamp": time.time(),
        }
        with open(filepath, "a") as f:
            f.write(json.dumps(sample) + "\n")

    def _load_training_data(self):
        """Load previously saved training samples."""
        filepath = os.path.join(TRAINING_DATA_DIR, "samples.jsonl")
        if not os.path.exists(filepath):
            return

        try:
            with open(filepath, "r") as f:
                for line in f:
                    sample = json.loads(line.strip())
                    self._training_features.append(np.array(sample["features"], dtype=np.float32))
                    self._training_labels.append(sample["label"])

            if self._training_labels:
                logger.info(
                    "xgboost_classifier.training_data_loaded",
                    samples=len(self._training_labels),
                )
                # Retrain if we have enough data but no model
                if len(self._training_labels) >= MIN_TRAINING_SAMPLES and not self._is_trained:
                    self.train()
        except Exception as e:
            logger.warning("xgboost_classifier.load_training_failed", error=str(e))
