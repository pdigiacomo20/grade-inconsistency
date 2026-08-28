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
COMPLETE_CHAR_DETAIL_TYPE = "complete_char"
GATEKEEPER_UNAVAILABLE = "Information is not available."
GATEKEEPER_TYPE_CATEGORICAL = "categorical_gatkeeper"
GATEKEEPER_TYPE_FREE_RESPONSE = "free_response"
GATEKEEPER_TYPE_ALIASES = {
    "categorical_gatekeeper": GATEKEEPER_TYPE_CATEGORICAL,
    GATEKEEPER_TYPE_CATEGORICAL: GATEKEEPER_TYPE_CATEGORICAL,
    GATEKEEPER_TYPE_FREE_RESPONSE: GATEKEEPER_TYPE_FREE_RESPONSE,
}
MULTITURN_CATEGORIES = ("Methods", "Intervention/Comparator", "Participants", "Outcomes", "Results")
COMPLETE_CHAR_SECTION_NAMES = ("methods", "intervention_comparator", "participants", "outcomes", "results")
MULTITURN_CATEGORY_DEFINITIONS = """Methods
Study design:
- Parallel, factorial, crossover, cluster aspects of design for randomized trials, and/or study design features for non-randomized studies
- Single or multicentre study; if multicentre, number of recruiting centres
- Recruitment and sampling procedures used, including at the level of individual participants and clusters/sites if relevant
- Enrolment start and end dates; length of participant follow-up
- Details of random sequence generation, allocation sequence concealment, and masking for randomized trials, and methods used to prevent and control for confounding, selection biases, and information biases for non-randomized studies
- Methods used to prevent and address missing data
- Likelihood of reporting and other biases
- Source(s) of funding or other material support for the study
- Authors' financial relationship and other potential conflicts of interest
Statistical analysis:
- Unit of analysis, e.g. individual participant, clinic, village, body part
- Statistical methods used if computed effect estimates are extracted from reports, including any covariates included in the statistical model

Participants
- Setting
- Region(s) and country/countries from which study participants were recruited
- Study eligibility criteria, including diagnostic criteria
- Characteristics of participants at the beginning or baseline of the study, e.g. age, sex, comorbidity, socio-economic status

Intervention/Comparator
- Description of the intervention(s) and comparison intervention(s), ideally with sufficient detail for replication
- Components, routes of delivery, doses, timing, frequency, intervention protocols, length of intervention
- Factors relevant to implementation, e.g. staff qualifications, equipment requirements
- Integrity of interventions, i.e. the degree to which specified procedures or components of the intervention were implemented as planned
- Description of co-interventions
- Definition of control groups, e.g. no intervention, placebo, minimally active comparator, or components of usual care
- Components, dose, timing, frequency
- For observational studies: description of how intervention status was assessed; length of exposure, cumulative exposure

Outcomes
For each pre-specified outcome domain in the systematic review:
- Whether there is evidence that the outcome domain was assessed, especially important if the outcome was assessed but the results not presented
- Measurement tool or instrument, including definition of clinical outcomes or endpoints
- For a scale, name of the scale, upper and lower limits, and whether a high or low score is favourable, and definitions of thresholds if appropriate
- Specific metric, e.g. post-intervention anxiety, change in anxiety from baseline to a post-intervention time point, or post-intervention presence of anxiety
- Method of aggregation, e.g. mean and standard deviation of anxiety scores in each group, or proportion of people with anxiety
- Timing of outcome measurements, e.g. assessments at end of eight-week intervention period, events occurring during the eight-week intervention period
- Adverse outcomes need special attention depending on whether they are collected systematically or non-systematically, e.g. by voluntary report

Results
- For each group, and for each outcome at each time point: number of participants randomly assigned and included in the analysis; and number of participants who withdrew, were lost to follow-up or were excluded, with reasons for each
- Summary data for each group, e.g. 2x2 table for dichotomous data; means and standard deviations for continuous data
- Between-group estimates that quantify the effect of the intervention on the outcome, and their precision, e.g. risk ratio, odds ratio, mean difference
- If subgroup analysis is planned, the same information would need to be extracted for each participant subgroup"""
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
    gatekeeper_type: str = GATEKEEPER_TYPE_CATEGORICAL
    request_timeout_seconds: int = 120
    retry_count: int = 3
    max_tokens: int = 4096
    model_parameters: dict[str, Any] | None = None
    endpoint_env: str | None = None


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
    if "gatekeeper_type" in values:
        values["gatekeeper_type"] = normalize_gatekeeper_type(values["gatekeeper_type"])
    return EvaluationConfig(**values)


def normalize_gatekeeper_type(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    gatekeeper_type = GATEKEEPER_TYPE_ALIASES.get(normalized)
    if not gatekeeper_type:
        supported = ", ".join(sorted(GATEKEEPER_TYPE_ALIASES))
        raise ValueError(f"Unsupported gatekeeper_type {value!r}. Supported values: {supported}")
    return gatekeeper_type


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


def chat_completion_text(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "\n".join(parts)
    return str(content or "")


def anthropic_response_text(data: dict[str, Any]) -> str:
    parts = []
    for item in data.get("content", []):
        if item.get("type") == "text" and item.get("text"):
            parts.append(str(item["text"]))
    return "\n".join(parts)


def parse_answer(text: str) -> str:
    answer = clean_answer(text)
    if answer:
        return answer
    match = ANSWER_RE.search(text.strip().lower())
    if match:
        return match.group(1).lower()
    raise ValueError(f"Model did not return y, n, or m: {text!r}")


def merged_model_parameters(config: EvaluationConfig) -> dict[str, Any]:
    model = config.model.lower()
    provider = config.provider.lower()
    params: dict[str, Any] = {}
    if provider == "anthropic":
        if model.startswith("claude-fable-5"):
            params["output_config"] = {"effort": "high"}
        elif model.startswith("claude-haiku-4-5"):
            params["temperature"] = 0
    elif provider == "together":
        params.update(
            {
                "temperature": 0,
                "top_p": 1.0,
                "repetition_penalty": 1.0,
                "presence_penalty": 0,
                "frequency_penalty": 0,
            }
        )
    params.update(config.model_parameters or {})
    return params


def provider_name(config: EvaluationConfig) -> str:
    return config.provider.strip().lower()


def openai_text(messages: list[dict[str, str]], *, config: EvaluationConfig) -> str:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    payload = {
        "model": config.model,
        "input": messages,
    }
    if config.max_tokens:
        payload["max_output_tokens"] = config.max_tokens
    payload.update(merged_model_parameters(config))
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


def anthropic_messages(messages: list[dict[str, str]]) -> tuple[str, list[dict[str, str]]]:
    system_parts: list[str] = []
    request_messages: list[dict[str, str]] = []
    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")
        if role == "system":
            system_parts.append(content)
        elif role in {"user", "assistant"}:
            request_messages.append({"role": role, "content": content})
    return "\n\n".join(system_parts), request_messages


def anthropic_text(messages: list[dict[str, str]], *, config: EvaluationConfig) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    system, request_messages = anthropic_messages(messages)
    payload: dict[str, Any] = {
        "model": config.model,
        "max_tokens": config.max_tokens,
        "messages": request_messages,
    }
    if system:
        payload["system"] = system
    payload.update(merged_model_parameters(config))
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=payload,
        timeout=config.request_timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return anthropic_response_text(response.json())


def together_endpoint(config: EvaluationConfig) -> str:
    endpoint_env = config.endpoint_env or f"TOGETHER_ENDPOINT_{config.model}"
    endpoint = os.environ.get(endpoint_env, "").strip()
    if not endpoint:
        raise RuntimeError(f"{endpoint_env} is not set.")
    if endpoint.endswith("/chat/completions") or endpoint.endswith("/completions"):
        return endpoint
    return endpoint.rstrip("/") + "/v1/chat/completions"


def together_text(messages: list[dict[str, str]], *, config: EvaluationConfig) -> str:
    api_key = os.environ.get("TOGETHER_API_KEY")
    if not api_key:
        raise RuntimeError("TOGETHER_API_KEY is not set.")
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "max_tokens": config.max_tokens,
    }
    payload.update(merged_model_parameters(config))
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = requests.post(
        together_endpoint(config),
        headers=headers,
        json=payload,
        timeout=config.request_timeout_seconds,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
    return chat_completion_text(response.json())


def provider_text(messages: list[dict[str, str]], *, config: EvaluationConfig) -> str:
    provider = provider_name(config)
    if provider == "openai":
        return openai_text(messages, config=config)
    if provider == "anthropic":
        return anthropic_text(messages, config=config)
    if provider == "together":
        return together_text(messages, config=config)
    raise ValueError(f"Unsupported provider for evaluation: {config.provider}")


def provider_answer(prompt: str, *, config: EvaluationConfig) -> dict[str, Any]:
    messages = [
        {"role": "system", "content": "Answer medical multiple choice questions with exactly one lowercase character: y, n, or m."},
        {"role": "user", "content": prompt},
    ]
    last_error = ""
    for attempt in range(max(1, config.retry_count)):
        try:
            raw_text = provider_text(messages, config=config)
            return {"answer": parse_answer(raw_text), "raw_response": raw_text, "error": ""}
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = str(exc)
            if attempt + 1 < max(1, config.retry_count):
                time.sleep(2.0 * (attempt + 1))
    return {"answer": "", "raw_response": "", "error": last_error}


def model_answer(prompt: str, *, config: EvaluationConfig) -> dict[str, Any]:
    return provider_answer(prompt, config=config)


def model_text(messages: list[dict[str, str]], *, config: EvaluationConfig) -> str:
    last_error = ""
    for attempt in range(max(1, config.retry_count)):
        try:
            return provider_text(messages, config=config)
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


def complete_char_section_label(section_name: str) -> str:
    labels = {
        "methods": "Methods",
        "intervention_comparator": "Intervention/Comparator",
        "participants": "Participants",
        "outcomes": "Outcomes measured",
        "results": "Results",
    }
    return labels.get(section_name, section_name)


def complete_char_detail(article: dict[str, Any]) -> str:
    sections = study_detail_sections(article)
    body = []
    for name in COMPLETE_CHAR_SECTION_NAMES:
        text = sections.get(name, "")
        if text:
            body.append(f"{complete_char_section_label(name)}:\n{text}")
    return "\n\n".join(body)


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


def gatekeeper_categorization_prompt(request: dict[str, str]) -> list[dict[str, str]]:
    categories = "|".join(MULTITURN_CATEGORIES)
    return [
        {
            "role": "system",
            "content": (
                "You are a gatekeeper that classifies study follow-up questions. "
                "Return only JSON matching this template: "
                "{\"category\":\"Methods|Intervention/Comparator|Participants|Outcomes|Results\"}"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Classify the follow-up question into exactly one category: {categories}.\n\n"
                f"Category definitions:\n{MULTITURN_CATEGORY_DEFINITIONS}\n\n"
                f"Study ID: {request.get('study_id', '')}\n"
                f"Follow-up question: {request.get('question', '')}"
            ),
        },
    ]


def categorize_gatekeeper_request(request: dict[str, str], *, config: EvaluationConfig) -> tuple[str, str, dict[str, Any]]:
    messages = gatekeeper_categorization_prompt(request)
    gatekeeper_log: dict[str, Any] = {
        "mode": GATEKEEPER_TYPE_FREE_RESPONSE,
        "messages": [dict(message) for message in messages],
        "raw_response": "",
        "classification_raw_response": "",
    }
    try:
        raw_response = model_text(messages, config=config)
        gatekeeper_log["classification_raw_response"] = raw_response
        data = extract_json_object(raw_response) or {}
        category_label, category_key = normalize_multiturn_category(data.get("category") or raw_response)
    except (RuntimeError, ValueError) as exc:
        gatekeeper_log["error"] = str(exc)
        return "", "", gatekeeper_log
    gatekeeper_log["selected_category"] = category_label
    return category_label, category_key, gatekeeper_log


def gatekeeper_response(
    request: dict[str, str],
    *,
    study_states: dict[str, dict[str, Any]],
    config: EvaluationConfig,
) -> dict[str, Any]:
    gatekeeper_type = normalize_gatekeeper_type(config.gatekeeper_type)
    study_id = request["study_id"]
    state = study_states.get(study_id)
    if not state:
        return {
            **request,
            "response": GATEKEEPER_UNAVAILABLE,
            "classification": "unavailable",
            "revealed_section": "",
            "gatekeeper": {"mode": gatekeeper_type, "raw_response": GATEKEEPER_UNAVAILABLE},
        }

    category = request.get("category") or ""
    category_key = request.get("category_key") or ""
    gatekeeper_log: dict[str, Any] = {
        "mode": gatekeeper_type,
        "selected_category": category,
        "raw_response": "",
    }
    if gatekeeper_type == GATEKEEPER_TYPE_FREE_RESPONSE:
        category, category_key, gatekeeper_log = categorize_gatekeeper_request(request, config=config)
    if not category_key:
        response = GATEKEEPER_UNAVAILABLE
        gatekeeper_log["raw_response"] = gatekeeper_log.get("raw_response") or response
        return {
            **request,
            "category": category,
            "category_key": category_key,
            "response": response,
            "classification": "unavailable",
            "revealed_section": "",
            "gatekeeper": gatekeeper_log,
        }

    exposed_names = [*state["exposed_names"], *state["revealed_names"]]
    if category_key in exposed_names:
        response = f"Complete {category} information provided."
        gatekeeper_log["raw_response"] = gatekeeper_log.get("raw_response") or response
        return {
            **request,
            "category": category,
            "category_key": category_key,
            "response": response,
            "classification": "prior",
            "revealed_section": "",
            "gatekeeper": gatekeeper_log,
        }

    if category_key in state["hidden_names"] and state["sections"].get(category_key):
        state["revealed_names"].append(category_key)
        response = f"{category}:\n{state['sections'][category_key]}"
        gatekeeper_log["raw_response"] = gatekeeper_log.get("raw_response") or response
        return {
            **request,
            "category": category,
            "category_key": category_key,
            "response": response,
            "classification": "hidden",
            "revealed_section": category_key,
            "gatekeeper": gatekeeper_log,
        }

    response = GATEKEEPER_UNAVAILABLE
    gatekeeper_log["raw_response"] = gatekeeper_log.get("raw_response") or response
    return {
        **request,
        "category": category,
        "category_key": category_key,
        "response": response,
        "classification": "unavailable",
        "revealed_section": "",
        "gatekeeper": gatekeeper_log,
    }


def prompt_multiturn_initial(question: str, studies_context: str, maximum_follow_ups: int, gatekeeper_type: str) -> str:
    categories = ", ".join(MULTITURN_CATEGORIES)
    if gatekeeper_type == GATEKEEPER_TYPE_FREE_RESPONSE:
        return (
            "Respond to the following medical multiple-choice question with either a final answer or follow-up questions.\n"
            "Use exactly one of these JSON templates and no other text:\n"
            "{\"action\":\"answer\",\"answer\":\"y|n|m\"}\n"
            "{\"action\":\"follow_up\",\"questions\":[{\"study_id\":\"ART_00001\",\"question\":\"...\"}]}\n\n"
            "Answer choices are y=yes, n=no, and m=maybe. "
            "Answer the question only if you are confident that sufficient information is provided to make a decision. "
            "If asking follow-up question(s), direct each follow-up question to one individual study using its study_id. "
            "Ask as many follow-up questions as are necessary, including additional follow-ups if prior answers are insufficient. "
            f"You may ask at most {maximum_follow_ups} rounds of follow-up questions; after that, you must answer.\n\n"
            f"Question: {question}\n\n"
            f"Study context:\n{studies_context}"
        )

    follow_up_template = "{\"action\":\"follow_up\",\"questions\":[{\"study_id\":\"ART_00001\",\"category\":\"Methods|Intervention/Comparator|Participants|Outcomes|Results\",\"question\":\"...\"}]}"
    category_instruction = (
        f"For each follow-up question, choose exactly one category from: {categories}.\n\n"
        f"Category definitions:\n{MULTITURN_CATEGORY_DEFINITIONS}"
    )
    return (
        "Respond to the following medical multiple-choice question with either a final answer or follow-up questions.\n"
        "Use exactly one of these JSON templates and no other text:\n"
        "{\"action\":\"answer\",\"answer\":\"y|n|m\"}\n"
        f"{follow_up_template}\n\n"
        "Answer choices are y=yes, n=no, and m=maybe. Ask follow-up questions only when needed. "
        "Direct each follow-up question to one individual study using its study_id. "
        f"{category_instruction} "
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

    gatekeeper_type = normalize_gatekeeper_type(config.gatekeeper_type)
    initial_prompt = prompt_multiturn_initial(question, "\n\n---\n\n".join(context_parts), config.maximum_follow_ups, gatekeeper_type)
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
            gatekeeper_response(request, study_states=study_states, config=config)
            for request in parsed["questions"]
        ]
        turn["gatekeeper_responses"] = gatekeeper_responses
        if gatekeeper_type == GATEKEEPER_TYPE_FREE_RESPONSE:
            response_text_for_model = "\n\n".join(
                f"Study {item['study_id']}\n"
                f"Question: {item['question']}\n"
                f"Gatekeeper response: {item['response']}"
                for item in gatekeeper_responses
            )
        else:
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
            "gatekeeper_type": gatekeeper_type,
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
    if normalized == COMPLETE_CHAR_DETAIL_TYPE:
        return "Complete study characteristics/results", complete_char_detail(article)
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
    before_manual_exclusion_filter = len(outcomes)
    outcomes = [outcome for outcome in outcomes if not outcome.get("manually_excluded")]
    skipped_for_manual_exclusion = before_manual_exclusion_filter - len(outcomes)
    if skipped_for_manual_exclusion:
        print(f"Skipping {skipped_for_manual_exclusion} manually excluded outcomes.")
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
            "skipped_outcomes_manually_excluded": skipped_for_manual_exclusion,
            "skipped_outcomes_not_very_low": skipped_for_certainty,
            "detail_exposure_types": list(config.detail_exposure_types),
            "irrelevant_docs_per_context": config.irrelevant_docs_per_context,
            "maximum_follow_ups": config.maximum_follow_ups,
            "gatekeeper_type": normalize_gatekeeper_type(config.gatekeeper_type),
            "max_tokens": config.max_tokens,
            "model_parameters": merged_model_parameters(config),
            "endpoint_env": config.endpoint_env,
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
