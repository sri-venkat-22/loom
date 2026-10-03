"""The command line: pipeline train and pipeline evaluate, both from a config."""

import argparse
import json
import sys

from pipeline.config import ConfigError, load_config
from pipeline.data import DataError, load_data, split
from pipeline.evaluate import evaluate, write_metrics
from pipeline.train import load_model, save_model, train


def run(config, command):
    data = config["data"]
    frame = load_data(data["source"], data["target"])
    X_train, X_test, y_train, y_test = split(
        frame, data["target"], data.get("test_size", 0.2), config["seed"], data.get("stratify")
    )
    if command == "train":
        model = train(X_train, y_train, config)
        save_model(model, config["outputs"]["model"])
    else:
        model = load_model(config["outputs"]["model"])
    metrics = evaluate(model, X_test, y_test)
    write_metrics(metrics, config["outputs"]["metrics"])
    return metrics


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pipeline", description=__doc__)
    parser.add_argument("command", choices=["train", "evaluate"])
    parser.add_argument("--config", default="configs/config.yaml")
    args = parser.parse_args(argv)
    try:
        metrics = run(load_config(args.config), args.command)
    except (ConfigError, DataError, OSError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return 1
    print(json.dumps(metrics, indent=2))
    return 0
