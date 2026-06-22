from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import yaml

from pipeline.env import load_repo_env


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int | None) -> int | None:
    value = os.getenv(name)
    if value is None or value.strip().lower() in {"", "none", "null"}:
        return default
    return int(value)


@dataclass(frozen=True)
class PipelineConfig:
    limit: int = 25
    pause_seconds: float = 0.8
    create_tables: bool = True
    force_reprocess: bool = False
    abstract_text_dir: str = "data/articles/abstracts"
    full_text_dir: str = "data/articles/full_text"
    aws_region: str = "us-west-2"
    dynamodb_endpoint_url: str | None = "http://localhost:8000"
    reviews_table: str = "reviews"
    outcomes_table: str = "outcomes"
    articles_table: str = "articles"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.5"
    openai_timeout_seconds: int = 300


def load_config(path: str | Path) -> PipelineConfig:
    load_repo_env()
    env_defaults: dict[str, Any] = {
        "aws_region": os.getenv("AWS_REGION", PipelineConfig.aws_region),
        "dynamodb_endpoint_url": os.getenv("DYNAMODB_ENDPOINT_URL", PipelineConfig.dynamodb_endpoint_url),
        "reviews_table": os.getenv("REVIEWS_TABLE", PipelineConfig.reviews_table),
        "outcomes_table": os.getenv("OUTCOMES_TABLE", PipelineConfig.outcomes_table),
        "articles_table": os.getenv("ARTICLES_TABLE", PipelineConfig.articles_table),
        "abstract_text_dir": os.getenv("ABSTRACT_TEXT_DIR", PipelineConfig.abstract_text_dir),
        "full_text_dir": os.getenv("FULL_TEXT_DIR", PipelineConfig.full_text_dir),
        "openai_api_key": os.getenv("OPENAI_API_KEY"),
        "openai_model": os.getenv("OPENAI_MODEL", PipelineConfig.openai_model),
        "openai_timeout_seconds": _env_int("OPENAI_TIMEOUT_SECONDS", PipelineConfig.openai_timeout_seconds),
    }
    with Path(path).open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    return PipelineConfig(**{**env_defaults, **raw})
