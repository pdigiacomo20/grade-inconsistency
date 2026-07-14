from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path
import json
import os
import re
from typing import Any

from pipeline.env import load_repo_env


VALID_ANSWERS = {"y", "n", "m"}
RUN_FILENAME_RE = re.compile(r"^[A-Za-z0-9_.-]+\.json$")


def evaluations_dir(path: str | Path | None = None) -> Path:
    load_repo_env()
    return Path(path or os.getenv("EVALUATIONS_DIR", "data/evaluations"))


def clean_answer(value: Any) -> str:
    answer = str(value or "").strip().lower()
    return answer if answer in VALID_ANSWERS else ""


def is_valid_run_filename(filename: str) -> bool:
    return bool(RUN_FILENAME_RE.fullmatch(filename)) and "/" not in filename and "\\" not in filename


def run_path(filename: str, directory: str | Path | None = None) -> Path:
    if not is_valid_run_filename(filename):
        raise ValueError("Invalid evaluation filename.")
    return evaluations_dir(directory) / filename


def read_run(filename: str, directory: str | Path | None = None) -> dict[str, Any]:
    path = run_path(filename, directory)
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("filename", path.name)
    return data


def list_runs(directory: str | Path | None = None) -> list[dict[str, Any]]:
    root = evaluations_dir(directory)
    if not root.exists():
        return []
    runs: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), reverse=True):
        try:
            data = read_run(path.name, root)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        metadata = data.get("metadata", {})
        runs.append(
            {
                "filename": path.name,
                "task": data.get("task") or metadata.get("task") or "",
                "run_id": metadata.get("run_id") or data.get("run_id") or "",
                "model": metadata.get("model") or data.get("model") or "",
                "provider": metadata.get("provider") or data.get("provider") or "",
                "created_at": metadata.get("created_at") or data.get("created_at") or "",
                "metrics": compute_metrics(data),
            }
        )
    return runs


def _rate(memory_count: int, source_count: int) -> float | None:
    denominator = memory_count + source_count
    if denominator == 0:
        return None
    return memory_count / denominator


def _fraction(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def compute_metrics(run: dict[str, Any]) -> dict[str, Any]:
    memory = 0
    source = 0
    by_article_type: dict[str, Counter[str]] = defaultdict(Counter)
    by_parametric_and_article_type: dict[str, dict[str, Counter[str]]] = defaultdict(lambda: defaultdict(Counter))
    accuracy_by_article_type: dict[str, Counter[str]] = defaultdict(Counter)
    parametric_counts: Counter[str] = Counter()
    parametric_total = 0
    parametric_correct = 0
    contextual_total = 0
    contextual_correct = 0
    contextual_errors = 0

    for outcome in run.get("outcomes", []):
        ground_truth_answer = clean_answer(outcome.get("ground_truth_answer")) or "m"
        parametric_answer = clean_answer((outcome.get("parametric") or {}).get("answer"))
        if parametric_answer:
            parametric_counts[parametric_answer] += 1
            parametric_total += 1
            if parametric_answer == ground_truth_answer:
                parametric_correct += 1
        for item in outcome.get("contexts", []):
            contextual_answer = clean_answer(item.get("answer"))
            if not contextual_answer:
                contextual_errors += 1
                continue
            contextual_total += 1
            is_correct = contextual_answer == ground_truth_answer
            if is_correct:
                contextual_correct += 1
            item["accuracy_label"] = "correct" if is_correct else "incorrect"
            article_type = str(item.get("article_type") or "included_study")
            accuracy_by_article_type[article_type]["correct" if is_correct else "incorrect"] += 1
            if not parametric_answer:
                continue
            label = "memory" if contextual_answer == parametric_answer else "source"
            by_article_type[article_type][label] += 1
            by_parametric_and_article_type[parametric_answer or "unknown"][article_type][label] += 1
            if label == "memory":
                memory += 1
            else:
                source += 1
            item["memorization_label"] = label

    article_type_rates = {
        article_type: {
            "memory_count": counts["memory"],
            "source_count": counts["source"],
            "memorization_rate": _rate(counts["memory"], counts["source"]),
        }
        for article_type, counts in sorted(by_article_type.items())
    }
    article_type_accuracy = {
        article_type: {
            "correct_count": counts["correct"],
            "incorrect_count": counts["incorrect"],
            "accuracy": _fraction(counts["correct"], counts["correct"] + counts["incorrect"]),
        }
        for article_type, counts in sorted(accuracy_by_article_type.items())
    }
    cross_product = {
        answer: {
            article_type: {
                "memory_count": counts["memory"],
                "source_count": counts["source"],
                "memorization_rate": _rate(counts["memory"], counts["source"]),
            }
            for article_type, counts in sorted(article_type_counts.items())
        }
        for answer, article_type_counts in sorted(by_parametric_and_article_type.items())
    }
    parametric_distribution = {
        answer: {
            "count": parametric_counts[answer],
            "percentage": (parametric_counts[answer] / parametric_total) if parametric_total else None,
        }
        for answer in ("y", "n", "m")
    }
    return {
        "outcome_count": len(run.get("outcomes", [])),
        "parametric_total": parametric_total,
        "parametric_correct": parametric_correct,
        "parametric_accuracy": _fraction(parametric_correct, parametric_total),
        "contextual_total": contextual_total,
        "contextual_correct": contextual_correct,
        "contextual_accuracy": _fraction(contextual_correct, contextual_total),
        "contextual_errors": contextual_errors,
        "memory_count": memory,
        "source_count": source,
        "memorization_rate": _rate(memory, source),
        "memorization_rate_by_article_type": article_type_rates,
        "accuracy_by_article_type": article_type_accuracy,
        "parametric_distribution": parametric_distribution,
        "memorization_rate_by_parametric_answer_and_article_type": cross_product,
    }
