from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
import requests

from grade_inconsistency import fetch_article_html
from pipeline.dynamodb import DynamoStore
from pipeline.env import load_repo_env
from pipeline.manual_extraction import (
    PDF_LINK_RE,
    PDF_META_RE,
    build_session,
    enrich_and_store_article,
    parse_agree_oppose_extraction,
    parse_sof_extraction,
)

load_repo_env()


class ExtractionRequest(BaseModel):
    text: str


def get_store() -> DynamoStore:
    return DynamoStore(
        region_name=os.getenv("AWS_REGION", "us-west-2"),
        endpoint_url=os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000"),
        reviews_table=os.getenv("REVIEWS_TABLE", "reviews"),
        outcomes_table=os.getenv("OUTCOMES_TABLE", "outcomes"),
        articles_table=os.getenv("ARTICLES_TABLE", "articles"),
    )


def _openai_config() -> dict[str, Any]:
    return {
        "openai_api_key": os.getenv("OPENAI_API_KEY"),
        "openai_model": os.getenv("OPENAI_MODEL", "gpt-5.5"),
        "openai_timeout_seconds": int(os.getenv("OPENAI_TIMEOUT_SECONDS", "300")),
    }


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
        "outcome_id": article.get("outcome_id"),
        "stance": article.get("stance"),
        "study_label": article.get("study_label"),
        "citation": article.get("citation"),
        "pmid": article.get("pmid"),
        "pmcid": article.get("pmcid"),
        "title": article.get("title"),
        "journal": article.get("journal"),
        "year": article.get("year"),
        "pubmed_url": article.get("pubmed_url"),
        "pmc_url": article.get("pmc_url"),
        "abstract_path": article.get("abstract_path"),
        "full_text_path": article.get("full_text_path"),
        "match_status": article.get("match_status"),
        "enrichment_errors": article.get("enrichment_errors", []),
    }


def _hydrate_outcome(store: DynamoStore, outcome: dict[str, Any]) -> dict[str, Any]:
    article_ids = list(outcome.get("agreeing_articles", [])) + list(outcome.get("opposing_articles", []))
    articles = store.batch_get_articles(article_ids)
    return {
        **outcome,
        "agreeing_article_refs": [
            _article_summary(articles[article_id])
            for article_id in outcome.get("agreeing_articles", [])
            if article_id in articles
        ],
        "opposing_article_refs": [
            _article_summary(articles[article_id])
            for article_id in outcome.get("opposing_articles", [])
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
    outcomes = [_hydrate_outcome(store, item) for item in store.list_outcomes_for_review(str(review["pmid"]))]
    articles = [_article_summary(item) for item in store.list_articles_for_review(str(review["review_id"]))]
    return {"review": review, "outcomes": outcomes, "articles": articles}


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
    review["agree_oppose_extracted_at"] = None
    review["sof_overall_notes"] = extraction.overall_notes
    review["agree_oppose_overall_notes"] = ""
    review["has_inconsistency"] = extraction.has_inconsistency
    review["status"] = "no_inconsistency" if not extraction.has_inconsistency else "sof_extracted"
    store.put_review(review)
    return {"review": review, "outcomes": outcomes, "message": "No inconsistency." if not extraction.has_inconsistency else "SoF extracted."}


@app.post("/api/reviews/{review_id}/extract-agree-oppose")
def extract_agree_oppose(review_id: str, payload: ExtractionRequest) -> dict[str, Any]:
    store = get_store()
    review = _review_or_404(store, review_id)
    existing = store.list_outcomes_for_review(str(review["pmid"]))
    if not existing:
        raise HTTPException(status_code=400, detail="Extract SoF must be completed before Extract Agree Oppose.")
    try:
        extraction = parse_agree_oppose_extraction(payload.text, existing)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = build_session()
    article_count = 0
    for item in extraction.outcomes:
        outcome = item["outcome"]
        agreeing_ids: list[str] = []
        opposing_ids: list[str] = []
        for stance, key, output in (
            ("agreeing", "agreeing_citations", agreeing_ids),
            ("opposing", "opposing_citations", opposing_ids),
        ):
            for citation in item[key]:
                article = enrich_and_store_article(
                    store=store,
                    session=session,
                    citation=citation["citation"],
                    study_label=citation["study_label"],
                    review=review,
                    outcome=outcome,
                    stance=stance,
                    abstract_dir=_abstract_dir(),
                    full_text_dir=_full_text_dir(),
                    **_openai_config(),
                )
                output.append(str(article["article_id"]))
                article_count += 1
        outcome.update(
            {
                "forest_plot_title": item["forest_plot_title"],
                "agreeing_articles": agreeing_ids,
                "opposing_articles": opposing_ids,
                "extraction_status": "agree_oppose_extracted",
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        store.put_outcome(outcome)

    review["agree_oppose_extracted_at"] = datetime.now(UTC).isoformat()
    review["agree_oppose_overall_notes"] = extraction.overall_notes
    review["status"] = "agree_oppose_extracted"
    store.put_review(review)
    outcomes = [_hydrate_outcome(store, item) for item in store.list_outcomes_for_review(str(review["pmid"]))]
    articles = [_article_summary(item) for item in store.list_articles_for_review(str(review["review_id"]))]
    return {"review": review, "outcomes": outcomes, "articles": articles, "article_count": article_count}


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
