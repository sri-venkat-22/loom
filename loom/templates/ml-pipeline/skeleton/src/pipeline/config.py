"""The run's settings, from a YAML config file."""

from pathlib import Path

import yaml

REQUIRED = ("seed", "data", "model", "outputs")


class ConfigError(Exception):
    pass


def load_config(path):
    """The config at path, checked. Raises ConfigError if it's missing anything."""
    try:
        config = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise ConfigError(f"Unable to read {path}: {err}")
    return check_config(config)


def check_config(config):
    if not isinstance(config, dict):
        raise ConfigError("The config must be a mapping")
    missing = [key for key in REQUIRED if key not in config]
    if missing:
        raise ConfigError(f"The config is missing: {', '.join(missing)}")
    for key in ("source", "target"):
        if not config["data"].get(key):
            raise ConfigError(f"The config's data needs a {key}")
    if not 0 < config["data"].get("test_size", 0.2) < 1:
        raise ConfigError("data.test_size must be between 0 and 1")
    config.setdefault("features", {})
    return config
