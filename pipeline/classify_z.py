from __future__ import annotations

from datetime import UTC, datetime
import math
import re
from statistics import NormalDist
from typing import Any

from pipeline.dynamodb import _to_dynamodb_value


LOG_MEASURE_RE = re.compile(r"\b(relative risk|risk ratio|rate ratio|odds ratio)\b", re.IGNORECASE)
LOGIT_MEASURE_RE = re.compile(r"\b(sensitivity|specificity)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"[-+]?(?:(?:\d+(?:,\d{3})*)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
LOGIT_EPSILON = 1e-6


def parse_number(value: Any) -> float | None:
    text = str(value or "").replace(",", "").strip()
    if not text:
        return None
    match = NUMBER_RE.search(text)
    if not match:
        return None
    token = match.group(0)
    if token in {"", "+", "-", "."}:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def transform_kind(effect_measure: Any) -> str:
    text = str(effect_measure or "")
    if LOG_MEASURE_RE.search(text):
        return "log"
    if LOGIT_MEASURE_RE.search(text):
        return "logit"
    return "none"


def transform(value: float, kind: str) -> float:
    if kind == "log":
        if value <= 0:
            raise ValueError("log transform requires positive values")
        return math.log(value)
    if kind == "logit":
        if value > 1 and value < 100:
            value = value / 100.0
        if value < 0 or value > 1:
            raise ValueError("logit transform requires proportions from 0 to 1 or percentages from 0 to 100")
        value = min(max(value, LOGIT_EPSILON), 1.0 - LOGIT_EPSILON)
        return math.log(value / (1.0 - value))
    return value


def default_null(kind: str) -> float:
    if kind == "log":
        return 1.0
    if kind == "logit":
        return 0.5
    return 0.0


def ci_z_critical(article: dict[str, Any]) -> float:
    percentage = parse_number(article.get("confidence_interval_percentage"))
    if percentage is None:
        return 1.96
    level = percentage / 100.0
    if level <= 0 or level >= 1:
        return 1.96
    return NormalDist().inv_cdf((1.0 + level) / 2.0)


def wald_category(wald_z: float) -> str:
    prefix = "COM" if wald_z >= 0 else "INT"
    magnitude = abs(wald_z)
    if magnitude < 1:
        suffix = "Z0"
    elif magnitude < 2:
        suffix = "Z1"
    else:
        suffix = "Z2+"
    return f"{prefix}-{suffix}"


def polarity_favors_higher(value: Any) -> bool:
    normalized = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return normalized == "higher favors intervention"


def classify_article(article: dict[str, Any]) -> tuple[float | None, str, str]:
    kind = transform_kind(article.get("effect_measure"))
    estimate = parse_number(article.get("effect_estimate"))
    lower = parse_number(article.get("confidence_interval_begin"))
    upper = parse_number(article.get("confidence_interval_end"))
    null_value = parse_number(article.get("comparator_effect_measure"))
    if null_value is None:
        null_value = parse_number(article.get("line_of_no_effect"))
    if null_value is None:
        null_value = default_null(kind)
    if estimate is None or lower is None or upper is None:
        return None, "UNCLASSIFIED", "missing effect estimate or confidence interval"
    try:
        transformed_estimate = transform(estimate, kind)
        transformed_lower = transform(lower, kind)
        transformed_upper = transform(upper, kind)
        transformed_null = transform(null_value, kind)
    except ValueError as exc:
        return None, "UNCLASSIFIED", str(exc)
    z_critical = ci_z_critical(article)
    se = abs(transformed_upper - transformed_lower) / (2.0 * z_critical)
    if se <= 0:
        return None, "UNCLASSIFIED", "confidence interval produces zero standard error"
    wald_z = (transformed_estimate - transformed_null) / se
    if polarity_favors_higher(article.get("polarity_of_measure")):
        wald_z = -wald_z
    return wald_z, wald_category(wald_z), ""


def classify_and_store_article(table: Any, article: dict[str, Any]) -> dict[str, Any]:
    wald_z, category, error = classify_article(article)
    article["wald_z_category"] = category
    if wald_z is not None:
        article["wald_z"] = round(wald_z, 6)
        article.pop("wald_z_error", None)
    else:
        article.pop("wald_z", None)
        article["wald_z_error"] = error
    article["wald_z_classified_at"] = datetime.now(UTC).isoformat()
    table.put_item(Item=_to_dynamodb_value(article))
    return article
