"""Loading the data and splitting it for training and testing."""

import pandas as pd
from sklearn.model_selection import train_test_split


class DataError(Exception):
    pass


def load_data(source, target):
    """The data frame in the CSV at source, which must have the target column."""
    try:
        frame = pd.read_csv(source)
    except (OSError, ValueError) as err:
        raise DataError(f"Unable to read {source}: {err}")
    if target not in frame.columns:
        raise DataError(f"{source} has no {target!r} column")
    if frame.empty:
        raise DataError(f"{source} has no rows")
    return frame


def split(frame, target, test_size, seed, stratify=True):
    """(X_train, X_test, y_train, y_test), the same for the same seed."""
    X = frame.drop(columns=[target])
    y = frame[target]
    return train_test_split(
        X, y, test_size=test_size, random_state=seed, stratify=y if stratify else None
    )
