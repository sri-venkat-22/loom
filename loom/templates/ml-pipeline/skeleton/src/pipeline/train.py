"""Training: the features and the model as one scikit-learn pipeline."""

from pathlib import Path

import joblib
from pipeline.features import build_features
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline

MODELS = {
    "logistic_regression": LogisticRegression,
    "random_forest": RandomForestClassifier,
}


def make_model(settings, seed):
    kind = settings.get("kind", "logistic_regression")
    if kind not in MODELS:
        raise ValueError(f"Unknown model {kind!r}; use one of: {', '.join(MODELS)}")
    return MODELS[kind](random_state=seed, **(settings.get("params") or {}))


def train(X, y, config):
    """The fitted pipeline for the training data X and y."""
    model = Pipeline(
        [
            ("features", build_features(X, config["features"])),
            ("model", make_model(config["model"], config["seed"])),
        ]
    )
    return model.fit(X, y)


def save_model(model, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, path)
    return path


def load_model(path):
    return joblib.load(path)
