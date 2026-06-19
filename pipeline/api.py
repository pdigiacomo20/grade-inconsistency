from __future__ import annotations

import os
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from pipeline.dynamodb import DynamoStore
from pipeline.study_enrichment import summarize_study_for_outcome


def get_store() -> DynamoStore:
    return DynamoStore(
        region_name=os.getenv("AWS_REGION", "us-west-2"),
        endpoint_url=os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000"),
        reviews_table=os.getenv("REVIEWS_TABLE", "reviews"),
        outcomes_table=os.getenv("OUTCOMES_TABLE", "outcomes"),
        studies_table=os.getenv("STUDIES_TABLE", "studies"),
    )


def forest_plots_dir() -> Path:
    return Path(os.getenv("FOREST_PLOTS_DIR", "data/forest_plots"))


def hydrate_study_refs(store: DynamoStore, outcome: dict) -> dict:
    agreeing_ids = [str(study_id) for study_id in outcome.get("agreeing_studies", [])]
    opposing_ids = [str(study_id) for study_id in outcome.get("opposing_studies", [])]
    studies = store.batch_get_studies(agreeing_ids + opposing_ids)
    return {
        **outcome,
        "forest_plot_url": f"/api/forest-plots/{outcome['pmid']}/{outcome['outcome_id']}"
        if outcome.get("forest_plot_path")
        else outcome.get("forest_plot_url"),
        "agreeing_study_refs": [
            summarize_study_for_outcome(studies[study_id]) for study_id in agreeing_ids if study_id in studies
        ],
        "opposing_study_refs": [
            summarize_study_for_outcome(studies[study_id]) for study_id in opposing_ids if study_id in studies
        ],
    }


app = FastAPI(title="Grade Inconsistency API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:5173,http://localhost:3000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/reviews")
def list_reviews() -> dict:
    return {"reviews": get_store().list_reviews()}


@app.get("/api/reviews/{pmid}")
def get_review(pmid: str) -> dict:
    store = get_store()
    review = store.get_review(pmid)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    return {"review": review, "outcomes": [hydrate_study_refs(store, item) for item in store.list_outcomes_for_review(pmid)]}


@app.get("/api/outcomes")
def list_outcomes() -> dict:
    store = get_store()
    reviews_by_pmid = {review["pmid"]: review for review in store.list_reviews()}
    outcomes = []
    for outcome in store.list_outcomes():
        review = reviews_by_pmid.get(outcome["pmid"], {})
        outcomes.append(
            hydrate_study_refs(
                store,
                {
                    **outcome,
                    "review_title": review.get("title", ""),
                    "review_year": review.get("year", ""),
                    "review_journal": review.get("journal", ""),
                    "full_text_url": review.get("full_text_url", ""),
                },
            )
        )
    return {"outcomes": outcomes}


@app.get("/api/forest-plots/{pmid}/{outcome_id}")
def get_forest_plot(pmid: str, outcome_id: int) -> FileResponse:
    outcome = get_store().get_outcome(pmid, outcome_id)
    saved_path = Path(str(outcome.get("forest_plot_path"))) if outcome and outcome.get("forest_plot_path") else None
    if saved_path and saved_path.exists():
        return FileResponse(saved_path)
    directory = forest_plots_dir() / str(pmid)
    matches = list(directory.glob(f"outcome-{outcome_id}.*"))
    if not matches:
        raise HTTPException(status_code=404, detail="Forest plot not found")
    return FileResponse(matches[0])


@app.get("/api/studies/{study_id}", response_class=HTMLResponse)
def get_study(study_id: str) -> HTMLResponse:
    study = get_store().get_study(study_id)
    if not study:
        raise HTTPException(status_code=404, detail="Study not found")

    pmc_link = (
        f'<a href="{escape(str(study["pmc_url"]))}" target="_blank" rel="noreferrer">PMC full text</a>'
        if study.get("pmc_url")
        else '<span class="muted">PMC full text unavailable</span>'
    )
    pubmed_link = (
        f'<a href="{escape(str(study["pubmed_url"]))}" target="_blank" rel="noreferrer">PubMed</a>'
        if study.get("pubmed_url")
        else '<span class="muted">PubMed match unavailable</span>'
    )
    abstract = escape(str(study.get("abstract") or "No abstract was available from PubMed."))
    html_body = f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <title>{escape(str(study.get("label") or study_id))}</title>
    <style>
      body {{ color: #202124; font-family: system-ui, sans-serif; line-height: 1.5; margin: 32px; max-width: 980px; }}
      h1 {{ color: #17324d; font-size: 26px; line-height: 1.2; }}
      dl {{ display: grid; grid-template-columns: 140px 1fr; gap: 8px 18px; }}
      dt {{ color: #667085; font-weight: 700; }}
      dd {{ margin: 0; }}
      a {{ color: #0f6b8e; font-weight: 650; }}
      .muted {{ color: #667085; }}
      .abstract {{ background: #f6f7f9; border: 1px solid #d8dee6; border-radius: 8px; padding: 16px; white-space: pre-wrap; }}
    </style>
  </head>
  <body>
    <h1>{escape(str(study.get("title") or study.get("label") or study_id))}</h1>
    <dl>
      <dt>Study label</dt><dd>{escape(str(study.get("label") or ""))}</dd>
      <dt>PMID</dt><dd>{escape(str(study.get("pmid") or "Unavailable"))}</dd>
      <dt>PMCID</dt><dd>{escape(str(study.get("pmcid") or "Unavailable"))}</dd>
      <dt>Journal</dt><dd>{escape(str(study.get("journal") or "Unavailable"))}</dd>
      <dt>Year</dt><dd>{escape(str(study.get("year") or "Unavailable"))}</dd>
      <dt>Links</dt><dd>{pmc_link} &nbsp; {pubmed_link}</dd>
      <dt>Search query</dt><dd>{escape(str(study.get("search_query") or ""))}</dd>
    </dl>
    <h2>Abstract</h2>
    <div class="abstract">{abstract}</div>
  </body>
</html>"""
    return HTMLResponse(html_body)
