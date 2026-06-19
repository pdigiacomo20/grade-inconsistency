from __future__ import annotations

import json
from typing import Any

import requests

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"


REVIEW_EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["review_status", "title", "summary_tables", "outcomes", "notes"],
    "properties": {
        "review_status": {
            "type": "string",
            "enum": ["review_processed", "protocol_skipped", "no_summary_of_findings", "failed"],
        },
        "title": {"type": "string"},
        "summary_tables": {"type": "integer"},
        "notes": {"type": "string"},
        "outcomes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "table_title",
                    "outcome",
                    "question",
                    "consensus_answer",
                    "inconsistency",
                    "subgroup_differences",
                    "certainty",
                    "downgrade_categories",
                    "inconsistency_reason",
                    "footnote_labels",
                    "footnotes",
                    "forest_plot_url",
                    "forest_plot_caption",
                    "agreeing_studies",
                    "opposing_studies",
                    "extraction_notes",
                ],
                "properties": {
                    "table_title": {"type": "string"},
                    "outcome": {"type": "string"},
                    "question": {"type": "string"},
                    "consensus_answer": {"type": "string"},
                    "inconsistency": {"type": "integer", "enum": [0, 1]},
                    "subgroup_differences": {"type": "integer", "enum": [0, 1]},
                    "certainty": {"type": "string"},
                    "downgrade_categories": {"type": "array", "items": {"type": "string"}},
                    "inconsistency_reason": {"type": "string"},
                    "footnote_labels": {"type": "array", "items": {"type": "string"}},
                    "footnotes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "text"],
                            "properties": {
                                "label": {"type": "string"},
                                "text": {"type": "string"},
                            },
                        },
                    },
                    "forest_plot_url": {"type": "string"},
                    "forest_plot_caption": {"type": "string"},
                    "agreeing_studies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "rationale"],
                            "properties": {
                                "label": {"type": "string"},
                                "rationale": {"type": "string"},
                            },
                        },
                    },
                    "opposing_studies": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["label", "rationale"],
                            "properties": {
                                "label": {"type": "string"},
                                "rationale": {"type": "string"},
                            },
                        },
                    },
                    "extraction_notes": {"type": "string"},
                },
            },
        },
    },
}


def _extract_output_text(response: dict[str, Any]) -> str:
    if response.get("output_text"):
        return str(response["output_text"])
    parts: list[str] = []
    for item in response.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                parts.append(str(content["text"]))
    return "\n".join(parts)


def _normalize_outcome(outcome: dict[str, Any], extraction_model: str) -> dict[str, Any]:
    footnotes = {
        str(item.get("label", "")): str(item.get("text", ""))
        for item in outcome.get("footnotes", [])
        if item.get("label") or item.get("text")
    }
    return {
        **outcome,
        "footnotes": footnotes,
        "openai_forest_plot_url": str(outcome.get("forest_plot_url") or ""),
        "openai_forest_plot_caption": str(outcome.get("forest_plot_caption") or ""),
        "openai_agreeing_study_labels": [
            {"label": str(item.get("label", "")), "rationale": str(item.get("rationale", ""))}
            for item in outcome.get("agreeing_studies", [])
            if item.get("label")
        ],
        "openai_opposing_study_labels": [
            {"label": str(item.get("label", "")), "rationale": str(item.get("rationale", ""))}
            for item in outcome.get("opposing_studies", [])
            if item.get("label")
        ],
        "extraction_method": "openai",
        "extraction_model": extraction_model,
    }


def extract_review_with_openai(
    *,
    api_key: str,
    model: str,
    article_url: str,
    pmid: str,
    title_hint: str,
    timeout_seconds: int,
    use_web_search: bool,
) -> dict[str, Any]:
    instructions = (
        "You are extracting structured evidence data from a PubMed Central systematic review. "
        "Use web search/open-page behavior to inspect the provided PMC full-text URL. "
        "Do not guess. If an item cannot be found, return an empty string or empty list. "
        "For each Summary of Findings outcome row, extract the row text, certainty, GRADE footnotes, "
        "whether the row is downgraded for inconsistency, whether subgroup differences are discussed, "
        "and the best-matching forest plot URL/caption. For outcomes with inconsistency or subgroup "
        "differences, inspect the corresponding forest plot and classify named studies on the side "
        "supporting the pooled/primary effect as agreeing_studies and studies on the opposite side "
        "or materially contributing to heterogeneity as opposing_studies. Study labels should look "
        "like author/year labels from the plot, for example 'Smith 2020'."
    )
    user_input = (
        f"PMC full-text URL: {article_url}\n"
        f"Review PMID: {pmid}\n"
        f"Title hint: {title_hint}\n\n"
        "Return JSON only according to the schema."
    )
    payload: dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": [{"role": "user", "content": user_input}],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "pmc_review_extraction",
                "strict": True,
                "schema": REVIEW_EXTRACTION_SCHEMA,
            }
        },
    }
    if use_web_search:
        payload["tools"] = [{"type": "web_search"}]

    response = requests.post(
        OPENAI_RESPONSES_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    data = response.json()
    parsed = json.loads(_extract_output_text(data))
    return {
        **parsed,
        "outcomes": [_normalize_outcome(outcome, model) for outcome in parsed.get("outcomes", [])],
        "openai_response_id": data.get("id"),
    }
