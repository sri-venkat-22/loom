# pipeline

A reproducible machine-learning pipeline: load the data, build features, train a
scikit-learn model and evaluate it, all set by `configs/config.yaml`.

## Run

```
pip install -e ".[dev]"
pipeline train --config configs/config.yaml
pipeline evaluate --config configs/config.yaml
```

Training saves the model to `models/` and the metrics to `reports/metrics.json`. The same
config, data and seed give the same model and metrics.

## Test

```
python -m pytest -q
```
