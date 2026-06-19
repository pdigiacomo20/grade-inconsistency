from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

from grade_inconsistency import (
    PMC_ARTICLE_URL,
    PUBMED_QUERY,
    analyze_summary_section,
    extract_article_title,
    extract_summary_sections,
    fetch_article_html,
    fetch_pubmed_summaries,
    is_protocol_article,
    lookup_pmcids,
    search_pubmed,
)
from pipeline.config import PipelineConfig, load_config
from pipeline.dynamodb import DynamoStore
from pipeline.openai_extraction import extract_review_with_openai
from pipeline.study_enrichment import (
    classify_study,
    extract_study_labels,
    find_forest_plot,
    resolve_study,
    save_forest_plot,
    summarize_study_for_outcome,
    study_id_for_label,
)


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36 grade-inconsistency/0.1"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json",
        }
    )
    return session


def publication_year(summary: dict[str, Any]) -> str:
    pubdate = str(summary.get("pubdate") or summary.get("epubdate") or "")
    return pubdate[:4] if pubdate[:4].isdigit() else ""


def journal_name(summary: dict[str, Any]) -> str:
    return str(summary.get("fulljournalname") or summary.get("source") or "")


def build_review_item(
    *,
    pmid: str,
    summary: dict[str, Any],
    pmcid: str | None,
    title: str,
    status: str,
    summary_tables: int = 0,
    fetch_error: str | None = None,
) -> dict[str, Any]:
    return {
        "pmid": str(pmid),
        "title": title or str(summary.get("title") or ""),
        "year": publication_year(summary),
        "journal": journal_name(summary),
        "pmcid": pmcid,
        "full_text_url": PMC_ARTICLE_URL.format(pmcid=pmcid) if pmcid else None,
        "pubtypes": summary.get("pubtype", []),
        "status": status,
        "summary_tables": summary_tables,
        "fetch_error": fetch_error,
        "indexed_at": datetime.now(UTC).isoformat(),
    }


def build_outcome_items(pmid: str, outcomes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for outcome_id, outcome in enumerate(outcomes):
        items.append(
            {
                "pmid": str(pmid),
                "outcome_id": outcome_id,
                "table_title": outcome.get("table_title", ""),
                "outcome": outcome.get("outcome", ""),
                "question": outcome.get("question", ""),
                "consensus_answer": outcome.get("consensus_answer", ""),
                "inconsistency": int(outcome.get("inconsistency", 0)),
                "subgroup_differences": int(outcome.get("subgroup_differences", 0)),
                "certainty": outcome.get("certainty", ""),
                "downgrade_categories": outcome.get("downgrade_categories", []),
                "inconsistency_reason": outcome.get("inconsistency_reason", ""),
                "footnote_labels": outcome.get("footnote_labels", []),
                "footnotes": outcome.get("footnotes", {}),
                "extraction_method": outcome.get("extraction_method", "deterministic"),
                "extraction_model": outcome.get("extraction_model", ""),
                "extraction_notes": outcome.get("extraction_notes", ""),
                "openai_forest_plot_url": outcome.get("openai_forest_plot_url", ""),
                "openai_forest_plot_caption": outcome.get("openai_forest_plot_caption", ""),
                "openai_agreeing_study_labels": outcome.get("openai_agreeing_study_labels", []),
                "openai_opposing_study_labels": outcome.get("openai_opposing_study_labels", []),
            }
        )
    return items


def extract_review_outcomes(
    *,
    config: PipelineConfig,
    article_url: str,
    article_html: str,
    pmid: str,
    title: str,
) -> tuple[list[dict[str, Any]], int, str, str | None]:
    mode = config.extraction_mode.lower().strip()
    if mode in {"openai", "hybrid"} and config.openai_api_key:
        try:
            result = extract_review_with_openai(
                api_key=config.openai_api_key,
                model=config.openai_model,
                article_url=article_url,
                pmid=pmid,
                title_hint=title,
                timeout_seconds=config.openai_timeout_seconds,
                use_web_search=config.openai_web_search,
            )
            outcomes = result.get("outcomes", [])
            if outcomes:
                return outcomes, int(result.get("summary_tables", 0) or 0), "openai", None
            if mode == "openai":
                return [], int(result.get("summary_tables", 0) or 0), "openai", "OpenAI returned no outcomes"
        except (json.JSONDecodeError, requests.RequestException, RuntimeError, KeyError, TypeError, ValueError) as exc:
            if mode == "openai":
                return [], 0, "openai_failed", str(exc)
            print(f"PMID {pmid} OpenAI extraction failed; falling back to deterministic parser: {exc}", file=sys.stderr)

    summary_sections = extract_summary_sections(article_html)
    outcomes: list[dict[str, Any]] = []
    for section_title, section_html in summary_sections:
        outcomes.extend(analyze_summary_section(section_title, section_html))
    return outcomes, len(summary_sections), "deterministic", None


def index_reviews(config: PipelineConfig) -> dict[str, int]:
    store = DynamoStore(
        region_name=config.aws_region,
        endpoint_url=config.dynamodb_endpoint_url,
        reviews_table=config.reviews_table,
        outcomes_table=config.outcomes_table,
        studies_table=config.studies_table,
    )
    if config.create_tables:
        store.ensure_tables()

    session = build_session()
    pmids, total_query_results = search_pubmed(session, config.limit)
    summaries = fetch_pubmed_summaries(session, pmids)
    pmcid_map = lookup_pmcids(session, pmids)

    stats = {
        "query_results": total_query_results,
        "selected": len(pmids),
        "skipped_existing": 0,
        "indexed": 0,
        "missing_pmcid": 0,
        "protocol_skipped": 0,
        "fetch_failed": 0,
    }

    for index, pmid in enumerate(pmids, start=1):
        if not config.force_reprocess and store.review_exists(pmid):
            stats["skipped_existing"] += 1
            print(f"[{index}/{len(pmids)}] PMID {pmid} already indexed; skipping", file=sys.stderr)
            continue

        summary = summaries.get(pmid, {})
        pmcid = pmcid_map.get(pmid)
        if not pmcid:
            store.put_review(
                build_review_item(
                    pmid=pmid,
                    summary=summary,
                    pmcid=None,
                    title=str(summary.get("title") or ""),
                    status="missing_pmcid",
                )
            )
            store.replace_outcomes(pmid, [])
            stats["missing_pmcid"] += 1
            print(f"[{index}/{len(pmids)}] PMID {pmid} missing PMCID", file=sys.stderr)
            continue

        article_url = PMC_ARTICLE_URL.format(pmcid=pmcid)
        try:
            article_html = fetch_article_html(session, article_url, config.pause_seconds)
        except RuntimeError as exc:
            store.put_review(
                build_review_item(
                    pmid=pmid,
                    summary=summary,
                    pmcid=pmcid,
                    title=str(summary.get("title") or ""),
                    status="fetch_failed",
                    fetch_error=str(exc),
                )
            )
            store.replace_outcomes(pmid, [])
            stats["fetch_failed"] += 1
            print(f"[{index}/{len(pmids)}] PMID {pmid} fetch failed: {exc}", file=sys.stderr)
            continue

        title = extract_article_title(article_html, str(summary.get("title") or ""))
        if is_protocol_article(article_html):
            store.put_review(
                build_review_item(
                    pmid=pmid,
                    summary=summary,
                    pmcid=pmcid,
                    title=title,
                    status="protocol_skipped",
                )
            )
            store.replace_outcomes(pmid, [])
            stats["protocol_skipped"] += 1
            print(f"[{index}/{len(pmids)}] PMID {pmid} protocol skipped", file=sys.stderr)
            continue

        outcomes, summary_table_count, extraction_method, extraction_error = extract_review_outcomes(
            config=config,
            article_url=article_url,
            article_html=article_html,
            pmid=pmid,
            title=title,
        )

        review_status = "extraction_failed" if extraction_error and not outcomes else "review_processed"
        store.put_review(
            build_review_item(
                pmid=pmid,
                summary=summary,
                pmcid=pmcid,
                title=title,
                status=review_status,
                summary_tables=summary_table_count,
                fetch_error=extraction_error,
            )
        )
        store.replace_outcomes(pmid, build_outcome_items(pmid, outcomes))
        stats["indexed"] += 1
        print(
            f"[{index}/{len(pmids)}] PMID {pmid} indexed via={extraction_method} "
            f"tables={summary_table_count} outcomes={len(outcomes)}",
            file=sys.stderr,
        )

    return stats


def _flagged_for_study_enrichment(outcome: dict[str, Any]) -> bool:
    return bool(int(outcome.get("inconsistency", 0) or 0) or int(outcome.get("subgroup_differences", 0) or 0))


def _needs_study_enrichment(outcome: dict[str, Any], force: bool) -> bool:
    if force:
        return _flagged_for_study_enrichment(outcome)
    return _flagged_for_study_enrichment(outcome) and not outcome.get("study_enrichment_status")


def _openai_study_labels(outcome: dict[str, Any], key: str) -> list[dict[str, str]]:
    labels: list[dict[str, str]] = []
    for item in outcome.get(key, []) or []:
        if isinstance(item, dict) and item.get("label"):
            labels.append({"label": str(item["label"]), "context": str(item.get("rationale", ""))})
        elif isinstance(item, str) and item:
            labels.append({"label": item, "context": ""})
    return labels


def _resolve_study_labels(
    *,
    session: requests.Session,
    store: DynamoStore,
    labels: list[dict[str, str]],
    study_cache: dict[str, dict[str, Any]],
    force: bool,
) -> list[str]:
    study_ids: list[str] = []
    for label in labels:
        study_id = study_id_for_label(label["label"])
        study = study_cache.get(study_id) or store.get_study(study_id)
        if not study or force:
            study = resolve_study(session, label["label"])
            store.put_study(study)
        study_cache[study_id] = study
        study_ids.append(str(study["study_id"]))
    return study_ids


def enrich_flagged_outcomes(config: PipelineConfig) -> dict[str, int]:
    store = DynamoStore(
        region_name=config.aws_region,
        endpoint_url=config.dynamodb_endpoint_url,
        reviews_table=config.reviews_table,
        outcomes_table=config.outcomes_table,
        studies_table=config.studies_table,
    )
    if config.create_tables:
        store.ensure_tables()

    session = build_session()
    flagged_outcomes = [outcome for outcome in store.list_outcomes() if _flagged_for_study_enrichment(outcome)]
    if config.study_enrichment_limit is not None:
        flagged_outcomes = flagged_outcomes[: max(config.study_enrichment_limit, 0)]
    candidates = [
        outcome
        for outcome in flagged_outcomes
        if _needs_study_enrichment(outcome, config.force_study_enrichment)
    ]

    stats = {
        "study_enrichment_selected": len(candidates),
        "study_enrichment_done": 0,
        "study_enrichment_no_plot": 0,
        "study_enrichment_no_studies": 0,
        "study_enrichment_failed": 0,
    }
    html_cache: dict[str, str] = {}
    study_cache: dict[str, dict[str, Any]] = {}

    for index, outcome in enumerate(candidates, start=1):
        pmid = str(outcome["pmid"])
        outcome_id = int(outcome["outcome_id"])
        review = store.get_review(pmid) or {}
        article_url = str(review.get("full_text_url") or "")
        if not article_url and review.get("pmcid"):
            article_url = PMC_ARTICLE_URL.format(pmcid=review["pmcid"])
        if not article_url:
            outcome.update(
                {
                    "study_enrichment_status": "missing_review_full_text",
                    "agreeing_studies": [],
                    "opposing_studies": [],
                    "study_enrichment_error": "review has no PMC full-text URL",
                }
            )
            store.put_outcome(outcome)
            stats["study_enrichment_failed"] += 1
            print(
                f"[study {index}/{len(candidates)}] PMID {pmid} outcome {outcome_id} missing review full text",
                file=sys.stderr,
            )
            continue

        try:
            if pmid not in html_cache:
                html_cache[pmid] = fetch_article_html(session, article_url, config.pause_seconds)
            article_html = html_cache[pmid]

            openai_agreeing_labels = _openai_study_labels(outcome, "openai_agreeing_study_labels")
            openai_opposing_labels = _openai_study_labels(outcome, "openai_opposing_study_labels")
            openai_plot_url = str(outcome.get("openai_forest_plot_url") or "").strip()
            if openai_plot_url or openai_agreeing_labels or openai_opposing_labels:
                saved_plot = None
                if openai_plot_url:
                    saved_plot = save_forest_plot(
                        session,
                        forest_plot={"image_url": urljoin(article_url, openai_plot_url)},
                        output_dir=config.forest_plots_dir,
                        pmid=pmid,
                        outcome_id=outcome_id,
                        force=config.force_study_enrichment,
                    )
                agreeing_studies = _resolve_study_labels(
                    session=session,
                    store=store,
                    labels=openai_agreeing_labels,
                    study_cache=study_cache,
                    force=config.force_study_enrichment,
                )
                opposing_studies = _resolve_study_labels(
                    session=session,
                    store=store,
                    labels=openai_opposing_labels,
                    study_cache=study_cache,
                    force=config.force_study_enrichment,
                )
                study_ids = agreeing_studies + opposing_studies
                studies_by_id = store.batch_get_studies(study_ids)
                outcome.update(
                    {
                        "study_enrichment_status": "enriched_openai" if study_ids else "no_studies_parsed",
                        "forest_plot_path": saved_plot["path"] if saved_plot else None,
                        "forest_plot_source_url": saved_plot["image_url"] if saved_plot else openai_plot_url or None,
                        "forest_plot_url": f"/api/forest-plots/{pmid}/{outcome_id}" if saved_plot else None,
                        "agreeing_studies": agreeing_studies,
                        "opposing_studies": opposing_studies,
                        "agreeing_study_refs": [
                            summarize_study_for_outcome(studies_by_id[study_id])
                            for study_id in agreeing_studies
                            if study_id in studies_by_id
                        ],
                        "opposing_study_refs": [
                            summarize_study_for_outcome(studies_by_id[study_id])
                            for study_id in opposing_studies
                            if study_id in studies_by_id
                        ],
                        "study_enrichment_error": None,
                    }
                )
                store.put_outcome(outcome)
                if study_ids:
                    stats["study_enrichment_done"] += 1
                else:
                    stats["study_enrichment_no_studies"] += 1
                print(
                    f"[study {index}/{len(candidates)}] PMID {pmid} outcome {outcome_id} "
                    f"openai studies={len(study_ids)}",
                    file=sys.stderr,
                )
                continue

            forest_plot = find_forest_plot(article_html, article_url, outcome)
            if not forest_plot:
                outcome.update(
                    {
                        "study_enrichment_status": "no_forest_plot",
                        "agreeing_studies": [],
                        "opposing_studies": [],
                        "forest_plot_path": None,
                        "forest_plot_source_url": None,
                        "study_enrichment_error": None,
                    }
                )
                store.put_outcome(outcome)
                stats["study_enrichment_no_plot"] += 1
                print(
                    f"[study {index}/{len(candidates)}] PMID {pmid} outcome {outcome_id} no forest plot",
                    file=sys.stderr,
                )
                continue

            saved_plot = save_forest_plot(
                session,
                forest_plot=forest_plot,
                output_dir=config.forest_plots_dir,
                pmid=pmid,
                outcome_id=outcome_id,
                force=config.force_study_enrichment,
            )
            labels = extract_study_labels(forest_plot.get("context_text", ""))
            agreeing_studies: list[str] = []
            opposing_studies: list[str] = []

            for label in labels:
                study_ids_for_label = _resolve_study_labels(
                    session=session,
                    store=store,
                    labels=[label],
                    study_cache=study_cache,
                    force=config.force_study_enrichment,
                )
                if not study_ids_for_label:
                    continue
                if classify_study(label, outcome) == "opposing":
                    opposing_studies.extend(study_ids_for_label)
                else:
                    agreeing_studies.extend(study_ids_for_label)

            study_ids = agreeing_studies + opposing_studies
            studies_by_id = store.batch_get_studies(study_ids)
            outcome.update(
                {
                    "study_enrichment_status": "enriched" if study_ids else "no_studies_parsed",
                    "forest_plot_path": saved_plot["path"],
                    "forest_plot_source_url": saved_plot["image_url"],
                    "forest_plot_url": f"/api/forest-plots/{pmid}/{outcome_id}",
                    "agreeing_studies": agreeing_studies,
                    "opposing_studies": opposing_studies,
                    "agreeing_study_refs": [
                        summarize_study_for_outcome(studies_by_id[study_id])
                        for study_id in agreeing_studies
                        if study_id in studies_by_id
                    ],
                    "opposing_study_refs": [
                        summarize_study_for_outcome(studies_by_id[study_id])
                        for study_id in opposing_studies
                        if study_id in studies_by_id
                    ],
                    "study_enrichment_error": None,
                }
            )
            store.put_outcome(outcome)
            if study_ids:
                stats["study_enrichment_done"] += 1
            else:
                stats["study_enrichment_no_studies"] += 1
            print(
                f"[study {index}/{len(candidates)}] PMID {pmid} outcome {outcome_id} "
                f"plot studies={len(study_ids)}",
                file=sys.stderr,
            )
        except (RuntimeError, requests.RequestException, OSError) as exc:
            outcome.update(
                {
                    "study_enrichment_status": "failed",
                    "study_enrichment_error": str(exc),
                }
            )
            store.put_outcome(outcome)
            stats["study_enrichment_failed"] += 1
            print(
                f"[study {index}/{len(candidates)}] PMID {pmid} outcome {outcome_id} failed: {exc}",
                file=sys.stderr,
            )

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Index Cochrane systematic reviews into DynamoDB.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.example.yml"),
        help="YAML config path.",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    stats = index_reviews(config)
    study_stats = enrich_flagged_outcomes(config)
    print(f"PubMed query: {PUBMED_QUERY}")
    for key, value in {**stats, **study_stats}.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
