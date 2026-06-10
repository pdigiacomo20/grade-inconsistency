from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
            }
        )
    return items


def index_reviews(config: PipelineConfig) -> dict[str, int]:
    store = DynamoStore(
        region_name=config.aws_region,
        endpoint_url=config.dynamodb_endpoint_url,
        reviews_table=config.reviews_table,
        outcomes_table=config.outcomes_table,
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

        summary_sections = extract_summary_sections(article_html)
        outcomes: list[dict[str, Any]] = []
        for section_title, section_html in summary_sections:
            outcomes.extend(analyze_summary_section(section_title, section_html))

        store.put_review(
            build_review_item(
                pmid=pmid,
                summary=summary,
                pmcid=pmcid,
                title=title,
                status="review_processed",
                summary_tables=len(summary_sections),
            )
        )
        store.replace_outcomes(pmid, build_outcome_items(pmid, outcomes))
        stats["indexed"] += 1
        print(
            f"[{index}/{len(pmids)}] PMID {pmid} indexed tables={len(summary_sections)} "
            f"outcomes={len(outcomes)}",
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
    print(f"PubMed query: {PUBMED_QUERY}")
    for key, value in stats.items():
        print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
