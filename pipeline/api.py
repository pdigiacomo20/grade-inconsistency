from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import requests

from grade_inconsistency import fetch_article_html
from pipeline.classify_z import classify_article
from pipeline.dynamodb import DynamoStore
from pipeline.env import load_repo_env
from pipeline.evaluations import compute_metrics, list_runs, read_run
from pipeline.manual_extraction import (
    PDF_LINK_RE,
    PDF_META_RE,
    build_session,
    copy_enrichment_fields,
    create_and_store_article,
    enrich_article_with_pmid,
    lookup_pmid_for_article,
    mark_manual_extraction_failed,
    parse_excluded_extraction,
    parse_pico_extraction,
    parse_sof_extraction,
    parse_studies_extraction,
)

load_repo_env()


class ExtractionRequest(BaseModel):
    text: str


class ProcessPmidRequest(BaseModel):
    pmid: str


def get_store() -> DynamoStore:
    return DynamoStore(
        region_name=os.getenv("AWS_REGION", "us-west-2"),
        endpoint_url=os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000"),
        reviews_table=os.getenv("REVIEWS_TABLE", "reviews"),
        outcomes_table=os.getenv("OUTCOMES_TABLE", "outcomes"),
        articles_table=os.getenv("ARTICLES_TABLE", "articles"),
    )


def _abstract_dir() -> str:
    return os.getenv("ABSTRACT_TEXT_DIR", "data/articles/abstracts")


def _full_text_dir() -> str:
    return os.getenv("FULL_TEXT_DIR", "data/articles/full_text")


def _review_or_404(store: DynamoStore, review_id: str) -> dict[str, Any]:
    review = store.get_review(review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return review


def _article_summary(article: dict[str, Any]) -> dict[str, Any]:
    return {
        "article_id": article.get("article_id"),
        "article_type": article.get("article_type", "included_study"),
        "outcome_id": article.get("outcome_id"),
        "study_label": article.get("study_label"),
        "effect_measure": article.get("effect_measure"),
        "effect_estimate": article.get("effect_estimate"),
        "confidence_interval_begin": article.get("confidence_interval_begin"),
        "confidence_interval_end": article.get("confidence_interval_end"),
        "confidence_interval_percentage": article.get("confidence_interval_percentage"),
        "sample_size": article.get("sample_size"),
        "line_of_no_effect": article.get("line_of_no_effect"),
        "population": article.get("population"),
        "intervention": article.get("intervention"),
        "comparator": article.get("comparator"),
        "outcome": article.get("outcome"),
        "reason_for_exclusion": article.get("reason_for_exclusion"),
        "wald_z": article.get("wald_z"),
        "wald_z_category": article.get("wald_z_category"),
        "wald_z_error": article.get("wald_z_error"),
        "citation": article.get("citation"),
        "pmid": article.get("pmid"),
        "pmcid": article.get("pmcid"),
        "title": article.get("title"),
        "relaxed_search": article.get("relaxed_search"),
        "journal": article.get("journal"),
        "year": article.get("year"),
        "pubmed_url": article.get("pubmed_url"),
        "pmc_url": article.get("pmc_url"),
        "abstract_path": article.get("abstract_path"),
        "full_text_path": article.get("full_text_path"),
        "match_status": article.get("match_status"),
        "manual_extraction_failed": bool(article.get("manual_extraction_failed", False)),
        "enrichment_errors": article.get("enrichment_errors", []),
    }


def _hydrate_outcome(store: DynamoStore, outcome: dict[str, Any]) -> dict[str, Any]:
    article_ids = list(outcome.get("included_articles", []))
    articles = store.batch_get_articles(article_ids)
    return {
        **outcome,
        "included_article_refs": [
            _article_summary(articles[article_id])
            for article_id in outcome.get("included_articles", [])
            if article_id in articles
        ],
    }


def _find_pdf_url(session: requests.Session, review: dict[str, Any]) -> str:
    pmc_url = str(review.get("pmc_url") or "")
    if not pmc_url:
        return ""
    html = fetch_article_html(session, pmc_url, 0.0)
    meta = PDF_META_RE.search(html)
    if meta:
        return urljoin(pmc_url, meta.group(1))
    link = PDF_LINK_RE.search(html)
    if link:
        return urljoin(pmc_url, link.group(1))
    return ""


def _review_payload(store: DynamoStore, review: dict[str, Any], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    outcomes = [_hydrate_outcome(store, item) for item in store.list_outcomes_for_review(str(review["pmid"]))]
    articles = [_article_summary(item) for item in store.list_articles_for_review(str(review["review_id"]))]
    return {"review": review, "outcomes": outcomes, "articles": articles, **(extra or {})}


def _enrich_evaluation_contexts(store: DynamoStore, run: dict[str, Any]) -> dict[str, Any]:
    article_ids = [
        str(context.get("article_id") or "")
        for outcome in run.get("outcomes", [])
        for context in outcome.get("contexts", [])
        if context.get("article_id")
    ]
    articles = store.batch_get_articles(article_ids)
    for outcome in run.get("outcomes", []):
        for context in outcome.get("contexts", []):
            article = articles.get(str(context.get("article_id") or ""))
            if not article:
                continue
            computed_wald_z = None
            computed_wald_z_category = None
            computed_wald_z_error = None
            if article.get("wald_z_category") in (None, "") or article.get("wald_z") in (None, ""):
                computed_wald_z, computed_wald_z_category, computed_wald_z_error = classify_article(article)
            if context.get("wald_z") in (None, ""):
                context["wald_z"] = article.get("wald_z") if article.get("wald_z") not in (None, "") else computed_wald_z
            if context.get("wald_z_category") in (None, ""):
                context["wald_z_category"] = article.get("wald_z_category") or computed_wald_z_category
            if context.get("wald_z_error") in (None, ""):
                context["wald_z_error"] = article.get("wald_z_error") or computed_wald_z_error
    return run


def _evaluation_for_review(run: dict[str, Any], review: dict[str, Any]) -> dict[str, Any]:
    pmid = str(review.get("pmid") or "")
    outcomes = [outcome for outcome in run.get("outcomes", []) if str(outcome.get("pmid") or "") == pmid]
    scoped_run = _enrich_evaluation_contexts(get_store(), {"outcomes": outcomes})
    return {
        "filename": run.get("filename") or run.get("metadata", {}).get("filename") or "",
        "metadata": run.get("metadata", {}),
        "metrics": compute_metrics(scoped_run),
        "outcomes": scoped_run["outcomes"],
    }


def _try_auto_process_article(
    *,
    store: DynamoStore,
    session: requests.Session,
    article: dict[str, Any],
) -> dict[str, Any]:
    title = str(article.get("title") or "")
    review_id = str(article.get("review_id") or "")
    if title and review_id:
        for duplicate in store.list_articles_for_review_title(review_id, title):
            if str(duplicate.get("article_id")) == str(article.get("article_id")):
                continue
            if duplicate.get("pmid") or duplicate.get("manual_extraction_failed"):
                duplicate_status = "duplicate_manual_extraction_failed" if duplicate.get("manual_extraction_failed") else "duplicate_title_copied"
                copy_enrichment_fields(article, duplicate, match_status=duplicate_status)
                store.put_article(article)
                return article

    try:
        pmid, query, match_status = lookup_pmid_for_article(
            session,
            title=title,
            relaxed_search=str(article.get("relaxed_search") or ""),
        )
        if pmid:
            return enrich_article_with_pmid(
                store=store,
                session=session,
                article=article,
                pmid=pmid,
                abstract_dir=_abstract_dir(),
                full_text_dir=_full_text_dir(),
                pubmed_query=query,
                match_status=match_status,
            )
        article["pubmed_query"] = query
        article["match_status"] = match_status
        article["updated_at"] = datetime.now(UTC).isoformat()
        store.put_article(article)
    except (RuntimeError, requests.RequestException) as exc:
        errors = list(article.get("enrichment_errors", []))
        errors.append(f"pubmed_lookup: {exc}")
        article["enrichment_errors"] = errors
        article["match_status"] = "pubmed_lookup_failed"
        article["updated_at"] = datetime.now(UTC).isoformat()
        store.put_article(article)
    return article


app = FastAPI(title="Grade Inconsistency Manual Extraction API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:5173,http://localhost:5174,http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/reviews")
def list_reviews() -> dict[str, Any]:
    return {"reviews": get_store().list_reviews()}


@app.get("/api/reviews/{review_id}")
def get_review(review_id: str) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    return _review_payload(store, review)


@app.get("/api/outcomes")
def list_outcomes() -> dict[str, Any]:
    store = get_store()
    reviews_by_pmid = {review["pmid"]: review for review in store.list_reviews()}
    outcomes = []
    for outcome in store.list_outcomes():
        review = reviews_by_pmid.get(outcome["pmid"], {})
        outcomes.append(
            _hydrate_outcome(
                store,
                {
                    **outcome,
                    "review_title": review.get("title", ""),
                    "pmc_url": review.get("pmc_url", ""),
                },
            )
        )
    return {"outcomes": outcomes}


@app.get("/api/evaluations")
def list_evaluations() -> dict[str, Any]:
    return {"evaluations": list_runs()}


@app.get("/api/evaluations/{filename}")
def get_evaluation(filename: str) -> dict[str, Any]:
    try:
        run = read_run(filename)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    run = _enrich_evaluation_contexts(get_store(), run)
    run["metrics"] = compute_metrics(run)
    return run


@app.get("/api/reviews/{review_id}/evaluations/{filename}")
def get_review_evaluation(review_id: str, filename: str) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    try:
        run = read_run(filename)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _evaluation_for_review(run, review)


@app.post("/api/reviews/{review_id}/extract-sof")
def extract_sof(review_id: str, payload: ExtractionRequest) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    try:
        extraction = parse_sof_extraction(payload.text, pmid=str(review["pmid"]), review_id=str(review["review_id"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    outcomes = extraction.outcomes
    store.replace_outcomes(str(review["pmid"]), outcomes)
    review["sof_extracted_at"] = datetime.now(UTC).isoformat()
    review["studies_extracted_at"] = None
    review["pico_extracted_at"] = None
    review["excluded_extracted_at"] = None
    review["sof_overall_notes"] = extraction.overall_notes
    review["studies_overall_notes"] = ""
    review["pico_overall_notes"] = ""
    review["excluded_overall_notes"] = ""
    review["extraction_result"] = extraction.extraction_result
    review["has_inconsistency"] = extraction.extraction_result == "extracted"
    review["status"] = extraction.extraction_result if extraction.extraction_result != "extracted" else "sof_extracted"
    store.put_review(review)
    messages = {
        "no_inconsistency": "No inconsistency.",
        "inconsistency_not_very_low": "Inconsistency not very low.",
        "extracted": "SoF extracted.",
    }
    return {"review": review, "outcomes": outcomes, "message": messages.get(extraction.extraction_result, "SoF extracted.")}


@app.post("/api/reviews/{review_id}/extract-studies")
def extract_studies(review_id: str, payload: ExtractionRequest) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    existing = store.list_outcomes_for_review(str(review["pmid"]))
    if not existing:
        raise HTTPException(status_code=400, detail="Extract SoF must be completed before Extract Studies.")
    try:
        extraction = parse_studies_extraction(payload.text, existing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    article_count = 0
    session = build_session()
    for item in extraction.outcomes:
        outcome = item["outcome"]
        included_ids: list[str] = []
        for study in item["studies"]:
            article = create_and_store_article(
                store=store,
                citation=study["citation"],
                study_label=study["study_label"],
                review=review,
                outcome=outcome,
                article_type="included_study",
                effect_measure=item["effect_measure"],
                line_of_no_effect=item["line_of_no_effect"],
                effect_estimate=study.get("effect_estimate", ""),
                confidence_interval_begin=study.get("confidence_interval_begin", ""),
                confidence_interval_end=study.get("confidence_interval_end", ""),
                confidence_interval_percentage=study.get("confidence_interval_percentage", ""),
                sample_size=study.get("sample_size", ""),
                title=study.get("title", ""),
                relaxed_search=study.get("relaxed_search", ""),
            )
            if not article.get("manual_extraction_failed"):
                article = _try_auto_process_article(store=store, session=session, article=article)
            included_ids.append(str(article["article_id"]))
            article_count += 1
        outcome.update(
            {
                "forest_plot_title": item["forest_plot_title"],
                "effect_measure": item["effect_measure"],
                "line_of_no_effect": item["line_of_no_effect"],
                "aggregated_effect_estimate": item["aggregated_effect_estimate"],
                "aggregated_confidence_interval_begin": item["aggregated_confidence_interval_begin"],
                "aggregated_confidence_interval_end": item["aggregated_confidence_interval_end"],
                "aggregated_confidence_interval_percentage": item["aggregated_confidence_interval_percentage"],
                "aggregated_sample_size": item["aggregated_sample_size"],
                "included_articles": included_ids,
                "extraction_status": "studies_extracted",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        store.put_outcome(outcome)

    review["studies_extracted_at"] = datetime.now(UTC).isoformat()
    review["studies_overall_notes"] = extraction.overall_notes
    review["status"] = "studies_extracted"
    store.put_review(review)
    return _review_payload(store, review, {"article_count": article_count})


@app.post("/api/reviews/{review_id}/extract-pico")
def extract_pico(review_id: str, payload: ExtractionRequest) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    try:
        extraction = parse_pico_extraction(payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated_count = 0
    articles = store.list_articles_for_review(str(review["review_id"]))
    for study in extraction.studies:
        label = str(study.get("study_label") or "")
        matches = [article for article in articles if article.get("article_type", "included_study") == "included_study" and str(article.get("study_label") or "") == label]
        for article in matches:
            article.update(
                {
                    "population": study.get("population") or None,
                    "intervention": study.get("intervention") or None,
                    "comparator": study.get("comparator") or None,
                    "outcome": study.get("outcome") or None,
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            store.put_article(article)
            updated_count += 1

    review["pico_extracted_at"] = datetime.now(UTC).isoformat()
    review["pico_overall_notes"] = extraction.overall_notes
    review["status"] = "pico_extracted"
    store.put_review(review)
    return _review_payload(store, review, {"updated_article_count": updated_count})


@app.post("/api/reviews/{review_id}/extract-excluded")
def extract_excluded(review_id: str, payload: ExtractionRequest) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    try:
        extraction = parse_excluded_extraction(payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    article_count = 0
    session = build_session()
    for study in extraction.studies:
        article = create_and_store_article(
            store=store,
            citation=study.get("citation", ""),
            study_label=study.get("study_label", ""),
            review=review,
            article_type="excluded_study",
            title=study.get("title", ""),
            relaxed_search=study.get("relaxed_search", ""),
            reason_for_exclusion=study.get("reason_for_exclusion", ""),
        )
        if not article.get("manual_extraction_failed"):
            _try_auto_process_article(store=store, session=session, article=article)
        article_count += 1

    review["excluded_extracted_at"] = datetime.now(UTC).isoformat()
    review["excluded_overall_notes"] = extraction.overall_notes
    review["status"] = "excluded_extracted"
    store.put_review(review)
    return _review_payload(store, review, {"article_count": article_count})


@app.post("/api/articles/{article_id}/process-pmid")
def process_article_pmid(article_id: str, payload: ProcessPmidRequest) -> dict[str, Any]:
    pmid = payload.pmid.strip()
    if not re.fullmatch(r"\d{1,9}", pmid):
        raise HTTPException(status_code=400, detail="PMID must contain only digits.")

    store = get_store()
    article = store.get_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")

    session = build_session()
    try:
        updated = enrich_article_with_pmid(
            store=store,
            session=session,
            article=article,
            pmid=pmid,
            abstract_dir=_abstract_dir(),
            full_text_dir=_full_text_dir(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    review = store.get_review(str(updated.get("review_id") or updated.get("review_pmid") or ""))
    if not review:
        return {"article": _article_summary(updated)}

    return _review_payload(store, review, {"article": _article_summary(updated)})


@app.post("/api/articles/{article_id}/manual-extraction-failed")
def manual_article_extraction_failed(article_id: str) -> dict[str, Any]:
    store = get_store()
    article = store.get_article(article_id)
    if not article:
        raise HTTPException(status_code=404, detail="Article not found")
    updated = mark_manual_extraction_failed(store=store, article=article)
    review = store.get_review(str(updated.get("review_id") or updated.get("review_pmid") or ""))
    if not review:
        return {"article": _article_summary(updated)}
    return _review_payload(store, review, {"article": _article_summary(updated)})


@app.get("/api/reviews/{review_id}/pdf")
def download_review_pdf(review_id: str) -> Response:
    store = get_store()
    review = _review_or_404(store, review_id)
    session = build_session()
    try:
        pdf_url = _find_pdf_url(session, review)
    except (RuntimeError, requests.RequestException) as exc:
        raise HTTPException(status_code=404, detail=f"PDF lookup failed: {exc}") from exc
    if not pdf_url:
        raise HTTPException(status_code=404, detail="No PDF link was found for this PMC review.")

    try:
        response = session.get(pdf_url, stream=True, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(status_code=404, detail=f"PDF download failed: {exc}") from exc

    filename = f"{review['review_id']}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(response.iter_content(chunk_size=65536), media_type="application/pdf", headers=headers)


@app.get("/api/articles/{article_id}/abstract")
def get_article_abstract(article_id: str) -> FileResponse:
    article = get_store().get_article(article_id)
    path = Path(str(article.get("abstract_path") or "")) if article else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Abstract text file not found")
    return FileResponse(path, media_type="text/plain")


@app.get("/api/articles/{article_id}/full-text")
def get_article_full_text(article_id: str) -> FileResponse:
    article = get_store().get_article(article_id)
    path = Path(str(article.get("full_text_path") or "")) if article else None
    if not path or not path.exists():
        raise HTTPException(status_code=404, detail="Full text file not found")
    return FileResponse(path, media_type="text/plain")
