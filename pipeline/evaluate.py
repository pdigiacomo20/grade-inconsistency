from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Any

import boto3
import requests
import yaml

from pipeline.dynamodb import _from_dynamodb_value
from pipeline.env import load_repo_env
from pipeline.evaluations import clean_answer, compute_metrics, evaluations_dir


ANSWER_RE = re.compile(r"\b([ynm])\b", re.IGNORECASE)


@dataclass(frozen=True)
class EvaluationConfig:
    task: str = "TASK2A"
    run_id: str = "TASK2A"
    provider: str = "openai"
    model: str = "gpt-5.5"
    evaluations_dir: str = "data/evaluations"
    aws_region: str = "us-west-2"
    dynamodb_endpoint_url: str | None = "http://localhost:8000"
    outcomes_table: str = "outcomes"
    articles_table: str = "articles"
    max_questions: int | None = None
    max_outcomes: int | None = None
    max_contexts_per_outcome: int | None = None
    request_timeout_seconds: int = 120
    retry_count: int = 3


def load_config(path: str | Path) -> EvaluationConfig:
    load_repo_env()
    with Path(path).open("r", encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    env_defaults = {
        "model": os.getenv("OPENAI_MODEL", EvaluationConfig.model),
        "aws_region": os.getenv("AWS_REGION", EvaluationConfig.aws_region),
        "dynamodb_endpoint_url": os.getenv("DYNAMODB_ENDPOINT_URL", EvaluationConfig.dynamodb_endpoint_url),
        "outcomes_table": os.getenv("OUTCOMES_TABLE", EvaluationConfig.outcomes_table),
        "articles_table": os.getenv("ARTICLES_TABLE", EvaluationConfig.articles_table),
        "evaluations_dir": os.getenv("EVALUATIONS_DIR", EvaluationConfig.evaluations_dir),
    }
    allowed = set(EvaluationConfig.__dataclass_fields__)
    values = {key: value for key, value in {**env_defaults, **raw}.items() if key in allowed}
    return EvaluationConfig(**values)


def dynamodb_resource(config: EvaluationConfig) -> Any:
    kwargs: dict[str, Any] = {"region_name": config.aws_region, "endpoint_url": config.dynamodb_endpoint_url}
    if config.dynamodb_endpoint_url:
        kwargs.update({"aws_access_key_id": "local", "aws_secret_access_key": "local"})
    return boto3.resource("dynamodb", **kwargs)


def scan_all(table: Any) -> list[dict[str, Any]]:
    response = table.scan()
    items = response.get("Items", [])
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    return [_from_dynamodb_value(item) for item in items]


def response_text(data: dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"])
    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def parse_answer(text: str) -> str:
    answer = clean_answer(text)
    if answer:
        return answer
    match = ANSWER_RE.search(text.strip().lower())
    if match:
        return match.group(1).lower()
    raise ValueError(f"Model did not return y, n, or m: {text!r}")


def openai_answer(prompt: str, *, config: EvaluationConfig) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    payload = {
        "model": config.model,
        "input": [
            {"role": "system", "content": "Answer medical multiple choice questions with exactly one lowercase character: y, n, or m."},
            {"role": "user", "content": prompt},
        ],
        "max_output_tokens": 16,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = ""
    for attempt in range(max(1, config.retry_count)):
        try:
            response = requests.post(
                "https://api.openai.com/v1/responses",
                headers=headers,
                json=payload,
                timeout=config.request_timeout_seconds,
            )
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                time.sleep(2.0 * (attempt + 1))
                continue
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
            raw_text = response_text(response.json())
            return {"answer": parse_answer(raw_text), "raw_response": raw_text, "error": ""}
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = str(exc)
            if attempt + 1 < max(1, config.retry_count):
                time.sleep(2.0 * (attempt + 1))
    return {"answer": "", "raw_response": "", "error": last_error}


def model_answer(prompt: str, *, config: EvaluationConfig) -> dict[str, Any]:
    if config.provider != "openai":
        raise ValueError(f"Unsupported provider for TASK2A: {config.provider}")
    return openai_answer(prompt, config=config)


def prompt_parametric(question: str) -> str:
    return f"Respond to the following question with a single character: y, n, or m, corresponding to yes, no, or maybe. Question: {question}"


def prompt_contextual(question: str, citation: str, abstract: str) -> str:
    context = f"Citation: {citation}\n\nAbstract: {abstract.strip()}"
    return (
        "Respond to the following question with a single character: y, n, or m, corresponding to yes, no, or maybe. "
        "You may use the provided context below to inform your response.\n\n"
        f"Question: {question}\n\n"
        f"Context: {context}"
    )


def read_abstract(article: dict[str, Any]) -> str:
    path = Path(str(article.get("abstract_path") or ""))
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def run_evaluation(config: EvaluationConfig) -> dict[str, Any]:
    resource = dynamodb_resource(config)
    outcomes = sorted(
        scan_all(resource.Table(config.outcomes_table)),
        key=lambda item: (str(item.get("review_id") or ""), str(item.get("pmid") or ""), int(item.get("outcome_id", 0))),
    )
    question_limit = config.max_questions or config.max_outcomes
    if question_limit:
        outcomes = outcomes[: question_limit]
    articles = scan_all(resource.Table(config.articles_table))
    articles_by_outcome: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for article in articles:
        key = (str(article.get("review_pmid") or ""), int(article.get("outcome_id") or 0))
        articles_by_outcome.setdefault(key, []).append(article)

    started_at = datetime.now(UTC).isoformat()
    results: list[dict[str, Any]] = []
    for index, outcome in enumerate(outcomes, start=1):
        question = str(outcome.get("question") or "")
        print(f"[{index}/{len(outcomes)}] PMID {outcome.get('pmid')} outcome {outcome.get('outcome_id')} parametric")
        parametric = model_answer(prompt_parametric(question), config=config)
        contexts: list[dict[str, Any]] = []
        source_articles = [
            article
            for article in sorted(articles_by_outcome.get((str(outcome.get("pmid")), int(outcome.get("outcome_id") or 0)), []), key=lambda item: str(item.get("article_id") or ""))
            if article.get("abstract_path")
        ]
        if config.max_contexts_per_outcome:
            source_articles = source_articles[: config.max_contexts_per_outcome]
        for article in source_articles:
            abstract = read_abstract(article)
            if not abstract:
                continue
            print(f"  context {article.get('article_id')} ({article.get('stance')})")
            answer = model_answer(prompt_contextual(question, str(article.get("citation") or ""), abstract), config=config)
            contexts.append(
                {
                    "article_id": article.get("article_id"),
                    "stance": article.get("stance"),
                    "citation": article.get("citation"),
                    "title": article.get("title"),
                    "pmid": article.get("pmid"),
                    "abstract_path": article.get("abstract_path"),
                    **answer,
                }
            )
        results.append(
            {
                "pmid": outcome.get("pmid"),
                "review_id": outcome.get("review_id"),
                "outcome_id": outcome.get("outcome_id"),
                "question": question,
                "benchmark_mc_answer": outcome.get("mc_answer"),
                "consensus_answer": outcome.get("consensus_answer"),
                "certainty": outcome.get("certainty"),
                "parametric": parametric,
                "contexts": contexts,
            }
        )

    finished_at = datetime.now(UTC).isoformat()
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", config.run_id).strip("._") or "TASK2A"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{safe_run_id}-{timestamp}.json"
    run = {
        "task": config.task,
        "metadata": {
            "task": config.task,
            "run_id": config.run_id,
            "provider": config.provider,
            "model": config.model,
            "created_at": started_at,
            "finished_at": finished_at,
            "filename": filename,
            "outcomes_table": config.outcomes_table,
            "articles_table": config.articles_table,
        },
        "outcomes": results,
    }
    run["metrics"] = compute_metrics(run)
    root = evaluations_dir(config.evaluations_dir)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / filename
    destination.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {destination}")
    print(json.dumps(run["metrics"], indent=2, sort_keys=True))
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="Run TASK2A memorization-ratio evaluations.")
    parser.add_argument("--config", default="config.yml", help="Path to TASK2A YAML config file.")
    args = parser.parse_args()
    run_evaluation(load_config(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
