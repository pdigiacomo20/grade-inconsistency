from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
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
GROUND_TRUTH_ANSWER = "m"
MULTITURN_CHAR_DETAIL_TYPE = "multiturn_char"
GATEKEEPER_UNAVAILABLE = "Information is not available."
MULTITURN_CATEGORIES = ("Methods", "Intervention/Comparator", "Participants", "Outcomes", "Results")
CATEGORY_KEY_BY_LABEL = {
    "Methods": "methods",
    "Intervention/Comparator": "intervention_comparator",
    "Participants": "participants",
    "Outcomes": "outcomes",
    "Results": "results",
}


@dataclass(frozen=True)
class EvaluationConfig:
    task: str = "evaluation"
    run_id: str = "evaluation"
    provider: str = "openai"
    model: str = "gpt-5.5"
    evaluations_dir: str = "data/evaluations"
    aws_region: str = "us-west-2"
    dynamodb_endpoint_url: str | None = "http://localhost:8000"
    outcomes_table: str = "outcomes"
    articles_table: str = "articles"
    starting_review: str | None = None
    review_count: int | None = None
    max_questions: int | None = None
    max_outcomes: int | None = None
    max_contexts_per_outcome: int | None = None
    detail_exposure_types: tuple[str, ...] = ("abstract",)
    irrelevant_docs_per_context: int = 0
    maximum_follow_ups: int = 4
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
    if isinstance(values.get("detail_exposure_types"), list):
        values["detail_exposure_types"] = tuple(str(item) for item in values["detail_exposure_types"])
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


def openai_text(messages: list[dict[str, str]], *, config: EvaluationConfig) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    payload = {
        "model": config.model,
        "input": messages,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = requests.post(
        "https://api.openai.com/v1/responses",
        headers=headers,
        json=payload,
        timeout=config.request_timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return response_text(response.json())


def openai_answer(prompt: str, *, config: EvaluationConfig) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": "Answer medical multiple choice questions with exactly one lowercase character: y, n, or m."},
        {"role": "user", "content": prompt},
    ]
    last_error = ""
    for attempt in range(max(1, config.retry_count)):
        try:
            raw_text = openai_text(messages, config=config)
            return {"answer": parse_answer(raw_text), "raw_response": raw_text, "error": ""}
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = str(exc)
            if attempt + 1 < max(1, config.retry_count):
                time.sleep(2.0 * (attempt + 1))
    return {"answer": "", "raw_response": "", "error": last_error}


def model_answer(prompt: str, *, config: EvaluationConfig) -> dict[str, Any]:
    if config.provider != "openai":
        raise ValueError(f"Unsupported provider for evaluation: {config.provider}")
    return openai_answer(prompt, config=config)


def model_text(messages: list[dict[str, str]], *, config: EvaluationConfig) -> str:
    if config.provider != "openai":
        raise ValueError(f"Unsupported provider for evaluation: {config.provider}")
    last_error = ""
    for attempt in range(max(1, config.retry_count)):
        try:
            return openai_text(messages, config=config)
        except (requests.RequestException, RuntimeError) as exc:
            last_error = str(exc)
            if attempt + 1 < max(1, config.retry_count):
                time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(last_error)


def prompt_parametric(question: str) -> str:
    return f"Respond to the following question with a single character: y, n, or m, corresponding to yes, no, or maybe. Question: {question}"


def prompt_contextual(question: str, citation: str, detail_label: str, detail_text: str, distractors: list[dict[str, str]] | None = None) -> str:
    context_parts = [f"Source document\nCitation: {citation}\n\n{detail_label}: {detail_text.strip()}"]
    for index, distractor in enumerate(distractors or [], start=1):
        context_parts.append(
            f"Distractor document {index}\n"
            f"Citation: {distractor.get('citation', '')}\n\n"
            f"{distractor.get('detail_label', 'Abstract')}: {distractor.get('detail_text', '').strip()}"
        )
    context = "\n\n---\n\n".join(context_parts)
    return (
        "Respond to the following question with a single character: y, n, or m, corresponding to yes, no, or maybe. "
        "You may use the provided context below to inform your response.\n\n"
        f"Question: {question}\n\n"
        f"Context: {context}"
    )


def normalized_detail_type(detail_type: str) -> str:
    return detail_type.strip().lower().replace("-", "_")


def stable_int(*parts: Any) -> int:
    text = "::".join(str(part or "") for part in parts)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return int(digest[:16], 16)


def maybe_text(value: Any) -> str:
    return str(value or "").strip()


def study_results(article: dict[str, Any]) -> str:
    fields = [
        ("Effect measure", article.get("effect_measure")),
        ("Unit", article.get("unit_of_measure")),
        ("Polarity", article.get("polarity_of_measure")),
        ("Comparator effect", article.get("comparator_effect_measure") or article.get("line_of_no_effect")),
        ("Effect estimate", article.get("effect_estimate")),
        ("Confidence interval begin", article.get("confidence_interval_begin")),
        ("Confidence interval end", article.get("confidence_interval_end")),
        ("Confidence interval percentage", article.get("confidence_interval_percentage")),
        ("Sample size", article.get("sample_size")),
    ]
    lines = [f"{label}: {maybe_text(value)}" for label, value in fields if maybe_text(value)]
    return "\n".join(lines)


def study_detail_sections(article: dict[str, Any]) -> dict[str, str]:
    return {
        "participants": maybe_text(article.get("characteristics_participants")),
        "methods": maybe_text(article.get("characteristics_methods")),
        "intervention_comparator": maybe_text(article.get("characteristics_interventions")),
        "outcomes": maybe_text(article.get("characteristics_outcomes")),
        "results": study_results(article),
    }


def section_label(section_name: str) -> str:
    labels = {
        "methods": "Methods",
        "intervention_comparator": "Intervention/Comparator",
        "participants": "Participants",
        "outcomes": "Outcomes",
        "results": "Results",
    }
    return labels.get(section_name, section_name)


def normalize_multiturn_category(value: Any) -> tuple[str, str]:
    normalized = re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()
    aliases = {
        "method": "Methods",
        "methods": "Methods",
        "intervention": "Intervention/Comparator",
        "interventions": "Intervention/Comparator",
        "intervention comparator": "Intervention/Comparator",
        "interventions comparator": "Intervention/Comparator",
        "intervention comparators": "Intervention/Comparator",
        "interventions comparators": "Intervention/Comparator",
        "comparator": "Intervention/Comparator",
        "comparators": "Intervention/Comparator",
        "participants": "Participants",
        "participant": "Participants",
        "population": "Participants",
        "outcome": "Outcomes",
        "outcomes": "Outcomes",
        "result": "Results",
        "results": "Results",
    }
    label = aliases.get(normalized, "")
    return label, CATEGORY_KEY_BY_LABEL.get(label, "")


def format_study_sections(study_id: str, sections: dict[str, str], section_names: list[str]) -> str:
    header = [f"Study ID: {study_id}"]
    body: list[str] = []
    for name in section_names:
        text = sections.get(name, "")
        if text:
            body.append(f"{section_label(name)}:\n{text}")
    return "\n".join(header) + "\n\n" + "\n\n".join(body)


def initial_multiturn_sections(article: dict[str, Any], outcome: dict[str, Any]) -> tuple[list[str], list[str]]:
    if stable_int(outcome.get("pmid"), outcome.get("outcome_id"), article.get("article_id"), "initial") % 2 == 0:
        return ["participants", "methods", "intervention_comparator"], ["outcomes", "results"]
    return ["outcomes", "results"], ["participants", "methods", "intervention_comparator"]


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    match = re.search(r"\{.*\}", stripped, re.S)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def parse_multiturn_response(text: str) -> dict[str, Any]:
    data = extract_json_object(text)
    if data:
        action = str(data.get("action") or "").strip().lower()
        if action == "answer":
            return {"action": "answer", "answer": parse_answer(str(data.get("answer") or "")), "questions": []}
        if action == "follow_up":
            questions = data.get("questions") or []
            if isinstance(questions, dict):
                questions = [questions]
            parsed_questions = []
            for item in questions:
                if not isinstance(item, dict):
                    continue
                study_id = str(item.get("study_id") or "").strip()
                question = str(item.get("question") or "").strip()
                category_label, category_key = normalize_multiturn_category(item.get("category"))
                if study_id and question:
                    parsed_questions.append(
                        {
                            "study_id": study_id,
                            "category": category_label,
                            "category_key": category_key,
                            "question": question,
                        }
                    )
            if parsed_questions:
                return {"action": "follow_up", "answer": "", "questions": parsed_questions}
    return {"action": "answer", "answer": parse_answer(text), "questions": []}


def gatekeeper_response(
    request: dict[str, str],
    *,
    study_states: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    study_id = request["study_id"]
    state = study_states.get(study_id)
    if not state:
        return {
            **request,
            "response": GATEKEEPER_UNAVAILABLE,
            "classification": "unavailable",
            "revealed_section": "",
            "gatekeeper": {"mode": "deterministic_category", "raw_response": GATEKEEPER_UNAVAILABLE},
        }

    category = request.get("category") or ""
    category_key = request.get("category_key") or ""
    if not category_key:
        response = GATEKEEPER_UNAVAILABLE
        return {
            **request,
            "response": response,
            "classification": "unavailable",
            "revealed_section": "",
            "gatekeeper": {"mode": "deterministic_category", "selected_category": category, "raw_response": response},
        }

    exposed_names = [*state["exposed_names"], *state["revealed_names"]]
    if category_key in exposed_names:
        response = f"Complete {category} information provided."
        return {
            **request,
            "response": response,
            "classification": "prior",
            "revealed_section": "",
            "gatekeeper": {"mode": "deterministic_category", "selected_category": category, "raw_response": response},
        }

    if category_key in state["hidden_names"] and state["sections"].get(category_key):
        state["revealed_names"].append(category_key)
        response = f"{category}:\n{state['sections'][category_key]}"
        return {
            **request,
            "response": response,
            "classification": "hidden",
            "revealed_section": category_key,
            "gatekeeper": {"mode": "deterministic_category", "selected_category": category, "raw_response": response},
        }

    response = GATEKEEPER_UNAVAILABLE
    return {
        **request,
        "response": response,
        "classification": "unavailable",
        "revealed_section": "",
        "gatekeeper": {"mode": "deterministic_category", "selected_category": category, "raw_response": response},
    }


def prompt_multiturn_initial(question: str, studies_context: str, maximum_follow_ups: int) -> str:
    categories = ", ".join(MULTITURN_CATEGORIES)
    return (
        "Respond to the following medical multiple-choice question with either a final answer or follow-up questions.\n"
        "Use exactly one of these JSON templates and no other text:\n"
        "{\"action\":\"answer\",\"answer\":\"y|n|m\"}\n"
        "{\"action\":\"follow_up\",\"questions\":[{\"study_id\":\"ART_00001\",\"category\":\"Methods|Intervention/Comparator|Participants|Outcomes|Results\",\"question\":\"...\"}]}\n\n"
        "Answer choices are y=yes, n=no, and m=maybe. Ask follow-up questions only when needed. "
        "Direct each follow-up question to one individual study using its study_id. "
        f"For each follow-up question, choose exactly one category from: {categories}. "
        "Ask as many follow-up questions as are necessary, including additional follow-ups if prior answers are insufficient. "
        f"You may ask at most {maximum_follow_ups} rounds of follow-up questions; after that, you must answer.\n\n"
        f"Question: {question}\n\n"
        f"Study context:\n{studies_context}"
    )


def prompt_multiturn_force_answer(question: str) -> str:
    return (
        "You have reached the maximum number of follow-up rounds. "
        "Answer now using exactly this JSON template and no other text: "
        "{\"action\":\"answer\",\"answer\":\"y|n|m\"}\n\n"
        f"Question: {question}"
    )


def run_multiturn_char(
    question: str,
    outcome: dict[str, Any],
    source_studies: list[dict[str, Any]],
    *,
    config: EvaluationConfig,
) -> dict[str, Any]:
    study_states: dict[str, dict[str, Any]] = {}
    context_parts: list[str] = []
    for index, article in enumerate(source_studies, start=1):
        study_id = str(article.get("article_id") or f"ART_{index:05d}")
        sections = study_detail_sections(article)
        exposed_names, hidden_names = initial_multiturn_sections(article, outcome)
        exposed_names = [name for name in exposed_names if sections.get(name)]
        hidden_names = [name for name in hidden_names if sections.get(name)]
        if not exposed_names and hidden_names:
            exposed_names = [hidden_names.pop(0)]
        if not exposed_names:
            continue
        study_states[study_id] = {
            "article": article,
            "sections": sections,
            "exposed_names": exposed_names,
            "hidden_names": hidden_names,
            "revealed_names": [],
        }
        context_parts.append(format_study_sections(study_id, sections, exposed_names))

    if not study_states:
        return {"answer": "", "raw_response": "", "error": "No usable study characteristics/results for multiturn_char.", "multiturn": {"turns": []}}

    initial_prompt = prompt_multiturn_initial(question, "\n\n---\n\n".join(context_parts), config.maximum_follow_ups)
    messages = [
        {"role": "system", "content": "You answer medical multiple-choice questions. Follow the requested JSON response templates exactly."},
        {"role": "user", "content": initial_prompt},
    ]
    turns: list[dict[str, Any]] = []
    final_raw = ""
    final_answer = ""
    error = ""
    for follow_up_round in range(config.maximum_follow_ups + 1):
        if follow_up_round == config.maximum_follow_ups:
            messages.append({"role": "user", "content": prompt_multiturn_force_answer(question)})
        llm_request = {"messages": [dict(message) for message in messages]}
        try:
            raw = model_text(messages, config=config)
            parsed = parse_multiturn_response(raw)
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
            final_raw = ""
            break
        turn: dict[str, Any] = {"round": follow_up_round + 1, "llm_request": llm_request, "raw_response": raw, "parsed": parsed}
        turns.append(turn)
        messages.append({"role": "assistant", "content": raw})
        if parsed["action"] == "answer":
            final_raw = raw
            final_answer = parsed["answer"]
            break
        if follow_up_round == config.maximum_follow_ups:
            final_raw = raw
            error = "Model requested follow-up after maximum follow-up rounds."
            break
        gatekeeper_responses = [
            gatekeeper_response(request, study_states=study_states)
            for request in parsed["questions"]
        ]
        turn["gatekeeper_responses"] = gatekeeper_responses
        response_text_for_model = "\n\n".join(
            f"Study {item['study_id']} category: {item.get('category') or 'unavailable'}\n"
            f"Question: {item['question']}\n"
            f"Gatekeeper response: {item['response']}"
            for item in gatekeeper_responses
        )
        messages.append({"role": "user", "content": f"Gatekeeper responses:\n{response_text_for_model}\n\nContinue with one JSON response template."})

    primary_state = next(iter(study_states.values()))
    primary_article = primary_state["article"]
    return {
        "article_id": primary_article.get("article_id"),
        "article_type": primary_article.get("article_type", "included_study"),
        "citation": primary_article.get("citation"),
        "title": primary_article.get("title"),
        "pmid": primary_article.get("pmid"),
        "abstract_path": primary_article.get("abstract_path"),
        "full_text_path": primary_article.get("full_text_path"),
        "wald_z": primary_article.get("wald_z"),
        "wald_z_category": primary_article.get("wald_z_category"),
        "detail_exposure_type": MULTITURN_CHAR_DETAIL_TYPE,
        "irrelevant_doc_count": 0,
        "answer": final_answer,
        "raw_response": final_raw,
        "error": error,
        "multiturn": {
            "maximum_follow_ups": config.maximum_follow_ups,
            "study_count": len(study_states),
            "studies": {
                study_id: {
                    "article_id": state["article"].get("article_id"),
                    "initially_exposed_sections": state["exposed_names"],
                    "initially_hidden_sections": state["hidden_names"],
                    "revealed_sections": state["revealed_names"],
                }
                for study_id, state in study_states.items()
            },
            "turns": turns,
        },
    }


def read_abstract(article: dict[str, Any]) -> str:
    path = Path(str(article.get("abstract_path") or ""))
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def read_full_text(article: dict[str, Any]) -> str:
    path = Path(str(article.get("full_text_path") or ""))
    if not path.exists() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def csr_sort_key(value: Any) -> tuple[int, str]:
    text = str(value or "")
    match = re.fullmatch(r"CSR_(\d+)", text)
    return (int(match.group(1)) if match else 10**12, text)


def selected_review_ids(items: list[dict[str, Any]], *, starting_review: str | None, review_count: int | None) -> set[str] | None:
    if not starting_review and not review_count:
        return None
    review_ids = sorted({str(item.get("review_id") or "") for item in items if item.get("review_id")}, key=csr_sort_key)
    if starting_review:
        start_key = csr_sort_key(starting_review)
        review_ids = [review_id for review_id in review_ids if csr_sort_key(review_id) >= start_key]
    if review_count:
        review_ids = review_ids[:review_count]
    return set(review_ids)


def is_very_low_certainty(value: Any) -> bool:
    normalized = re.sub(r"[^a-z]+", " ", str(value or "").lower())
    return "very low" in normalized


def article_detail(article: dict[str, Any], detail_type: str) -> tuple[str, str]:
    normalized = normalized_detail_type(detail_type)
    if normalized == "full_text":
        return "Full text", read_full_text(article)
    return "Abstract", read_abstract(article)


def choose_distractors(
    articles: list[dict[str, Any]],
    *,
    source_article: dict[str, Any],
    detail_type: str,
    count: int,
) -> list[dict[str, str]]:
    if count <= 0:
        return []
    selected: list[dict[str, str]] = []
    for article in sorted(articles, key=lambda item: str(item.get("article_id") or "")):
        if str(article.get("article_id") or "") == str(source_article.get("article_id") or ""):
            continue
        if str(article.get("review_pmid") or "") == str(source_article.get("review_pmid") or "") and int(article.get("outcome_id") or 0) == int(source_article.get("outcome_id") or 0):
            continue
        detail_label, detail_text = article_detail(article, detail_type)
        if not detail_text:
            continue
        selected.append({"citation": str(article.get("citation") or ""), "detail_label": detail_label, "detail_text": detail_text})
        if len(selected) >= count:
            break
    return selected


def write_run_snapshot(destination: Path, run: dict[str, Any]) -> None:
    run["metrics"] = compute_metrics(run)
    destination.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_evaluation(config: EvaluationConfig) -> dict[str, Any]:
    resource = dynamodb_resource(config)
    outcomes = sorted(
        scan_all(resource.Table(config.outcomes_table)),
        key=lambda item: (str(item.get("review_id") or ""), str(item.get("pmid") or ""), int(item.get("outcome_id", 0))),
    )
    review_ids = selected_review_ids(outcomes, starting_review=config.starting_review, review_count=config.review_count)
    if review_ids is not None:
        outcomes = [outcome for outcome in outcomes if str(outcome.get("review_id") or "") in review_ids]
    before_certainty_filter = len(outcomes)
    outcomes = [outcome for outcome in outcomes if is_very_low_certainty(outcome.get("certainty"))]
    skipped_for_certainty = before_certainty_filter - len(outcomes)
    if skipped_for_certainty:
        print(f"Skipping {skipped_for_certainty} outcomes without very low certainty.")
    question_limit = config.max_questions or config.max_outcomes
    if question_limit:
        outcomes = outcomes[: question_limit]
    articles = [
        article
        for article in scan_all(resource.Table(config.articles_table))
        if article.get("article_type", "included_study") == "included_study"
    ]
    articles_by_outcome: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for article in articles:
        key = (str(article.get("review_pmid") or ""), int(article.get("outcome_id") or 0))
        articles_by_outcome.setdefault(key, []).append(article)

    started_at = datetime.now(UTC).isoformat()
    safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", config.run_id).strip("._") or "evaluation"
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{safe_run_id}-{timestamp}.json"
    root = evaluations_dir(config.evaluations_dir)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / filename
    results: list[dict[str, Any]] = []
    run = {
        "task": config.task,
        "metadata": {
            "task": config.task,
            "run_id": config.run_id,
            "provider": config.provider,
            "model": config.model,
            "created_at": started_at,
            "finished_at": "",
            "filename": filename,
            "status": "running",
            "completed_outcomes": 0,
            "total_outcomes": len(outcomes),
            "outcomes_table": config.outcomes_table,
            "articles_table": config.articles_table,
            "starting_review": config.starting_review,
            "review_count": config.review_count,
            "max_questions": config.max_questions,
            "target_certainty": "very low",
            "ground_truth_answer": GROUND_TRUTH_ANSWER,
            "skipped_outcomes_not_very_low": skipped_for_certainty,
            "detail_exposure_types": list(config.detail_exposure_types),
            "irrelevant_docs_per_context": config.irrelevant_docs_per_context,
            "maximum_follow_ups": config.maximum_follow_ups,
        },
        "outcomes": results,
    }
    write_run_snapshot(destination, run)
    for index, outcome in enumerate(outcomes, start=1):
        question = str(outcome.get("question") or "")
        print(f"[{index}/{len(outcomes)}] PMID {outcome.get('pmid')} outcome {outcome.get('outcome_id')} accuracy target={GROUND_TRUTH_ANSWER}")
        parametric = model_answer(prompt_parametric(question), config=config)
        contexts: list[dict[str, Any]] = []
        source_articles = [
            article
            for article in sorted(articles_by_outcome.get((str(outcome.get("pmid")), int(outcome.get("outcome_id") or 0)), []), key=lambda item: str(item.get("article_id") or ""))
        ]
        if config.max_contexts_per_outcome:
            source_articles = source_articles[: config.max_contexts_per_outcome]
        for article in source_articles:
            for detail_type in config.detail_exposure_types:
                if normalized_detail_type(detail_type) == MULTITURN_CHAR_DETAIL_TYPE:
                    print(f"  context {article.get('article_id')} ({detail_type})")
                    multiturn_context = run_multiturn_char(question, outcome, [article], config=config)
                    if multiturn_context.get("article_id"):
                        contexts.append(multiturn_context)
                    continue
                detail_label, detail_text = article_detail(article, detail_type)
                if not detail_text:
                    continue
                print(f"  context {article.get('article_id')} ({detail_type})")
                distractors = choose_distractors(
                    articles,
                    source_article=article,
                    detail_type=detail_type,
                    count=config.irrelevant_docs_per_context,
                )
                answer = model_answer(
                    prompt_contextual(question, str(article.get("citation") or ""), detail_label, detail_text, distractors),
                    config=config,
                )
                contexts.append(
                    {
                        "article_id": article.get("article_id"),
                        "article_type": article.get("article_type", "included_study"),
                        "citation": article.get("citation"),
                        "title": article.get("title"),
                        "pmid": article.get("pmid"),
                        "abstract_path": article.get("abstract_path"),
                        "full_text_path": article.get("full_text_path"),
                        "wald_z": article.get("wald_z"),
                        "wald_z_category": article.get("wald_z_category"),
                        "detail_exposure_type": detail_type,
                        "irrelevant_doc_count": len(distractors),
                        **answer,
                    }
                )
        results.append(
            {
                "pmid": outcome.get("pmid"),
                "review_id": outcome.get("review_id"),
                "outcome_id": outcome.get("outcome_id"),
                "question": question,
                "ground_truth_answer": GROUND_TRUTH_ANSWER,
                "benchmark_mc_answer": GROUND_TRUTH_ANSWER,
                "source_mc_answer": outcome.get("mc_answer"),
                "consensus_answer": outcome.get("consensus_answer"),
                "certainty": outcome.get("certainty"),
                "parametric": parametric,
                "contexts": contexts,
            }
        )
        run["metadata"]["completed_outcomes"] = len(results)
        write_run_snapshot(destination, run)

    finished_at = datetime.now(UTC).isoformat()
    run["metadata"]["finished_at"] = finished_at
    run["metadata"]["status"] = "complete"
    run["metadata"]["completed_outcomes"] = len(results)
    write_run_snapshot(destination, run)
    print(f"Wrote {destination}")
    print(json.dumps(run["metrics"], indent=2, sort_keys=True))
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description="Run LLM evaluation.")
    parser.add_argument("--config", default="config.evaluation.yml", help="Path to evaluation YAML config file.")
    args = parser.parse_args()
    run_evaluation(load_config(args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
