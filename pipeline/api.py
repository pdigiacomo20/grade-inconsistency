from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
import shutil
from typing import Any
from urllib.parse import urljoin

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import requests

from grade_inconsistency import fetch_article_html
from pipeline.classify_z import classify_and_store_article, classify_article
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
    parse_characteristics_extraction,
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


def _review_documents_dir() -> str:
    return os.getenv("REVIEW_DOCUMENTS_DIR", "data/review_documents")


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
        "unit_of_measure": article.get("unit_of_measure"),
        "polarity_of_measure": article.get("polarity_of_measure"),
        "comparator_effect_measure": article.get("comparator_effect_measure"),
        "effect_estimate": article.get("effect_estimate"),
        "confidence_interval_begin": article.get("confidence_interval_begin"),
        "confidence_interval_end": article.get("confidence_interval_end"),
        "confidence_interval_percentage": article.get("confidence_interval_percentage"),
        "sample_size": article.get("sample_size"),
        "line_of_no_effect": article.get("line_of_no_effect"),
        "characteristics_methods": article.get("characteristics_methods"),
        "characteristics_participants": article.get("characteristics_participants"),
        "characteristics_interventions": article.get("characteristics_interventions"),
        "characteristics_outcomes": article.get("characteristics_outcomes"),
        "characteristics_risk_of_bias": article.get("characteristics_risk_of_bias"),
        "characteristics_markdown": article.get("characteristics_markdown"),
        "characteristics_extraction_failed": bool(article.get("characteristics_extraction_failed", False)),
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


def _clear_characteristics_fields(store: DynamoStore, review_id: str) -> int:
    updated_count = 0
    for article in store.list_articles_for_review(review_id):
        if article.get("article_type", "included_study") != "included_study":
            continue
        for key in (
            "population",
            "intervention",
            "comparator",
            "outcome",
            "characteristics_methods",
            "characteristics_participants",
            "characteristics_interventions",
            "characteristics_outcomes",
            "characteristics_risk_of_bias",
            "characteristics_markdown",
            "characteristics_extraction_failed",
        ):
            article.pop(key, None)
        article["updated_at"] = datetime.now(UTC).isoformat()
        store.put_article(article)
        updated_count += 1
    return updated_count


def _reset_characteristics_review_fields(review: dict[str, Any]) -> None:
    for key in ("pico_extracted_at", "pico_raw_extraction_text", "pico_overall_notes"):
        review.pop(key, None)
    review["characteristics_extracted_at"] = None
    review["characteristics_raw_extraction_text"] = ""
    review["characteristics_overall_notes"] = ""
    review["plain_language_summary"] = ""


def _reset_extraction_review_fields(review: dict[str, Any]) -> None:
    for key in (
        "sof_extracted_at",
        "studies_extracted_at",
        "characteristics_extracted_at",
        "excluded_extracted_at",
        "pico_extracted_at",
        "extraction_result",
        "has_inconsistency",
    ):
        review.pop(key, None)
    for key in (
        "sof_raw_extraction_text",
        "studies_raw_extraction_text",
        "characteristics_raw_extraction_text",
        "excluded_raw_extraction_text",
        "pico_raw_extraction_text",
        "sof_overall_notes",
        "studies_overall_notes",
        "characteristics_overall_notes",
        "excluded_overall_notes",
        "pico_overall_notes",
        "plain_language_summary",
    ):
        review[key] = ""
    review["status"] = "protocol_only" if review.get("is_protocol_only") else "ready_for_extraction"


def _empty_studies_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    outcome.update(
        {
            "forest_plot_title": "",
            "effect_measure": "",
            "unit_of_measure": "",
            "polarity_of_measure": "",
            "comparator_effect_measure": "",
            "line_of_no_effect": "",
            "aggregated_effect_estimate": "",
            "aggregated_confidence_interval_begin": "",
            "aggregated_confidence_interval_end": "",
            "aggregated_confidence_interval_percentage": "",
            "aggregated_sample_size": "",
            "included_articles": [],
            "extraction_status": "sof_extracted",
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
    return outcome


def _safe_filename(filename: str) -> str:
    cleaned = Path(filename or "document").name
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "_", cleaned).strip(" .")
    return cleaned or "document"


def _saved_documents(review: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in review.get("saved_documents", []) if isinstance(item, dict) and item.get("path")]


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
    except (RuntimeError, ValueError, requests.RequestException) as exc:
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


@app.post("/api/reviews/{review_id}/documents")
def upload_review_documents(review_id: str, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one document.")
    store = get_store()
    review = _review_or_404(store, review_id)
    review_dir = Path(_review_documents_dir()) / str(review["review_id"])
    review_dir.mkdir(parents=True, exist_ok=True)
    saved = _saved_documents(review)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")

    for index, upload in enumerate(files, start=1):
        original_name = _safe_filename(upload.filename or f"document-{index}")
        destination = review_dir / f"{timestamp}-{index:02d}-{original_name}"
        with destination.open("wb") as handle:
            shutil.copyfileobj(upload.file, handle)
        saved.append(
            {
                "filename": original_name,
                "path": str(destination),
                "content_type": upload.content_type or "application/octet-stream",
                "uploaded_at": datetime.now(UTC).isoformat(),
                "size_bytes": destination.stat().st_size,
            }
        )

    review["saved_documents"] = saved
    review["updated_at"] = datetime.now(UTC).isoformat()
    store.put_review(review)
    return _review_payload(store, review, {"saved_document_count": len(saved)})


@app.get("/api/reviews/{review_id}/documents/{document_index}")
def download_review_document(review_id: str, document_index: int) -> FileResponse:
    store = get_store()
    review = _review_or_404(store, review_id)
    documents = _saved_documents(review)
    if document_index < 0 or document_index >= len(documents):
        raise HTTPException(status_code=404, detail="Saved document not found.")
    document = documents[document_index]
    path = Path(str(document.get("path") or ""))
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Saved document file not found.")
    filename = _safe_filename(str(document.get("filename") or path.name))
    return FileResponse(
        path,
        media_type=str(document.get("content_type") or "application/octet-stream"),
        filename=filename,
    )


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
    store.delete_articles_for_review(str(review["review_id"]), article_type="included_study")
    store.replace_outcomes(str(review["pmid"]), outcomes)
    review["sof_extracted_at"] = datetime.now(UTC).isoformat()
    review["studies_extracted_at"] = None
    review["sof_raw_extraction_text"] = payload.text
    review["studies_raw_extraction_text"] = ""
    review["sof_overall_notes"] = extraction.overall_notes
    review["studies_overall_notes"] = ""
    _reset_characteristics_review_fields(review)
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


@app.delete("/api/reviews/{review_id}/extractions")
def delete_review_extractions(review_id: str) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    article_count = store.delete_articles_for_review(str(review["review_id"]))
    outcome_count = len(store.list_outcomes_for_review(str(review["pmid"])))
    store.replace_outcomes(str(review["pmid"]), [])
    _reset_extraction_review_fields(review)
    store.put_review(review)
    return _review_payload(
        store,
        review,
        {
            "deleted_article_count": article_count,
            "deleted_outcome_count": outcome_count,
            "message": "Extractions deleted.",
        },
    )


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

    store.delete_articles_for_review(str(review["review_id"]), article_type="included_study")
    for outcome in existing:
        store.put_outcome(_empty_studies_outcome(outcome))

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
                effect_measure=study.get("effect_measure", item["effect_measure"]),
                unit_of_measure=study.get("unit_of_measure", item["unit_of_measure"]),
                polarity_of_measure=study.get("polarity_of_measure", item["polarity_of_measure"]),
                comparator_effect_measure=study.get("comparator_effect_measure", item["comparator_effect_measure"]),
                line_of_no_effect=study.get("line_of_no_effect", item["line_of_no_effect"]),
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
            article = classify_and_store_article(store.articles, article)
            included_ids.append(str(article["article_id"]))
            article_count += 1
        outcome.update(
            {
                "forest_plot_title": item["forest_plot_title"],
                "effect_measure": item["effect_measure"],
                "unit_of_measure": item["unit_of_measure"],
                "polarity_of_measure": item["polarity_of_measure"],
                "comparator_effect_measure": item["comparator_effect_measure"],
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
    review["studies_raw_extraction_text"] = payload.text
    review["studies_overall_notes"] = extraction.overall_notes
    _reset_characteristics_review_fields(review)
    review["status"] = "studies_extracted"
    store.put_review(review)
    return _review_payload(store, review, {"article_count": article_count})


@app.post("/api/reviews/{review_id}/extract-characteristics")
def extract_characteristics(review_id: str, payload: ExtractionRequest) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    try:
        extraction = parse_characteristics_extraction(payload.text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    updated_count = 0
    _clear_characteristics_fields(store, str(review["review_id"]))
    articles = store.list_articles_for_review(str(review["review_id"]))
    for study in extraction.studies:
        label = str(study.get("study_label") or "")
        matches = [article for article in articles if article.get("article_type", "included_study") == "included_study" and str(article.get("study_label") or "") == label]
        for article in matches:
            article.update(
                {
                    "characteristics_methods": study.get("characteristics_methods") or None,
                    "characteristics_participants": study.get("characteristics_participants") or None,
                    "characteristics_interventions": study.get("characteristics_interventions") or None,
                    "characteristics_outcomes": study.get("characteristics_outcomes") or None,
                    "characteristics_risk_of_bias": study.get("characteristics_risk_of_bias") or None,
                    "characteristics_markdown": study.get("characteristics_markdown") or None,
                    "characteristics_extraction_failed": bool(study.get("characteristics_extraction_failed", False)),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
            store.put_article(article)
            updated_count += 1

    for key in ("pico_extracted_at", "pico_raw_extraction_text", "pico_overall_notes"):
        review.pop(key, None)
    review["characteristics_extracted_at"] = datetime.now(UTC).isoformat()
    review["characteristics_raw_extraction_text"] = payload.text
    review["characteristics_overall_notes"] = extraction.overall_notes
    review["plain_language_summary"] = extraction.plain_language_summary
    review["status"] = "characteristics_extracted"
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

    store.delete_articles_for_review(str(review["review_id"]), article_type="excluded_study")
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
    review["excluded_raw_extraction_text"] = payload.text
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
