"""Evaluating a trained model on the test data, and writing its metrics."""

import json
from pathlib import Path

from sklearn.metrics import accuracy_score, f1_score


def evaluate(model, X, y):
    predicted = model.predict(X)
    return {
        "accuracy": round(float(accuracy_score(y, predicted)), 4),
        "f1_macro": round(float(f1_score(y, predicted, average="macro")), 4),
        "rows": int(len(y)),
    }


def write_metrics(metrics, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return path
