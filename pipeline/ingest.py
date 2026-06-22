from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any

import requests

from grade_inconsistency import (
    PMC_ARTICLE_URL,
    extract_article_title,
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
            "User-Agent": "grade-inconsistency/0.2 (review indexer)",
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
    review_id: str,
    pmid: str,
    summary: dict[str, Any],
    pmcid: str | None,
    title: str,
    status: str,
    is_protocol_only: bool,
    fetch_error: str | None = None,
) -> dict[str, Any]:
    return {
        "review_id": review_id,
        "pmid": str(pmid),
        "title": title or str(summary.get("title") or ""),
        "year": publication_year(summary),
        "journal": journal_name(summary),
        "pmcid": pmcid,
        "pmc_url": PMC_ARTICLE_URL.format(pmcid=pmcid) if pmcid else None,
        "pubmed_url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
        "pubtypes": summary.get("pubtype", []),
        "status": status,
        "is_protocol_only": is_protocol_only,
        "fetch_error": fetch_error,
        "indexed_at": datetime.now(UTC).isoformat(),
    }


def index_reviews(config: PipelineConfig) -> dict[str, int]:
    store = DynamoStore(
        region_name=config.aws_region,
        endpoint_url=config.dynamodb_endpoint_url,
        reviews_table=config.reviews_table,
        outcomes_table=config.outcomes_table,
        articles_table=config.articles_table,
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
        "protocol_only": 0,
        "fetch_failed": 0,
    }

    for index, pmid in enumerate(pmids, start=1):
        existing = store.get_review_by_pmid(pmid)
        if existing and not config.force_reprocess:
            stats["skipped_existing"] += 1
            print(f"[{index}/{len(pmids)}] PMID {pmid} already indexed as {existing.get('review_id')}")
            continue

        review_id = str(existing.get("review_id")) if existing and existing.get("review_id") else store.next_review_id()
        summary = summaries.get(pmid, {})
        pmcid = pmcid_map.get(pmid)
        if not pmcid:
            store.put_review(
                build_review_item(
                    review_id=review_id,
                    pmid=pmid,
                    summary=summary,
                    pmcid=None,
                    title=str(summary.get("title") or ""),
                    status="missing_pmcid",
                    is_protocol_only=False,
                )
            )
            stats["missing_pmcid"] += 1
            print(f"[{index}/{len(pmids)}] {review_id} PMID {pmid} missing PMCID")
            continue

        article_url = PMC_ARTICLE_URL.format(pmcid=pmcid)
        try:
            article_html = fetch_article_html(session, article_url, config.pause_seconds)
            title = extract_article_title(article_html, str(summary.get("title") or ""))
            protocol_only = is_protocol_article(article_html)
            status = "protocol_only" if protocol_only else "ready_for_extraction"
            store.put_review(
                build_review_item(
                    review_id=review_id,
                    pmid=pmid,
                    summary=summary,
                    pmcid=pmcid,
                    title=title,
                    status=status,
                    is_protocol_only=protocol_only,
                )
            )
            if protocol_only:
                stats["protocol_only"] += 1
            else:
                stats["indexed"] += 1
            print(f"[{index}/{len(pmids)}] {review_id} PMID {pmid} {status}")
        except RuntimeError as exc:
            store.put_review(
                build_review_item(
                    review_id=review_id,
                    pmid=pmid,
                    summary=summary,
                    pmcid=pmcid,
                    title=str(summary.get("title") or ""),
                    status="fetch_failed",
                    is_protocol_only=False,
                    fetch_error=str(exc),
                )
            )
            stats["fetch_failed"] += 1
            print(f"[{index}/{len(pmids)}] {review_id} PMID {pmid} fetch failed: {exc}")

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Index 2025 open-access Cochrane reviews into DynamoDB.")
    parser.add_argument("--config", default="config.yml", help="Path to YAML config file.")
    args = parser.parse_args()
    stats = index_reviews(load_config(args.config))
    print(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
