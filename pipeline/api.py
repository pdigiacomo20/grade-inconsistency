from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from pipeline.dynamodb import DynamoStore


def get_store() -> DynamoStore:
    return DynamoStore(
        region_name=os.getenv("AWS_REGION", "us-west-2"),
        endpoint_url=os.getenv("DYNAMODB_ENDPOINT_URL", "http://localhost:8000"),
        reviews_table=os.getenv("REVIEWS_TABLE", "reviews"),
        outcomes_table=os.getenv("OUTCOMES_TABLE", "outcomes"),
    )


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
    return {"review": review, "outcomes": store.list_outcomes_for_review(pmid)}


@app.get("/api/outcomes")
def list_outcomes() -> dict:
    store = get_store()
    reviews_by_pmid = {review["pmid"]: review for review in store.list_reviews()}
    outcomes = []
    for outcome in store.list_outcomes():
        review = reviews_by_pmid.get(outcome["pmid"], {})
        outcomes.append(
            {
                **outcome,
                "review_title": review.get("title", ""),
                "review_year": review.get("year", ""),
                "review_journal": review.get("journal", ""),
                "full_text_url": review.get("full_text_url", ""),
            }
        )
    return {"outcomes": outcomes}
