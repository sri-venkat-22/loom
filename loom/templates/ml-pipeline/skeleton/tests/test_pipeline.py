import json

import numpy as np
import pandas as pd
import pytest
import yaml
from pipeline.cli import main
from pipeline.config import ConfigError, check_config
from pipeline.data import DataError, load_data, split
from pipeline.train import train


@pytest.fixture
def frame():
    """Small synthetic data: two numbers and a category, and a target that follows them."""
    rng = np.random.default_rng(0)
    size = 200
    frame = pd.DataFrame(
        {
            "x1": rng.normal(size=size),
            "x2": rng.normal(size=size),
            "color": rng.choice(["red", "green", "blue"], size=size),
        }
    )
    frame["target"] = ((frame.x1 + frame.x2) > 0).astype(int)
    return frame


@pytest.fixture
def config(tmp_path, frame):
    source = tmp_path / "data.csv"
    frame.to_csv(source, index=False)
    return check_config(
        {
            "seed": 42,
            "data": {"source": str(source), "target": "target", "test_size": 0.25},
            "model": {"kind": "logistic_regression", "params": {"max_iter": 500}},
            "outputs": {
                "model": str(tmp_path / "models" / "model.joblib"),
                "metrics": str(tmp_path / "reports" / "metrics.json"),
            },
        }
    )


def test_config_needs_its_sections():
    with pytest.raises(ConfigError):
        check_config({"seed": 1})


def test_loading_checks_the_target(tmp_path, frame):
    source = tmp_path / "data.csv"
    frame.drop(columns=["target"]).to_csv(source, index=False)
    with pytest.raises(DataError):
        load_data(source, "target")


def test_split_is_reproducible(frame):
    first = split(frame, "target", 0.25, seed=7)
    second = split(frame, "target", 0.25, seed=7)
    assert len(first[1]) == 50
    assert first[1].index.equals(second[1].index)


def test_training_is_reproducible(frame, config):
    X_train, X_test, y_train, _ = split(frame, "target", 0.25, seed=42)
    first = train(X_train, y_train, config).predict(X_test)
    second = train(X_train, y_train, config).predict(X_test)
    assert (first == second).all()


def test_the_command_line_trains_and_evaluates(tmp_path, config, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")

    assert main(["train", "--config", str(path)]) == 0
    metrics = json.loads((tmp_path / "reports" / "metrics.json").read_text(encoding="utf-8"))
    assert 0.8 <= metrics["accuracy"] <= 1
    assert metrics["rows"] == 50

    assert main(["evaluate", "--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out.split("}\n", 1)[1]) == metrics


def test_errors_are_reported(tmp_path, capsys):
    assert main(["train", "--config", str(tmp_path / "missing.yaml")]) == 1
    assert "Error:" in capsys.readouterr().err
