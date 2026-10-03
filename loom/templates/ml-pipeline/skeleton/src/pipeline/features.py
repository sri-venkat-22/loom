"""Turning the data's columns into features: numbers are scaled, categories one-hot."""

from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler


def feature_columns(X, settings):
    """(numeric columns, categorical columns): the config's, or found from the types."""
    numeric = list(settings.get("numeric") or [])
    categorical = list(settings.get("categorical") or [])
    if not numeric and not categorical:
        numeric = X.select_dtypes(include="number").columns.tolist()
        categorical = [column for column in X.columns if column not in numeric]
    return numeric, categorical


def build_features(X, settings):
    numeric, categorical = feature_columns(X, settings)
    return ColumnTransformer(
        [
            ("numeric", make_pipeline(SimpleImputer(), StandardScaler()), numeric),
            (
                "categorical",
                make_pipeline(
                    SimpleImputer(strategy="most_frequent"),
                    OneHotEncoder(handle_unknown="ignore"),
                ),
                categorical,
            ),
        ]
    )
