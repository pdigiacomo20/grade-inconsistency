from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import math
import os
from pathlib import Path
import re
from statistics import NormalDist
from typing import Any

import yaml

from pipeline.dynamodb import _to_dynamodb_value
from pipeline.env import load_repo_env
from pipeline.evaluate import dynamodb_resource, scan_all, selected_review_ids


LOG_MEASURE_RE = re.compile(r"\b(relative risk|risk ratio|rate ratio|odds ratio)\b", re.IGNORECASE)
LOGIT_MEASURE_RE = re.compile(r"\b(sensitivity|specificity)\b", re.IGNORECASE)
NUMBER_RE = re.compile(r"[-+]?(?:(?:\d+(?:,\d{3})*)(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
LOGIT_EPSILON = 1e-6


@dataclass(frozen=True)
class ClassifyZConfig:
    aws_region: str = "us-west-2"
    dynamodb_endpoint_url: str | None = "http://localhost:8000"
    articles_table: str = "articles"
    starting_review: str | None = None
    review_count: int | None = None


def load_config(path: str | Path) -> ClassifyZConfig:
    load_repo_env()
    with Path(path).open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    env_defaults = {
        "aws_region": os.getenv("AWS_REGION", ClassifyZConfig.aws_region),
        "dynamodb_endpoint_url": os.getenv("DYNAMODB_ENDPOINT_URL", ClassifyZConfig.dynamodb_endpoint_url),
        "articles_table": os.getenv("ARTICLES_TABLE", ClassifyZConfig.articles_table),
    }
    allowed = set(ClassifyZConfig.__dataclass_fields__)
    values = {key: value for key, value in {**env_defaults, **raw}.items() if key in allowed}
    return ClassifyZConfig(**values)


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


def classify_article(article: dict[str, Any]) -> tuple[float | None, str, str]:
    kind = transform_kind(article.get("effect_measure"))
    estimate = parse_number(article.get("effect_estimate"))
    lower = parse_number(article.get("confidence_interval_begin"))
    upper = parse_number(article.get("confidence_interval_end"))
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
    return wald_z, wald_category(wald_z), ""


def run_classification(config: ClassifyZConfig) -> dict[str, Any]:
    resource = dynamodb_resource(config)
    table = resource.Table(config.articles_table)
    articles = sorted(scan_all(table), key=lambda item: (str(item.get("review_id") or ""), str(item.get("article_id") or "")))
    review_ids = selected_review_ids(articles, starting_review=config.starting_review, review_count=config.review_count)
    if review_ids is not None:
        articles = [article for article in articles if str(article.get("review_id") or "") in review_ids]

    counts: dict[str, int] = {}
    for index, article in enumerate(articles, start=1):
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
        counts[category] = counts.get(category, 0) + 1
        print(f"[{index}/{len(articles)}] {article.get('article_id')} {category}" + (f" ({error})" if error else ""))

    summary = {"article_count": len(articles), "category_counts": dict(sorted(counts.items()))}
    print(summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Classify article Wald-Z categories.")
    parser.add_argument("--config", default="config.classify_z.yml", help="Path to classify_z YAML config file.")
    args = parser.parse_args()
    run_classification(load_config(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
